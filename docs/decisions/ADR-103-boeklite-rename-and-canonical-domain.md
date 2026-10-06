# ADR-103: The product is Boeklite; boeklite.nl is the one origin and boeklite.com redirects to it

- **Status**: Accepted
- **Date**: 2026-10-05
- **Builds on**: [ADR-063](ADR-063-single-origin-document-serving.md), [ADR-066](ADR-066-api-serves-the-built-spa.md)

## Context

LEDGR was a working name (prd.md). The product is now **Boeklite**, with two domains: `boeklite.nl`
and `boeklite.com`. ADR-063 requires exactly one origin — session cookies are host-only (SEC-004)
and a passkey is bound to one relying-party id — so "host on both domains" cannot mean serving the
app on both.

## Decision

**Canonical origin: `https://boeklite.nl`.** Dutch SMBs are the customer, and `resolveLanguage`
already treats a `.nl` host as a Dutch signal. `boeklite.com` (and `www.` variants) answer with a
308 redirect to the canonical origin via `api.canonical_host.CanonicalHostMiddleware`, driven by
`REDIRECT_HOSTS` and `APP_BASE_URL`. Only listed hosts redirect, so the Railway service URL keeps
working and `/health` is never redirected.

**Production settings** (Railway variables on the `ledgr` service):
`APP_BASE_URL=https://boeklite.nl`, `WEBAUTHN_ORIGIN=https://boeklite.nl`,
`WEBAUTHN_RP_ID=boeklite.nl`, `WEBAUTHN_RP_NAME=Boeklite`,
`REDIRECT_HOSTS=boeklite.com,www.boeklite.com,www.boeklite.nl`,
`GOOGLE_REDIRECT_URI=https://boeklite.nl/` (also registered on the Google OAuth client).

**Renamed:** everything a person or a browser sees — UI and API strings, the message catalogue,
page title, PWA manifest, wordmark files, TOTP issuer, WebAuthn RP name, PDF producer, SEPA
reference prefix (`BOEKLITE-`), the session cookie (`boeklite_session`), the theme storage key,
`BOEKLITE_API_URL`, and the default sender `noreply@boeklite.nl`.

**Deliberately not renamed:** the Postgres database and roles (`ledgr_app`, `ledgr_ops`,
`ledgr_ledger`), existing migrations (checksummed; editing one breaks `migrate.py` on deploy), the
KMS key ring, the Railway service name, the `@ledgr/*` workspace package scope and `--ledgr-*` CSS
variables, and historical ADRs. None is visible to a customer, and each has a real cost to change
(a live-database role rename, key-ring re-creation, a lockfile rewrite). Rename them only if there
is a reason beyond tidiness.

## Consequences

- **Everyone is signed out once** on deploy: the cookie name changed. Pre-launch, so accepted.
- **Passkeys registered against the Railway hostname stop working** — an RP id cannot move between
  domains. Re-register after the cutover.
- **E-mail** from `noreply@boeklite.nl` needs SPF/DKIM/DMARC published for boeklite.nl by the SMTP
  provider before invoice or verification mail is sent from it.
- `boeklite.com` must have its own DNS records and a Railway custom domain (for the TLS certificate)
  even though it only redirects.
