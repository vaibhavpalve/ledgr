# ADR-060: Session-backed tenant context, account security settings, and e-mail verification

- **Status**: Accepted
- **Date**: 2026-09-14

## Context

ADR-054 shipped signup and login with a deliberate, named limitation: the bearer JWT
`api.auth.tokens` mints *restates* a session row's claims (`mfa_verified`, `sid`, the active
administration) at the moment of issuance, and `TenantContextMiddleware` reads only the JWT. A
session revoked on one device kept working on another until the token's own `exp`; a
`PUT /v1/switcher/{id}` had to re-mint a token for the header to change; a TOTP step-up handed
back a new token because the old one could not learn it had happened. ADR-054 recorded all of
this as "a follow-up affecting every route" rather than smuggling it into the change that first
made tokens flow from real sessions.

The founder review of 2026-09-14 (§4.1, §4.4) asked for that follow-up, and for the two things
that sit on top of it: the account's own security settings (IAM-017's session list and per-device
revocation, passkey and authenticator-app management, password change) and IAM-010b's e-mail
verification for password signup, which ADR-054 also named as an open gap.

Three questions this ADR answers: what the token is still allowed to assert once the row is read
on every request; how a middleware that now needs a database round trip stays unit-testable
without Postgres and without a switch that turns the check off; and what an unverified account
may and may not do.

## Decision

### The JWT proves who; the session row says whether they are still in

`TenantContextMiddleware` decodes the token for exactly three claims - `sub`, `org_id`, `sid` - and
refuses a token missing any of them (`api.auth.tokens` has never minted one without). It then
resolves `sid` against `sessions` through `SessionService.validate_session_by_id`: one primary-key
read, the same revocation / absolute-expiry / idle-timeout checks the raw-token
`validate_session` has run since ADR-005, an ownership check that the row's `user_id` is the
token's `sub`, and a `last_active_at` touch. `mfa_verified` and `active_administration_id` on the
resulting `TenantContext` come from the row. The token's `mfa_verified` claim is still written -
the login response tells the client where to route - but the server never reads it back, and the
`adm` claim is gone.

Consequences that follow directly: a session revoked anywhere (logout, `DELETE
/v1/me/sessions/{id}`, a password change, IAM-018 recovery) is refused on the very next request
with `reason: session_revoked`; IAM-016's idle timeout is finally measured against real activity;
the switcher takes effect without a re-mint; and the MFA step-up endpoints' re-minted token is a
convenience for the client, not the mechanism.

### The lookup is injectable, never optional

`api.auth.session_validation.SessionValidator` is a one-method Protocol. The middleware takes one
at construction, reads one off `app.state.session_validator` when a test has installed it on the
real `api.main` app, and otherwise builds the SQL-backed one lazily from the shared engine (lazily
because `api.db` imports `api.tenancy`; the SQL implementation therefore lives in `api.auth`, not
in `api.tenancy`). This is the shape ADR-008 chose for `MfaEnforcementMiddleware`'s enrolment
checker, for the same reason: the unit suite runs the middleware against
`tests/support/fake_session_validator.py`, which is the real `SessionService` over the in-memory
repository the rest of `tests/auth/` already uses, so it exercises the real revocation, expiry,
idle and ownership logic with only the storage swapped. There is no setting that disables the
lookup, and a validator that cannot be reached raises rather than passes.

The DB-backed suite runs `SqlSessionValidator` for real. `tests/support/seed.seed_user` now gives
every seeded user one live, MFA-verified session and records it, and `make_token` puts that
session's id in `sid` when a test names none - so the forty-odd existing isolation tests go
through the real lookup unchanged rather than being exempted from it, and
`tests/integration/test_session_backed_context.py` proves revoke-then-request, the ownership
check, and switch-then-read against real rows.

### Account security settings are `/v1/me` routes, exempt from the permission check for the `/v1/me/language` reason

