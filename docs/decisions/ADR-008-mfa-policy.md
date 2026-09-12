# ADR-008: MFA policy — requirement, factors, and HTTP-layer enforcement

- **Status**: Accepted
- **Date**: 2026-08-22

## Context

PRD §8.2's IAM-011 requires MFA mandatory, with no opt-out, for any user with write permission and
for every user of an administration that has filed a tax return. IAM-012 defines the supported
factors as passkey, TOTP, and platform biometrics bound to a device key, with SMS explicitly not
offered. IAM-010e requires that Google sign-in not satisfy MFA on its own unless the ID token's
`amr` claim asserts a second factor. The task's own instruction — "add the enforcement check as
middleware, not as a per-endpoint concern" — is itself a requirement, not just an implementation
preference.

Three design questions this ADR answers: what "required" means when the two systems IAM-011's
triggers depend on (RBAC, VAT filing) don't exist yet; how the three named factors map onto actual
storage and verification mechanisms (only one of which, TOTP, needed to be built from scratch);
and how the middleware gets a real per-session "was MFA verified" signal given the HTTP layer still
runs on the placeholder JWT from `api.tenancy`, not real sessions.

## Decision

### The interim requirement policy is "always," not a partial implementation of IAM-011's triggers

`AlwaysRequireMfaPolicy` requires MFA for every authenticated user, unconditionally. This is not a
placeholder that ignores IAM-011 — it's a deliberately conservative superset of both of its
triggers. "Any user with write permission" needs the role/permission system (IAM-030+), which no
ADR in this codebase has built yet. "Every user of an administration that has filed a tax return"
needs VAT filing (FR-VAT-003), also unbuilt. Until either exists to narrow the requirement, treating
everyone as in-scope is safe by construction: the set "everyone" already contains both named
subsets, so this can never under-enforce relative to what IAM-011 asks for, only over-enforce
relative to its precise boundary. `MfaRequirementPolicy` is a typed Protocol specifically so a
future `RoleBasedMfaRequirementPolicy` replaces this default without touching the middleware or the
policy-evaluation logic at all.

### Passkey and "platform biometrics bound to a device key" are one storage mechanism

IAM-012 names two items; both are WebAuthn credentials. `Passkey.authenticator_attachment`
(migration 0006, sourced from the registration response's own `authenticatorAttachment` field,
which was already being received and simply not stored before this change) records whether a given
credential is `platform` (device-bound) or `cross-platform` (roaming/synced) for audit purposes.
`MfaEnrollmentChecker` treats any non-revoked passkey as satisfying this factor regardless of
attachment. Building two independently-tracked "factor kinds" for one underlying credential
mechanism and one verification code path (`api.auth.passkeys.WebAuthnService`) was judged
unnecessary complexity — there is no behavioral difference IAM-012 asks for between them.

### TOTP secrets are field-level encrypted by reusing the existing KMS, not a new key scheme

SEC-024 requires field-level encryption for authentication secrets, distinct from storage-level
encryption. `user_totp_credential` (migration 0006) generates a fresh 32-byte DEK per credential,
wraps it via the *same* `KeyManagementService` abstraction already used for document encryption
(`api.crypto.kms`, ADR-004), and stores only the wrapped DEK and the AES-256-GCM-encrypted secret —
the raw TOTP seed is never persisted. This was chosen over inventing a second, separate encryption
scheme (e.g. a static key from settings) specifically to avoid a second, weaker trust boundary
existing in parallel with the KMS-backed one: one KMS, multiple purpose-scoped DEKs, is a smaller
overall attack surface than one KMS plus one ad hoc static-key scheme.

TOTP verification implements replay protection via `last_used_step`: a submitted code's matched
30-second time step must exceed the credential's last used step, even if the code is otherwise
valid — closing the window an attacker who intercepts a valid code in transit would otherwise have
to reuse it. `webauthn`'s own step arithmetic was not reused here (this is `pyotp`, a different
library); `pyotp.TOTP.timecode`'s source was read directly before relying on it, which surfaced a
real, easy-to-miss issue: it computes UTC-correct step numbers only for timezone-*aware* datetimes,
falling back to the *server's local timezone* via `time.mktime` for naive ones. Every datetime
`api.auth.totp` passes into `pyotp` is deliberately UTC-aware for exactly this reason.

