# ADR-059: The onboarding transaction, and the grant a firm's creator holds on a new client

- **Status**: Accepted
- **Date**: 2026-09-14

## Context

The founder review (docs/founder-review-2026-09-14.md §1) found the funnel broken at step three:
a signed-up user has an organization and an Owner grant, and nothing else — no administration, no
route that can create one. `ChartOfAccountsService.seed` (FR-ONB-005) and `FiscalYearService.
open_year` (FR-ONB-006) existed with no HTTP surface. §4.2 of the brief asks for one
`POST /v1/administrations` that leaves a fresh signup with an administration, a seeded chart, an
open fiscal year, and the session pointed at it — for a self-managed business (Model B,
FR-ONB-004) and for a firm creating a client with no account of its own (Model A, FR-MDL-004).

Building it exposed five questions the existing rules did not answer on their own:

1. Who grants the **creating firm user** access to a client administration that, a moment ago,
   did not exist? IAM-063 forbids self-grants; `FirmStaffAccessService` (IAM-107) authorizes at the
   firm and bounds by the engagement, but its self-grant check refuses the granter as subject.
2. `ChartOfAccountsService` and `FiscalYearService` file their audit entries under the
   administration's **owning** organization (the client), while `audit_log_insert` (migration 0019)
   admits a row only for the session's own tenant (the firm). Seeding a client's chart from a firm
   session was therefore impossible through the services — nothing had ever tried.
3. `EngagementRevocationService.switch_to` — the one sanctioned writer of
   `sessions.active_administration_id` — only offers administrations the caller holds an
   **administration-scoped** grant on. A Model B Owner holds an organization-scoped grant and so
   could never be switched into their own books; `PUT /v1/switcher/{id}` answered 404 for them.
4. `FiscalYearService.years()` requires "Year-end close" (Owner/Accountant only), but `GET /v1/me`,
   the trial balance and every year selector need the list for a Bookkeeper and a Viewer too.
5. Appendix A has no row for "edit an administration's details" (PATCH) and no row for "read the
   journal" (§4.3); ADR-012 forbids inventing permissions outside the matrix.
6. Nothing had ever created an administration through the application, so nothing had ever
   provisioned its SEC-022 data-encryption key outside the test fixtures — and the two account
   models need it provisioned in two different places.

## Decision

### One transaction, four writes

`POST /v1/administrations` runs the administration row, `ChartOfAccountsService.seed`,
`FiscalYearService.open_year` and `EngagementRevocationService.switch_to` on one request-scoped
session (`api.db.get_db_session`), so they commit together or not at all. An administration with
no chart is one nothing can be posted into and one with no year is one no posting can be dated
in; a half-created administration is not a lesser one, it is a support ticket. The legal form is
canonicalised through `app.canonical_legal_form` (the same `legal_form_alias` lookup the seed
performs, migration 0024) **before** anything is written, so an unplaceable spelling is a 422
naming the accepted forms rather than a database error three statements in. Nothing is
re-implemented: the row is a plain INSERT (Model B, `administration_insert_own`) or
`app.create_firm_client_administration` (Model A); the chart, the year and the switch are the
existing services. A request carrying no session identifier is not switched and is not refused —
the administration was asked for and exists; `GET /v1/me` reports `active_administration_id`
honestly either way.

### The creating firm user receives a founding Accountant grant, inserted directly

For Model A, after `app.create_firm_client_administration` returns, the route inserts one
`role_assignment` row — Accountant, administration-scoped, on the new client — for the creating
user, in the same transaction, under the firm's tenant context. This is ADR-054's founding-Owner
reasoning applied to a client administration: nobody else could have granted it, because until
this moment nobody holds anything on it. It is deliberately **not** routed through
`FirmStaffAccessService.grant_access`: that path's IAM-063 self-grant check exists to stop a
Firm Manager elevating themselves inside an existing power structure, and teaching it a
"founding" carve-out would hand every later caller an exception that legitimately fires once per
client's lifetime. A narrow, separately reviewable insert keeps the shared library free of it.