`GET/DELETE /v1/me/sessions[/{id}]`, `GET/DELETE /v1/me/passkeys[/{id}]`, `POST /v1/me/password`
and `DELETE /v1/me/mfa/totp` (`api.account.security_routes`) read or change only rows the verified
token's own user owns, and no route names another user's. A person with no role assignment yet
must still be able to see where they are signed in and end a session on a device they no longer
hold, so a permission would only ever stand between them and their own account. They remain
behind tenant context, the MFA gate and idempotency, and every mutation is an IAM-090
authentication event recorded from the handler via `AuditTrail.authentication`, listed in
`tests/test_audit_coverage.py` with that reason - the mechanism ADR-054 established for the
auth routes.

Three guards decide what may be removed, and they are checked in this order:

- IAM-010f, from the other side: a passkey cannot be removed when the account would be left with
  no password and no other active passkey - Google-only, or with no way in at all
  (`last_sign_in_method`). "Set a password" is the instruction.
- IAM-011 (mandatory, no opt-out): the only second factor cannot be removed. A passkey with no
  authenticator app and no other passkey behind it, or the authenticator app with no passkey,
  is refused (`last_mfa_factor`). Enrol the replacement first - the order anyone replacing a lost
  phone follows anyway.
- IAM-016 (re-authentication for sensitive actions): `POST /v1/me/password` verifies the current
  password before writing, rate-limited per account like login (IAM-019) because a stolen open
  session would otherwise have a password oracle. An account with no password yet (Google-only)
  sets its first without one; its MFA-verified session is the proof. A successful change revokes
  every *other* live session and marks the current one freshly re-authenticated.

### E-mail verification is a ceremony kind, a nullable column, and one gate

`users.email_verified_at` (migration 0049; nullable, no backfill - NULL is the honest state of
every existing password account) and an `auth_ceremony` of kind `email_verification` whose row
id is the single-use token in the link, exactly the shape ADR-054's addendum gave the Google
signup ticket and with 0047's own CHECK-widening as the precedent. Issuing a new link retires any
earlier unconsumed one for that user, so "request a new link" means the newest works and an old
one in a forwarded mail does not. The TTL is 24 hours, not the sign-in ceremonies' five minutes,
because a link is read from a mailbox, not completed in the same sitting as a WebAuthn prompt.

Signup issues the link after its own transaction commits (an e-mail is an irreversible external
act and must not be sent for a signup that rolled back), in the language the signup screen was
in, through `api.mail.outbox.get_email_sender` - a process-wide holder for the collecting sender,
because `build_email_sender` returns a fresh instance each call and a collected message nobody
holds a reference to is lost. `POST /v1/auth/verify-email` is tenant-exempt (the link is opened
wherever the mail is read; the token is the proof) and records the event against the user's home
organization; `POST /v1/auth/verify-email/resend` is the signed-in account holder asking again,
rate-limited. Google-created accounts and accounts that link Google are stamped verified at that
moment, since a `GoogleIdentity` cannot be constructed from an unverified address (ADR-006).

An unverified account can sign in, enrol MFA and complete onboarding: none of those create
records anybody else relies on. It cannot post to the ledger. `require_verified_email` is a
FastAPI dependency in `require_ledger_posting_eligibility`'s exact shape (ADR-009) and is declared
on expense posting, invoice issue and credit note issue - the three routes that write journal
entries - not as middleware, because it gates one action.

`GET /v1/dev/outbox` exists only when `settings.expose_dev_outbox` is true: `register` does not add
the route otherwise, and the two exemption lists that name it are built from the same flag, so
the exemption cannot outlive the route. The default is false and stays false.

Full implementation: [src/api/tenancy.py](../../apps/api/src/api/tenancy.py),
[src/api/auth/session_validation.py](../../apps/api/src/api/auth/session_validation.py),
[src/api/auth/sessions.py](../../apps/api/src/api/auth/sessions.py),
[src/api/account/security_routes.py](../../apps/api/src/api/account/security_routes.py),
[src/api/auth/email_verification.py](../../apps/api/src/api/auth/email_verification.py),
[src/api/mail/outbox.py](../../apps/api/src/api/mail/outbox.py),
[migrations/0049_email_verification.sql](../../apps/api/migrations/0049_email_verification.sql).

