# ADR-066: The API serves the built React SPA, completing ADR-063's single origin

- **Status**: Accepted
- **Date**: 2026-09-18
- **Completes**: [ADR-063](ADR-063-single-origin-document-serving.md), whose Consequences section named
  this exact gap: *"The API must serve the built SPA for the single-origin deployment to be
  complete. Not done in this ADR; named here as the remaining work it implies."*

## Context

ADR-063 decided LEDGR serves one origin — the API, the built SPA, and stored file bytes all come
from the same host — for reasons that are hard requirements, not preference: session cookies are
`SameSite=Strict` with no `domain=` (SEC-004), there is no CORS middleware anywhere in `api.main`,
and WebAuthn requires the browser to see every request as same-origin with the page that called
`navigator.credentials.create()`/`get()`. A frontend on a separate host — Cloudflare Pages, Vercel,
a second Railway service — could not authenticate anyone; ADR-063's own rejected-alternatives table
already covers this ("Frontend on its own origin, with CORS").

The API was deployed and running (Railway, ADR-062) with nothing implementing this. Asked to give
the frontend "its own hosting step," the correct answer was that no such step exists to build —
serving it from the already-deployed API is the architecture, not a choice being made here.

## Decision

**`api.spa.SpaMiddleware`, not a route.** A route registered on `app` still passes through every
middleware between the request and the router — `TenantContextMiddleware`,
`MfaEnforcementMiddleware`, `AuthorizationEnforcementMiddleware` among them. An SPA asset or the app
shell's `index.html` needs none of that: it is the same public bytes for every visitor, authenticated
or not. Rather than teaching those three security-critical files a "this path is public" exemption
each, `SpaMiddleware` sits outside all of them (added in `api.main` between
`CsrfProtectionMiddleware` and `SecurityHeadersMiddleware`) and simply never calls `call_next` for a
non-API path (anything that isn't `/health`, doesn't start with `/v1/`, and isn't a FastAPI framework
path) — the request never reaches those six layers, the same way it never reaches them for a request
Starlette itself couldn't route. Nothing about `/v1/*` or `/health` handling changed; verified by the
full existing test suite passing unmodified (2344 passed).

**The CSP required a real implementation, not a static file serve.** `SecurityHeadersMiddleware`'s
`Content-Security-Policy` is `script-src 'nonce-<random-per-request>' 'strict-dynamic'` — written,
per its own docstring, "for when [an HTML response] might exist, rather than only for the JSON
responses that exist now." That "now" is this ADR: `index.html` carries two `<script>` tags (the
ADR-055 pre-paint theme script, inline, and the built JS module), and under a nonce-based CSP a
script with no matching nonce does not run at all — a byte-for-byte static serve would ship a page
whose own script never executes. `SpaMiddleware` reads the build output once at startup and injects
the *current request's* nonce (already generated upstream by `SecurityHeadersMiddleware`) into every
`<script` tag on each request. Verified against a running instance, not assumed: the nonce in the
`Content-Security-Policy` response header was confirmed to match the nonce on both script tags in the
returned HTML, byte for byte.

**Where the built dist comes from is an environment variable, not a wheel `force-include`.**
`api.i18n.catalogue` resolves the message catalogue the same "packaged copy first, checkout second"
way — but that catalogue is checked into git, present in every checkout. `apps/web/dist` is a *build
artifact*, absent from a plain checkout, and every API-only CI job (lint, typecheck, test) runs
`uv sync --frozen` without ever building the web app. A `force-include` entry for a path that does
not exist would have failed `uv sync` in all of those jobs, not merely left the SPA unavailable — so
`settings.web_dist_dir` (an ordinary `Settings` field, unset by default) is the mechanism instead.
`apps/api/Dockerfile` gained a `node:20-slim` build stage that produces `apps/web/dist`, copies it
into the final image at a fixed path, and sets `WEB_DIST_DIR` to point there. Nothing about the
Python package's own build changed.

**Static file serving falls back to the checkout path when `WEB_DIST_DIR` is unset** — the common
case for API-only local development, where Vite's own dev server on :5173 proxies to the API on :8000
(`apps/web/vite.config.ts`, unchanged). `SpaMiddleware` becomes a permanent no-op in that mode.

## Alternatives considered

| Option | Rejected because |
|---|---|
| A separate static host (Cloudflare Pages, Vercel, a second Railway service) | Already rejected by ADR-063 itself, for reasons that don't change here: cookies are host-only, there is no CORS layer, and WebAuthn needs same-origin. Revisit only if ADR-063's decision itself is revisited. |
| Register the SPA as a normal FastAPI route (`@app.get("/{full_path:path}")`) | Would still pass through `TenantContextMiddleware` and the rest — a route is reached BY the middleware stack, not around it. `TenantContextMiddleware` would 401 an anonymous visit to `/` (the login page itself), and `MfaEnforcementMiddleware`/`AuthorizationEnforcementMiddleware` would fail similarly, since none of those middlewares have a concept of "this route needs no tenant." Confirmed by reading all three before writing any code — this is what changed the design from a route to a middleware. |
| `force-include` the dist into the wheel, like the i18n catalogue | Requires the source path to exist at `uv sync` time. `apps/web/dist` doesn't exist in a fresh checkout, so every API-only CI job would fail `uv sync --frozen` outright — a build-breaking risk for a feature that only needed to work at deploy time, not at every `uv sync`. |
| Serve `dist/index.html` as a static file, unmodified | Breaks under the existing nonce-based CSP: neither `<script>` tag in it carries a nonce, so neither executes, and the page never paints past its empty `<div id="root">`. Confirmed by reading `api.security.headers` before implementing — this is the finding that turned "mount StaticFiles and be done" into "read the docstring's own anticipation of this exact case and implement it." |

## Consequences

- **ADR-063 is now actually complete**, not merely decided. The only two literal-colour exceptions
  ADR-065 already updated (`PreAuthScreen`'s marketing rail, the MFA TOTP QR panel) apply here too:
  visiting the deployed origin now serves the real login screen, not a 404.
- **Verified end-to-end against a running instance**, not just unit tests: `/` returns the app shell
  with matching nonces on both script tags and `Cache-Control: no-cache`; `/assets/*` returns the
  hashed bundle with a year-long immutable cache; `/v1/whoami` and a nonexistent `/v1/*` path both
  still 401 exactly as before (through `TenantContextMiddleware`, untouched); an arbitrary
  client-side route (`/clients/abc-123`) falls back to the app shell, which is what lets React
  Router take over.
- **`WEBAUTHN_ORIGIN` and `APP_BASE_URL` on the Railway deployment still need to be set to the real
  production origin** (`https://ledgr-production-3fa8.up.railway.app`, or a future custom domain) —
  not done here, since it's an environment-variable value only the operator can choose, not a code
  change. Passkeys and e-mail verification links will target `localhost` until that's set.
- **The Docker build gets slower and heavier** (a full `node:20-slim` + `pnpm install` stage before
  the Python stage even starts) in exchange for a working frontend at all. Not optimised here —
  worth revisiting (build caching, a smaller Node base) if build time becomes a real friction point.
- **What this forecloses**: `SpaMiddleware`'s ordering (between `CsrfProtectionMiddleware` and
  `SecurityHeadersMiddleware`) is load-bearing, not incidental — moving it outside
  `SecurityHeadersMiddleware` would serve the app shell with no CSP/HSTS headers at all; moving it
  inside `TenantContextMiddleware` would 401 the login page. A future middleware-ordering change
  needs to preserve both properties, not just avoid breaking the tests that happen to exist today.
