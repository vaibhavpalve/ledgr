# ADR-005: Authentication foundation — users, password credentials, sessions

- **Status**: Accepted
- **Date**: 2026-08-21

## Context

PRD §8.2 defines the authentication requirements. This decision covers the two the user scoped
this work to: IAM-013 (password policy — NIST SP 800-63B: 12-character minimum, breached-password
screening, no forced rotation, no composition rules) and IAM-016 (session lifetime — 12-hour
absolute maximum, 30-minute idle timeout for privileged roles, absolute re-authentication for
sensitive actions). No login UI or HTTP endpoint was requested — this is the storage and
service-layer foundation a future login flow will sit on top of.

The design question this ADR answers: how are sessions represented (stateful rows vs. stateless
tokens), how is a password actually stored and checked, and how does "breached-password
screening" work without either a live network dependency in every test or a fake that quietly
never screens anything in production.

## Decision

### Sessions are stateful database rows, not JWTs

Each session is a row in `sessions`, looked up by the SHA-256 hash of an opaque bearer token
(`secrets.token_urlsafe(32)` — 256 bits of entropy). This is a deliberate departure from the
placeholder JWT approach used earlier in `api.tenancy.TenantContextMiddleware` (built before any
user/session system existed, purely to carry a tenant id for isolation testing) — that middleware
is unchanged by this ADR and remains a stand-in a future task should replace with real session
validation.

Three IAM-016/017 requirements specifically rule out a stateless token:

- **30-minute idle timeout** needs a "last activity" timestamp updated on every validated request
  and compared against wall-clock time — a stateless JWT can't express a sliding window without
  either reissuing itself constantly (which just makes it stateful again) or accepting a fixed
  `exp` that can't reflect actual idleness.
- **IAM-017's "list sessions, revoke individually"** requires enumerating and invalidating a
  specific session on demand. A stateless token can only be "revoked" via a server-side denylist —
  which is a database table anyway, so there's no statelessness being preserved.
- **"Absolute re-authentication for sensitive actions"** needs a per-session
  `last_reauthenticated_at` a future sensitive-action check can read and update — again, state.

### Password storage: Argon2id, explicit parameters, no composition rules

`api.auth.passwords` hashes with Argon2id (`argon2-cffi`), parameters set explicitly
(`time_cost=2, memory_cost=19456, parallelism=1` — OWASP's 2023+ recommendation for an
interactive login path) rather than left to library defaults, so a future parameter change is a
deliberate, documented decision, not a side effect of a dependency bump (SEC-026: cryptographic
agility as configuration).

`validate_password_policy` checks **only** length (12–128 characters) and breach status. There is
no uppercase/digit/symbol check anywhere, and no `password_changed_at`-driven expiry anywhere —
IAM-013's "no composition rules, no forced rotation" is implemented by the absence of that code,
not a flag that disables it. The module docstring says so explicitly, so a future contributor
adding "just one" composition check reads it as a regression, not a gap to fill.

### Breached-password screening is an adapter, same shape as the KMS layer

`PasswordBreachChecker` is a Protocol with two implementations — `HibpBreachChecker` (production;
k-anonymity against the real Have I Been Pwned API, so only 5 hex characters of a SHA-1 hash ever
leave the process) and `LocalDenylistBreachChecker` (dev/test; a handful of hardcoded common
passwords, no network call). Selected via `settings.breach_checker_provider`, defaulting to
`"local"` — the same "safe local default, explicit production opt-in" pattern as
`kms_provider` in ADR-004, for the same reason: a test suite should never depend on a third-party
service being reachable, and a misconfigured production deployment should fail loudly (weak
screening) rather than silently (no screening at all, indistinguishable from working).

### "Privileged" is caller-supplied, not derived

`sessions.privileged` — the flag that decides whether the 30-minute idle timeout applies — is
supplied by whoever calls `issue_session`, not computed here. There is no role or permission model
yet (IAM-030+, not built) for this code to derive it from. Callers without a real answer should
pass `True`, the tighter-timeout default: an over-applied timeout is a UX cost, an
under-applied one is a security gap, and this module can't tell which case it's in without the
RBAC system that doesn't exist yet.