The grant is a **firm staff grant** in every respect that matters to the client: it runs under
the firm's context so `role_assignment_firm_staff_guard_trg` (0016) records
`granted_by_organization_id` = the firm and verifies the engagement the bootstrap function just
created (IAM-107); it appears in IAM-109's register; the client can revoke it, and IAM-110's
revocation clears it like any other. Accountant, because seeding a chart and opening a year *are*
bookkeeping — "Maintain the chart of accounts" and "Year-end close" are its Appendix A
capabilities — and because §8.4's "cannot post to a client's ledger without also holding
Accountant on it" is satisfied by exactly this shape: an explicit, visible, administration-scoped
grant. A Firm Manager who creates a client and does not want to keep the books hands the grant
to a colleague and revokes their own through the existing IAM-107 service.

### Seeding and year-opening run under the new client's tenant context

Between the founding grant and the switch, the route sets `app.current_org_id` to the **client**
organization it just created (transaction-local `set_config`, exactly as `SignupService` does the
moment a self-managed organization exists), records the founding grant as a
`PERMISSION_CHANGE` event in the client's log, seeds the chart and opens the year, then restores
the firm's context. This is the only way the services' audit entries — which belong in the
client's log, because "my accountant seeded my chart" is what IAM-094 lets the client read —
can pass `audit_log_insert`. It is a bootstrap of a new tenant performed by its founding firm,
not a widening of what the firm may see: every read inside the window is of rows the engagement
already admits, and the context is reset in a `finally` before anything else runs.

The general form of this problem — a firm acting on a client's books files audit entries the
firm's session cannot insert — is real and older than this change; only seeding has never been
reached from a firm session before. Widening `audit_log_insert` to `has_administration_access`
would be the systemic fix and is a migration for a separate change, not this one.

### The switcher includes administrations reached by an organization-scoped grant

`_SWITCHER_SQL` (`api.authz.firm_access_repository`) and `SqlSwitcherRepository`'s base query now
join a grant to an administration two ways: directly (administration-scoped) or through
`administration.organization_id` (organization-scoped, the administrations that organization
**owns**). That is ADR-011's `_scope_covers` cascade, applied to the list rather than only to the
decision — a Model B Owner sees their own books in the switcher with the role "Owner", which is
what `GET /v1/me`'s contract already shows. The join keys on ownership, never on
`firm_engagement`, so a firm's organization grant still reaches no client. Where a user holds
both kinds of grant on one administration, the administration-scoped one wins the `DISTINCT ON`.

### `FiscalYearService.visible_years` and `build_fiscal_year_service`

A read-level listing gated by the baseline `view administration` every role holds, beside the
unchanged `years()`; and a builder mirroring `build_chart_service`, so routes obtain the service
without naming `api.ledger.fiscal_repository` (tests/ledger/test_bounded_context.py).

### Permissions for the new routes, all from the matrix

| Route | Permission | Why |
|---|---|---|
| `POST /v1/administrations` | `manage administration` at the organization | Appendix A's "Create/delete administrations"; the Firm Manager holds it (§8.4 "client onboarding"). |
| `PATCH /v1/administrations/{id}` | `manage administration` at the organization | The closest row for an administration's identity fields, and it must be requested **organization-scoped like the POST**, not against the administration in the path: `AuthorizationService` refuses an organization-level capability aimed at a single administration outright (`resource_scope_mismatch`) rather than promoting the target to its owner, which IAM-032 forbids. Asking per-administration therefore denied *every* caller, the owner of the books included. Which row the request may actually touch is RLS's answer — `administration_update` (0001) admits the owning organization or a firm with an active engagement — not this check's. |
| `GET/POST .../fiscal-years` | `view administration` / `close fiscal_year` | What the service evaluates. |
| `GET .../chart-of-accounts` | `view chart_of_accounts` | The row's own name. |
| `GET .../trial-balance`, `.../journal-entries[/{id}]` | `view report` | Same choice `api.dashboard.routes` made: there is no "read the journal" row, and these are reports — read-only, asserting nothing. |
| `GET /v1/me`, `GET /v1/fiscal-years/preview` | exempt, with reasons in `AUTHORIZATION_EXEMPT_PATHS` | The caller's own memberships (the `/v1/switcher` argument); arithmetic on two dates naming no administration. |

Every mutating route declares `CONFIGURATION`; every ledger read declares `FINANCIAL_READ`.

