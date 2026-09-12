# LEDGR

Monorepo scaffold. See [CLAUDE.md](CLAUDE.md) for architecture rules and [prd.md](prd.md) for
product requirements. This repo currently contains shell only — no application code, no database
schema.

## Layout

```
apps/
  api/    FastAPI service (Python, uv-managed)
  web/    React + TypeScript SPA (Vite)
packages/
  shared-types/   TypeScript types shared with the web app
  i18n/           The message catalogue and its runtime (FR-LOC-001)
  offline-queue/  The offline capture queue's policy, platform-free (FR-EXP-001f, MOB-003)
docker-compose.yml   Local Postgres + Azurite (Azure Blob emulator)
Makefile             make dev / make test / make lint / ...
```

## Prerequisites

Install these on a clean machine before anything else:

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose v2
- [Node.js](https://nodejs.org/) 20+
- [pnpm](https://pnpm.io/installation) 9+ (`corepack enable` will provide it on Node 20+)
- [Python](https://www.python.org/) 3.12+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) for Python dependency management

## Setup

```bash
git clone <repo-url>
cd Boekly

cp .env.example .env
# fill in .env with local values — never commit it

make install   # pnpm install + uv sync
make dev-up    # start Postgres and Azurite in Docker
```

## Everyday commands

| Command | What it does |
|---|---|
| `make dev` | Starts local infra (Postgres, Azurite) and the web dev server |
| `make test` | Runs the web/shared-types test suites (Vitest) and the API test suite (pytest) |
| `make lint` | ESLint over the TypeScript packages, Ruff over the API |
| `make format` | Prettier over the TypeScript packages, Ruff formatter over the API |
| `make typecheck` | `tsc --noEmit` over the TypeScript packages, mypy over the API |
| `make dev-down` | Stops the local Docker infra |
| `make scan` | Dependency vulnerability scan (pnpm audit, pip-audit) — same check CI enforces |
| `make secret-scan` | Runs Gitleaks against the working tree (requires Docker) |

Each of `test`, `lint`, `format`, `typecheck`, `scan` can also be run scoped to one side, e.g.
`make test-api` or `make lint-web`.

## Tenant isolation tests (IAM-005)

`apps/api/tests/` enforces "a new endpoint cannot ship without an isolation test" structurally:
`make test-api` always runs `test_isolation_coverage.py`, which fails if any registered FastAPI
route lacks a `@pytest.mark.isolation(method, path)` test — no database needed for that check, so
it always runs.

The isolation tests themselves (`apps/api/tests/integration/`) need a real Postgres with
migrations applied, since they assert against actual row-level security, not a mock of it:

```bash
make dev-up            # Postgres + Azurite
make test-api-db-setup # applies migrations, sets ledgr_app's test password
make test-api-isolation
```

`make test-api-db-setup` needs `TEST_DATABASE_ADMIN_URL` (a superuser connection) and
`TEST_LEDGR_APP_PASSWORD` in `.env`; `DATABASE_URL` must then point at the same database as
`ledgr_app`. Without this setup, `make test-api` still passes — the DB-backed tests skip with a
clear reason rather than failing — but CI always runs them for real (see below).

Docker is not required. Any PostgreSQL 16 will do, including a local install: `initdb` a throwaway
cluster on a spare port with `-A trust`, `createdb ledgr_test`, and point the three URLs at it.
`OPS_DATABASE_URL` needs a password set on `ledgr_ops` (the migrations create it `PASSWORD NULL`),
which is what `scripts/load_rgs_version.py` and `scripts/verify_ledger_integrity.py` connect as.

## Envelope encryption (SEC-022, IAM-004, SEC-023)

`apps/api/src/api/crypto/` implements the per-tenant data-encryption-key (DEK) / key-encryption-key
(KEK) hierarchy described in
[ADR-004](docs/decisions/ADR-004-envelope-encryption.md). `make test-api` always runs the pure-logic
suite (`tests/crypto/`) against a local, in-memory KMS stand-in — no database or real KMS needed.
The DB-backed tests (`tests/integration/test_encryption_key_isolation.py`) follow the same
`TENANT_ISOLATION_TESTS_ENABLED` gating as the rest of the isolation suite above.

`KMS_PROVIDER=local` (the default) is dev/test only. Production sets `KMS_PROVIDER=azure-key-vault`
with `AZURE_KEY_VAULT_URL`/`AZURE_KEK_NAME` — the app authenticates via managed identity, never a
stored credential.

Three operator scripts, all connecting as `ledgr_ops` (`OPS_DATABASE_URL`) — the narrowly-scoped,
cross-tenant role documented in ADR-003/ADR-004:

| Script | Use |
|---|---|
| `scripts/rotate_annual_keys.py` | Scheduled job: rotates every administration's DEK (SEC-023) |
| `scripts/emergency_rotate_key.py --administration-id <uuid>` | One tenant's DEK is suspected compromised |
| `scripts/rewrap_after_kek_rotation.py --old-kek-key-id <id>` | After the KMS-side KEK rotates: re-wraps every DEK still on the old version |

## Authentication foundation (IAM-013, IAM-016)

`apps/api/src/api/auth/` implements user records, password credential storage, and session
issuance/validation — see [ADR-005](docs/decisions/ADR-005-authentication-foundation.md). No login
HTTP endpoint or UI exists yet; this is the service layer a future login flow will call.
`make test-api` always runs the pure-logic suite (`tests/auth/`) against in-memory fakes and a
local (no-network) breach checker; `tests/integration/test_auth_schema.py` follows the same
`TENANT_ISOLATION_TESTS_ENABLED` gating as the rest of the DB-backed suite.

`BREACH_CHECKER_PROVIDER=local` (the default) never calls the network. Production sets
`BREACH_CHECKER_PROVIDER=hibp` to screen against the real Have I Been Pwned corpus.

Google sign-in (IAM-010a, IAM-010b, IAM-010c) is part of the same package —
[ADR-006](docs/decisions/ADR-006-google-sign-in.md) covers the OIDC/PKCE flow, why the scope list
(`openid email profile` only) must never grow, why `email_verified` is checked inside the OIDC
client itself rather than left to a caller, and why linking a Google identity to an existing
account requires re-authenticating with the existing method first — enforced by the service, not
just documented. `tests/auth/test_google_account_linking.py` includes the attack case: an
attacker who controls a verified Google account for a victim's email gains nothing without the
victim's password. `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`GOOGLE_REDIRECT_URI` configure it;
no HTTP endpoint or UI exists yet.

WebAuthn passkeys (IAM-010) round out the three sign-in methods —
[ADR-007](docs/decisions/ADR-007-webauthn-passkeys.md) covers the registration/authentication
ceremonies, why clone detection reuses the `webauthn` library's own signature-counter check rather
than a reimplementation, and how individual revocation is enforced (checked before cryptographic
verification runs, and scoped to exactly one passkey). `tests/auth/webauthn_helpers.py` builds
real, cryptographically valid ceremonies for tests — a genuine EC keypair, CBOR/COSE encoding, and
ECDSA signatures — so the test suite exercises the real verification path, not a mock of it.
`WEBAUTHN_RP_ID`/`WEBAUTHN_RP_NAME`/`WEBAUTHN_ORIGIN` configure it.

MFA policy (IAM-011, IAM-012, IAM-010e) ties the three sign-in pieces together —
[ADR-008](docs/decisions/ADR-008-mfa-policy.md) covers the interim "always required" policy (a
deliberately conservative stand-in for the not-yet-built RBAC/VAT-filing triggers), why TOTP
(`api/auth/totp.py`) reuses the document-encryption KMS rather than a second key scheme, and the
closed two-member factor enum that keeps SMS from being addable without a code change.
`api/mfa_middleware.py` enforces this on every non-exempt request — middleware ordering
(`TenantContextMiddleware` must run before it) was verified empirically, not assumed, before being
wired into `main.py`.

IAM-010f's account-continuity control — [ADR-009](docs/decisions/ADR-009-google-only-account-continuity.md)
— blocks a Google-only account from posting to the ledger until it adds a passkey or password, so
losing access to Google can never lock someone out of statutory records. Deliberately *not*
middleware, unlike MFA: it gates one specific action, so it's a FastAPI dependency
(`api/auth/account_continuity.py`'s `require_ledger_posting_eligibility`) a future ledger-posting
route will declare, the same per-endpoint mechanism `get_tenant_context`/`get_db_session` already
use.

Session listing, recovery, and abuse prevention round out this package —
[ADR-010](docs/decisions/ADR-010-session-listing-recovery-and-rate-limiting.md) covers IAM-017
(`SessionService.list_sessions_with_location`, location resolved on demand via
`api/auth/geolocation.py`, never stored), IAM-018 (`api/auth/account_recovery.py` — recovery via a
proven TOTP code or passkey, or an admin-initiated reset that mandates an identified actor and a
reason, never via email alone), and IAM-019 (`api/auth/rate_limiting.py`'s progressive lockout and
credential-stuffing detection, backed by Postgres rather than an in-memory counter since this stack
runs multiple worker processes; `api/rate_limit_middleware.py` is ready to protect real
authentication endpoints once they exist, but isn't wired into `main.py` yet since none do).

## Authorization (IAM-030 – IAM-037)

`apps/api/src/api/authz/` is **the** authorization library — CLAUDE.md's third non-negotiable, "one
authorization library, used everywhere; there is no second implementation." See
[ADR-011](docs/decisions/ADR-011-authorization-model.md). Permissions are `(action, resource_type,
resource_scope)` triples, roles are named bundles (PRD §8.4's twelve standard roles are seeded from
Appendix A by `migrations/0009_authorization.sql`), and assignments are scoped to one organization
or one administration. Nothing is cached: `AuthorizationService.authorize()` reads live rows on
every call, so a revoked grant stops working on the next request rather than within IAM-037's
60-second bound.

A route declares what it needs:

```python
@app.post("/v1/administrations/{administration_id}/journal-entries")
async def post_entry(
    administration_id: uuid.UUID,
    _: AuthorizationDecision = Depends(
        require_permission(
            "post", "journal_entry",
            scope=administration_from_path("administration_id"),
            attributes=AttributeSources(
                amount=from_body("total_amount"), journal_id=from_body("journal_id")
            ),
        )
    ),
) -> ...:
```

**A new endpoint cannot forget to declare one.** `api/authz_middleware.py` inspects the matched
route's dependency tree before dispatching and answers 500 (not 403 — a missing check is a bug, not
a denial) for any route declaring neither a requirement nor an explicit exemption, so the handler
never runs. `tests/test_authz_coverage.py` walks the same route table at collection time and fails
CI first, naming the route; it needs no database, so it runs on every CI invocation, exactly like
the IAM-005 check above. Opting out means adding a path to `AUTHORIZATION_EXEMPT_PATHS` in
`api/authz/dependencies.py` with a reason — one reviewable list, and a test fails if it names a
route that no longer exists.

IAM-033 attribute conditions (`amount_ceiling`, `cost_centre_ids`, `journal_ids`, `period_ids`,
`ip_allowlist`) live on the individual assignment, so an Approver's ceiling varies per person rather
than being frozen into the role. Every omission fails closed: an unknown condition key denies rather
than being ignored, and an attribute the request never supplied denies rather than counting as
unrestricted. Amount ceilings are stored and compared as decimals end to end — request bodies are
parsed with `parse_float=Decimal` and the migration refuses a ceiling stored as a JSON number
(NFR-031).

`make test-api` always runs `tests/authz/` against the in-memory repository fake; the DB-backed
tests (`tests/integration/test_authorization_isolation.py`, which assert the guard triggers and RLS
policies actually fire) follow the same `TENANT_ISOLATION_TESTS_ENABLED` gating as the rest.

### Appendix A is the source of truth for the standard roles

PRD §8.4's twelve standard roles and Appendix A's 31 × 10 capability matrix are encoded as data in
[matrix.py](apps/api/src/api/authz/matrix.py) — see
[ADR-012](docs/decisions/ADR-012-appendix-a-as-source-of-truth.md). No conditional anywhere
special-cases a role by name; `Owner` holds everything because reading its column yields everything.
Four artifacts, three mechanical checks, so drift is caught rather than noticed:

```
prd.md  ──①──▶  api/authz/matrix.py  ──②──▶  0010_role_catalogue.sql  ──③──▶  database
```

| | Check | Needs a DB? |
|---|---|---|
| ① | `tests/authz/test_appendix_a_conformance.py` parses Appendix A and §8.4 out of `prd.md` and asserts the module matches — every cell, row order, column order, role names, scopes | no |
| ② | `tests/authz/test_role_catalogue_generation.py` re-renders the migration and fails if the checked-in file is stale (CI also runs `--check` before migrating) | no |
| ③ | `tests/integration/test_authorization_isolation.py` asserts the live `permission`/`role`/`role_permission` rows match what the module derives | yes |

### Custom roles (IAM-036)

An organization admin can compose a role from raw permissions, from other roles, or both —
`AuthorizationService.create_custom_role`, see
[ADR-013](docs/decisions/ADR-013-custom-role-composition.md). Three gates, in order: the author must
hold `manage user_role` in that organization; every component must be visible, unarchived and
scope-compatible; and the **flattened** effective set — explicit permissions plus everything the
components bundle — must be within what the author holds **unconditionally**.

Two properties do the work:

- **Composition is flattened at creation.** `role_component` records provenance and is never read
  when a request is authorized. A component gaining permissions later cannot widen a role already
  composed from it, evaluation stays a flat join, and nesting is transitive for free (every role's
  rows are already its closure — nothing recurses).
- **A conditioned grant doesn't raise the ceiling.** An Approver capped at €5,000 doesn't hold
  "approve purchase invoices"; conditions live on assignments, so the uncapped permission isn't
  theirs to hand out. Without this, an admin holding both a capped grant and `manage user_role`
  could author an uncapped role and assign it to themselves.

`assign_role` carries the same ceiling, because an admin barred from *authoring* a role granting
more than they hold could otherwise just assign an existing one that does.

`tests/authz/test_custom_roles.py` covers the escalation routes: directly, through nesting, through
two levels of nesting, through conditions, through nesting a conditioned role, across organizations,
via expired and revoked grants, and the full compose-then-self-assign chain. Each guard was verified
by breaking it and confirming a test fails.

### Segregation of duties (IAM-060 – IAM-065)

`apps/api/src/api/authz/sod.py` — see
[ADR-014](docs/decisions/ADR-014-segregation-of-duties.md). Four prohibitions, and the qualifiers in
them are load-bearing:

| Rule | | Note |
|---|---|---|
| IAM-060 | invoice creator/editor cannot be the **sole** approver | may approve when a 2nd approval is still required |
| IAM-061 | payment approver cannot release | only **above two** active users |
| IAM-062 | expense submitter cannot approve it | absolute — no "sole" escape |
| IAM-063 | nobody grants themselves a permission they lack, or approves their own access request | not deviable |

Rules are pure functions over a `DutyContext`; the service applies exemptions *after* a rule objects,
so an exemption is only recorded when it actually changed the outcome. SoD **composes with**
`authorize()` rather than replacing it — a caller needs both.

**IAM-064 deviations are rows, not settings.** Each names the rule, the Owner who acknowledged it, a
non-blank reason (a CHECK, not just NOT NULL), and when — immutable except for revocation, so a
justification can't be rewritten after an auditor asks. Authority is checked against the Owner
**role by name**, not a permission, because any permission-based check could be satisfied by a custom
role composed to hold it (IAM-036) — routing around "the Owner acknowledged this."

**IAM-065 exemptions are disclosed structurally.** `audit_report()` computes the exemption from the
live user count and states it in every report, including when nothing is exempt — a reader must never
infer enforcement from the absence of a warning. The single-user exemption is deliberately *not*
recorded per action (one row per action forever is not disclosure); blocked actions and every use of
a deviation are.

`tests/authz/test_sod.py` has one section per requirement. Eighteen deliberate breakages of the
guards were each confirmed to produce a real test failure.

### Client access profiles (IAM-100 – IAM-104)

`apps/api/src/api/authz/profiles.py` — see
[ADR-015](docs/decisions/ADR-015-client-access-profiles.md). In Model A the firm picks what the
**client's own** users may do inside their administration. Three built-ins ship (`Capture only`,
`Invoice and capture`, `Full self-service`), and a firm may compose its own.

**A profile is a cap, not a grant.** `authorize()` intersects it with what the user's roles already
carry, so a permissive profile assigned to someone with no role grants nothing, and no profile at all
means no cap — never no access. Firm staff are not capped by the cap they set.

**IAM-102 is the constraint that matters**: a firm cannot grant a client's users a permission the
firm itself does not hold *on that administration*. The ceiling is the firm's holdings there — not
the acting user's, since a Firm Manager holds no ledger permissions yet assigning profiles is their
job — and it is enforced on assignment **and** on publishing a new version, since checking only the
first would let a firm assign a modest profile and edit it upward.

Profiles are versioned: publishing appends an immutable version, and assignments point at the
*profile*, so a new version takes effect everywhere it's used (IAM-103's 60 seconds). IAM-104's
restrictions compile down to the existing model — the booleans withhold permissions, journal and
amount limits become ordinary IAM-033 conditions — so there's no second enforcement path. IAM-103's
notification is part of the change: a notifier that fails aborts it, because "silent reduction of a
client's access is prohibited".

`migrations/0014_builtin_client_access_profiles.sql` is generated from the same `BUILTIN_PROFILES`
definition the service and the tests read. Fifteen guards were each broken to confirm a test catches
them.

### Firm staff access (IAM-107, IAM-108)

`apps/api/src/api/authz/firm_staff.py` — see
[ADR-017](docs/decisions/ADR-017-firm-staff-access.md). Most of IAM-107 was already true and is worth
naming rather than re-implementing: **"not per firm"** holds because an organization-scoped grant
cascades only to administrations that organization *owns*, and a firm never owns its client's;
**"zero clients on hire"** holds because access is a `role_assignment` row or it doesn't exist. Both
are consequences of earlier shapes, not checks that could be forgotten.

What was missing is **provenance**. `role_assignment.granted_by_organization_id` (migration 0016) is
set by a trigger from `app.current_org_id()` — never from a parameter, so it can't be spoofed or
forgotten. That closes the limitation ADR-016 flagged: the client access profile cap now reads which
organization made a grant instead of guessing from a heuristic.

**The engagement is the bound, not a permission ceiling.** ADR-013's "can't confer what you don't
hold" can't apply here — a ceiling on the *granter* makes Firm Manager unable to do its §8.4 job
(that role holds no ledger permissions), and a ceiling on the *firm* deadlocks on a newly engaged
client where nobody holds anything yet. The active `firm_engagement` is the client's consent, and
they can revoke it. IAM-063 still applies, so a Firm Manager assigns Accountant to a colleague but
never to themselves.

IAM-108's "defined client set for a defined period" is validated in full before anything is written —
a set where one engagement has lapsed must not leave the rest granted. Ten guards were each broken to
confirm a test catches them.

### The audit log (IAM-090 – IAM-093)

`apps/api/src/api/audit/` and `migrations/0019_audit_log.sql` — see
[ADR-020](docs/decisions/ADR-020-audit-log.md). The guarantees are in the **schema**, not the
application: the threat model for an audit log includes someone holding a database connection.

**IAM-092 is enforced in four layers, each stopping something the one before it doesn't:**

| | Layer | Stops |
|---|---|---|
| 1 | No UPDATE/DELETE/TRUNCATE granted to anyone, revoked even from the owner | `ledgr_app` |
| 2 | Triggers that `RAISE` unconditionally | `ledgr_migrator` (owners aren't privilege-checked) and `ledgr_ops` — **BYPASSRLS skips RLS, never a trigger** |
| 3 | Table owned by `ledgr_audit`: NOLOGIN, granted to nobody | the owner dropping the trigger — `ledgr_migrator` doesn't own this table |
| 4 | Hash chaining | nothing — it *detects* what a superuser can still do |

Layer 4 exists because layers 1–3 stop every role this application creates and **none of them stop a
Postgres superuser**. Nothing in the database can. IAM-092 asks for immutable *and* tamper-evident
precisely because prevention alone can't survive that. One integration test drops the trigger as
superuser, edits an entry, and shows `app.verify_audit_chain` naming the row.

The hash is computed **by the database from the row's own values** — an application that could name
its own hash could forge a consistent chain. Fields are length-prefixed, so `post`+`entry` can't hash
as `poste`+`ntry`. Chains are per organization, so verification works under RLS and a break is
attributable to one tenant.

**GA evidence:** PRD §22 blocks release on *"audit log immutability verified by attempted tamper
test"*. That is `tests/integration/test_audit_tamper_evidence.py` — 63 tests enumerating every
interface (application, direct SQL as each role, migration/superuser) and classifying each attempt as
`PREVENTED`, `DETECTED`, `DETECTED_BY_ANCHOR` or `UNDETECTED`. Three attacks in there are ones a
privilege-only review misses: an upsert onto an existing entry (`ON CONFLICT … DO UPDATE` is an
UPDATE in disguise), a rewrite rule that would silently swallow updates, and `TRUNCATE organization
CASCADE`, which reaches the audit log without naming it.

**The honest limit:** an unanchored chain detects everyone except whoever owns the server — a
superuser can recompute the chain, and deleting from the *end* leaves one that verifies perfectly
(there's a test asserting exactly that negative). `scripts/anchor_audit_chain.py` collects the head
hash for external anchoring; where anchors go is a deployment decision, and the only sink today is a
log line written by the same infrastructure it checks. That's remaining work, not done work.

### Audit wiring, and the check that keeps it wired (IAM-090)

See [ADR-021](docs/decisions/ADR-021-audit-wiring.md). A route declares one thing —
`require_permission(..., audit=AuditCategory.POSTING)` — and everything else is derived: action and
resource type from the permission the route already requires, actor and tenant from the tenant
context, outcome from the status code.

**Recording is middleware, not the dependency that already runs on every route.** `require_permission`
looks like the obvious home but runs *before* the handler (so it can't know the outcome) and *inside*
the request transaction (so denials and failures roll the entry back — the entries most worth
having). `AuditMiddleware` runs after the response with its own session.

**The CI check:** `tests/test_audit_coverage.py` fails the build if a mutating route (POST/PUT/PATCH/
DELETE) declares no category and isn't explicitly exempt. Third check of this shape here, alongside
IAM-005 isolation coverage and authorization coverage; no database, runs every CI invocation. It also
asserts *it has routes to check* — a coverage check that silently checks nothing is worse than none.

Service-layer paths with no endpoint yet (permission grants/revocations, profile and SoD
configuration changes) record at the **service**, so a future route calling `assign_role` gets the
entry for free. **Refusals are recorded too** — someone repeatedly trying to grant themselves a role
is what a reviewer is looking for. Firm-side events are recorded against the **client's** log, one
entry per client, since IAM-094 is what lets a customer read their own.

**Two honest gaps.** Sign-in can't be audited per tenant — authentication happens before a tenant is
chosen and `organization_id` is NOT NULL (ADR-020); MFA denials *are* recorded, and pre-tenant
attempts stay in `auth_attempt`. And five categories (`export`, `posting`, `approval`, `filing`,
`support_access`) have no producer because the features don't exist — `tests/audit/test_wiring.py`
pins exactly which, so building a feature without wiring its event is a failing test. (`posting`
came off that list when the ledger landed; the registry entry now names `api.ledger.service` and a
test in the same file holds it to it.)

### The ledger (FR-GL-001 – FR-GL-007, FR-GL-013)

`apps/api/src/api/ledger/` and `migrations/0020_ledger.sql` — see
[ADR-022](docs/decisions/ADR-022-ledger-bounded-context.md).

**The bounded context is a privilege boundary, not a package boundary.** CLAUDE.md's first
non-negotiable — "nothing writes to posting tables except the ledger service" — is enforced by
`ledgr_app` holding **SELECT and nothing else** on `journal_entry`, `journal_line`,
`journal_sequence` and the ledger's master-data tables. No INSERT, no UPDATE, no DELETE, no
TRUNCATE. Code that ignores every convention and writes its own INSERT gets `permission denied`.

The only writers are the `SECURITY DEFINER` functions in the `ledger` schema, which run as
`ledgr_ledger` (NOLOGIN, granted to nobody, owns the tables). Their signatures *are* the narrow API.
Tenant isolation survives that elevation because RLS reads `app.current_org_id()`, which is session
state, not role state — the elevation is over **what** may be written, never over **whose** data.

Every invariant holds structurally, not by convention:

| | |
|---|---|
| Debits = credits (FR-GL-001) | deferred constraint trigger, evaluated at COMMIT |
| Immutability (FR-GL-003, CMP-009) | no grants + unconditional RAISE triggers + a NOLOGIN owner |
| Sealed at commit | `created_xid` compared on every line insert |
| Control accounts (FR-GL-006) | a biconditional trigger: control ⟺ names a party |
| Gapless numbers (FR-GL-013) | an allocator row, not a `SEQUENCE` |
| Decimal only (NFR-031) | `numeric(19,2)`; jsonb amounts must be strings, never JSON numbers |

Three worth calling out. **An empty entry** passes a naive balance check — `SUM()` over no rows is
NULL and `NULL <> 0` is not TRUE — so the line count is the check, not decoration. **Sealing** is
what stops a line being *appended* to yesterday's entry, which unbalances it as effectively as
editing one and which withheld UPDATE/DELETE does nothing about. And **the sub-ledger is derived**:
a line on a control account must name a party and a line elsewhere must not, so AR/AP *is* the
control account's own lines grouped by party. FR-GL-006's "reconciled continuously" is a property of
the schema, not a job that can fall behind.

**Gaplessness costs concurrency**, and that's the trade: postings to the same journal in the same
year serialise on one allocator row. A Postgres `SEQUENCE` is non-transactional by design and would
leave a hole on every rollback — disqualifying for a statutory series (CMP-009).

One case table (`tests/ledger/cases.py`) runs twice: against an in-memory fake that reimplements the
invariants, and against real Postgres. That's what keeps the fake honest. 20 guards were each broken
to confirm a test catches them, including two about *ordering* rather than presence.

`0020` **has** now been executed against a real PostgreSQL 16, which is how `0027` was found: the
`revoke update ... from ledgr_ledger` here made `ledger.post_entry` fail outright, because a foreign
key checks its parent with a `SELECT ... FOR KEY SHARE` row lock and a row lock needs UPDATE or
DELETE privilege. See [ADR-025](docs/decisions/ADR-025-ledger-integrity-job.md) and `0027`'s header.

### The chart of accounts and RGS (FR-GL-005, FR-ONB-004, FR-ONB-005, CMP-003)

`apps/api/src/api/ledger/chart*.py`, `migrations/0024_chart_of_accounts.sql`,
`data/rgs/rgs-3.8-mkb.json` and `scripts/load_rgs_version.py` — see
[ADR-026](docs/decisions/ADR-026-chart-of-accounts.md).

**The RGS version is data.** A release is a JSON file loaded into `rgs_version` / `rgs_element` /
`rgs_profile_account` / `rgs_code_mapping`; each administration is pinned to one. Supporting RGS 4.0
is a file, not a deploy:

```bash
make load-rgs                       # load + publish the shipped dataset
make check-rgs                      # validate a file without touching a database
make load-rgs ARGS="--file data/rgs/rgs-4.0-mkb.json"
```

That is what CMP-003's "versioned upgrade path" needs: **load** the new version, **plan** what it
would do to a chart (`ledger.plan_rgs_upgrade`, one row per account), then **apply** — which refuses
while any account needs a human, because a half-upgraded chart files some accounts under the new
taxonomy and some under the old with nothing on the screen saying which. The old version stays
loaded (CMP-014).

**Seeding follows the legal form** (FR-ONB-004). `administration.legal_form` holds the KvK's
spelling, so `legal_form_alias` maps `'BV'`, `'b.v.'` and `'Besloten Vennootschap'` onto the five
forms the PRD names; a spelling nobody has an alias for **refuses to seed** rather than seeding the
wrong chart. The five profiles genuinely differ — a BV gets share capital, a director's current
account and corporate income tax; an eenmanszaak gets capital and drawings and no payroll.

**"May extend but not break RGS mapping integrity"** (FR-ONB-005) is four checks in one trigger, so
they bind every writer: the code must **exist** in the pinned version (a composite FK, not a text
column), the version is **derived** from the pin rather than supplied, the element must be
**postable** (a heading holds a subtree), and its **type must match** the account's. That last one is
the failure nothing downstream would notice — an expense account mapped to a revenue element
balances, reports, and files wrongly. Users can still add accounts, correct a mapping within its
type, and keep an account with no RGS code at all; unmapped accounts are reported, not refused.

Reference data is **append-only**: an element retyped after accounts map to it would silently change
what every one of them files as. Correcting a version means publishing another one. `ledgr_app` holds
SELECT on it and nothing else — otherwise application code could invent an RGS code and then map an
account to it, satisfying every foreign key and breaking the requirement completely.

> **The shipped dataset is provisional.** `data/rgs/rgs-3.8-mkb.json` is a ~60-element starter subset
> in RGS's shape, not the official publication. It is enough to build and test seeding, the integrity
> rules and the upgrade path against; it is **not** a basis for the XAF export (CMP-002) or an SBR
> filing (CMP-004). The loader refuses it without `--allow-provisional`, `ledger.rgs_readiness()`
> reports every administration pinned to it as not ready to file, and a test fails if the file stops
> declaring itself provisional. Replacing it with the official publication from
> referentiegrootboekschema.nl is a GA blocker — and a data change, not a code one.

**`0024` has been executed** against a real PostgreSQL 16 and its DB-gated tests pass. Doing so is
what found `0027`: the append-only `revoke update ... from ledgr_ledger` here made `rgs_element`
unusable as a foreign-key target, so no RGS version could be loaded at all.

### Fiscal years and period derivation (FR-ONB-006)

`apps/api/src/api/ledger/fiscal*.py` and `migrations/0029_fiscal_year_definition.sql` — see
[ADR-028](docs/decisions/ADR-028-fiscal-year-definition.md).

One rule: walk from the year's start taking whole calendar months (or quarters), and clip the first
and last to the year's actual bounds. `ledger.open_fiscal_year` writes the year and its periods in
one statement — separately, an administration could hold a year with no periods, which nothing else
can read because every posting names one (FR-GL-004).

| Year | Scheme | Periods |
|---|---|---|
| 2026-01-01 .. 2026-12-31 | monthly | twelve whole months |
| 2026-07-01 .. 2027-06-30 | monthly | twelve months, July to June |
| 2026-03-15 .. 2026-12-31 | monthly | a 17-day stub, then nine whole months |
| 2026-05-01 .. 2027-04-30 | quarterly | **five** periods, two of them stubs |
| 2026-03-31 .. 2026-12-31 | monthly | a **one-day** first period |

The last two rows are where the design decisions are. **Blocks are calendar-aligned even for a
non-calendar year**, because a period is also the unit a VAT return is filed for (`0021`, CMP-014)
and Dutch returns are filed on calendar months and quarters whatever the fiscal year does — fiscal
quarters counted from a May year start would give four tidy periods that could not be filed as
anything. And **a one-day period is now legal**: `0029` relaxed `0001`'s `end_date > start_date`
rather than merging the stub forward, since a merged period would straddle two calendar months and
hit the same problem.

`derive_periods` (Python) and `ledger.derive_fiscal_periods` (SQL) are the same rule written twice —
the database derives inside the transaction that creates the year, an onboarding screen previews
before anything is written, and neither can borrow the other's answer. One case table runs against
both. Five property tests cover the spans nobody wrote a case for: the periods tile the year with no
gap or overlap, each lies inside one calendar block, the numbers run 1..n densely, and only the first
and last may be partial.

Years and periods now carry gist exclusion constraints so they cannot overlap —
`unique (administration_id, start_date)` stopped two years sharing a start date and happily allowed
one year to sit on top of another.

### Effective-dated tax rules (CMP-014)

`apps/api/src/api/vat/`, `migrations/0028_effective_dated_tax_rules.sql`,
`data/vat/nl-vat-rules.json` and `scripts/load_vat_rules.py` — see
[ADR-027](docs/decisions/ADR-027-effective-dated-tax-rules.md).

VAT rates, rubriek definitions and RGS versions are effective-dated, so an invoice dated 2018-12-31
is 6% forever and one dated 2019-01-01 is 9%. Every rule row carries **`valid_from` and nothing
else** — the rule in force is the one with the greatest `valid_from` at or before the date, and
superseding is an INSERT. There is deliberately no `valid_to`: an end date would have to be written
onto an existing row when its successor arrived, and a reference table you routinely edit is one
where history gets rewritten by accident.

```bash
make check-vat                                    # validate a ruleset, no database
make load-vat                                     # load the shipped Dutch rules
make load-vat ARGS="--file data/vat/nl-2027-01.json"   # a rate change
```

**"Retroactively changing a rate must not alter a filed period"** is the half that needs more than
dating, and it gets two independent mechanisms:

| | |
|---|---|
| **Prevented** | A rule may not take effect on or before the last day any period has been VAT-filed through. The refusal names the route that *is* open — a suppletie (FR-VAT-005) |
| **Detected** | A period records a **fingerprint** of every rule in force on its end date when it is filed. `vat.filed_period_drift()` recomputes it and reports anything that has moved |

Always empty, and built anyway — the same argument `0020` makes for its gap report. The integration
suite disarms the barrier deliberately and asserts the drift *is* caught, because a detector nobody
has watched detect is not a detector.

The frontier is **one date for the whole system**, held in `vat_filing_watermark` and advanced by
`ledger.mark_period_filed`. It cannot be a scan of `period`: that table is RLS-protected, so a
trigger reading it would see one tenant and let a rate be backdated over another tenant's filed
return. It only ever moves forward. That also means any tenant filing a period constrains what rules
can be introduced for every tenant — right for national tax law, and something a tenant-specific
rule would need its own frontier for.

> **The rates are the published ones; the rubriek mapping is not verified.** Which box a treatment
> reports into is the substance of FR-VAT-001, which is not built — and getting it wrong misstates a
> return without misstating the ledger, so it survives every other check here. The loader refuses the
> shipped ruleset without `--allow-provisional`.

One wart, recorded rather than papered over: `btw_21` and `btw_9` name a rate in an identifier that
has already outlived it — btw_9 was 6% until 2019. `vat_treatment.role` (`standard`, `reduced`, …) is
the stable semantics; the codes are stored in `ledger_account.default_vat_code` and in the RGS
profile dataset, so renaming them is its own migration.

### The integrity job (NFR-033)

`apps/api/src/api/ledger/integrity*.py`, `migrations/0023_ledger_integrity.sql` and
`scripts/verify_ledger_integrity.py` — see
[ADR-025](docs/decisions/ADR-025-ledger-integrity-job.md).

Verifies the three things NFR-033 names — debit/credit balance (FR-GL-001), sub-ledger to control
account agreement (FR-GL-006), numbering continuity (FR-GL-013) — and alerts on any deviation. All
three are enforced at write time by `0020`, so **it should find nothing, forever**. That is the
argument for it, not against it: the write-time guards bind every writer *at write time*, and they
do not bind a superuser who disabled the triggers, a restore of a mid-transaction backup, a
replication failover that lost the tail of a series, or a migration that drops a guard. `0020`
already makes this argument for one check, in its comment on `ledger.numbering_gaps()`: a report
that can only ever say "no gaps" is the check **on** the allocator.

```bash
make verify-ledger-integrity                                   # every tenant, as ledgr_ops
make verify-ledger-integrity ARGS="--administration <uuid>"    # one administration
make verify-ledger-integrity ARGS="--app-connection --organization <uuid> --json"
```

Exit codes are the interface — `0` intact, `1` deviations, `2` could not run, **`3` nothing
examined**. The last one is the point. Every check reports a problem by *returning a row*, so a run
that can see nothing returns nothing and looks exactly like a perfect ledger; an unscoped `ledgr_app`
run is precisely that, because RLS fails closed. `ledger.integrity_scope()` travels with every report
so a caller can refuse to call an empty sweep a pass.

Two design choices worth knowing about. The checks are **`SECURITY INVOKER`**, unlike everything else
in the `ledger` schema — `ledgr_app` gets one tenant through RLS, `ledgr_ops` gets all of them
through BYPASSRLS, and `SECURITY DEFINER` would have made the nightly sweep report every tenant
clean, since `ledgr_ledger` is NOBYPASSRLS under FORCE RLS. And the **allocator is checked against
`max(entry_number) + 1`**, which is what catches a deleted *tail* — delete entries 4 and 5 and the
remaining 1..3 is perfectly gapless, so `journal_sequence` is the only surviving record that a fourth
number was issued. Same role an external anchor plays for the audit chain.

Fourteen deviation keys (`entry_unbalanced`, `orphan_line`, `control_line_without_party`,
`sequence_drift`, …), each one a runbook key. `tests/ledger/integrity_cases.py` breaks the ledger
thirteen ways and names what must come back; that table runs against the in-memory ledger on every
`make test-api` and against real Postgres with the append-only triggers disarmed, and a test fails
the build if a check is added without a corruption that triggers it — a check that has only ever seen
healthy books is indistinguishable from `WHERE false`. **`0023` has been executed** against a real
PostgreSQL 16 and its DB-gated tests pass.

### Idempotency keys (NFR-032)

`apps/api/src/api/idempotency*.py` and `migrations/0022_idempotency.sql` — see
[ADR-024](docs/decisions/ADR-024-idempotency.md).

**Middleware, not a per-route declaration**, and that is the one real design decision. The other
three "cannot forget" guarantees are opt-in per route because the route knows something the
middleware cannot. Idempotency knows nothing route-specific, so it applies to every
POST/PUT/PATCH/DELETE: a new endpoint is covered the moment it is registered, and *opting out* is
what takes a deliberate act. `tests/test_idempotency_coverage.py` is shaped accordingly — it guards
the opt-out list and asserts the middleware is installed and correctly placed, rather than checking
that routes declare anything.

Four outcomes: **claimed** (run it), **replay** (serve the stored response), **in-flight** (409),
**mismatch** (422 — the key was reused for a *different* request). Mismatch is checked *before*
state: reporting a reused key as "still in flight" would send the caller into a retry loop that can
never succeed.

Two layers, neither subsuming the other: this stops the *request* executing twice (24h window),
while `journal_entry.idempotency_key` stops a *second posting* for the same key forever. A retry
after the window re-executes — stated rather than hidden, and the reason the window is generous.

A 5xx **releases** the key so a retry is a real retry; a 4xx is **stored and replayed**, because a
denial is a deterministic answer to this exact request. Expiry is honoured by the claim itself, not
by the purge job — correctness must not depend on a cron schedule.

### Period locking and the suppletie flow (FR-GL-007)

`apps/api/src/api/ledger/periods.py` and `migrations/0021_period_locking.sql` — see
[ADR-023](docs/decisions/ADR-023-period-locking.md).

**The unlock authority is Appendix A's**, not a new one: "Lock / unlock periods" is Full for Owner
and Accountant, and PRD §8.4 says a Bookkeeper explicitly cannot unlock closed periods. Filing takes
a different permission (`file vat_return`), and opening a suppletie a third (`prepare vat_return`) —
which is where the Bookkeeper sits: they may prepare a correction and may not file it.

**What stops the check being skipped is a privilege, not a convention.**
`period_status_transition()` refuses a status change unless `current_user` is `ledgr_ledger`, which
is only true inside the `ledger.*` SECURITY DEFINER functions the service calls *after* checking the
permission. A direct `UPDATE period SET status = 'open'` is rejected — for `ledgr_app` and for
`ledgr_ops` with BYPASSRLS alike.

**`vat_filed` is terminal.** No role, no flag, no method reopens it. So "requires a suppletie flow
to change" is implemented as *the change happens somewhere else*: a `vat_suppletie` is opened
against the filed period, corrections are posted into whichever period is **open**, and they carry
`suppletie_id` — FR-VAT-005's link back to the original filing. A correction pointed at the period
it corrects is refused. The correction return itself (rubrieken, Digipoort) is FR-VAT-005 and is not
built.

`HardLocked` is its own exception, checked before the permission check: telling someone their role
is insufficient would send them looking for a bigger one, and nobody has it.

NFR-042's property tests are Hypothesis (`tests/ledger/test_properties.py`). Balance, sub-ledger
equality and reversal symmetry are `@given` properties; immutability, gapless numbering and period
integrity are `@invariant`s on a `RuleBasedStateMachine`, because none of those is a property of a
single input — an entry is immutable *across whatever happens next*, and a numbering series only
ever goes wrong on the failure path, so refusals are a generated rule too.

### The client switcher (FR-FRM-000, FR-FRM-000a)

`apps/api/src/api/firm/switcher.py` and `apps/web/src/client/` — see
[ADR-019](docs/decisions/ADR-019-client-switcher.md).

FR-FRM-000a names a **harm** ("posting to the wrong client is the single worst usability failure in
this product"), not a widget, so it's treated as correctness. Three mechanisms, in descending order
of how much they actually protect:

1. **A mutating request naming a different client from the one the session has open is refused** —
   409 from `require_permission`, before the handler runs. The only one that *prevents* the failure.
2. **The header badge is derived from the session's active client**, never from a parameter the
   screen passes in, so it can't drift from what the page posts to.
3. **The colour marker**, which makes a wrong client noticeable before anything is submitted.

The guard runs *before* authorization (the dangerous request is the one that would otherwise
**succeed**) and answers 409 not 403 (client-state desync, not a permission problem). Reads are never
constrained. `allows_cross_client` defaults to False, so the switcher — the one route whose job is
changing which client is open — declares its exception at its own definition site.

**Colour is never the only signal.** It's a token (`'amber'`, not hex) so clients own the palette;
every badge also carries `initials`, which survive a monochrome rendering or a narrow header. Ten
colours won't cover hundreds of clients, so the switcher reports `colour_is_ambiguous` and the header
says so rather than letting someone rely on a signal that isn't distinguishing for them.

**Search**: names by substring, KvK by prefix (an all-digit query is a company-number search and
isn't matched against names). Ordered exact → prefix → substring, because keyboard reachability is
type-then-Enter and that needs position one to be predictable. Filtering happens in the query — a
switcher that fetched everything and hid rows would leak client names into a response body first.

**Keyboard-reachable** means Ctrl/Cmd+K from anywhere, focus in the search field, ↓/↑ wrapping at
both ends, Home/End, Enter, Escape, with ARIA combobox/listbox and `aria-activedescendant`. 27 web
tests cover it; thirteen API guards were each broken to confirm a test catches them.

### Firm access visibility and revocation (IAM-109, IAM-110)

`apps/api/src/api/authz/firm_access_register.py` and `engagement_revocation.py` — see
[ADR-018](docs/decisions/ADR-018-firm-access-visibility-and-revocation.md).

**IAM-109** needed only one new column of the four: who, what role and since when were already on
`role_assignment`, and ADR-017's provenance says which rows are the firm's. "When each last accessed
it" is `administration_access` (migration 0017) — one row per (user, administration), upserted and
throttled to one write per 5 minutes, not an event log. **"At any time and without asking" is a
requirement about power, not UX**: it rules out a report the firm sends (visibility depending on the
party it exists to check) and anything the firm can switch off. Where that holds is the RLS policy
letting the owning organization read every row.

**IAM-110**'s three clauses are three different guarantees. "Terminate immediately" is mostly already
true — live evaluation means the next request is denied, with no cache to invalidate. What needed
code is `sessions.active_administration_id`, so "sessions *for that administration*" names specific
rows instead of forcing a choice between killing a firm employee's whole login and killing nothing;
clearing it returns them to the switcher rather than signing them out. "Disappears from the switcher"
is a *consequence* — the switcher is derived from live grants, never stored. "The client retains all
data" is a non-action, stated positively in `RevocationReceipt.retained`.

Only the client may revoke (IAM-105 puts that right in the floor).

Both are wired end to end. `require_permission` records the access after authorizing — it runs on
exactly the requests that constitute one, and a per-route call would be forgettable in the way the
enforcement middleware exists to prevent. Recording participates in the request transaction rather
than being best-effort: an access to a client's books that couldn't be recorded shouldn't complete.
`GET /v1/switcher` lists the caller's live grants (exempt — it discloses nothing they can't already
reach, and an org-scoped check would break it for firm staff, whose grants are administration-scoped);
`PUT /v1/switcher/{id}` enters one and carries a real check plus a `sid` claim, so the write names a
single session rather than moving every device the person owns.

Twenty guards were each broken to confirm a test catches them.

### The client rights floor (IAM-105)

`apps/api/src/api/authz/rights_floor.py` — see
[ADR-016](docs/decisions/ADR-016-client-rights-floor.md). Six rights the client's Owner keeps
whatever the firm does: source documents, filed returns, complete data export, their own audit log,
revoking the firm's access, and managing their users' sign-in security.

Everywhere else access is granted and can be lost; the floor inverts that, and the reason is
statutory — a Dutch business must retain its own books (CMP-001), so an accountant able to lock a
client out of their filed returns would put the client in breach of an obligation they can't
delegate. Four independent layers:

| | Layer | Stops |
|---|---|---|
| 1 | `authorize()` short-circuits before grants are even loaded | any cap or condition |
| 2 | `resolve()` restores the floor *after* IAM-104 restrictions; IAM-102 excludes it | a profile withholding it |
| 3 | 0015's **triggers** — no expiry, no conditions, can't revoke the last Owner | the Owner grant being dismantled |
| 4 | no DELETE grant on `role_permission`; system roles unwritable | the Owner bundle being hollowed out |

Layer 3 uses triggers, not RLS policies, because `BYPASSRLS` (which `ledgr_ops` holds, and the
operator scripts connect as) skips row-level security but never skips a trigger. That's the "no admin
action" clause.

The floor follows the Owner **role**, not a set of permissions — a permission-based test could be
satisfied by composing a custom role that holds them (IAM-036 allows that). Handover stays possible:
the floor needs *an* Owner, so only the last one is protected.

`tests/authz/test_client_rights_floor.py` is mostly attacks — every mechanism for removing access,
pointed at each floor right. DB-level paths (raw-SQL revocation, expiry, cross-tenant writes) are in
the integration suite where real triggers run. Thirteen defences were each broken to confirm a test
catches them.

`migrations/0010_role_catalogue.sql` is **generated** — don't edit it. After changing the matrix:

```bash
make generate-role-catalogue   # rewrite it
make check-role-catalogue      # or just check whether it's current
```

Editing `prd.md`'s Appendix A or §8.4 without updating `matrix.py` (or the reverse) fails CI with a
message naming the cell. All six drift scenarios — changed matrix cell, changed PRD cell, new PRD
capability row, changed role scope, hand-edited migration, dropped conditional note — were verified
by deliberately introducing each one.

## CI

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs on every push: lint, type-check, unit
tests and a build for both the web/shared-types side and the API, plus:

- **Dependency vulnerability scanning** (`dependency-scan-web`, `dependency-scan-api`) — fails
  the build on any new high or critical severity finding, per `SEC-051`.
- **Secret scanning** (`secret-scan`) — Gitleaks over the full history on every push.
- **Tenant isolation** (`test-api`) — spins up Postgres, applies migrations, and runs the full
  API test suite with `TENANT_ISOLATION_TESTS_ENABLED=1`, so the IAM-005 isolation tests execute
  for real (not skipped) on every push, alongside the always-on coverage check.

A final `ci` job requires every other job to succeed, so branch protection only needs to require
that one check.

## Running the API directly

```bash
cd apps/api
uv run uvicorn api.main:app --reload
```

## Notes

- Object storage locally is [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite),
  the official Azure Blob Storage emulator — this matches the Azure Blob choice in
  [CLAUDE.md](CLAUDE.md), so code written against it behaves the same in Azure.
- `.env` is git-ignored. `.env.example` names every variable the stack needs; it holds no real
  values by design.