### `InvalidCredentialsError` covers "no such user" and "wrong password" identically

SEC-008 requires error responses to never disclose whether an account exists. Both failure paths
in `authenticate_with_password` raise the exact same exception type, and both run a real Argon2
verify — against `DUMMY_HASH`, a fixed hash of an unassigned value, when no real credential
exists — so the two paths cost approximately the same wall-clock time. Account-status rejection
(`AccountNotActiveError`, suspended/deactivated) is a **separate** exception raised only *after* a
correct password is verified, since at that point the caller has already proven they hold valid
credentials and naming the actual status leaks nothing SEC-008 is concerned with.

Full implementation: [migrations/0003_authentication.sql](../../apps/api/migrations/0003_authentication.sql),
[src/api/auth/](../../apps/api/src/api/auth/).

## Alternatives considered

| Option | Rejected because |
|---|---|
| Stateless JWTs as sessions (extending the placeholder already in `api.tenancy`) | Can't express a sliding idle timeout or per-session revocation without becoming stateful anyway — see above. Would also mean two different session concepts coexisting (the tenancy JWT and a "real" session), rather than one that future work replaces the placeholder with. |
| bcrypt instead of Argon2id | Both are NIST-800-63B-acceptable. Argon2id is OWASP's current first recommendation and is memory-hard (better resistance to GPU/ASIC cracking than bcrypt's fixed, small memory footprint) — no reason to pick the weaker-by-default option when starting from nothing. |
| Composition rules (require uppercase/digit/symbol) "for extra safety" beyond what IAM-013 asks | IAM-013 explicitly prohibits this — NIST 800-63B's own research basis is that composition rules push users toward predictable patterns (`Password1!`) without meaningfully raising entropy, while breach screening catches the passwords actually being exploited. Adding rules "to be safe" would directly contradict the requirement being implemented. |
| Store the raw session token, not its hash | Same reasoning as password storage: a database read (backup leak, replication snapshot, compromised read replica) should not hand over live, usable session tokens. Hashing costs one cheap SHA-256 per validation and closes that path entirely. |
| No `breach_checker_provider` setting — always call HIBP | Makes every test in `tests/auth/` and `tests/crypto/`-adjacent CI runs depend on a third-party service's uptime and rate limits, for no benefit in a test context that isn't validating HIBP integration specifically. The `local`/`hibp` split isolates that dependency to where it's actually needed. |
| Derive `privileged` from `user.mfa_enrolled` or some other existing column, instead of taking it as a parameter | Would quietly conflate "has MFA enrolled" with "holds a privileged role" — two different IAM-011/IAM-016 concepts that happen to both exist today only because nothing else does yet. Making the caller supply it explicitly keeps that conflation from being baked into the schema by accident. |

## Consequences

- A real login endpoint (not built here — "no login UI yet") will call `AuthenticationService.
  authenticate_with_password`, then `SessionService.issue_session` with `privileged` computed
  from whatever the future RBAC system says about the user's roles — today, in the absence of
  that system, it should pass `True`.
- `api.tenancy.TenantContextMiddleware`'s JWT-based tenant context is unchanged and still a
  placeholder. Wiring real session validation into the HTTP layer (replacing or complementing
  that middleware) is the natural next step this ADR sets up but does not do.
- MFA enrollment (IAM-011/012), Google OAuth (IAM-010), passkeys (IAM-012), account recovery
  (IAM-018), and SSO (IAM-014/015) are separate, unbuilt features. `users.mfa_enrolled` exists as
  a column because PRD §15 lists it as part of the User entity, but nothing in this change sets
  it to `true` — that's IAM-011/012's work.
- `users`, `user_password_credential`, and `sessions` deliberately have no row-level security —
  unlike every tenant-scoped table in this schema, there is no `organization_id`/
  `administration_id` predicate that applies to global identity data, and RLS with no meaningful
  predicate would just be a blanket allow. Least-privilege here is "only `api.auth.*` code ever
  queries these tables" plus ordinary `GRANT`s (no `DELETE`), not a tenant boundary that doesn't
  exist for this data.
