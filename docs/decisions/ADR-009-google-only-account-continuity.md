# ADR-009: Business continuity against a Google-only account (IAM-010f)

- **Status**: Accepted
- **Date**: 2026-08-23

## Context

PRD §8.2's IAM-010f: "Loss of the Google account must not lock a user out of their books: any user
whose only method is Google is prompted to add a passkey or password before they can post to the
ledger." The user framed this precisely as a business-continuity control, not a security one:
LEDGR does not control account recovery for a third-party identity provider, so an account with no
LEDGR-controlled recovery path is a real risk to a user's continued access to records Dutch fiscal
law requires them to retain (CMP-001; PRD §10.4's 7-year retention).

The design question this ADR answers is narrower than it looks: not "how do we check whether an
account is Google-only" (mechanically simple, given the three sign-in-method tables ADR-005/006/007
already built), but where that check belongs architecturally, given the immediately preceding
decision (ADR-008) built MFA enforcement as blanket middleware for a superficially similar-sounding
requirement.

## Decision

### A FastAPI dependency, not middleware — deliberately different from ADR-008

IAM-011 (MFA, ADR-008) applies broadly — "mandatory for all users with any write permission,"
effectively every authenticated action once real write-permission scoping exists — which is why it
is ASGI middleware, run on every non-exempt request. IAM-010f is narrower by its own wording:
"before they can post to the ledger" names one specific action. A Google-only account can still
sign in, browse, and read its books freely under this requirement; only the ledger-posting action
is gated. Applying blanket middleware to a single-action requirement would either over-block
(treating every request as if it were a ledger post) or require the middleware to pattern-match
routes by path/method against a set of "ledger-posting" endpoints that don't exist yet — a worse
design than simply declaring the check on the route that needs it.

`require_ledger_posting_eligibility` (`api.auth.account_continuity`) is therefore a FastAPI
dependency, the same mechanism `api.tenancy.get_tenant_context` and `api.db.get_db_session` already
use for per-endpoint concerns. A future ledger-posting route declares
`Depends(require_ledger_posting_eligibility)`; nothing else in the application is affected.

### TOTP is deliberately excluded from "sign-in method"

`SignInMethodChecker` consults exactly three sources — password, Google, passkey — matching
IAM-010's own enumeration of sign-in methods. TOTP (`api.auth.totp`) is not consulted, because
IAM-012 frames it as a *second* factor, never a standalone way to sign in; a user cannot have "only
TOTP" as their access method by construction (it is always paired with one of the three named
methods). Including it in the Google-only check would misrepresent what actually protects the
account's continuity.

### The check is evaluated fresh on every call, never cached

`SignInMethodStatus` is a plain snapshot; `ensure_can_post_to_ledger` re-derives it from the
repositories on every invocation. This matters concretely: an account with Google plus one passkey
is fine today, but if that passkey is later revoked (`api.auth.passkeys.WebAuthnService.
revoke_passkey`) and nothing else was added, the account becomes Google-only *at that moment* — the
next ledger-posting attempt must catch it, not rely on a decision made when the account looked
different. `test_revoking_the_only_passkey_reintroduces_the_block` exercises this directly.

### `is_google_only` deliberately does not fire on zero methods

An account with no sign-in method at all is a different, currently-unreachable failure mode (no
signup path in this codebase produces one) — not the scenario IAM-010f describes. Conflating "no
methods" with "Google-only" would answer a broader question than this property is meant to,
muddying what a future caller can rely on it meaning. `test_no_methods_at_all_is_not_reported_as_
google_only` pins this down explicitly rather than leaving it to be discovered as a surprise later.

### Testable without a database, via FastAPI's own override mechanism

`get_sign_in_method_checker` and `get_tenant_context` are both ordinary FastAPI dependencies, so
HTTP-layer tests use `app.dependency_overrides` — FastAPI's built-in substitution mechanism — to
inject an in-memory-backed checker and a fixed tenant context, with no real database or JWT/
middleware stack involved. This is a cleaner fit than the injectable-constructor-parameter pattern
`MfaEnforcementMiddleware` needed in ADR-008, because dependencies (unlike ASGI middleware) are
already part of FastAPI's DI system and designed to be swapped this way.

Full implementation: [src/api/auth/account_continuity.py](../../apps/api/src/api/auth/account_continuity.py).

## Alternatives considered

| Option | Rejected because |
|---|---|
| ASGI middleware, matching ADR-008's MFA pattern | IAM-010f names one specific action, not a blanket concern — middleware would need to guess which requests are "posting to the ledger" without any real ledger-posting routes to pattern-match against, or over-block every request. A per-endpoint dependency, declared only where needed, is the correct-shaped tool. |
| Including TOTP in the sign-in-method check | TOTP is a second factor (IAM-012), not a standalone access path — no account can be "TOTP-only" by construction, so including it would not change any real account's status, only obscure why the check consults the sources it does. |
| Caching `SignInMethodStatus` per request/session instead of re-deriving it each call | Would let a passkey revocation leave a stale "fine" result in effect until the cache expired — directly undermining the continuity guarantee this control exists for. The check is cheap (three small, indexed lookups); there is no performance case for caching a security-relevant boolean whose whole value depends on being current. |
| Treating zero-methods as Google-only (i.e., `is not has_password and not has_passkey` without requiring `has_google`) | Would fire the "add a passkey or password" prompt for a scenario IAM-010f isn't describing (no method at all, not "Google is the only one"), and — since this state isn't reachable today — would be an untested, speculative branch dressed up as handling a real case. |

## Consequences

- No ledger-posting endpoint exists to declare this dependency on yet (FR-GL is unbuilt).
  `require_ledger_posting_eligibility` is the primitive such a route will use; wiring it in is
  future work, consistent with every prior auth ADR's boundary.
- The "prompt" IAM-010f calls for is, today, the structured `reason: "google_only_account"` detail
  on the `HTTPException(403)` this dependency raises. Turning that into an actual UI prompt ("add a
  passkey or password now") is a future frontend concern this ADR does not build.
- Should IAM-012's factor set ever grow beyond passkey/TOTP (it must not gain SMS — see ADR-008),
  or should a fourth sign-in method be added to IAM-010's list, `SignInMethodChecker` is the one
  place that would need a new source consulted — deliberately small and explicit rather than a
  generic registry, matching the same reasoning ADR-008 applied to `MfaFactorKind`.
