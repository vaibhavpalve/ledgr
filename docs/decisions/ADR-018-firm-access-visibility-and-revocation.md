# ADR-018: Firm access visibility and revocation

- **Status**: Accepted
- **Date**: 2026-08-27
- **Implements**: IAM-109, IAM-110 (PRD §8.6)

## Context

> **IAM-109.** The client sees, at any time and without asking, which firm users hold access to their
> administration, with what role, since when, and when each last accessed it.
>
> **IAM-110.** On revocation of firm access, firm sessions for that administration terminate
> immediately, the administration disappears from the firm switcher, and the client retains all data
> including entries the firm made.

Three of IAM-109's four columns already existed on `role_assignment`: **who** (`user_id`), **with what
role** (`role_id`), **since when** (`created_at`). ADR-017's `granted_by_organization_id` added the
provenance that says which of those rows are the firm's rather than the client's own. Only "when each
last accessed it" had nowhere to live — nothing in this system had ever recorded that a user *looked
at* an administration.

IAM-110 presumes something too: "sessions **for that administration**". Sessions (0003) are per user
and carry no administration context, because a firm employee works across many clients in one login.
Without that context the only options are terminating their entire login — cutting them off from
every other client — or terminating nothing.

## Decision

### `administration_access`: one row per (user, administration), not an event log

IAM-109 asks for "when each last accessed it" — a single timestamp. An append-only event log would
mean a row per page view forever, plus an aggregation on every read of a screen the client is invited
to check *at any time*. So: one row, upserted, carrying first/last access and a count.

This is deliberately **not** the general-purpose audit trail (IAM-090+, not built). It answers one
question for one screen; an auditor wanting full history will want that other system.

**Recording is throttled** to one write per five-minute window. A write on every request would put a
write on the read path of every page a firm user opens. The window is a lower bound on staleness,
never an upper bound on truth: the recorded timestamp is always a real access, only possibly an
earlier one than the most recent.

`administration_access_monotonic_trg` clamps `last_accessed_at` so it never moves backwards. On the
ordinary upsert path the throttle already absorbs a stale timestamp (an `at` older than the stored
value never clears the cutoff), so the trigger defends the paths that skip it — a backfill, an ops
script, a future service writing the row directly. That distinction is now explicit in the tests:
one exercises the throttle, one exercises the trigger through a direct-write path.

### "At any time and without asking" is a requirement about power

The phrase rules out two otherwise-reasonable designs. It rules out a report the firm generates and
sends, because that makes the client's visibility depend on the cooperation of the party the
visibility exists to check. And it rules out anything the firm can switch off.

Where that actually holds is the RLS policy: the administration's owning organization can read every
`administration_access` row on it. No firm involvement, nothing to request, nothing to enable.

The register lists **live** access only — "which firm users *hold* access", present tense. A client
scanning the list to decide whether to revoke must not have to work out which rows are still in
force. History remains available in the append-only `role_assignment` table.

A firm user who has **never** opened the client's books still appears, with a null last-access. That
is the row a client most needs to see, and an INNER JOIN on the access table would silently drop
exactly it.

### `sessions.active_administration_id`

The administration a session is currently working in — what a firm switcher sets. It carries **no
authority**: every request still evaluates live grants (IAM-034), so a session pointing at an
administration whose grants were revoked is denied exactly as if the column were empty. It exists so
IAM-110 can name *which* sessions to act on.

### IAM-110's three clauses are three different kinds of guarantee

**"Terminate immediately"** is mostly already true and needs no code: authorization is evaluated per
request against live grants, so the moment the grants are revoked the firm's next request is denied.
Nothing is cached, so there is nothing to invalidate and no window to close.

What needs code is the session sitting *inside* the revoked administration. Clearing the context is
deliberately narrower than revoking the session: `revoke_session` would sign the user out of LEDGR
entirely, while `clear_administration_context` puts them back at the switcher — which is what losing
one client means when they still have four others. A firm employee whose only client revokes them
lands at an empty switcher, which is correct and is not the same as being signed out.

