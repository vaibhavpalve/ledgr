# Founder review and delivery plan — 14 September 2026

> **Status, end of 14 September.** §3's golden path is reachable: a new customer signs up, enrols
> MFA, confirms their e-mail, is taken through onboarding, and lands on a dashboard with their
> chart of accounts seeded and their fiscal year open. Dashboard, capture, review, overview, sales
> invoices, customers, grootboek, settings and the firm portfolio all render on a real account, at
> 1440 and 390 px. Backend §4 is built and tested (2832 API tests); the web app is routed with one
> URL per screen (454 tests, typecheck, lint and production build clean).
>
> **What is not done, and is the next block of work:**
> - **The design pass in §5.2 never ran** — that agent was cut off before it produced anything. No
>   new artboards, no `docs/design/system.md`, no brand assets. The app follows ADR-055's existing
>   design system, which is why it looks coherent; it has not been through a designer.
> - **PWA icons are still the ADR-046 placeholders** and the wordmark is the ADR-055 glyph.
> - **The two QA waves in §5.1/§5.2 have not run**: no full keyboard pass, no axe sweep over the new
>   screens, no Playwright golden path.
> - **Google sign-in stays unavailable** until the founder creates an OAuth client (§6).
> - Known gaps recorded in ADR-059: the audit-log tenancy widening a firm acting on a client's books
>   will eventually need, and the single mid-request tenant-context switch that stands in for it.

This document is the single brief for the two delivery teams (frontend + design, backend). It
records what the product actually does today when a customer meets it, the gaps that stop a
demo, the API contract both teams build against, and the working rules. Requirement IDs refer to
[prd.md](../prd.md). Architectural rules are in [CLAUDE.md](../CLAUDE.md) and are not negotiable.

## 1. Verdict

The engine room is far ahead of the shop floor. Below the HTTP layer LEDGR is unusually strong:
row-level-security tenancy, a single authorization library with CI coverage checks, an append-only
ledger with structural invariants, hash-chained audit log, effective-dated VAT rules, RGS chart
seeding, document archive with retention, receipt capture with an encrypted offline queue,
invoicing with PDF rendering and delivery, and a customer master. Roughly 2,300 tests pass, 47
migrations have been executed on a real Postgres, and 57 ADRs record why things are the way they
are.

None of that is reachable by a customer. The funnel breaks at step three:

| Step | What a new customer does | What happens today |
|---|---|---|
| 1 | Opens the app | Login/marketing screen. Acceptable (ADR-057), not yet a brand. |
| 2 | Signs up, enrols MFA | Works. Creates a user, an organization and an Owner grant. |
| 3 | Expects to see their books | **Blank page with a header and a sign-out button.** No administration exists, no route can create one, and the authenticated shell renders nothing without a tenant context it is never given (`App.tsx`, `mobileContext` is never passed by `main.tsx`). |
| 4 | Captures a receipt, sends an invoice | Unreachable. The screens exist but only inside a mobile five-tab shell that never mounts. |
| 5 | Signs in with Google | Clean 503 "not available": no OAuth client is configured. Only the founder can create one (see §6). |
| 6 | Signs in with a passkey | Works once a passkey is enrolled, but enrolment is only reachable inside MFA enrolment. No settings screen to manage passkeys, sessions or password. |

Design: a real design system exists (ADR-055: tokens, light/dark, contrast-tested) and a four-
artboard canvas in `.design/` prescribes a desktop product (left rail, client header with KvK,
fiscal year, search, dashboard, ledger table, review stack, client switcher). Only the login
screen and the mobile home tab follow it. The authenticated web app has no desktop layout, no
router, no URLs, no customers screen, the template designer is not mounted, and there is no
settings area. The PWA icons are placeholders. Fonts load from Google, which ADR-055 itself flags
as a PRIV concern.

So the honest summary for an investor or a design partner: the product cannot be demoed today,
not because the hard parts are missing but because the easy parts that connect them are.

## 2. What exists and must be reused, not rebuilt

