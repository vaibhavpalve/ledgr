# ADR-050: Security headers, cookie flags and CSRF protection

- **Status**: Accepted
- **Date**: 2026-09-13

## Context

SEC-003 requires a nonce-based Content-Security-Policy with no `unsafe-inline`/`unsafe-eval`.
SEC-004 requires HSTS with preload, `SameSite=Strict`/`Secure`/`HttpOnly` session cookies, and
anti-CSRF protection on state-changing requests. Neither had any implementation before this
change, and both are release blockers under PRD §9.2.

Two things about the current codebase shape this decision more than the requirements text does:

- There is no server-rendered HTML anywhere in this repo. `apps/web` is a pure Vite SPA with no
  server component, and the API (`apps/api`) returns JSON only. A CSP is still worth sending on
  every response (defense in depth, and correctness for whatever eventually serves the SPA's
  `index.html`), but there is no template today that would consume `request.state.csp_nonce`.
- There is no cookie-based session anywhere in this codebase. Per
  [ADR-005](ADR-005-authentication-foundation.md), authentication is a bearer token read from the
  `Authorization` header, and "no login UI or HTTP endpoint was requested" is still true — grepping
  `apps/web/src` finds no `credentials: 'include'` or cookie handling anywhere. This matters
  because CSRF is specifically an attack on credentials a browser attaches automatically; a bearer
  token an explicit client sets in a header is not exploitable that way.

## Decision

### Security headers: unconditional, outermost middleware

`api.security.headers.SecurityHeadersMiddleware` generates a per-request nonce, stores it on
`request.state.csp_nonce`, and sets `Content-Security-Policy` and `Strict-Transport-Security` on
every response. It is added as the **outermost** middleware in `api.main` (last `add_middleware`
call — Starlette wraps outermost-last) specifically so it also covers responses other middleware
short-circuits: a 401 from `TenantContextMiddleware`, a 403 from `AuthorizationEnforcementMiddleware`
or the new `CsrfProtectionMiddleware`, and any `HTTPException`-derived error response. A middleware
placed closer to the router would miss all of those.

The policy: `default-src 'none'` as the closed baseline, `script-src 'nonce-<value>'
'strict-dynamic'` (no `unsafe-inline`, no `unsafe-eval`, and no host allowlist a compromised CDN
could abuse), and narrow `self`-scoped directives for style/img/connect/font. HSTS is
`max-age=63072000; includeSubDomains; preload` — two years, both mandatory preload-list directives
present, comfortably above the one-year floor `hstspreload.org` requires.

### Cookie flags: one sanctioned function, enforced structurally

`api.security.cookies` provides `set_session_cookie`, `set_csrf_cookie` and `clear_session_cookie`
— each hardcodes `secure=True`, `httponly` fixed per cookie's purpose, and `samesite="strict"`,
with no parameter to relax any of them. Nothing else in the codebase is allowed to call
`Response.set_cookie`/`.delete_cookie` directly:
`tests/security/test_cookie_flags.py::test_no_other_file_sets_a_cookie_directly` walks every file
under `apps/api/src/api` and fails if any file other than `security/cookies.py` contains such a
call. This is deliberately built with no live caller today, the same posture ADR-005 already
established for `SessionService.record_reauthentication` — a primitive that exists so a future
login endpoint has no way to set an insecure session cookie by construction, rather than a
guideline a future PR could quietly not follow.

### CSRF: double-submit cookie, scoped to cookie-authenticated requests only

`api.security.csrf.CsrfProtectionMiddleware` checks unsafe methods (POST/PUT/PATCH/DELETE)
**only when the request already carries the session cookie**. A request with no session cookie —
which today means every real request this API serves — passes through untouched. This is not a
narrowed implementation of SEC-004; it is what "anti-CSRF" means: the attack requires a credential
the victim's browser sends without being asked, and a bearer token in an `Authorization` header a
JavaScript client must set explicitly is not that. Checking bearer-only requests against a CSRF
token would protect nothing and would need every existing route's tests taught about a header that
serves no purpose yet.