**"Disappears from the firm switcher"** is a consequence, not a step. `switchable_administrations`
derives the list from live grants every time it is called, so an entry vanishes because the grant
that put it there is gone — not because revocation remembered to remove it from somewhere. A cached
list would be one more place a revocation could be forgotten. Demonstrated by a grant *expiring*,
which no revocation code path touches at all.

**"The client retains all data"** is a pure non-action, and the hardest to test meaningfully because
what it forbids is a cascade nobody wrote. It holds structurally: no table grants DELETE to
`ledgr_app`, no foreign key uses `ON DELETE CASCADE`, and the ledger is append-only by CLAUDE.md's
second rule. `RevocationReceipt.retained` states it positively rather than leaving the absence of
deletion to be inferred — a client asking "what happens to my books if I fire my accountant" deserves
an answer, not silence.

The access *history* survives too. `administration_access` has no DELETE grant, so a firm losing
access cannot thereby erase the record of what it did.

### Only the client may revoke

IAM-105 puts "the ability to revoke the firm's access" in the client rights floor, so
`revoke_firm_access` refuses unless the acting organization owns the administration. Not because a
firm revoking itself would be harmful, but because letting it would make the audit trail lie about
who ended the relationship.

### Ordering inside revocation is load-bearing

Grants first — that is the step that actually stops access. Session context after, as housekeeping.
If the process died between the two, the firm would be locked out with a stale screen, which is the
failure direction that costs nothing.

## Alternatives considered

| Option | Rejected because |
|---|---|
| An append-only access event log instead of one row per (user, administration) | A row per page view forever, and an aggregation on every read of a screen the client is invited to check at any time. IAM-109 asks for one timestamp. |
| Recording every access without throttling | Puts a write on the read path of every page a firm user opens, to gain precision nobody asked for — "when were they last in my books" is answered by minutes, not milliseconds. |
| Generating the register as a report the firm sends the client | Makes the client's visibility depend on the cooperation of the party it exists to check. "Without asking" rules it out. |
| INNER JOIN on `administration_access` | Silently drops the firm user who holds access and has never used it — the row a client most needs to see. |
| Revoking firm users' sessions outright on revocation | Cuts a firm employee off from every other client they work on. Losing one client means returning to the switcher, not being signed out. |
| Storing the firm switcher as a list, updated on revocation | One more place a revocation could be forgotten. Deriving it from live grants makes "disappears" a consequence rather than a step. |
| Letting the firm revoke its own engagement | IAM-105 makes revocation the client's right; a firm-initiated revocation recorded as the client's would make the audit trail wrong about who ended the relationship. |
| Clearing session context before revoking grants | The step that actually stops access would come second; a crash between them would leave the firm still able to work with a cleared screen. |

## Consequences

- **Fourteen guards are mutation-tested** — the register including client users, dropping
  never-accessed users, showing expired grants as live; the throttle removed; the monotonic guard
  removed on the direct-write path; first-access overwritten; revocation not revoking grants,
  revoking everyone's grants, not clearing session context, clearing the wrong administration's;
  the firm permitted to revoke itself; a non-active engagement revocable; the switcher no longer
  filtering by live grants. All produced real test failures.
- **Two mutations initially passed and both were my test's fault, not the code's.** One used a
  non-unique anchor and mutated a different method than intended; the other asserted the monotonic
  guard but was actually satisfied by the throttle upstream of it. Both are now anchored and
  exercised properly, and the second produced a genuinely better pair of tests.
- **`record_access` is wired into `require_permission`** (added after the first draft of this ADR;
  see the "Wiring" section below). Every authorized administration-scoped request records an access,
  so the register's last-access column populates in a running system.
- **`active_administration_id` is set by `PUT /v1/switcher/{administration_id}`** (same addendum).
  IAM-110's session clause is now reachable end to end, not merely implemented.
- **Access is recorded for client users too, not only firm staff.** Filtering at write time would
  mean deciding who counts as firm staff on the hot path — a decision the read side already makes
  from provenance, correctly and once. It also gives a firm the same view of its own staff's activity.
