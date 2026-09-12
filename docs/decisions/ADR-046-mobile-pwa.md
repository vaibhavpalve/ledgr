# ADR-046: The P0 mobile experience — one installable PWA shell, not a second app

- **Status**: Accepted
- **Date**: 2026-09-12
- **Implements**: MOB-002 (receipt/invoice capture via installable PWA), MOB-004 (approve/reject
  purchase invoices and expenses; document viewable at full resolution), MOB-005 (create and send a
  sales invoice from the phone, using the administration's template)
- **Serves**: §5.2 (release phases — the PWA-in-P0 consequence), §7.4 (mobile is not a full clone
  of the web app), IAM-005 (tenant-isolation test on every new endpoint), NFR-032 (idempotency),
  FR-LOC-001/FR-UX-007 (every string translated)
- **Constrained by**: ADR-012 (no invented permissions — Appendix A only), CLAUDE.md rule 1 (tenant
  context on every request), CLAUDE.md rule 3 (the server is the actual authorization control)
- **Related**: [ADR-031](ADR-031-receipt-capture.md) and [ADR-036](ADR-036-capture-screens.md) —
  the capture mechanism reused unmodified here; [ADR-037](ADR-037-sales-invoices.md) — the
  create/issue/send sequence this task's Send-invoice tab drives; [ADR-038](ADR-038-customer-master.md)
  — why a Viewer-style "list customers" capability does not exist, which is why the customer
  picker below is deferred rather than built against nothing

## Context

    §5.2  "Consequence to accept deliberately: camera capture in P0 means a mobile client
          exists in P0. The cheapest route is an installable PWA with camera access, with the
          native apps following in P2."
    §7.4  "Mobile is deliberately not a full clone of the web app. It covers capture, approve,
          view and invoice — the tasks people do away from a desk."
    MOB-002  Receipt and invoice capture ... available in P0 via installable PWA; native camera
             path in P2. M (P0)
    MOB-004  Approve or reject purchase invoices and expenses, with the document viewable at
             full resolution.
    MOB-005  Create and send a sales invoice from the phone, using the administration's
             template. Template editing is web-only; the phone renders and sends.

`apps/web` already has the hard part of MOB-002 built: `CaptureScreen`, `CaptureQueueStatus`,
`ExpenseForm`, `@ledgr/offline-queue`, the quality/decode/crypto/encoding modules (ADR-031,
ADR-035, ADR-036). What it does not have is anywhere to put a person once they open the app on a
phone — `App.tsx`'s authenticated branch is a bare header, and there is no installable manifest, no
service worker, and no surface for MOB-004's approve/view or MOB-005's send-invoice at all.

Confirmed before writing anything: `apps/web` has no dependency on `react-router` or
`vite-plugin-pwa` (checked `package.json`); no PWA manifest, icons or service worker exist anywhere
in the repo; `App.tsx`'s `Shell` has no navigation to speak of. `api.expenses.model.ExpenseStatus`'s
own docstring says outright that "`READY` is as far as capture goes. FR-EXP-002's approval workflow
... [is] not built", and §5.2's phase table places purchase-invoice approval routing (FR-AP-006/007)
in P1.

## Decision

### 1. One app, extended — not a second package

`apps/web` becomes installable and gains a mobile-scoped navigation shell; the existing capture
flow is reused **unchanged**. No new app package, no router, no PWA build plugin.

§7.4's "not a full clone of the web app" is read literally: the mobile shell exposes a deliberately
narrow four-section surface (Capture / Approve / View / Send invoice) rather than trying to
replicate a fuller desktop app that, today, does not exist to clone — `App.tsx`'s authenticated
branch currently renders nothing but a header. Building a second package would duplicate
`@ledgr/offline-queue`'s already-hardened integration, the capture quality/crypto/encoding modules,
and the i18n wiring, for no benefit; a fourth dependency (a router) is not needed to switch between
four `useState`-held tabs.

`MobileShell.tsx` is a plain state-based tab switcher: `useState<"capture" | "approve" | "view" |
"invoice">`, a bottom tab bar (the mobile convention), four sections, each unmounted while another
is active. `App.tsx`'s `Shell` renders it inside the authenticated branch, behind a new optional
`mobileContext: SittingContext` prop — the identical seam `useSitting` already uses (`SittingContext`
is "supplied by whatever knows the session ... a prop, because there is no sign-in flow and no
active-client endpoint wired"). Left `undefined`, the authenticated branch renders exactly what it
renders today: the bare header. That is not a regression this task introduces; it is the
pre-existing gap ADR-036 already recorded, now shared by the whole mobile shell rather than only by
capture.

### 2. "Approve" means the existing draft→ready review, surfaced as a revisitable list

Confirmed by reading `api.expenses.model.ExpenseStatus`'s own docstring and §5.2's phase table:
FR-EXP-002's approval workflow and FR-AP-006/007's purchase-invoice approval routing are P1 and not
built. Building real approval routing now would be scope creep into deferred work.

For this task, "approve" is: a list of the administration's `DRAFT` expenses — captured but not yet
reviewed or completed — that a person can tap into, complete, and mark `READY` via the **existing**
`ExpenseForm`/`mark_expense_ready` flow, reachable and revisitable rather than only reachable
mid-capture-session. `ApproveList.tsx` fetches the list, hands a tapped item's id to a small loader
that calls the existing `GET .../expenses/{id}` and renders the unmodified `ExpenseForm`; the list
re-fetches once an item leaves `draft`, so a completed item drops off on its own.

### 3. Two new list endpoints, reusing existing Appendix A permissions

No list-by-status endpoint existed for expenses, and no list endpoint existed for sales invoices
(confirmed by reading `api.expenses.routes` and `api.invoicing.routes`). Both are added:

| Endpoint | Permission reused | Mirrors |
|---|---|---|
| `GET /v1/administrations/{administration_id}/expenses?status=draft` | `submit expense` — identical to `get_expense`/`update_expense` | Filling in and reviewing the form IS submitting the expense (ADR-012: no invented "view own submissions" capability) |
| `GET /v1/administrations/{administration_id}/sales-invoices` | `create sales_invoice` — identical to `get_invoice`/`create_invoice`/`set_invoice_lines` | Reading back what this administration may create is not a separate capability |

Both take a bounded `limit` (`Query(default=50, ge=1, le=200)`) with **no cursor pagination** —
the same shape `api.customers.routes.list_customers` already uses, the one other list endpoint this
codebase has. Nothing here paginates yet; a cursor is a follow-up if a list grows past what one
bounded page can hold.

Each returns a **lighter** shape than the single-resource view (`_expense_summary_json`,
`_invoice_summary_json`): no suggested category, no FR-EXP-001g duplicate warnings, no VAT
breakdown, no FR-AR-003 statutory-failure check. Those are per-item reads a list screen does not
need until somebody opens a specific one — computing them for every row of a bounded list would be
a query fan-out (duplicate-candidate lookup, VAT-rate resolution) proportional to `limit`.

The sales-invoice list includes **drafts**, so a half-finished mobile-created invoice is resumable
rather than disappearing until issued — deliberately not filtered to `issued` only, unlike the
expense list, which (via the client's own filtering — see §5 below) excludes `draft` because those
belong to the Approve tab.

Both endpoints ship with a tenant-isolation test (`@pytest.mark.isolation(...)`), which
`tests/test_isolation_coverage.py` enforces structurally at collection time — it fails CI even
without a live Postgres available if a registered route carries no such marker.

### 4. Send-invoice: typed customer entry, the existing four-call sequence, per-field inline errors

MOB-005 is invoice **creation and sending**, not template editing — `TemplateDesigner.tsx` stays
untouched. `SendInvoiceForm.tsx` collects customer details (name, address, country, VAT number,
typed — no `customer_id`), invoice date, and one or more lines (description, quantity, unit price,
discount, VAT treatment — reusing `VAT_TREATMENTS` from `@ledgr/shared-types`, exactly as
`ExpenseForm` already does), then calls the **existing** backend, unmodified, in order:

    POST .../sales-invoices          (create draft)
    PUT  .../sales-invoices/{id}/lines
    POST .../sales-invoices/{id}/issue
    POST .../sales-invoices/{id}/send

A created draft's id is remembered across a retry, so resubmitting after a later-step failure edits
the same draft rather than minting a second one. Two distinct 422 shapes surface at their own
control, never a generic banner — the same "server-validated field, shown at its own control"
discipline `TemplateDesigner`'s violations and `ExpenseForm`'s duplicate warnings already follow:

- `CustomerDetailsMissing`/`CustomerDetailsConflict` (from `create`) carry `missing_fields` as bare
  field-name strings with one combined sentence — shown at the customer section.
- `NotStatutoryCompliant` (from `issue`, FR-AR-003's gate) carries the same `{field, line_position,
  message}` shape `GET .../sales-invoices/{id}`'s own `statutory_failures` already uses — each
  placed at the field or line the server named; anything this form has no specific control for
  (e.g. a `supplier_*` failure) falls into an "other problems" list, mirroring `TemplateDesigner`'s
  own fallback for the same reason: the server is the authority (CLAUDE.md rule 3), so an
  un-rendered violation still has to be shown.

**Typed entry, not a customer picker.** `CustomerDetailsMissing`'s own docstring describes exactly
this one-off path, and it is the documented MVP choice for this task — a picker is a nice-to-have
deferred below (see Known gaps and ADR-038, which records that no "view customers" capability
exists to build a cheap picker against).

### 5. View: two lists, document viewing via fetch-and-blob, never a direct `src`

`ViewList.tsx` shows posted/ready expenses (fetched via the same `listExpenses` call the Approve
tab uses, with `status="draft"` rows filtered out client-side — one call already returns every
status, and there are at most two states to keep) and issued-plus-draft sales invoices, each
tappable to a minimal detail.

Where a document exists (today: a sales invoice's own `document_id`, already on its summary row),
MOB-004's "document viewable at full resolution" is served by `fetch()` + `Blob` +
`URL.createObjectURL` against `GET .../documents/{id}/content` — the exact technique
`TemplateDesigner`'s logo preview and `TemplateApi.fetchTemplateAssetBlob` already establish, never
a direct `<iframe src="/v1/...">`/`<img src="/v1/...">` at the download endpoint. That endpoint sets
`Content-Disposition: attachment` specifically so nothing navigates to it directly (SEC-005); a
client-rendered blob URL is what respects that.

## Alternatives considered

| Option | Rejected because |
|---|---|
| A separate mobile app package (`apps/mobile-web` or similar) | Duplicates `@ledgr/offline-queue`'s hardened integration, the capture quality/crypto/encoding modules, and the i18n wiring, for no benefit — `apps/web` already has everything MOB-002 needs. |
| Introduce `react-router` for the four tabs | Four sections switched by `useState` need no router; adding one is a new dependency and a new class of test (route matching, history) for a switcher this simple. |
| Introduce `vite-plugin-pwa` | Not present in `node_modules`; no internet access to install it. A hand-rolled manifest + a narrow app-shell service worker is small and sufficient for installability. |
| Build FR-EXP-002/FR-AP-006's real multi-actor approval workflow now | §5.2 places it in P1. Building it now is scope creep the PRD's own phase table deliberately defers. |
| A customer-picker in the Send-invoice tab | ADR-038 records that no "view customers" capability/endpoint exists to build one against cheaply; typed entry alone satisfies MOB-005 and is the simpler, correct P0 shape. |
| A single merged Approve+View list | An expense and a sales invoice are different documents with different detail views and different status vocabularies; merging them needs a synthetic sort key neither backend defines. |
| Compute full `ExpenseView`/`InvoiceView` (with duplicate warnings / VAT breakdown / statutory check) for every list row | A query fan-out proportional to `limit`, for information a list row does not render — paid only once an item is actually opened, via the existing single-resource endpoints. |
| Cursor-based pagination for the two new list endpoints | Nothing in this codebase paginates yet (`list_customers` is the one precedent, and it is bounded-`limit`-only). A cursor is a real design (opaque token, stability under concurrent writes) not worth inventing for a P0 mobile list. |
| A direct `<img src="/v1/.../content">` for document viewing | SEC-005's `Content-Disposition: attachment` exists precisely to discourage direct navigation/rendering; `fetch()` + `Blob` + `URL.createObjectURL` is the technique this codebase already established for the same reason in `TemplateDesigner`. |
| Real-time push for approval/queue state | Out of scope for a P0 mobile shell; the existing list endpoints are pull-based like everything else in this codebase. |

## Consequences

**Easier.** MOB-001's native apps (P2) reuse the same list endpoints, the same
`ExpenseSummaryView`/`SalesInvoiceSummaryView`/`StatutoryFailureView` shapes (added to
`@ledgr/shared-types` field-for-field against the routes' own JSON), and the same permission
reuse — there is nothing mobile-specific to re-derive server-side. A future customer picker is an
additive change to `SendInvoiceForm`'s customer section once `api.customers` grows a cheap list
capability (ADR-038's own noted gap), not a rework of the create/issue/send sequence.

**Harder.** The mobile shell now has two composition roots to keep straight — `MobileShell.tsx`
(pure UI, receives everything as props, thoroughly tested with in-memory fakes) and
`AuthenticatedMobileShell` in `App.tsx` (the real wiring: `captureQueue()`, `browserDecode()`,
`CaptureApi`, `SalesInvoiceApi`). Anything that needs to reach production has to be wired in the
latter, which is a step both `CaptureScreen`'s and `TemplateDesigner`'s own bootstrap already skip
today, for the same reason: there is no session flow to hand a real context to yet.

**Known gaps, each with a trigger.**

- **The manifest icons are placeholders.** `apps/web/public/icons/icon-{192,512}.png` and
  `icon-512-maskable.png` are flat indigo squares generated by a small hand-rolled PNG encoder
  (stdlib `zlib` only — no image library is vendored for a frontend asset and there is no internet
  access to install one), *not* real brand assets. The fix is a design pass producing an actual
  monogram or wordmark icon; nothing about the manifest's shape needs to change when that lands.
- **No cursor pagination.** Both new list endpoints return at most `limit` (default 50, max 200)
  rows with no way to page further. Acceptable today because nothing else in this codebase
  paginates either; becomes a real gap the day an administration's draft-expense or invoice list
  exceeds 200 rows in practice.
- **No customer picker.** `SendInvoiceForm` only accepts typed-in customer details. A person
  invoicing a repeat customer from the phone re-types their address every time. Deferred rather
  than built against a "list customers" capability that (per ADR-038) does not exist yet.
- **Expense documents are not viewable from the View tab.** A sales invoice's own PDF is (its
  `document_id` already rides on `SalesInvoiceSummaryView`); an expense's captured receipt image is
  not, because no endpoint today links an expense to its capture pages' document ids — adding one
  would touch `ExpenseFormService._view_of` and, with it, every existing test's fake repository.
  `ViewList`'s expense rows open to an explicit "no document available" state rather than a crash or
  a silently wrong link.
- **MOB-002's own explicit P2 boundary is unchanged by this task.** Auto edge detection and deskew
  remain unbuilt (ADR-036 §3 recorded this already); the native camera path stays P2. This task
  reuses `CaptureScreen` exactly as it stands and adds nothing to its image-processing surface.
- **No session/active-administration wiring.** `MobileShell` needs `administrationId`/
  `fiscalYearId`/tenant context from *somewhere*, and there is still no sign-in flow
  (`auth/SignInPending`) or active-client endpoint to supply it — the same gap ADR-036 recorded for
  `SittingContext`. `App.tsx`'s `mobileContext` prop is the seam a real session will fill; until
  then, `authenticated: true` with no context renders today's bare header, unchanged.
- **The service worker caches only the app shell.** It is not a second offline-data story:
  `@ledgr/offline-queue` already owns captured-document persistence and retry. `sw.js` passes every
  `/v1/...` request straight to the network — a stale cached API response would be actively wrong
  for financial data — so it buys "the installed app opens with no network," nothing more.
