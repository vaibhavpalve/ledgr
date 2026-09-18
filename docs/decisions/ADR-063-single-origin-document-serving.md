# ADR-063: Documents are served from the API's own origin, defended by headers alone

- **Status**: Accepted
- **Date**: 2026-09-18
- **Deviates from**: `SEC-005` (priority **M**), whose text names a separate origin explicitly
- **Amends**: [ADR-030](ADR-030-document-storage.md) (which quoted SEC-005's separate-origin
  clause as settled), [ADR-044](ADR-044-logo-upload-and-svg-sanitization.md) (which reused
  `verify_separate_origin_configured` for template assets)

## Context

SEC-005 reads, in full:

> File uploads: type verified by content not extension, size-capped, malware-scanned, stored
> outside the web root, **served from a separate origin** with `Content-Disposition: attachment`.

The separate origin defends against one specific thing: a file somebody uploaded being rendered
by a browser in the origin that holds the session cookie. A malicious SVG, an HTML file stored as
a receipt, a PDF carrying its own JavaScript — if any of them render where the session lives,
script runs with access to that session.

Until now `api.documents.routes.verify_separate_origin_configured` enforced the clause by
refusing to serve unless `DOCUMENT_ORIGIN` was set and differed from `API_ORIGIN`. Standing that
up for real surfaced something the check itself cannot see: **the deployment it describes cannot
authenticate anybody.**

- Session cookies are host-only — `api.security.cookies` passes no `domain=`, deliberately
  (SEC-004, `SameSite=Strict`). A cookie set by `api.<domain>` is never sent to
  `documents.<domain>`.
- There is no CORS middleware anywhere in `api.main`, so a credentialed cross-origin fetch is
  refused by the browser before it reaches us.
- Documents carry no bearer credential of their own: every read goes through the session and the
  authorization library, by design (`BlobStore` has no `url_for`, per ADR-030 §Protocol).

So a document request arriving at a genuinely separate origin carries nothing, `tenant.user_id`
is `None`, and every download 403s. The separate origin was enforced as configuration and had
never been exercised end to end.

That is not a configuration bug, it is a tension in the requirement as written: **the same
property that stops a malicious document reaching the session cookie also stops a legitimate
request presenting it.** Sharing the cookie across the two origins (`Domain=.<domain>`) would
resolve the authentication problem by destroying the isolation the clause exists to create.
Closing it properly needs a second mechanism — a short-lived, single-use capability token minted
by the API and redeemed at the document origin — which is real design and implementation work,
not a deployment setting.

## Decision

**LEDGR serves one origin.** The API, the built React SPA, and stored file bytes all come from
the same host. `DOCUMENT_ORIGIN` and `API_ORIGIN` are deleted from settings rather than left
configurable, and `verify_separate_origin_configured` is deleted along with both its call sites.

What SEC-005's separate origin was protecting is instead carried by the response headers, which
were already in place on both byte-serving routes:

| Header | What it does |
|---|---|
| `Content-Disposition: attachment` | The browser saves instead of rendering. No render, no script. |
| `X-Content-Type-Options: nosniff` | Stops the browser second-guessing the verified type and rendering it as something else. |
| `Content-Security-Policy: sandbox; default-src 'none'` | For anything that renders anyway: an **opaque origin**, with nothing loadable and nothing executable. |
| `Cache-Control: private, no-store` | Financial evidence under 7-year retention does not belong in a shared cache. |

The `sandbox` directive is the load-bearing one for this decision. A response carrying it is
placed by the browser in a unique opaque origin — which is, mechanically, the isolation a separate
hostname would have provided, obtained per-response instead of per-deployment.

### The headers move somewhere they cannot be forgotten

The previous design's own reasoning for keeping a separate origin was explicit: the headers are
*"one header away from being forgotten."* Removing the second layer without answering that would
be trading a real control for a comment.

