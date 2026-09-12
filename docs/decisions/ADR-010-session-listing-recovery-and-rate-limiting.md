# ADR-010: Device session listing, account recovery, and auth rate limiting

- **Status**: Accepted
- **Date**: 2026-08-23

## Context

Three related but distinct PRD §8.2 requirements, requested together: IAM-017 ("device sessions are
listed to the user with location and last-use, and individually revocable"), IAM-018 ("account
recovery never grants access on the strength of an email alone; recovery requires a second verified
factor or an admin-initiated, logged reset"), and IAM-019 ("rate limiting and progressive lockout on
authentication endpoints; credential-stuffing detection with anomaly alerting"). Each builds on
infrastructure already in place — sessions (ADR-005), passkeys (ADR-007), TOTP (ADR-008) — but each
also required a genuinely new piece: location resolution, a recovery subsystem that didn't exist at
all, and an abuse-detection subsystem that didn't exist at all.

## Decision

### IAM-017: location is enrichment, computed at listing time, never stored

`SessionService.list_sessions_with_location` pairs each `Session` with a `Location | None` resolved
on demand via a `GeoLocationResolver` — the same adapter shape as `api.crypto.kms` and
`api.auth.breach_check`: `NullGeoLocationResolver` (dev/test, no network, always `None`) and
`IpApiGeoLocationResolver` (production, one unauthenticated HTTP call to ip-api.com's free JSON
endpoint) selected by `geolocation_provider`. Location is never persisted on the `Session` row — it
is recomputed each time a listing is requested, and a resolver outage or a private/loopback address
degrades to `location=None` rather than raising, since it is enrichment, not a correctness-critical
value. Individual revocation (`SessionService.revoke_session`) already existed from ADR-005; IAM-017
needed only the location half.

### IAM-018: two logged recovery paths, no email-alone path at all

`AccountRecoveryService` (`api.auth.account_recovery`) provides exactly two ways to recover
without already holding an authenticated session: `recover_with_totp` (email identifies *whose*
secret to check, but only a valid, unused TOTP code — proven possession, not a mailbox click —
actually authorizes the reset) and `recover_with_passkey` (no email at all — WebAuthn is
discoverable, so the credential resolves its own owner; see ADR-007). Google is deliberately not a
recovery factor: if a user can still sign in via Google, they are not locked out and don't need
recovery at all — they can add a password through an ordinary authenticated settings flow instead
(IAM-010f, ADR-009). Recovery specifically serves the case where no authenticated path exists.

For accounts holding neither factor, `admin_reset_password` requires an identified
`performed_by_user_id` and a non-blank `reason` — both mandatory, both permanently recorded. This
module does not verify the calling actor is *authorized* to perform a reset (IAM-030+, not built);
it makes the actor's identity and the reason a hard, structural requirement of the function
signature itself, so "admin-initiated, logged" holds regardless of what future system authorizes
the call.

Every recovery, of any kind, calls `SessionService.revoke_all_for_user` before returning — a
credential reset must not leave a possibly-compromised prior session valid. Every recovery is
recorded in `account_recovery_event` (migration `0007_account_recovery.sql`), a dedicated,
append-only log for this one action — explicitly **not** the general-purpose, hash-chained
`AuditEvent` system IAM-090+ describes (not built anywhere in this codebase). Building that system
is out of scope for implementing IAM-018 specifically; if it is ever built, it likely subsumes this
table's narrower purpose.

A real, easy-to-miss bug surfaced while testing this: `InvalidRecoveryProofError` is raised for a
wrong TOTP code, a nonexistent account, *and* a passkey that authenticates successfully but belongs
to someone other than the account named — one exception type for all three, deliberately, so a
caller cannot distinguish "no such account" from "wrong proof" from the error alone (the same
SEC-008 reasoning as `api.auth.service.InvalidCredentialsError`).

### IAM-019: Postgres-backed, not in-memory or Redis

`AuthRateLimiter` computes two independent signals from one append-only log,
`auth_attempt` (migration `0008_auth_rate_limiting.sql`): a sliding-window attempt count per
`account_key` (rate limiting), and consecutive failures per `account_key` since its last success
(progressive lockout, mapped to an increasing duration by a fixed schedule — 1 minute at 5
failures, up to a 24-hour cap at 9+). A third query, distinct `account_key` values failed against
by one `source_ip` in a window, drives credential-stuffing detection — the orthogonal signature
from progressive lockout: one attacker hammering *many* accounts, not one account under sustained
attack.

This stack has no cache or queue infrastructure (`docker-compose.yml` is Postgres + Azurite only),
and an in-memory counter would not be correct across the multiple API worker processes a real
deployment runs — each would have its own, inconsistent count. A Postgres row per attempt, with
indexes matching each of the two query shapes, is the correct choice given what is actually in this
stack, at the cost of a database round trip per check (an accepted tradeoff, consistent with
`MfaEnforcementMiddleware`'s equivalent choice in ADR-008).

`check()` is called before any real authentication work, so a locked-out account never reaches a
password comparison; `record_attempt()` is called after, with the real outcome, updating what future
checks read. `AnomalyAlerter` follows the adapter pattern once more: `LoggingAnomalyAlerter` (a
structured warning-level log line — real, not a stub, but not wired to a SIEM) is the only
implementation today; wiring a real paging/SIEM integration (SEC-037) is future work this interface
is ready for.

`RateLimitingMiddleware` is genuinely middleware (unlike ADR-009's per-endpoint dependency for
IAM-010f) because it needs to intercept *before* the handler runs (to reject before any real work)
and again *after* (to observe the real outcome) — a wrapping concern middleware is naturally suited
for. It is configured with an explicit **include** list of `(method, path) -> endpoint`, the inverse
shape from `TenantContextMiddleware`'s exclude list: rate limiting only makes sense for a
deliberately chosen set of authentication actions, never the whole application by default. It is
**not wired into `api.main`** — there are no real authentication HTTP endpoints in this codebase yet
(login, password recovery, TOTP verification are all still service-layer only, per every prior auth
ADR's boundary); this middleware is ready to protect them the moment they exist, configured with
their exact `(method, path)`.

Full implementation: [migrations/0007_account_recovery.sql](../../apps/api/migrations/0007_account_recovery.sql),
[migrations/0008_auth_rate_limiting.sql](../../apps/api/migrations/0008_auth_rate_limiting.sql),
[src/api/auth/geolocation.py](../../apps/api/src/api/auth/geolocation.py),
[src/api/auth/account_recovery.py](../../apps/api/src/api/auth/account_recovery.py),
[src/api/auth/rate_limiting.py](../../apps/api/src/api/auth/rate_limiting.py),
[src/api/rate_limit_middleware.py](../../apps/api/src/api/rate_limit_middleware.py).

## Alternatives considered

| Option | Rejected because |
|---|---|
| Storing resolved location on the `Session` row at issuance time | A session can live up to 12 hours (IAM-016); an IP's resolved location isn't a fixed property of the session, and computing it once at issuance would go stale for a long-lived session in a way re-resolving at listing time does not. |
| Accepting Google re-authentication (with an amr `mfa` assertion) as a third recovery factor | If a user can still sign in via Google at all, they are not locked out and don't need recovery — they need `api.auth.account_continuity`'s ordinary "add a password" path instead. Building a third recovery branch for a case that isn't actually a recovery scenario would be unnecessary complexity. |
| A single generic `AccountRecoveryError` instead of separate `InvalidRecoveryProofError`/`UserNotFoundError`/`RecoveryReasonRequiredError` | The admin-reset path's caller is an already-authenticated admin flow, where disclosing "no such user" is safe and useful (unlike the anonymous self-service paths) — collapsing all three into one type would either over-hide information from the admin path or under-hide it from the self-service ones. |
| In-memory or Redis-backed rate-limit counters | No cache/queue infrastructure exists in this stack; an in-memory counter is actively wrong (inconsistent) across multiple worker processes, and introducing Redis as a new infrastructure dependency for this one feature was judged unjustified when Postgres, already present, is correct at the query volumes involved. |
| Wiring `RateLimitingMiddleware` into `api.main` now, configured against the existing demo routes (`/v1/whoami`, `/v1/administrations`) | None of those are authentication endpoints — rate limiting them would be inert or actively wrong (they are not where credential-stuffing or brute-force attempts occur). Unlike `MfaEnforcementMiddleware` (ADR-008), which meaningfully applies to any authenticated resource, this middleware has nothing real to protect until an actual login/recovery endpoint exists. |
| Checking `distinct_account_keys_from_ip` on every attempt, including successes | Credential stuffing is characterized by many wrong guesses against many accounts; checking only after a new *failure* avoids an extra query on every successful login while still catching the pattern well before an attacker could exhaust a stolen-credential list without triggering at least one failure. |

## Consequences

- No HTTP endpoint exists for any of the three pieces — `SessionService.list_sessions_with_location`,
  `AccountRecoveryService`, and `RateLimitingMiddleware` are the layer a future login/recovery/
  session-management UI will call or be wrapped by, consistent with every prior auth ADR's stated
  boundary.
- `IpApiGeoLocationResolver` depends on a free, unauthenticated third-party service with no SLA;
  production deployments that need reliability guarantees here would need to budget for a paid
  provider or a local MaxMind database — not built, since IAM-017 only requires location be shown
  when available, not guaranteed.
- `RateLimitingMiddleware`'s `source_ip` is read from the raw ASGI connection address only; a
  reverse-proxied production deployment needs its own trusted-proxy configuration (validating which
  upstream is allowed to set `X-Forwarded-For`) before `source_ip` reflects real client IPs rather
  than the proxy's own address — a deployment-specific decision deliberately left out of this change.
- `account_recovery_event` and `auth_attempt` are both append-only with no retention/purge job built
  — neither is fiscal data requiring the 7-year retention the rest of this schema follows, but
  unbounded growth is a known, undeferred loose end for whoever operationalizes this.
