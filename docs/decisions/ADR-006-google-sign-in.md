# ADR-006: Google sign-in — OIDC with PKCE, minimal scope, verified-email-only

- **Status**: Accepted (account linking implemented — see the update below; originally deferred)
- **Date**: 2026-08-21 (updated 2026-08-22)

## Context

PRD §8.2 requirement IAM-010a mandates Google sign-in use "OpenID Connect with PKCE, requesting
only `openid`, `email` and `profile`. No Gmail, Drive, Calendar or Contacts scopes are requested
at any point." IAM-010b requires the email address from Google to be verified
(`email_verified: true`) before any account is created or matched, rejecting unverified
identities outright. IAM-010c requires that when a Google sign-in matches an existing verified
email, the user authenticate with their existing method once before the identities are linked,
and that automatic linking on email alone be impossible. This ADR originally scoped the work to
IAM-010a/b only and left IAM-010c as a documented but unenforced caller obligation; the 2026-08-22
update closes that gap. MFA interaction and losing-the-Google-account recovery (IAM-010e/f) remain
separate, unbuilt work.

The design questions: how the PKCE + authorization-code flow is structured so the scope list
can't silently grow, how the ID token is actually verified (signature, issuer, audience, nonce),
and — since IAM-010c sits directly next to this and forbids something a naive implementation
would do by default (link on email match) — how to avoid violating it without building the full
linking flow it describes.

## Decision

### Scope list is a named constant with an explanatory comment, checked by tests

`api.auth.google_oidc._SCOPES = ("openid", "email", "profile")` is the single place scopes are
assembled into the authorization request. The module docstring explains why each of the three
is there and why nothing else may join them — reproduced in the code itself, not only here, since
that's where a future contributor deciding whether to add a scope will actually be reading. Four
parametrized tests (`test_start_sign_in_never_requests_gmail_drive_calendar_or_contacts`) assert
the authorization URL never contains those words, so a future scope addition would need to
knowingly break a passing test, not just slip past code review.

### PKCE plus a confidential client secret — not PKCE instead of one

`GoogleOidcClient` generates a PKCE `code_verifier`/`code_challenge` pair (RFC 7636, S256) for
every sign-in attempt, sent in the authorization request and again (as `code_verifier`) in the
token exchange. It also authenticates the token-exchange call with a server-held `client_secret`.
These protect different things: PKCE proves the token-exchange request comes from whoever
initiated the specific flow that produced the authorization code — relevant because the
authorization step happens in the user's browser, a redirect that could in principle be
intercepted. The client secret authenticates LEDGR's backend itself to Google as a confidential
client. Neither makes the other redundant.

### ID token validation: signature, audience, expiry via PyJWT; issuer and nonce by hand

Google's ID token is an RS256-signed JWT verified against Google's published JWKS
(`https://www.googleapis.com/oauth2/v3/certs`), via `jwt.PyJWKClient` — PyJWT's own tool for
exactly this "verify a JWT against a remote key set" case, avoiding a hand-rolled JWKS fetch/cache
implementation for security-sensitive code. `jwt.decode(..., audience=client_id)` handles
audience and expiry checking natively. `iss` (Google has historically used both
`https://accounts.google.com` and `accounts.google.com`) and `nonce` (replay protection — the
value generated at `start_sign_in` must reappear in the ID token) are OIDC-specific and checked
explicitly afterward, since PyJWT has no built-in concept of either.

**IAM-010b enforcement lives inside the OIDC client itself** (`_extract_identity`), not in a
caller that might forget to check: `GoogleOidcClient.complete_sign_in` cannot return a
`GoogleIdentity` for an unverified email under any code path, because the check happens before
the dataclass is even constructed. `claims.get("email_verified") is not True` rejects `False`, a
missing claim, and any non-boolean value identically — only an explicit `true` passes.

### IAM-010c: re-authentication is enforced by the service, not documented as the caller's job

**Update, 2026-08-22**: originally this ADR left the "authenticate with the existing method
first" step as an explicit gap — `link_google_identity` performed the mechanical link on a
caller's unenforced promise to have checked first. That is a real gap, not a stylistic one: a
docstring saying "call this only after..." does not make automatic linking on email alone
*impossible*, only discouraged, and IAM-010c's own wording ("automatic linking on email alone is
prohibited — it is an account takeover path") calls for the stronger guarantee.