Backend (`apps/api/src/api/`): `auth/` (password, passkey, Google OIDC, TOTP, sessions, rate
limiting, recovery), `authz/` (the one library, matrix from PRD Appendix A, SoD, profiles, firm
staff), `audit/`, `ledger/` (posting, reversal, chart + RGS, fiscal years, integrity, periods),
`vat/`, `expenses/` (capture sessions, expense form, posting), `invoicing/` (create, lines, issue,
PDF, delivery), `customers/`, `templates/` (invoice designer), `documents/`, `dashboard/`, `firm/`
(switcher). Services for chart seeding (`ledger/chart.py`) and fiscal years (`ledger/fiscal.py`)
exist with no HTTP route.

Frontend (`apps/web/src/`): `auth/` (login, signup, MFA, Google callback, WebAuthn adapter),
`capture/` (camera, offline queue, expense form), `approve/`, `view/`, `invoicing/SendInvoiceForm`,
`templates/TemplateDesigner` (1,277 lines, unmounted), `client/` (switcher, header), `home/`
(dashboard), `theme/`, `MobileShell`. Packages: `@ledgr/design-tokens`, `@ledgr/i18n` (catalogue
with CI completeness check), `@ledgr/offline-queue`, `@ledgr/shared-types`.

Local stack (no Docker on this machine): native Postgres 17 on port 55432, Azurite, uvicorn on
8000, Vite on 5173 proxying `/v1`. See `scripts/dev-stack.ps1`. VAT and RGS reference data are
loaded. Google credentials are not configured.

## 3. Definition of "demoable" for this round

A design partner, unassisted, on desktop and on a phone, in Dutch or English:

1. Signs up (password or Google when configured), enrols TOTP or a passkey, verifies email.
2. Is taken through onboarding: company details and legal form (FR-ONB-004), fiscal year
   (FR-ONB-006), chart seeded from RGS (FR-ONB-005). A firm lands on an empty portfolio with
   "Add your first client" (FR-ONB-001b).
3. Lands on a dashboard that leads with what needs attention (FR-UX-005).
4. Captures a receipt by camera or upload, completes the expense, posts it; sees it in the ledger.
5. Creates a customer, creates an invoice, previews the PDF, sends it; sees receivables move.
6. Customises the invoice template.
7. Opens settings: profile, language, appearance, security (password, TOTP, passkeys, active
   sessions with revoke), organization.
8. Switches client (firm), signs out, signs back in with a passkey, and lands where they were.

Every screen has a URL. Every list has a teaching empty state (FR-UX-004). Every error says what
happened and what to do next (D5). Nothing is a dead end. Both languages complete.

## 4. API contract (backend builds, frontend consumes)

All new routes follow the existing conventions: registered via `register(app)`, a
`require_permission(...)` or an explicit exemption with a reason, an isolation test
(`@pytest.mark.isolation`), an audit category on mutations, idempotency on mutations, i18n error
keys in `packages/i18n/catalogue/errors.json` in both languages, decimals as strings.

### 4.1 Session and identity

**Session-backed tenant context (replaces the "restated JWT" limitation named in ADR-054).**
`TenantContextMiddleware` resolves the `sid` claim against `sessions` on every request (one
primary-key read) and takes `mfa_verified`, `active_administration_id`, revocation and expiry
from the row. A revoked session is refused on the next request. `PUT /v1/switcher/{id}` therefore
takes effect immediately without re-minting a token. Needs an ADR.

`GET /v1/me` — authorization-exempt with reason (returns only the caller's own memberships, the
same argument `/v1/switcher` makes).

```json
{
  "user": { "id": "uuid", "email": "a@b.nl", "language": "nl", "email_verified": true },
  "organization": { "id": "uuid", "name": "Van Doorn Bouw B.V.", "kind": "business", "kvk_number": "34281907" },
  "administrations": [
    { "id": "uuid", "legal_name": "…", "trade_name": null, "legal_form": "BV", "kvk_number": "…",
      "vat_number": null, "formatting_locale": "nl-NL", "colour": "indigo", "initials": "VD",
      "role": "Owner", "role_is_system": true,
      "fiscal_years": [ { "id": "uuid", "start_date": "2026-01-01", "end_date": "2026-12-31", "period_scheme": "monthly", "is_current": true } ] }
  ],
  "active_administration_id": "uuid-or-null",
  "mfa": { "has_totp": true, "has_passkey": false },
  "onboarding": { "needs_administration": false }
}
```