- **IAM-111 is not built** — client access profiles and their changes appearing in both audit logs.
  `RevocationReceipt` is structured so a caller can record it once that log exists.
- **`0017_firm_access_visibility.sql` has not been executed here.** Parse-checked against the
  Postgres dialect only. It now has DB-gated coverage (see below), so CI applies it and exercises the
  trigger, the RLS policies and the upsert for real rather than merely applying it.

## Addendum: wiring, and the coverage that was missing

The first version of this ADR listed three loose ends. Closing them changed enough to be worth
recording.

### `record_access` belongs in `require_permission`, not in routes

It runs on exactly the requests that constitute an access, after they are known to be authorized, and
it has already resolved which administration and which user. A per-route call would be forgettable in
precisely the way `AuthorizationEnforcementMiddleware` exists to prevent.

Two constraints fall out of where it sits. Only **administration** targets are recorded — an
organization-level action is not access to anybody's books. And it runs **after** the deny check, so
a refused probe cannot write a row claiming someone was here.

It **participates in the request's transaction** rather than being best-effort. An access to a
client's books that could not be recorded should not complete: IAM-109 exists to stop exactly the
invisibility a silently-dropped record would create. The cost is a row lock per (user,
administration) — contention only between one person's own concurrent requests on one administration
— and the throttle makes the common case a no-op update rather than a rewrite.

### The switcher needed a session identifier

`TenantContext` gained `session_id`, read from a `sid` claim, because IAM-110's write has to name
*which* session to move. A token without one is refused rather than falling back to "all of this
user's sessions", which would move devices the person is not holding. `set_active_administration` is
scoped to `user_id` as well as `session_id`, so a leaked session identifier cannot reposition someone
else's session.

`GET /v1/switcher` is in `AUTHORIZATION_EXEMPT_PATHS`; `PUT /v1/switcher/{id}` is not. Listing returns
only administrations the caller already holds a live grant on, so it discloses nothing they cannot
already reach — and requiring an organization-scoped permission would break it for exactly the users
it exists for, since firm staff grants are administration-scoped (IAM-107). *Entering* one reaches
into it, and carries a real check.

`switch_to` re-checks the switcher even though the route already authorized `view administration`,
because the two sets differ: an organization Owner passes the permission check on every administration
their organization owns without appearing in a firm switcher. Writing a context the switcher would not
offer would put a session somewhere the UI cannot represent.

### Adding a dependency to `require_permission` broke every HTTP test at once

`get_firm_access_register` depends on `get_db_session`, so tests overriding only
`get_authorization_service` suddenly reached for a real database and hung. Worth recording because it
is a property of the design rather than an accident: `require_permission` is declared by every route,
so anything added to it is added to every route's dependency graph.

### Two guards were only covered by DB-gated tests, which is not coverage

Both the `record_access` wiring and `switch_to`'s checks were exercised only by the integration
suite, which skips without Postgres — so they could have been deleted and every no-DB CI run would
still have passed. Both now have no-DB tests against the in-memory fakes, and mutation testing
confirms all six new guards fail a test when broken.

One of those mutations initially passed and the test was at fault, not the code: deleting the
"no session identifier" guard still refused the request, because the repository's user-scoped lookup
matches no row for a null id. The behaviour held, but the caller would have received "session … is
not a live session for this user" — wrong and unactionable for a token that simply lacks a `sid`. The
test now asserts the reason, not just the refusal.

### 0017 now has DB-gated coverage

`tests/integration/test_authorization_isolation.py` exercises what only Postgres can: the monotonic
trigger clamping a backwards direct UPDATE, `first_accessed_at` resisting rewrite, the absence of a
DELETE grant, the RLS policy letting the **client** read a row the **firm** wrote (IAM-109's "without
asking", as SQL), the real `ON CONFLICT … WHERE` throttle, and `set_active_administration`'s
user-scoping. Two of the new routes also carry IAM-005 isolation tests — which the coverage harness
demanded the moment they were registered.
