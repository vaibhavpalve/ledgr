# ADR-003: RLS enforcement mechanism — roles, session variable, fail-closed behavior

- **Status**: Accepted
- **Date**: 2026-08-20

## Context

ADR-002 fixed the *shape* of the tenancy schema. This decision is about the *mechanism* that
makes IAM-002 ("tenant isolation is enforced at the data layer... not only in application code")
actually hold at the database, for every operation (SELECT/INSERT/UPDATE/DELETE) on every
tenant-scoped table, and about what happens when a request somehow reaches Postgres without
tenant context.

Writing explicit per-operation policies (rather than ADR-002's original blanket `FOR ALL`
policies) surfaced a real bug: `administration`'s policy checked
`app.has_administration_access(id)`, which looks the row up in the `administration` table
itself — during an INSERT, that row doesn't exist yet, so the check always failed and no
administration could ever be created. Fixing that properly required deciding how organization
and administration rows get created at all, since PRD §5.1's bootstrap cases (self-signup;
FR-MDL-004's firm-creates-an-unclaimed-client) have no tenant context to check against — the row
being inserted *is* the tenant context.

## Decision

**Three Postgres roles, each with one job:**

| Role | Login | BYPASSRLS | Purpose |
|---|---|---|---|
| `ledgr_migrator` | yes (ops/CI only) | no | Owns every table. Runs migrations. |
| `ledgr_app` | yes | no | What the API authenticates as for every request. SELECT/INSERT/UPDATE grants only — DELETE is never granted. |
| `ledgr_bootstrap` | **no** | **yes** | Owns exactly two `SECURITY DEFINER` functions. Nothing can open a session as it. |

`FORCE ROW LEVEL SECURITY` is set on every tenant table, so `ledgr_migrator`'s ownership does not
exempt it from policies — table ownership stops being a bypass path.

**Session variable**: `app.current_org_id`, set once per request via
`SELECT set_config('app.current_org_id', $1, true)` — the third argument (`is_local`) is what
makes this transaction-scoped rather than session-scoped, which matters because the API sits
behind a connection pool. Every policy reads it through one function,
`app.current_org_id()`, which wraps `current_setting(..., true)` so a missing value is SQL
`NULL`, not an error.

**Fail closed**: `NULL = anything` is `NULL`, not `TRUE`, in SQL's three-valued logic. Every
policy predicate below ultimately reduces to a comparison against `app.current_org_id()`, so an
absent session variable makes every policy evaluate to "not satisfied" — zero visible rows, on
every table, for every statement type. There is no second code path for "tenant context missing"
that falls back to something more permissive; the same predicate that grants access is the one
that denies it when unset.

**The organization-creation bootstrap problem**: solved by two `SECURITY DEFINER` functions
(`app.signup_self_managed_organization`, `app.create_firm_client_administration`) owned by
`ledgr_bootstrap`. They run with `ledgr_bootstrap`'s privileges, including `BYPASSRLS`, which
lets them insert the very first row of a new tenant relationship where no session context could
possibly authorize it yet. `ledgr_app` gets `EXECUTE` on these two functions and nothing else —
it can call them, but the bypass they carry does not leak onto any other statement `ledgr_app`
runs directly.

Full implementation: [apps/api/migrations/0001_tenancy_core.sql](../../apps/api/migrations/0001_tenancy_core.sql).

## How a developer could accidentally bypass this, and what stops it

