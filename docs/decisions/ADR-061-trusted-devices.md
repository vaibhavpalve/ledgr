# ADR-061: trusted devices — a bounded, revocable exception to "prove MFA every session"

- **Status**: Accepted
- **Date**: 2026-09-15

## Context

Two customer-facing bugs and one feature request came in together:

1. A user who resets their authenticator app (revokes the old TOTP credential and enrolls a new
   one, the supported flow per ADR-008/`remove_totp`) was shown fresh QR-code enrollment on every
   subsequent login instead of a plain code prompt — `TotpRepository.get_for_user`'s query carried
   no `revoked_at IS NULL` filter and no ordering, so it could return the old, revoked row instead
   of the new active one. Fixed independently of this ADR (`totp_repository.py`,
   `fake_totp_repository.py`, a regression test in `tests/auth/test_mfa.py`) — a plain query bug,
   not a design question.
2. The same user asked for a "remember this device for 7 days" option, so a returning session on
   the same browser does not re-prompt for a TOTP code every time.

IAM-011 states MFA is "mandatory... no opt-out" for every user (ADR-008 currently applies this to
everyone, as the conservative stand-in for RBAC/VAT-filing scoping that does not exist yet). A
literal reading of "no opt-out" could be taken to forbid remembering a device at all. This ADR
exists to draw that line precisely, because it is exactly the kind of security-policy boundary
CLAUDE.md's non-negotiables call out as needing explicit reasoning, not a local judgment call.

## Decision

### A trusted device is proof of a PAST verification, never a substitute for one

A `trusted_device` row (migration `0050_trusted_devices.sql`) is issued **only** from inside
`mfa_totp_verify` or `mfa_passkey_verify_finish` — i.e., only after a session has already presented
a valid second factor and been marked `mfa_verified=True` the ordinary way. Nothing can obtain a
trusted-device token without first doing the thing IAM-011 requires. `login()` then treats a valid,
unexpired, unrevoked token for the same user as license to start a **new** session pre-verified,
for up to 7 days (`settings.trusted_device_lifetime_days`) from issuance.

This is the same shape as `Session.mfa_verified_at` in ADR-008: a system that already tracks
"has this specific session proven a factor" is being extended to also track "has this specific
*device*, within a bounded window, proven a factor recently" — narrower in time, not narrower in
requirement. Every account still must enroll a factor; every account still must clear MFA at least
once on this device to be trusted at all; the checkbox never appears on the first-time enrollment
UI, only next to a returning device's code/passkey prompt (`MfaEnrollment.tsx`'s `alreadyEnrolled`
branch) — this is IAM-011 satisfied and then *scoped*, not IAM-011 skipped.

### Same token shape as sessions and TOTP — opaque bearer token, hash-only storage

`TrustedDeviceService.issue` mints a 256-bit `secrets.token_urlsafe` value exactly like
`SessionService.issue_session`; only its SHA-256 hash is persisted
(`trusted_device.token_hash`), and the raw value exists only in the HTTP response body at issuance
(`trusted_device_token` in `_auth_response`) and in the browser's own storage
(`ledgr.trusted_device.v1`, deliberately a *separate* `localStorage` key from
`ledgr.session.v1` — see `session.ts`'s comment — because signing out must not un-remember the
device: that is the entire feature).

### Revocable and bounded, not indefinite

- **Time-boxed**: 7 days from issuance, independent of how long the *session* it helped start then
  lives (`session_max_lifetime_hours` is unrelated and unchanged).
- **Listable and revocable**: `GET /v1/me/trusted-devices` / `DELETE /v1/me/trusted-devices/{id}`
  (`api.account.security_routes`), the same ownership-only, no-RLS shape as `/v1/me/sessions` and
  `/v1/me/passkeys` (users are global, ADR-005) — a person can see every device they ever
  remembered and can end that grant immediately, the same "no interface can extend trust beyond
  what the account holder can see and undo" principle IAM-017 already establishes for sessions.
