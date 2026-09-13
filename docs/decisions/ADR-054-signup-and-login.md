# ADR-054: Signup, login, and MFA enrolment — the HTTP surface every prior auth ADR deferred

- **Status**: Accepted
- **Date**: 2026-09-13

## Context

FR-MDL-001, FR-ONB-001a/b and IAM-010 require a working signup and sign-in flow offering Google,
passkey and email+password side by side. Every one of ADR-005 through ADR-008 built a real,
independently-tested building block — password hashing and sessions (005), Google OIDC
verification (006), WebAuthn ceremonies (007), MFA policy and enforcement middleware (008) — and
every one of them explicitly deferred the HTTP layer that wires them together: "no login endpoint
yet" appears in all four. Until this change, there was no way for a real business to get an
account at all.

That gap compounds with a second one already in place: `MfaEnforcementMiddleware` (ADR-008) blocks
every non-exempt request unconditionally, and no enrolment endpoint existed for it to accept as an
exemption. Shipping signup without shipping MFA enrolment in the same change means the first thing
every new account holder experiences is being locked out of the account they just created. This ADR
is the login/signup HTTP layer and the MFA enrolment endpoints, built and shipped together for
exactly that reason.

Three design questions this ADR answers: how a request with no tenant context yet (signing up,
logging in) coexists with `TenantContextMiddleware`'s "every request carries verified tenant
context" rule; how MFA enrolment can run inside an authenticated session without itself requiring
the verification it establishes; and who is allowed to grant a brand-new organization's first user
the Owner role, given IAM-063 forbids self-grants.

## Decision

### A JWT minted at session issuance restates the session's claims — it does not replace sessions with tokens

`api.auth.sessions.SessionService` (ADR-005) issues a real, stateful, revocable session row.
`TenantContextMiddleware` (ADR-002/003) only ever reads a JWT. Rather than teaching the middleware
to look up sessions in the database on every request — a change to every existing tested route's
request path — `api.auth.tokens.issue_access_token` mints a JWT whose claims (`sub`, `org_id`,
`sid`, `mfa_verified`, `exp`) restate the session's state at the moment of issuance. `sid` carries
the session's own id, not its raw bearer secret, so the token and the session remain two different
values pointing at the same row.

This is a deliberate, bounded limitation, not an oversight: a session revoked mid-lifetime (logout
elsewhere, an administrator forcing sign-out) does not retroactively invalidate a JWT already
issued from it — the token remains valid, per its own `exp`, until that expiry, because nothing
re-checks the session row on the requests in between. Closing that gap means
`TenantContextMiddleware` querying `sessions` on every request, which is a real architectural
change to the hot path of every route in this codebase, not something to fold silently into the
change that first makes tokens flow from real sessions. It is recorded here as a named follow-up,
not solved by this change.

### MFA's chicken-and-egg problem is solved with two exemption lists, not one

A brand-new session is minted with `mfa_verified=False` and is real and tenant-scoped from the
first response — it is not a "logged out" state. `MfaEnforcementMiddleware` (ADR-008) would refuse
every request carrying it except for a route in `MfaEnrollmentChecker`'s enrolment/verification
surface. That surface itself needs a *user id* to enrol a factor *for*, so it cannot live in
`api.tenancy.EXEMPT_PATHS` (no tenant context at all) — but it must not itself demand
`mfa_verified=True`, which is precisely the thing it exists to establish.

Two lists, kept deliberately separate, resolve this:

- `api.tenancy.EXEMPT_PATHS` — no tenant context is checked at all. Signup, password login, the two
  passkey sign-in legs, and the two Google sign-in legs: every one of these authenticates a caller
  from the request body/ceremony itself, not from a bearer token, so there is no tenant context yet
  for `TenantContextMiddleware` to verify.
- `api.auth.routes.MFA_EXEMPT_PATHS` — a real, tenant-scoped token is required (these routes still
  pass through `TenantContextMiddleware`), but `MfaEnforcementMiddleware` alone exempts them: the
  TOTP and passkey enrolment/verification endpoints, and logout (someone stuck mid-enrolment must
  still be able to leave).