| Bypass vector | What stops it |
|---|---|
| App code (or a debugging session) connects as a role with table ownership or superuser, which Postgres exempts from RLS by default | `FORCE ROW LEVEL SECURITY` on every tenant table removes the owner exemption. The only role exempt from RLS at all is `ledgr_bootstrap`, which is `NOLOGIN` — no connection string can ever authenticate as it. |
| `SET app.current_org_id = ...` (session-scoped) instead of `SET LOCAL` / `is_local=true`, combined with a pooled connection: tenant A's context silently persists onto tenant B's later query on the same physical connection | The migration's contract requires `is_local=true`. This is enforced by convention at the call site (the API's single DB-session helper), not by the database — call this out as the one link in the chain that lives in application code, and see the follow-up below. |
| A new `VIEW` or `MATERIALIZED VIEW` created over these tables, owned by `ledgr_migrator` or another role, silently applying the *creator's* row-security context instead of the querying role's | Any view over a tenant table must be created by `ledgr_app` (so RLS applies to the actual caller) or explicitly marked `security_invoker = true` (PG15+). This is a review checklist item, not something the schema itself can force — flagged here so it doesn't get missed. |
| A background worker or ops script (OCR queue consumer, one-off fix) connects directly to Postgres, bypassing the API's request-scoped context-setting helper entirely | If it connects as `ledgr_app` and forgets to set `app.current_org_id`, it sees zero rows — safe by default. The risk is a script reaching for `ledgr_migrator` or a superuser "just this once": `FORCE ROW LEVEL SECURITY` still applies to `ledgr_migrator`, and no human-usable role in the system carries `BYPASSRLS`. Getting genuine cross-tenant access (e.g. the NFR-033 nightly integrity job) requires a role decision at the infra level, gated the same way as any other privileged access (IAM-071/IAM-074's JIT approval flow) — not a connection string swap. |
| Someone grants `BYPASSRLS` to a new role "temporarily," for convenience, and it outlives the reason it was added | `ledgr_bootstrap` is the only role with `BYPASSRLS`, is `NOLOGIN`, and owns only the two functions above. Any future `GRANT ... BYPASSRLS` is then a visible, reviewable diff against a baseline of exactly one such role — not a norm the codebase already has three or four exceptions to. |
| App-layer code that filtered by `organization_id` gets refactored and the filter is accidentally dropped | This is the case IAM-002 is actually written for: application-layer filtering was never the control, RLS is. Dropping an app-layer filter changes nothing about what rows the query can see — the isolation guarantee doesn't depend on that code existing at all. |

## Alternatives considered

| Option | Rejected because |
|---|---|
| Single application role with table ownership, no `FORCE ROW LEVEL SECURITY` | The default Postgres behavior (owners bypass RLS) would make the owning role a standing, silent exception to every policy in the file — the opposite of "impossible by design." |
| Grant `ledgr_app` a direct `INSERT` policy on `organization`, gated by some "is this a signup request" flag | Any such flag lives in a column or a claim the request carries — which is exactly the kind of ambient, spoofable signal IAM-034 rules out ("client-side hiding... is presentation only and never the control"). A `SECURITY DEFINER` function has a fixed, reviewed body; a flag-gated policy has to be trusted every time it's evaluated. |
| Let the API's ORM layer enforce tenant filtering (skip DB-level RLS, rely on IAM-002's "application-layer checks" as primary) | This is the literal thing IAM-002 prohibits: application code as the *first* line rather than the second. It would also mean every new query site is a new place isolation can be gotten wrong, instead of a property that holds regardless of the query. |
| A single Postgres role for everything, with `app.current_org_id` as the only isolation mechanism (no role separation) | Works for ordinary requests but leaves no clean answer for how the very first row of a tenant relationship gets created, and makes "who can legitimately bypass RLS" a question about scattered code paths instead of about one narrowly-owned pair of functions. |

## Consequences

- Every future tenant-scoped table follows the same shape: `organization_id`/`administration_id`
  columns, `FORCE ROW LEVEL SECURITY`, policies built on `app.has_administration_access()` or
  `app.is_own_administration()`, no `DELETE` grant unless a real requirement needs row deletion.
- The `SET LOCAL` / transaction-scoping requirement is a contract the API's database-session
  layer must uphold; it is not enforced by Postgres itself. IAM-005's automated isolation tests
  should include a test that opens two "requests" on the same pooled connection back-to-back and
  asserts the second never sees context left over from the first.
- `ledgr_bootstrap`'s two functions are now the highest-scrutiny code in the schema — any change
  to them is a change to the entire BYPASSRLS surface of the system and warrants review on that
  basis alone.
- Not addressed here, deferred to a follow-on decision: a fourth role for legitimate cross-tenant
  batch reads (the NFR-033 nightly integrity job, ops reporting) — mentioned in the bypass table
  above but not implemented, since it belongs with the JIT/approval tooling in IAM-071/IAM-074
  rather than in this migration.
