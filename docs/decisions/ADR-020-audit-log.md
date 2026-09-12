# ADR-020: The audit log

- **Status**: Accepted
- **Date**: 2026-08-31
- **Implements**: IAM-090, IAM-091, IAM-092, IAM-093 (PRD §8.10)

## Context

> **IAM-092.** The audit log is immutable and tamper-evident (hash chaining or WORM storage). No
> role, including Owner or LEDGR staff, can edit or delete entries.

Every other append-only table in this schema (`auth_attempt`, `role_assignment`,
`account_recovery_event`) relies on withholding the DELETE privilege from `ledgr_app`. That is
sufficient when the threat is application code. It is not sufficient here, because the threat model
for an audit log explicitly includes someone holding a database connection — a migration, an operator
script, an administrator.

So the question this ADR has to answer is not "is it append-only" but **"append-only against whom,
and where does that end"**.

## Decision

### Four layers, and what each one actually stops

**1. No privileges.** `ledgr_app` and `ledgr_ops` get SELECT and INSERT. Nobody gets UPDATE, DELETE
or TRUNCATE — including the table's owner, from which all three are explicitly revoked. A statement
never granted cannot be issued.

**2. Triggers that always RAISE.** `audit_log_no_update_trg`, `audit_log_no_delete_trg` and
`audit_log_no_truncate_trg` reject unconditionally — a function whose only statement is `RAISE`
cannot be talked into permitting anything. This is the layer that stops the two roles a privilege
check would not:

- **`ledgr_migrator`** owns every other table in this schema, and Postgres does not privilege-check a
  table's owner. It could grant itself back anything it revoked.
- **`ledgr_ops`** holds `BYPASSRLS` for the operator scripts. **BYPASSRLS skips row-level security; it
  has never skipped a trigger.** That distinction is the single most load-bearing fact in this design,
  and `test_bypassrls_does_not_bypass_the_trigger` asserts both halves — that the role really does
  read across tenants, and that it still cannot write.

TRUNCATE needs its own trigger: it is neither UPDATE nor DELETE and would otherwise empty the table
without firing either, at the statement level rather than per row.

**3. Ownership by a role nobody can connect as.** A trigger protects a table only while it exists,
and a table's owner may `DROP TRIGGER`. So `audit_log` and its trigger functions are owned by
`ledgr_audit`: NOLOGIN, NOSUPERUSER, NOBYPASSRLS, granted to nobody. `ledgr_migrator` does not own
this table and therefore cannot disarm it — which is what closes the "not a database migration" case
specifically. This reuses the shape 0001 established for `ledgr_bootstrap`: put the dangerous
capability behind a role with no way in.

**4. Hash chaining, because layers 1–3 stop every role this application creates and none of them stop
a Postgres superuser.** Nothing in the database can. A superuser may drop a trigger, change an owner,
or rewrite a row. IAM-092 asks for immutable **and** tamper-evident precisely because prevention
alone cannot survive that, and detection can.

`test_a_superuser_who_disarms_the_trigger_is_still_caught_by_the_chain` does exactly that: drops the
trigger, edits an entry, and shows `app.verify_audit_chain` naming the row.

### The hash is computed by the database, from the row's own values

The sealing trigger derives `sequence_number`, `previous_hash`, `entry_hash` and `recorded_at` and
overwrites whatever the caller supplied. An application that could name its own hash could forge a
consistent chain, so it is not given the chance — the same derive-don't-trust pattern as
`granted_by_organization_id` in 0016.

Verification also runs in the database, recomputing each hash from the stored fields rather than only
checking that links agree. A tamperer who edited a field and left the hashes alone would pass a link
check and fail this one.

Fields are **length-prefixed** before concatenation. Without it, adjacent fields run together: an
action of `post` with resource type `entry` would hash identically to `poste` with `ntry`. Academic
as an attack, but two different entries hashing alike is the one thing this table cannot afford.

### Chains are per organization