`GoogleSignInService.sign_in` still never links on email match — it resolves by Google `sub`
first (a returning user, safe); if no link exists but an email match does, it returns
`GoogleSignInLinkRequired`, performing no account mutation. What changed: `link_google_identity`
is gone. The only way a `GoogleSignInLinkRequired` becomes a link is
`GoogleSignInService.confirm_link_with_password(link_required, password=...)`, which
re-authenticates the *existing* account via `AuthenticationService.authenticate_with_password` —
using the same email the Google identity presented — before linking anything. A wrong password,
or an account with no password credential at all, raises and links nothing. There is no other
public method on `GoogleSignInService` capable of performing this link — enforced by a test that
enumerates the class's public methods, not just asserted in prose (`tests/auth/
test_google_account_linking.py::test_google_sign_in_service_has_no_link_path_that_skips_reauthentication`).

A second, defense-in-depth check compares the freshly re-authenticated user's id against
`link_required.existing_user_id`, raising `LinkIdentityMismatchError` on any mismatch. In normal
operation this is unreachable (the lookup is by the same email the challenge carries), but a
security-relevant invariant worth relying on is worth checking, not assuming — and it's tested
directly by constructing a `GoogleSignInLinkRequired` with a tampered `existing_user_id`.

`user_google_identity` is keyed by `google_subject`, not `email`, so there is no email column to
join on for linking purposes even if application code tried — the schema itself doesn't offer
that path.

**The attack case**, tested directly: an attacker who controls a real, verified Google account
for a victim's email address gets a `GoogleSignInLinkRequired` from `sign_in`, exactly like a
legitimate user would. Attempting `confirm_link_with_password` with any password they don't
actually know raises `InvalidCredentialsError` and links nothing — repeatable any number of
times, with no accumulating state that eventually lets a wrong password through. The victim's
account and their own subsequent ability to sign in with their real password are unaffected by
the attempt.

Full implementation: [migrations/0004_google_identity.sql](../../apps/api/migrations/0004_google_identity.sql),
[src/api/auth/google_oidc.py](../../apps/api/src/api/auth/google_oidc.py),
[src/api/auth/google_signin.py](../../apps/api/src/api/auth/google_signin.py).

## Alternatives considered

| Option | Rejected because |
|---|---|
| Request `access_type=offline` for a refresh token, "in case it's useful later" | Nothing in LEDGR needs to call a Google API after sign-in completes — a refresh token would be a stored credential with no consumer, purely additional attack surface for a capability that doesn't exist. If a real future feature needs Google API access, that's a separate, explicit, additional grant with its own consent screen — never folded into the login scope. |
| Auto-link a Google identity to an existing user whenever the email matches | This is exactly what IAM-010c prohibits, and for a concrete reason: it lets a same-email match from *any* source silently merge into an existing account, without the account holder ever proving they control it via a method LEDGR already trusts. |
| A generic `user_identity` table (provider, external_id) covering Google/passkey/future-SSO uniformly, instead of a Google-specific table | The password credential table (0003) already established a per-method-table pattern for this codebase, and IAM-012/IAM-014's other methods (passkey, SAML/OIDC SSO) have different data shapes (multiple passkeys per user; org-wide SSO config) a shared polymorphic table would have to awkwardly accommodate. A dedicated table now, and a shared abstraction later only if the pattern across three or more methods actually turns out to be identical, avoids designing for a generalization that hasn't been needed yet. |
| Hand-roll JWKS fetching and RS256 verification instead of `jwt.PyJWKClient` | PyJWT already ships a maintained, tested tool for this exact case; reimplementing key-set fetching and caching for security-critical signature verification is unjustified risk for no benefit. |
| Skip the `nonce` check since `state` already provides CSRF protection | `state` and `nonce` protect different things: `state` ties the callback to the browser session that started the flow (CSRF); `nonce` ties the *ID token itself* to this specific flow, preventing a captured, still-valid ID token from a different flow being replayed into this one. OIDC specifies both for a reason; dropping either weakens a distinct guarantee. |

## Consequences

- No HTTP endpoint or UI exists yet — `GoogleOidcClient` and `GoogleSignInService` are the service
  layer a future `/v1/auth/google/start` and `/callback` pair would call. That pairing also has to
  decide where `state`/`nonce`/`code_verifier` live between the two requests (a server-held flow
  record, not a client-readable cookie) — a decision this ADR deliberately leaves open.
- `GoogleSignInLinkRequired` is a real, tested return type with no HTTP consumer yet. A future
  login flow presents it to the user as "sign in with your existing method to link your Google
  account" and calls `confirm_link_with_password` with whatever password they supply — the
  re-authentication guarantee is enforced by the service now, not left to that future flow to get
  right.
- IAM-010e (Google sign-in doesn't satisfy MFA on its own) and IAM-010f (a Google-only user must
  add a passkey or password before posting to the ledger) are unaffected by this change and remain
  unbuilt — both depend on the RBAC/permission system (IAM-030+) to know when a user is about to
  do something write-capable enough to trigger them.