Passkey sign-in is the one path that skips the intermediate `mfa_verified=False` state entirely:
IAM-012 already counts a passkey as a full MFA factor, so `login_passkey_finish` mints its token
with `mfa_verified=True` directly — successfully authenticating with a passkey both identifies the
user and satisfies MFA in the same step, per IAM-012's own factor list.

### WebAuthn and Google OIDC round-trip state gets one shared table, decided once

`api.auth.passkeys` and `api.auth.google_oidc` (ADR-006/007) both explicitly left "where does the
in-flight challenge/state/nonce/PKCE-verifier live between the two legs of the ceremony" to whatever
built the HTTP layer. `auth_ceremony` (migration 0046) is that decision, made once for both rather
than twice: same shape (issued once, read back once, expires in five minutes, single-use via an
atomic `UPDATE ... WHERE consumed_at IS NULL ... RETURNING`), same cleanup policy, one `kind` column
distinguishing WebAuthn's two ceremony types from Google's. It carries no `organization_id` and no
RLS, for the same reason `users`/`sessions` (ADR-005) don't: this is global authentication
plumbing that exists before any tenant is known, not tenant data.

### The firm-branch organization bootstrap function mirrors the existing self-managed one

FR-ONB-001b's firm signup branch needed a way to create an organization with `kind='firm'` before
any tenant context exists to scope an ordinary RLS-governed `INSERT` to. Migration 0001 already
solved this exact problem for the self-managed branch
(`app.signup_self_managed_organization`, `SECURITY DEFINER`, owned by the narrow `ledgr_bootstrap`
role). `app.signup_firm_organization` (migration 0046) is the same shape for `kind='firm'` — not a
generalized "create any organization kind" function, because the two branches ask two different
questions of the signup form (a KvK number is firm-specific) and a single parameterized function
would need to validate that combination anyway.

### The founding Owner grant bypasses `AuthorizationService.assign_role`, once, with the reason on record

IAM-063 forbids a user from granting themselves a role they do not already hold — a self-grant is a
privilege-escalation signal in every other context. A brand-new organization's very first user is
the one legitimate exception: nobody else could have granted them Owner, because until this moment
nobody else has any grant on this organization at all. Rather than teach the general-purpose
`AuthorizationService.assign_role` path a bootstrap special case — which would mean every future
caller of that path inherits a carve-out that only ever applies once per organization's lifetime —
`SignupService._grant_founding_owner` inserts the `role_assignment` row directly, in the same
database transaction as the organization's own creation, immediately after setting
`app.current_org_id` for that transaction. The insert is narrow (one specific role name, one
specific scope shape) and reviewable in isolation; it is not a second implementation of the
authorization library, which remains the single one every other grant path uses.

### Authentication events are recorded from the handler, not declared on the route

`api.audit.trail.AuditTrail.authentication` (built in a prior audit-wiring change, previously
uncalled) exists for exactly this: IAM-090's "authentication events" category, recorded from the
service/handler layer for the paths that have no `require_permission(audit=...)` dependency to hang
a declaration on. Every route in this ADR runs before any permission can be evaluated — the route
*is* how a permission-bearing identity gets established — so `tests/test_audit_coverage.py`'s
`AUDIT_EXEMPT_PATHS` lists all of them, with the reason pointing at
`api.auth.routes._record_authentication_event` as the actual recording mechanism. The "begin" half
of each WebAuthn/OIDC ceremony (which only writes a short-lived, single-use ceremony row) is exempt
for the ordinary reason instead — nothing IAM-090 asks about happens until the matching
finish/confirm/callback either succeeds or is abandoned.

### One thing this change deliberately does not build

- **Email verification before password/passkey signup.** IAM-010b's exemption is real for Google
  specifically — the identity provider already verified the address. For password and passkey
  signup, this change marks the account active immediately. A named, bounded gap: adding
  verification later needs a `users.email_verified_at` column and a gate on it, nothing this change
  would have to unwind.