Not one global chain. Per-tenant means verification works under RLS (a tenant can verify its own log
without reading anyone else's), a break is attributable to one tenant's log rather than invalidating
everyone's, IAM-094's "search and export **their own** log" has a natural boundary, and appends
contend only within a tenant.

Serialisation uses an **advisory lock**, not `SELECT … FOR UPDATE`: the latter requires an UPDATE
privilege that no role has on this table by design. Immutability and row locking are mutually
exclusive here, and immutability wins.

### Two timestamps

`occurred_at` is caller-supplied (when the event happened); `recorded_at` is set by the trigger from
the server clock (when the database sealed it). Both are in the hash. IAM-091 asks for one timestamp,
but a single one cannot distinguish an event recorded late from one backdated on the way in, and a
log whose whole job is being trusted should be able to tell those apart.

### The honest limit, stated plainly

**An unanchored chain detects tampering by everyone except whoever owns the server.** A superuser who
rewrites an entry can recompute the rest of the chain; one who deletes entries from the *end* leaves
a shorter chain that verifies perfectly. Both are demonstrated in the integration tests, including
the negative case — `test_removing_the_most_recent_entry_is_not_detected_by_the_chain_alone`.

What closes that is a copy of the head hash and sequence number held **outside** the database.
`app.audit_chain_head()` returns it and `scripts/anchor_audit_chain.py` collects it. Where anchors go
is deliberately a deployment decision (Azure Blob under a WORM policy in a separate subscription is
the obvious fit, since PRD §13 already chose that control for documents), and the only sink
implemented today is a structured log line — which is written by the same infrastructure it checks
and therefore does **not** satisfy the requirement on its own. That is remaining work, and it is
named as such rather than implied to be done.

### IAM-093 is satisfied by there being no delete path

Retention is unbounded, which is longer than seven years. Recorded in the migration because "we never
delete" is a decision someone might later mistake for an oversight and 'fix' with a purge job. A
purge that ever becomes necessary must not be a DELETE — it would have to be
export-then-drop-partition, which is a schema change and a new ADR, not a cron job.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Relying on withheld privileges alone, as the other append-only tables do | Does not bind `ledgr_migrator` (owners are not privilege-checked) or `ledgr_ops` (BYPASSRLS). Both are named by the requirement. |
| Leaving the table owned by `ledgr_migrator` like every other table | The owner can `DROP TRIGGER`, which makes layer 2 decorative against exactly the role the requirement calls out. |
| Computing the hash in application code | An application that can name its own hash can forge a consistent chain. The database computing it from the row means an attacker needs the database, not just the app. |
| One global chain across all tenants | Verification would need cross-tenant reads, conflicting with RLS; every append would serialise on one head; and one tenant's tampering would invalidate everyone's chain. |
| `SELECT … FOR UPDATE` to serialise appends | Requires an UPDATE privilege that no role has on this table. Advisory locks need none. |
| Concatenating fields with a delimiter | A delimiter appearing inside a value lets two different entries produce the same payload. Length-prefixing has no such case. |
| A single timestamp, as IAM-091 literally lists | Cannot distinguish a late-recorded event from a backdated one — a distinction an audit log specifically needs. |
| Claiming tamper-evidence is complete without external anchoring | It would be false. A superuser can recompute the chain, and truncating the tail is invisible to it entirely. |
| Granting `ledgr_ops` INSERT so support tooling can log its own access | A role that writes the log recording its own access is not an auditor of itself. IAM-090 requires support access to be audited, so the entries must come from the application path. |

## Consequences

- **Fourteen guards are mutation-tested** at the domain level — chain not linked, verification
  skipping hash recomputation / sequence gaps / previous-hash checks, `detail` and `occurred_at`
  dropped from the hash, length-prefixing removed, chains shared across tenants, search leaking
  across tenants, user-attributed entries naming nobody, blank actions, a dropped IAM-090 category,
  `denied` collapsed into `failure`, and an uncapped page size. All produced real test failures.
- **Two of those initially passed and both were my tests' fault.** The collision test used values
  that did not actually collide, and the page-cap test seeded five entries where an uncapped query
  returns the same five. Both now assert the property rather than a coincidence — the cap by
  inspecting the limit that reached the repository.
- **The in-memory fake reimplements the sealing algorithm**, so the pure tests exercise real
  chaining. `test_the_in_memory_fake_computes_the_same_hash_as_postgres` compares the two on a real
  row, so a drift is caught rather than silently making those tests meaningless.
- **The role-level tests need a superuser connection.** `TEST_DATABASE_ADMIN_URL` is already a
  job-level env var in CI, so they run there; locally they skip with the rest of the DB-gated suite.
  They are the tests that actually answer "what stops a migration or support tooling", so a run
  without them proves considerably less.
- **The GA evidence lives in `tests/integration/test_audit_tamper_evidence.py`** (added later; see
  the addendum below), which supersedes and extends the role tests originally in
  `test_audit_log.py`. That file now covers append behaviour only.

## Addendum: the GA tamper test

PRD §22 blocks GA on "audit log immutability verified by attempted tamper test". That is a document
someone reads as evidence, not just a passing suite, so it was written as its own file with its own
structure.

**One classification, applied uniformly.** Every attempt funnels through `attempt_tamper`, which
returns `PREVENTED`, `DETECTED`, `DETECTED_BY_ANCHOR`, or `UNDETECTED`. Expressing the assertion as a
disjunction matches what IAM-092 actually requires — immutable *and* tamper-evident, where which of
the two catches a given attack is an implementation detail. Each test still asserts *which* outcome
it expects, so a silent weakening from `PREVENTED` to `DETECTED` fails rather than passing.

**Interfaces enumerated:** the application (asserted by construction — the repository and service
expose no update, delete, purge or upsert at all), direct SQL as `ledgr_app` (22 statement forms),
as `ledgr_migrator` (13, including replacing the sealing function and the verifier itself), as
`ledgr_ops`, and the superuser/migration interface.

Three attacks in that list are worth naming because they are the ones a privilege-only review misses:
an **upsert onto an existing entry** (`ON CONFLICT … DO UPDATE` is an UPDATE wearing an INSERT's
clothes), a **rewrite rule** (`CREATE RULE … DO INSTEAD NOTHING` would silently swallow updates), and
**`TRUNCATE organization CASCADE`**, which reaches audit_log without naming it — and which is why
`audit_log_no_truncate_trg` exists as its own statement-level trigger.

**The two attacks that win are proved, not omitted.** Truncating the tail and rewriting the whole
chain consistently both leave a chain that verifies perfectly; both assert `DETECTED_BY_ANCHOR`. The
chain-rewrite test does the attacker's actual work — recomputing every hash with the same algorithm
Postgres uses. A tamper-evidence document showing only the cases it wins would be evidence of
nothing, and these are the argument for external anchoring being a deployment requirement.

**It has never been executed.** No Postgres is available in the environment it was written in, so it
is 63 DB-gated tests that CI runs and I have not. Every one of the 46 hand-written SQL statements was
parse-checked against the Postgres dialect, because a typo would surface as a statement erroring on
syntax rather than on the guard under test — which `PREVENTED` would happily accept. That reduces the
risk; it does not remove it. **The GA checklist item is not satisfiable until this suite has actually
run green.**
- **Nothing calls `record()` yet.** The categories IAM-090 names map to features that mostly do not
  exist (postings, filings, approvals). The enum declares all nine so a missing caller is visible as
  a category with no producer rather than as a category nobody thought of; wiring the ones whose
  features DO exist — authorization outcomes, permission changes, exports — is the obvious next step
  and is not in this change.
- **IAM-094 (customers search and export their own log) is half built.** `AuditLog.search` and the
  RLS policy are the query and the boundary it needs; there is no HTTP surface.
- **IAM-095 (SIEM streaming) is not built.**
- **`0019_audit_log.sql` has not been executed here.** Parse-checked against the Postgres dialect
  only; CI applies it and runs every trigger, privilege and role case for real.