### The closed factor set (IAM-012's "SMS is not offered... and must not be addable without a code change")

`MfaFactorKind` is a two-member Python `Enum` (`PASSKEY`, `TOTP`) with no generic, pluggable,
database-driven, or config-flag-driven factor registry anywhere in this design. Adding SMS would
require, at minimum: a new enum member (a code change to `api.auth.mfa`), a new credential table
and repository (a migration and new modules mirroring `api.auth.totp`), and new branches in
`MfaEnrollmentChecker.check` wiring it in — three separate, reviewable changes, not a runtime
toggle. `test_mfa_factor_kinds_are_exhaustive_and_exclude_sms` asserts the enum's exact membership
directly, so a future addition has to knowingly change a passing test, not slip past review.

### Enforcement is middleware, and "satisfied" means verified this session, not merely enrolled

`MfaEnforcementMiddleware` (`api.mfa_middleware`) runs on every non-exempt request, mirroring
`TenantContextMiddleware`'s own reasoning for being middleware rather than a per-route dependency: a
route someone forgets to annotate is a silent gap, and middleware has no such opt-out. Its decision
distinguishes two different flavors of "not satisfied" for a more useful response: `not_enrolled`
(the user has no factor at all — go set one up) versus `not_verified` (a factor exists, but this
specific session hasn't proven it — step up). Enrollment status alone is deliberately *not*
sufficient to pass: `TenantContext.mfa_verified` — a new claim on the same placeholder JWT
`TenantContextMiddleware` already reads — must be true, or the enrollment tables are consulted only
to pick which failure reason to report.

**Middleware ordering was verified empirically, not assumed**, before writing any production code:
in Starlette/FastAPI, the *last* middleware added via `app.add_middleware()` is the *outermost*
layer and therefore runs *first* on the way in. A small scratch script confirmed this precisely
(`['Second:before', 'First:before', 'handler', 'First:after', 'Second:after']` for
`add_middleware(First)` then `add_middleware(Second)`), because getting it backwards would mean
`MfaEnforcementMiddleware` runs before `request.state.tenant_context` exists — a silent, total
failure of MFA enforcement, not a loud one. `api.main` therefore adds `MfaEnforcementMiddleware`
*first*, then `TenantContextMiddleware` *last*, so `TenantContextMiddleware` always sees a request
before this one does.

**Testability without a database**: `MfaEnrollmentChecker` takes the same `PasskeyRepository`/
`TotpRepository` Protocols the in-memory test fakes already satisfy, so `MfaEnforcementMiddleware`
accepts an optional pre-built `enrollment_checker` — tests inject one backed by in-memory
repositories and never touch Postgres; production, when none is given, builds a real SQL-backed one
per request (a database session can't be shared across requests). This mirrors the
`requirement_policy` injection point already needed for the "not required" test path.

### IAM-010e: Google's `amr` claim is interpreted, not acted on, by this change

`GoogleIdentity.amr` (a new field on the dataclass ADR-006 introduced) carries the ID token's
Authentication Method Reference values through from verification. `google_asserts_second_factor`
(`api.auth.mfa`) is the accept/reject decision — `True` only when `amr` contains `"mfa"` (RFC 8176).
It does not itself mark a session verified; a future login flow calling
`SessionService.issue_session(..., mfa_verified=google_asserts_second_factor(identity))` is what
would act on it. Absence of `amr` (the common case for Google's real-world consumer token issuance)
returns `False`, never an exception or an assumed pass — exactly IAM-010e's fallback: "otherwise
LEDGR enrols its own."

### `Session.mfa_verified_at` exists for completeness, even though nothing wires it to HTTP yet

Added to `sessions` (migration 0006) and threaded through `SessionService.issue_session(...,
mfa_verified: bool = False)` and a new `record_mfa_verification` method, mirroring
`last_reauthenticated_at`'s precedent from ADR-005: a primitive a future login/step-up flow will
need, added now so it isn't a dangling gap when that flow is built. `TenantContext.mfa_verified`
(the JWT placeholder's claim) is what the middleware actually reads today; wiring the real,
stateful `Session.mfa_verified_at` into the HTTP layer is the same deferred integration every prior
auth ADR has flagged for tenant context generally, not solved by this change.

Full implementation: [migrations/0006_mfa.sql](../../apps/api/migrations/0006_mfa.sql),
[src/api/auth/mfa.py](../../apps/api/src/api/auth/mfa.py),
[src/api/auth/totp.py](../../apps/api/src/api/auth/totp.py),
[src/api/mfa_middleware.py](../../apps/api/src/api/mfa_middleware.py).

## Alternatives considered

| Option | Rejected because |
|---|---|
| Derive "MFA required" from HTTP method (require it only for POST/PUT/PATCH/DELETE) as a stand-in for "write permission" | IAM-011 scopes the requirement to the *user* ("mandatory for all users with any write permission"), a standing account property, not to individual requests — a user who has write permission should have MFA enforced on their reads too, not toggle per verb. `AlwaysRequireMfaPolicy` is the more faithful conservative stand-in. |
| A static, settings-based AES key for TOTP field-level encryption instead of reusing `api.crypto.kms` | Would introduce a second encryption trust boundary alongside the KMS-backed one used for documents, for no benefit — the existing `KeyManagementService` abstraction already supports exactly this per-secret wrap/unwrap pattern (ADR-004's `administration_encryption_key` is the same shape, scoped to one row instead of one administration). |
| Rely on `pyotp.TOTP.verify(code, valid_window=1)`'s built-in window matching instead of a hand-written step search | `verify()` doesn't report *which* step matched, which `last_used_step` replay protection needs to know. Matching steps explicitly, one at a time, via `pyotp.TOTP.at()` was necessary regardless — and doing so surfaced the naive-vs-aware-datetime timezone issue in `timecode()` before it could become a production bug. |
| Give `MfaEnforcementMiddleware` no injection points and test it only via the DB-gated integration suite | Would mean the middleware's core logic — ordering, the four evaluation outcomes, fail-closed behavior on a missing user id — has no fast, always-run test coverage, breaking the "every prior auth piece is testable without Postgres" discipline this codebase has maintained since ADR-004. |
| A separate `MfaFactorKind.PLATFORM_BIOMETRIC` member distinct from `PASSKEY` | Both are WebAuthn credentials verified through the identical code path in `api.auth.passkeys`; a second enum member with no behavioral difference would misrepresent the actual mechanism as two independent ones. `authenticator_attachment` captures the real distinction for audit without inventing a second verification path. |

## Consequences

- No HTTP endpoint or UI exists for TOTP enrollment, passkey-vs-TOTP factor selection, or a login
  flow that sets `mfa_verified` — consistent with every prior auth ADR's boundary. `TotpService`
  and `MfaPolicyService` are the service layer such a flow would call.
- `users.mfa_enrolled` (added in ADR-005) remains unpopulated by this change. `MfaEnrollmentChecker`
  deliberately queries `user_passkey`/`user_totp_credential` directly rather than trusting a cached
  boolean that nothing currently keeps in sync — avoiding a staleness risk rather than introducing
  one. Whether to maintain that column as a denormalized cache (e.g. for an admin "who lacks MFA"
  dashboard query) is an open question for whoever builds that feature, not resolved here.
- `MfaEnforcementMiddleware`'s production path opens a database session on every non-exempt
  request, in addition to whatever the route handler's own `get_db_session` dependency opens — two
  connections per request rather than one. This is a deliberate, documented tradeoff: a stateful
  security check needs a database round trip somewhere, and centralizing it in middleware (per this
  task's own instruction) means paying that cost once per request rather than duplicating the check
  per endpoint.
- `AlwaysRequireMfaPolicy` will over-enforce relative to IAM-011's literal boundary until RBAC and
  VAT filing exist — every user, including one who will only ever hold read-only access once roles
  exist, is required to enroll a factor today. This is the conservative direction to err in, but it
  is a real UX cost worth resolving once `MfaRequirementPolicy` has a real implementation to swap
  in.