Google sign-in creating a brand-new account with no prior signup was also named here as a deferred
gap in the original version of this ADR; it is now built — see the Addendum below.

Full implementation: [migrations/0046_signup_and_ceremonies.sql](../../apps/api/migrations/0046_signup_and_ceremonies.sql),
[src/api/auth/routes.py](../../apps/api/src/api/auth/routes.py),
[src/api/auth/signup.py](../../apps/api/src/api/auth/signup.py),
[src/api/auth/ceremony.py](../../apps/api/src/api/auth/ceremony.py),
[src/api/auth/tokens.py](../../apps/api/src/api/auth/tokens.py).

## Alternatives considered

| Option | Rejected because |
|---|---|
| Have `TenantContextMiddleware` query `sessions` on every request instead of trusting a restated JWT | Changes the request path of every existing tested route in the codebase to add a database round trip that only this change's revocation edge case needs. The restated-JWT approach ships the required flow now; live revocation checking is a named, separate follow-up, not something to smuggle into this change. |
| One combined exemption list instead of `EXEMPT_PATHS` and `MFA_EXEMPT_PATHS` | Collapses two different properties into one: "no tenant context needed" (signup/login) and "tenant context needed but MFA is exempt" (enrolment/verification/logout) are answers to different questions asked by two different middlewares. A single list would either let signup skip MFA enforcement by accident (harmless here, but for the wrong reason) or force enrolment routes through `TenantContextMiddleware`'s stricter check unnecessarily. |
| A generic, parameterized `app.signup_organization(kind, name, kvk)` bootstrap function instead of a second kind-specific one | The self-managed and firm branches validate different inputs (KvK number is firm-specific) and read less clearly as one function with conditional branches inside a `SECURITY DEFINER` body — the exact kind of code a security review of a privilege-escalation-adjacent function should not have to untangle. |
| Teach `AuthorizationService.assign_role` a bootstrap exception for the founding Owner grant | Every future caller of the general-purpose grant path would inherit a carve-out that only ever legitimately fires once per organization's lifetime. A narrow, separately reviewable insert inside `SignupService` keeps the shared authorization library free of a special case it does not otherwise need. |
| Build Google-sign-in account creation and email verification in this change, since the routes already exist | Both are real, separate pieces of design work (a multi-step OAuth-redirect account-model choice; an email-verification-state machine and its resend/expiry behavior) that would have delayed shipping the one thing blocking every other requirement: a working way to get an account at all. Named explicitly as gaps rather than silently omitted. |

## Consequences

- A session revoked between issuance and its JWT's `exp` does not immediately deny requests
  bearing that JWT — see the JWT-restates-session-claims decision above. Closing this requires
  `TenantContextMiddleware` to consult `sessions` per request, a follow-up affecting every route.
- `AUDIT_EXEMPT_PATHS` in `tests/test_audit_coverage.py` grows by fourteen entries. This is not a
  weakening of IAM-090 coverage — every authentication event these routes produce is recorded via
  `AuditTrail.authentication`, just not through the route-declaration mechanism the exemption list's
  comment explains route-by-route.
- Passkey enrolment/verification "finish" ceremonies are claimed (`consumed_at` set) inside the same
  database transaction as the rest of the request. A request that fails after claiming a ceremony
  but before an explicit `session.commit()` rolls the claim back along with everything else in that
  transaction — a rejected hijack attempt against someone else's ceremony does not burn it for the
  legitimate owner, but this also means "claimed" is not durable until the whole request succeeds.
- ~~No frontend exists yet for any of this~~ — built the same day as this ADR: `apps/web/src/auth/`
  (`LoginForm`, `SignupForm`, `MfaEnrollment`, `GoogleCallback`), `AuthApi`, a hand-written
  `navigator.credentials` adapter (`auth/webauthn.ts`, no WebAuthn browser package existed in this
  repo), and `App.tsx`'s `Shell` wiring the whole flow together.
- One known, named gap remains open by design: no email verification for password/passkey signup.
  Scoped out explicitly above, not silently missing.