- **Fails closed, not loud**: `TrustedDeviceService.check` returns `False` for absent, wrong-user,
  revoked, or expired tokens — never raises, never distinguishes which — precisely so an invalid or
  stale token falls through to the ordinary MFA prompt (the pre-existing, still-fully-enforced
  path) rather than becoming an error a client has to special-case, or an oracle an attacker could
  use to enumerate device state.

### Consulted only by `login()`, not by `MfaEnforcementMiddleware`

`MfaEnforcementMiddleware`'s own logic (ADR-008) is unchanged: it still refuses every non-exempt
request on a session whose JWT does not carry `mfa_verified=True`. This feature only changes
**one input** to one decision — whether `login()` mints that claim as `True` from the start for a
brand-new session — never what the middleware itself checks afterward. There is exactly one call
site for `TrustedDeviceService.check` (`login()`) and exactly two for `TrustedDeviceService.issue`
(`mfa_totp_verify`, `mfa_passkey_verify_finish`), matching ADR-008's own preference for a small,
auditable set of places that can affect an `mfa_verified` claim.

## Alternatives considered

| Option | Rejected because |
|---|---|
| An HTTP cookie (e.g. `Set-Cookie` on the verify response, read back automatically by the browser) | Every other credential in this system (the bearer token itself, per `docs/decisions/ADR-054-signup-and-login.md`) is deliberately explicit — stored in `localStorage`, attached by client code, never implicit browser behavior. Introducing cookies for exactly one credential would mean two different trust-transport mechanisms in one API, with their own CORS/`SameSite`/CSRF surface, for a system that has consciously avoided that shape everywhere else. |
| Make `trusted_device_lifetime_days` unlimited (or very long) by default | Bounded exposure was the point of asking "how does this interact with IAM-011's no opt-out" at all — an indefinite grant is functionally identical to disabling MFA for that browser forever, which is the literal opt-out IAM-011 forbids. 7 days is what was asked for; the config knob exists so this can be tightened without a schema change. |
| Extend `Session` itself with a "remembered" flag instead of a new table | A session already means one signed-in browser tab's lifetime (max 12 hours, `SESSION_MAX_LIFETIME_HOURS`); trusted-device trust must outlive many such sessions over 7 days. Conflating the two would mean either shortening the session model's own meaning or growing its lifetime far past what IAM-016 sets for a normal session — a new table scoped to exactly this purpose was the smaller, clearer change, mirroring how `user_totp_credential` and `user_passkey` are already separate from `sessions` for the same "different lifetime, different revocation unit" reason. |
| Silently skip the ADR and treat "remember device" as a routine feature add | The literal words "mandatory... no opt-out" in a document CLAUDE.md calls a non-negotiable are exactly the case that document says needs an ADR in the same commit, not local judgment — see the architectural non-negotiables list and this repo's own ADR discipline (memory: "write an ADR in the same commit for any significant architectural decision"). |

## Consequences

- `trusted_device` carries no `organization_id` and no RLS, the same as `sessions`,
  `user_totp_credential`, `user_passkey` (ADR-005): ownership is enforced in the handler
  (`api.account.security_routes`), not the database, because users are a global concept, not a
  tenant-scoped one.
- A person who clears their browser's `localStorage` (or uses a private window) loses the
  remembered-device grant without needing to visit the security settings screen — expected,
  matching how clearing storage already signs a person out of their session (`ledgr.session.v1`).
- The trusted-device list carries no explicit "is this the device I'm looking at right now" marker
  (unlike `SessionView.is_current`) — revoking an entry from the settings screen does not also clear
  this browser's own stored token if it happens to be the one revoked; the next login attempt from
  that browser will simply fail the server-side check and fall back to an ordinary MFA prompt, which
  is correct but not maximally polished. Adding an "is this device" indicator is a small, separable
  follow-up, not blocking here.
- `TotpRepository.get_for_user`'s missing `revoked_at IS NULL` filter (bug #1 above) is fixed
  independently and is unrelated to this feature's own correctness — it was the cause of the
  QR-code-every-login symptom, not "remember this device" being needed to fix it. Both were reported
  together but are separate changes for separate reasons.
