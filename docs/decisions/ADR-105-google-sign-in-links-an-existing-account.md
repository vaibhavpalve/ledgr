# ADR-105: Google sign-in links to an existing account by proving its password

- **Status**: Accepted
- **Date**: 2026-10-07
- **Builds on**: [ADR-054](ADR-054-signup-and-login.md) (signup and login), IAM-010c

## Context

A Google sign-in whose verified email matches an account created with a password returned
`link_required` and stopped there. `GoogleSignInService.confirm_link_with_password` existed and was
tested, but no route called it, and the callback screen offered only "Back to login" under a message
asking for a password it had no field for. Every existing password user who pressed "Google" hit a
dead end.

## Decision

- **A server-held ticket.** On `link_required`, `login_google_callback` stores the verified Google
  identity (subject, email, name, picture, amr) in a new `google_link` `auth_ceremony` row (migration
  0076, column `google_link_identity jsonb`) against the existing user's id, and returns only the row
  id as `ticket`. The browser never holds or sends identity data, so it cannot name a Google subject of
  its own choosing.
- **`POST /v1/auth/login/google/link {ticket, password}`** re-authenticates the existing account
  through `confirm_link_with_password` (unchanged), links the identity, and signs in exactly like a
  returning Google user (`google_asserts_second_factor`, then the ordinary MFA gate).
- **One lockout budget.** Wrong passwords count against the same `login` rate-limit key as
  `/v1/auth/login` for that account (IAM-019) and return the same 401. The link leg is not a second
  budget for guessing the password.
- **A typo does not burn the ticket.** The ticket is read without consuming it and claimed in the same
  transaction as the link, only once the password is right. Retries are bounded by the lockout and the
  five-minute TTL. A concurrent second claim rolls its link back.
- **Clean refusals.** A ticket of another kind or an unknown id is the same 410 as an expired one. If
  either side of `user_google_identity` is already taken, the request is refused with 409 before
  anything is written.
- The path is pre-authentication, so it joins `/v1/auth/login/google/callback` in the tenant-context,
  authorization and audit-route exemption lists, for the same reason. Its success is recorded as the
  authentication event `login_google_link`.

The web callback also shares one request per authorization code. React's StrictMode ran the callback
effect twice in development, replayed the single-use code, and showed the failure instead of the
success.

## Consequences

- Existing password users can add Google sign-in once, with their password, and use Google directly
  afterwards.
- Migration 0076 is additive and reversible (the down steps are in the file). It is safe to apply while
  the previous version serves.
- Not built: linking Google from Settings while signed in, and unlinking it.