So every response carrying stored bytes now goes through one function,
`api.security.stored_files.stored_file_response`, which applies all four headers itself. Call
sites pass content and a content type; they cannot pass any of the four, and a colliding key in
`extra_headers` raises rather than silently winning or losing (matched case-insensitively, since
HTTP header names are). Both existing call sites — `api.documents.routes.download_document` and
`api.templates.routes` for logo assets — go through it. A new byte-serving route written without
it returns no bytes at all, rather than returning them unprotected.

### The verified content type is still sent

`stored_file_response` sends the type sniffed from the bytes at upload
(`api.documents.content_type`), not `application/octet-stream`. Forcing octet-stream would close
the rendering path even more firmly, at the cost of any future inline preview. `nosniff` is only
meaningful next to a type worth not sniffing past, and the disposition plus sandbox already
prevent rendering.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Keep the separate origin, add short-lived single-use capability tokens so cross-origin downloads can authenticate | The correct answer to SEC-005 as literally written, and the one to revisit if the residual risk below ever stops being acceptable. Rejected *now* because it is a new credential type with its own minting, TTL, replay and revocation semantics, in a product with no customers yet — and because the capability URL it produces has leak paths (history, referrer, logs) that need their own mitigations. Cost is real; the marginal protection over `CSP: sandbox` is not. |
| Keep the separate origin and share the session cookie across subdomains via `Domain=` | Self-defeating: it sends the session cookie to the origin serving untrusted bytes, which is the exact outcome the separate origin exists to prevent. Also weakens SEC-004 for every other route. |
| Serve documents from a separate origin that sets its own session cookie after a token handoff | Re-creates ambient authority on the untrusted-content origin: a document rendering there could fetch that user's other documents. Two session lifecycles to expire and revoke in step, for a worse security property than per-request tokens. |
| Pre-signed R2 URLs (`url_for` on `BlobStore`) | Gives a separate origin for free, but a pre-signed URL is a bearer capability that bypasses the authorization library for its whole lifetime — colliding with CLAUDE.md's non-negotiable that every authorization decision is evaluated server-side per request. ADR-030 already excluded `url_for` for this reason. |
| Frontend on its own origin, with CORS | Would have reintroduced the cross-origin problem for the whole API, not just documents, and added a credentialed-origin allowlist as new security surface. `apps/web/vite.config.ts` already documents why dev proxies rather than using CORS: WebAuthn requires the browser to see requests as same-origin with the page that called `navigator.credentials`. Same-origin in production is the consistent choice, not a new one. |

## Consequences

- **This is a deviation from an M-priority security requirement, recorded as such.** SEC-005's
  other four clauses (content-sniffed type, size cap, malware scan, stored outside the web root)
  are unaffected and still enforced. Only the separate-origin clause is replaced.
- **The residual risk is real and narrower than before.** Previously, a byte-serving route that
  omitted the headers still landed on a cookie-less origin. Now there is no layer behind them.
  The mitigation is structural rather than procedural — the headers cannot be omitted by a route
  that goes through `stored_file_response`, and no other way to return stored bytes exists — but
  it is one layer where there were two. A future route that builds a `Response` by hand and
  bypasses the helper is the failure mode to watch for in review.
- **`CSP: sandbox` is doing real work, so it is now a correctness dependency**, not a belt-and-
  braces extra. Any change that relaxes or drops it on these responses reopens the original
  threat and must be treated as a security change.
- **Documents become functional immediately**, with no second hostname and no domain purchase —
  the previous check refused to serve in any single-origin environment, including local
  development, where `API_ORIGIN` and `DOCUMENT_ORIGIN` both defaulted to `localhost` and
  therefore compared equal.
- **The API must serve the built SPA** for the single-origin deployment to be complete. Not done
  in this ADR; named here as the remaining work it implies.
- **Revisit trigger**: if LEDGR ever serves uploaded content inline (preview rather than
  download), or accepts a file type whose renderer is harder to reason about than today's set,
  the capability-token design in the alternatives table should be reconsidered before that
  feature ships, not after.