## Addendum: Google-initiated signup and MOB-009 sign-out purge (2026-09-13, same day)

Two of the gaps this ADR originally named as deliberately deferred are now closed.

### Google sign-in creating a brand-new account

`GoogleSignInService.sign_in` (ADR-006) already created a bare `users` row and linked the identity
the moment a Google sign-in matched neither an existing linked subject nor an existing email —
`login_google_callback` simply refused to proceed past that point, telling the person to sign up
with a password first. What was missing was FR-MDL-001's one question (account model, organization
name, KvK number) and the organization itself, and Google's own OAuth redirect has no room to carry
those three fields through it — the callback request is exactly `code` and `state`.

The fix is a second, smaller ceremony kind rather than a new table: `google_signup` (migration
0047 widens `auth_ceremony`'s `kind` CHECK constraint) stores only `user_id`, handed to the frontend
as a one-time `ticket` in `login_google_callback`'s new `{"status": "signup_required", "ticket":
..., "email": ...}` response. A follow-up screen collects the one question and posts it to
`POST /v1/auth/signup/google`, which consumes the ticket, resolves the already-created bare user,
and calls `SignupService.provision_organization` — the part of `signup()` that runs after a `User`
row already exists, factored out of `signup()` for this exact reuse (`signup()` itself is
unchanged in behavior, just restructured to call the same shared method). No new migration was
needed for the ticket's own fields, because `auth_ceremony.user_id` already existed for a different
purpose (WebAuthn's `passkey_registration`/`passkey_authentication` kinds) and the existing
`auth_ceremony_kind_has_matching_fields` constraint already permits a kind that sets neither the
WebAuthn nor the Google-OIDC column group.

The finished account always mints `mfa_verified=False`, same as password signup — `amr` (which
would let a step-up-free Google sign-in skip MFA per IAM-010e) is only available at the moment the
ID token is first verified, and is deliberately not carried into the ticket alongside `user_id`;
persisting it would mean the ticket also has to guard against a stale, unused `amr` claim
outliving the token that produced it. Requiring MFA enrolment unconditionally here is the same
conservative default `login()`'s password path already takes, not a new decision.

Frontend: `GoogleCallback.tsx` gained a third outcome (`signup_required`) alongside `signed_in`/
`link_required`, rendering a small `GoogleSignupForm` that asks only the account-model question —
never email or password, both already settled by the Google sign-in that got here.

### MOB-009's sign-out purge trigger

`capture/queue.ts`'s `purgeCaptureQueue`/`capturesAtRisk` existed, unwired, from the mobile capture
work that predates ADR-054. `App.tsx`'s `Shell` now calls `capturesAtRisk()` when the sign-out
button is clicked; a count of zero (the common case — nothing was ever captured, or everything
already uploaded) signs out immediately as before. A positive count renders `SignOutConfirm`
(`apps/web/src/SignOutConfirm.tsx`), mirroring `capture/CaptureScreen.tsx`'s `QualityPrompt`
exactly — an `alertdialog`, `useModalFocus`, the safe option (cancel) before the destructive one
(sign out anyway) — rather than inventing a second confirmation pattern. Confirming calls
`purgeCaptureQueue("logout")` before completing sign-out; cancelling leaves the session untouched.

Full implementation: [migrations/0047_google_initiated_signup.sql](../../apps/api/migrations/0047_google_initiated_signup.sql),
the `signup_google` handler and the edited `login_google_callback` branch in
[src/api/auth/routes.py](../../apps/api/src/api/auth/routes.py),
`SignupService.provision_organization` in
[src/api/auth/signup.py](../../apps/api/src/api/auth/signup.py),
the `google_signup` ceremony methods in
[src/api/auth/ceremony.py](../../apps/api/src/api/auth/ceremony.py),
[apps/web/src/auth/GoogleCallback.tsx](../../apps/web/src/auth/GoogleCallback.tsx),
[apps/web/src/SignOutConfirm.tsx](../../apps/web/src/SignOutConfirm.tsx).