### Every administration gets its data-encryption key in the same transaction

SEC-022/ADR-004 gives each administration one DEK, wrapped under the KMS-held KEK. Nothing had
ever created an administration through the application before this change, so nothing had ever
had to decide *when* that key appears — and both branches got it wrong on the first pass.

- **Model A.** Migration 0002 dropped 0001's five-argument
  `app.create_firm_client_administration` and replaced it with an eight-argument one taking the
  wrapped key material, precisely so the key row is inserted in the same statement as the
  administration. The route was calling the old signature, so *every* firm-side creation failed
  with `function ... does not exist`. It now generates the DEK through
  `api.crypto.envelope.generate_wrapped_dek` (new, public, beside `_DEK_LENGTH`, so the DEK's
  length stays decided in one place) and passes all eight.
- **Model B.** A plain `INSERT INTO administration` creates no key at all, and nothing else would
  have: `EnvelopeEncryptionService.encrypt` raises "has no active encryption key" on first use, so
  a self-managed business would have onboarded successfully and then been unable to store a single
  receipt or template asset. The route now calls `provision_key` in the same transaction.

The rule, stated once: an administration and its key are created together or neither is. A
lazily-provisioned key would mean a window in which an administration exists that cannot hold a
document, and the failure would surface at upload time, far from its cause.

### Two smaller shapes

`PostedLine` gained optional `account_code`/`account_name`, joined in the ledger repository, so an
entry's detail read needs no second call for the chart. The journal list is keyset-paginated on
`(posted_at, id)` via an opaque `EntryCursor`: an offset over an append-only table drifts the
moment a posting lands between two pages.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Grant the firm creator through `FirmStaffAccessService.grant_access` with the SoD service omitted | Sidesteps IAM-063 by constructor argument rather than by a visible, reasoned exception; the next caller would copy it. |
| Widen `audit_log_insert` in this change so a firm session can file entries under a client | The right systemic fix, but a migration touching the hash-chained audit log's tenancy policy deserves its own review; the transaction-local context switch is the same move `SignupService` already makes and stays inside one function. |
| Give a Model B Owner an extra administration-scoped grant so `switch_to` finds them | A redundant row per administration that IAM-032's immutability rules would then have to be revoked and re-granted around; the cascade already exists in the decision path, the switcher was simply not reading it. |
| Route `GET /v1/me`'s year list through `years()` and catch the denial | Every non-Owner would silently see no years, and each request would leave a DENIED audit entry about a person who did nothing wrong. |
| Offset pagination for the journal | Skips or repeats an entry whenever a posting lands between two pages — on a table whose whole point is that postings keep landing. |
| Make the request's fiscal year mandatory | FR-ONB-006's default — the calendar year, monthly — is what nearly every Dutch SMB has; the exceptional year is the one worth stating. |

## Consequences

- A fresh signup reaches a usable administration in one call; the golden path (brief §3 step 2) is
  reachable over HTTP for both account models.
- `GET /v1/switcher` and `/v1/switcher/search` now list a business's own administrations for its
  organization-scoped users. Nothing previously depended on them being absent; the isolation
  tests for both routes still pass because the join still keys on the caller's own grants.
- A client administration created by a firm always starts with exactly one firm staff grant —
  the creator's — visible in IAM-109's register and revocable by the client. That is a feature of
  the shape, and the client's audit log shows it as such.
- `PATCH /v1/administrations/{id}` is reachable by a firm's staff for a client they hold an active
  engagement on, because the permission is evaluated at the caller's own organization and the row
  is bounded by RLS. That is the same division of labour the administration listing already uses,
  and it is what FR-MDL-004 implies — a firm that creates a client administration on the client's
  behalf can correct the legal name it typed. A firm without an engagement reaches nothing.
- `POST /v1/administrations` is the first route to change `app.current_org_id` mid-request. It is
  bounded (one function, `try/finally`) and documented at the call site; a second such site should
  prompt the `audit_log_insert` widening rather than a third.
- `GET /v1/me`'s `email_verified` reads `users.email_verified_at` (migration 0049, ADR-060), wired
  by backend-auth in the same working day; the route, the DB-backed suite (`seed_user`) and the
  running API all require that migration to be applied.