`kind` is `business` (Model B) or `firm` (Model A). For a firm, `administrations` are the client
administrations the caller holds a grant on (the switcher's own list).

### 4.2 Onboarding

`POST /v1/administrations` — creates an administration and everything it needs to be usable, in
one transaction. Model B: the organization's own administration. Model A: a client administration
via `app.create_firm_client_administration`, with the creating firm user granted access on it
(the founding-grant argument of ADR-054 applies to a new client administration; record the
decision in an ADR). Idempotent by key. Audit `CONFIGURATION`.

```json
{
  "legal_name": "Van Doorn Bouw B.V.", "trade_name": null, "legal_form": "BV",
  "kvk_number": "34281907", "vat_number": "NL001234567B01",
  "formatting_locale": "nl-NL",
  "fiscal_year": { "start_date": "2026-01-01", "end_date": "2026-12-31", "period_scheme": "monthly" }
}
```

Response: the administration entry as in `GET /v1/me`, plus `chart: { seeded: 62, rgs_version: "3.8-provisional" }`.
Side effects: chart seeded (`ChartService.seed`), fiscal year opened (`FiscalYearService.open_year`),
session's active administration set to the new one.

`GET /v1/fiscal-years/preview?start_date&end_date&period_scheme` — the period preview
(`FiscalYearService.preview`), no administration needed.
`GET /v1/administrations/{id}/fiscal-years`, `POST /v1/administrations/{id}/fiscal-years`.
`PATCH /v1/administrations/{id}` — legal name, trade name, VAT number, formatting locale.

### 4.3 Ledger reads (for the Grootboek screen)

`GET /v1/administrations/{id}/chart-of-accounts`
`GET /v1/administrations/{id}/trial-balance?fiscal_year_id=`
`GET /v1/administrations/{id}/journal-entries?fiscal_year_id=&cursor=&limit=` and
`GET /v1/administrations/{id}/journal-entries/{entry_id}` with lines. Amounts as strings.
Every read declares `audit=AuditCategory.FINANCIAL_READ` where the existing convention does.

### 4.4 Account and security settings

`GET /v1/me/sessions`, `DELETE /v1/me/sessions/{session_id}` (IAM-017),
`GET /v1/me/passkeys`, `DELETE /v1/me/passkeys/{passkey_id}` (with the IAM-010f continuity
guard: the last factor cannot be removed while it is the only one),
`POST /v1/me/password` `{ current_password, new_password }` (re-authenticates, breach-checked),
`POST /v1/auth/verify-email` `{ token }`, `POST /v1/auth/verify-email/resend`.

Email verification (IAM-010b, ADR-054 named gap): `users.email_verified_at`, a single-use token
in `auth_ceremony` (kind `email_verification`), sent through the existing `api.mail` sender. With
`email_provider=collecting` (local dev) the verification link is written to the API log and
exposed on `GET /v1/dev/outbox` **only when** `settings.expose_dev_outbox` is true, which is
never the default. Unverified accounts may complete onboarding but the client shows a persistent
banner; posting to the ledger requires verification (same dependency mechanism as
`require_ledger_posting_eligibility`).

### 4.5 Already exists, must keep working

Everything under `/v1/auth/*`, `/v1/switcher*`, `/v1/administrations/{id}/capture-sessions*`,
`/expenses*`, `/sales-invoices*`, `/customers*`, `/invoice-templates*`, `/documents*`,
`/dashboard`, `/v1/me/language`.

## 5. Team briefs

### 5.1 Backend team

Goal: every route in §4 exists, is tested (unit + isolation + one real end-to-end run against the
local stack), and the golden path in §3 can be driven start to finish over HTTP with curl.

Owners:
- **backend-onboarding** — §4.2, §4.3, `GET /v1/me`. New modules `api/onboarding/` and
  `api/account/` (routes only; reuse `ledger/chart.py`, `ledger/fiscal.py`, `firm/switcher.py`).
- **backend-auth** — §4.1 session-backed tenant context and revocation, §4.4 settings and email
  verification, Google sign-in wiring review, passkey flows exercised end to end with the
  `tests/auth/webauthn_helpers.py` ceremonies.
- **backend-qa** (after the two above) — drives the golden path with real HTTP against the running
  stack, records the transcript in `docs/qa/golden-path-2026-09-14.md`, fixes what breaks.

### 5.2 Frontend team

Goal: the product in §3, on the design system, at the quality of the design canvas.

Owners:
- **graphic-designer** — extends `.design/` with the screens the canvas lacks (onboarding,
  invoices list/detail, customers, settings, empty states, email-verification banner, the
  firm portfolio), produces brand assets (wordmark SVG, favicon, real PWA icons at 192/512 and
  maskable), self-hosted font files under `apps/web/public/fonts` with `@font-face` in
  `tokens.css` (closes the ADR-055 PRIV gap), and writes `docs/design/system.md`: spacing,
  type scale, component inventory, states, motion, and the rules a frontend engineer follows
  without asking. Design tokens changes go through `packages/design-tokens` and its tests.
- **frontend-lead** — routing (URL per screen, deep links, browser back), the desktop shell from
  `.design/Main.dc.html` (left rail, client header, fiscal-year selector, search, user menu)
  with the existing mobile tab bar below 64rem, onboarding wizard, `GET /v1/me` bootstrap,
  settings area, then the feature screens: dashboard (desktop), capture and review (restyle the
  existing components, do not rewrite them), invoices (list, new, detail with PDF, send),
  customers (list, create, edit; used by the invoice form), templates (mount `TemplateDesigner`),
  grootboek (trial balance, journal entries, chart), firm portfolio and client switcher.
- **frontend-qa** (after the above) — walks every screen in both languages, both themes, at
  1440 and 390 px, with keyboard only, and with axe; files and fixes what it finds; adds a
  Playwright golden-path test that runs against the local stack.

## 6. What only the founder can do

- Create a Google Cloud OAuth client (redirect URI `http://localhost:5173/`) and put
  `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REDIRECT_URI` in `.env`. Until then the
  Google button shows the "not available" message by design.
- Decide the production e-mail provider and SMTP host inside the EU (PRIV-010/011).
- Replace the provisional RGS and VAT datasets with the official publications before any filing.

## 7. Working rules for both teams

1. CLAUDE.md's four rules. Every new endpoint: isolation test, authorization declaration, audit
   category on mutations, idempotency key on mutations, decimals never floats.
2. No `git commit`. The lead integrates and commits at milestones with requirement IDs in the
   message. Do not run `git stash`, `git checkout --`, or anything that discards work.
3. Backend code lives in `apps/api`; frontend in `apps/web`, `packages/design-tokens`,
   `packages/i18n/catalogue` (frontend keys) and `packages/shared-types`. Backend adds only error
   keys to the catalogue. Both teams add keys in Dutch **and** English or CI fails
   (`scripts/check_translations.py`).
4. Any architectural decision gets an ADR in `docs/decisions/` in the same change. Check the next
   free number first: ADR-057 is the last one taken at the time of writing.
5. Run the checks before declaring done. Web: `corepack pnpm --filter @ledgr/web run typecheck`,
   `... run lint`, `... run test`. API: from `apps/api` with `$env:PYTHONPATH="$PWD\src"`:
   `.venv-check\Scripts\python.exe -m ruff format .`, `-m ruff check .`, `-m mypy src`,
   `-m pytest tests` (with `TENANT_ISOLATION_TESTS_ENABLED=1` and `DATABASE_URL` pointed at the
   local Postgres for the DB-backed suite). Full suite, not a subset — two checks need the whole
   tree collected.
6. `pnpm`, `uv`, `git`, `docker` are not on PATH. Use `corepack pnpm`, the `.venv-check`
   interpreter, and git at `C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\TeamFoundation\Team Explorer\Git\cmd\git.exe`.
7. The API runs with `--reload`; a saved file restarts it. Do not start a second API on 8000.
8. Reuse before rebuilding. If a component or service exists, restyle or extend it.
9. Plain language on the surface, the accounting term on demand (D2). One primary action per
   screen (D1). Empty states teach (FR-UX-004). Errors state the next step (D5).