When the session cookie is present, the check is a standard double-submit: a second,
**non-HttpOnly** `ledgr_csrf` cookie must match an `X-CSRF-Token` header, compared with
`hmac.compare_digest`. The CSRF cookie is deliberately readable by JavaScript — that is the whole
mechanism (same-site script can read it and echo it back; a cross-site attacker's forged request
carries the cookie automatically but cannot read its value to construct the header).

Wired into `api.main` immediately inside `SecurityHeadersMiddleware` — before tenant context is
even resolved, since the check depends only on the request's cookies and method, not on anything
`TenantContextMiddleware` establishes.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Synchronizer-token CSRF (a server-side token tied to the session, checked against a hidden form field or header) | Requires a place to persist the token per session and a way to hand it to the client on page load — real work for a session mechanism that does not exist yet. Double-submit needs no server-side state beyond the two cookies already being set, and is equivalent in strength against network attackers given the CSRF cookie is `Secure` (cannot be read or set by a network attacker on plain HTTP). |
| Apply the CSRF check to every unsafe-method request, bearer tokens included | Would either reject every real route in this codebase today (nothing sends `X-CSRF-Token`) or need an exemption list that swallows every existing route, which is indistinguishable from not protecting anything. Scoping to "session cookie present" is what makes the check meaningful rather than decorative. |
| `SameSite=Strict` alone, no separate CSRF token | `SameSite=Strict` is real cross-site request forgery mitigation and arguably sufficient alone for a modern-browser baseline, but SEC-004 asks for anti-CSRF as a named, separate control, and a defense-in-depth double-submit costs one extra cookie and header check. Kept both rather than treating the cookie flag as satisfying the requirement by itself. |
| Leave cookie flags to whichever route eventually calls `response.set_cookie` | This is exactly the failure mode SEC-004 exists to prevent — a correct login endpoint written eight months from now with no memory of this ADR, one keyword argument short of `Secure` or `SameSite=Strict`. The structural test in `test_cookie_flags.py` is what makes that failure mode a CI failure instead of a silent gap. |
| Skip HSTS/CSP until the web app has a server to attach them to | SEC-003/004 apply to "the application," and the API is the only server-side component that exists. Sending these headers on JSON responses costs nothing and is what a reverse proxy or edge CDN in front of this API would otherwise have to add itself, undocumented, at deploy time. |

## Consequences

- `CsrfProtectionMiddleware` and the cookie helpers are, like
  `api.rate_limit_middleware.RateLimitingMiddleware` before them, primitives with no live caller —
  there is no route today that sets the session cookie, so the CSRF check is a no-op on every real
  request. Unlike the rate limiter, `CsrfProtectionMiddleware` **is** wired into `api.main` now
  (there is no reason not to: it costs nothing on a cookie-less request), so a future cookie-based
  login endpoint is protected from the moment it starts setting the session cookie, with no
  follow-up wiring step to remember.
- A future login/session HTTP endpoint must call `api.security.cookies.set_session_cookie` *and*
  `set_csrf_cookie` together — the CSRF check assumes both cookies are issued as a pair. That
  endpoint's own tests will need to exercise the CSRF flow (issue cookies, then post through them)
  rather than only the session-issuance logic ADR-005 already covers.
- `request.state.csp_nonce` exists and is tested but has no consumer — the day this API (or
  something serving `apps/web`) renders any HTML with an inline `<script>`, that tag's `nonce`
  attribute is where this value belongs.
- No exemption list exists yet for either middleware (no route sets the session cookie, so nothing
  needs exempting from the CSRF check). The day one does, a webhook-style endpoint that
  legitimately cannot present a CSRF token (a PSP or bank callback, none of which exist yet either)
  would need one — this ADR does not design that mechanism because there is no such endpoint to
  design it against yet.