## Alternatives considered

| Option | Rejected because |
|---|---|
| Keep trusting the JWT's restated claims and add a revocation denylist checked per request | A denylist is a database table read on every request anyway - the exact cost this option exists to avoid - and it answers only revocation, not the idle timeout, the MFA step-up or the switcher. Reading the row answers all four with the same primary-key read. |
| Accept a token without `sid` as before, with claims trusted | No token this system mints lacks one, so refusing costs nothing legitimate; accepting would leave a second class of token with every property ADR-054 named as the gap. Fail closed. |
| A settings flag that skips the session lookup in test environments | Every gate in this codebase is testable without Postgres through injection, never through a switch (ADR-004, ADR-008). A switch is a production misconfiguration waiting to happen, and a test that passes because the check is off proves nothing. |
| Make `/v1/me/*` declare `require_permission` on some "own account" resource | There is no resource for a permission to be about (the `/v1/me/language` argument), and a user with no grants yet - invited, mid-onboarding - must still be able to end a session on a lost phone. |
| Let a password+passkey account remove its only passkey and be re-prompted to enrol at the next request | Leaves an account in the zero-factor state IAM-011 says must not exist, and the "not_enrolled" refusal on the next request is a worse experience than a 409 that says to add the replacement first. |
| Hash the verification token before storing it | A real property, but the ceremony table's posture since 0046 is "short-lived, single-use, superseded by the next issue", and Google `state` values and WebAuthn challenges are stored the same way. A second posture for one kind would be a second thing to reason about. |
| Send the verification mail inside the signup transaction | An e-mail cannot be rolled back; a signup that fails after the send leaves a person holding a link to nothing. Sending after commit is the order `api.invoicing.delivery` already follows for the same reason. |
| Block unverified accounts from everything but verification | Turns a delayed e-mail into a locked-out customer at the moment they are most likely to leave. Posting is the one action whose records outlive the session by seven years; that is where the line belongs, and it is the line IAM-010f already draws. |

## Consequences

- Every non-exempt request now costs one session read and one touch in its own short
  transaction, in addition to `MfaEnforcementMiddleware`'s enrolment lookup and the handler's own
  session - three connections per request. Accepted for the same reason ADR-008 accepted the
  second: a stateful security check needs a round trip somewhere, and the middleware is the one
  place it cannot be forgotten. Folding the enrolment lookup into the same transaction is the
  obvious optimisation if it ever matters.
- `TenantContext.mfa_verified` and `active_administration_id` are now facts about the row, so
  `api.account.routes._active_administration_id`'s own re-read of the session is redundant (kept,
  harmless), and the MFA endpoints' re-minted token is optional for a client that already holds
  one.
- The `adm` claim no longer exists. A client that stored it learns nothing from it; the value is
  in `GET /v1/me` and `GET /v1/switcher/active`.
- Tests that mint bearer tokens by hand must name a live session: `make_token` finds the one
  `seed_user` created, and the unit suite installs `InMemorySessionValidator` on the probe or
  production app. A token with no `sid` is refused with `reason: token carries no sid claim`.
- IAM-016's idle timeout is now enforced on every request for privileged sessions (all sessions,
  until roles decide otherwise - ADR-005). Thirty minutes without a request signs a person out;
  a long-idle browser tab meets `session_idle_timeout` on its next call.
- `users.email_verified_at` is NULL for every account that existed before this change and for
  every new password signup until the link is opened. `GET /v1/me` reports it, and posting
  refuses on it with `reason: email_not_verified`. A deployment migrating real accounts decides
  separately whether to backfill; nothing here does.
- The verification link points at `{app_base_url}/verify-email?token=...`. The web app owns that
  path and turns the query parameter into `POST /v1/auth/verify-email`.
- Not built: rate limiting on `POST /v1/auth/verify-email` itself (the token space is 122 bits;
  the ceremony expires and is single-use), an expiry sweep of consumed ceremonies (the same
  ops-side purge posture 0046 took), and `RoleBasedMfaRequirementPolicy` - still ADR-008's open
  item.
