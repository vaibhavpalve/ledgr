# ADR-048: The mobile Home tab — a prioritised dashboard, honestly labelled

- **Status**: Accepted
- **Date**: 2026-09-12
- **Implements**: FR-UX-005 (the home screen is a prioritised list of what needs the user's
  attention, not a menu of everything the product can do), MOB-006 (dashboard: cash position,
  receivables, VAT estimate, items needing attention)
- **Serves**: FR-UX-004 (empty states teach), NFR-031 (Decimal everywhere in the money path),
  IAM-005 (tenant-isolation test on every new endpoint), CLAUDE.md rule 3 (one authorization
  library, used everywhere), FR-LOC-001/FR-UX-007 (every string translated)
- **Constrained by**: ADR-012 (no invented permissions — Appendix A only), CLAUDE.md rule 1 (the
  ledger is a separate bounded context with a narrow API)
- **Related**: [ADR-046](ADR-046-mobile-pwa.md) — the four-tab shell this task adds a fifth
  section to and whose default tab it replaces; [ADR-047](ADR-047-one-tap-capture.md) — the
  Capture tab's own auto-start behaviour, unaffected by no longer being the default

## Context

    FR-UX-005  The home screen is a prioritised list of what needs the user's attention, not a
               menu of everything the product can do.
    MOB-006    Dashboard: cash position, receivables, VAT estimate, items needing attention.

ADR-046 built `MobileShell.tsx` as a four-tab switcher (Capture / Approve / View / Send invoice),
opening on Capture because MOB-002's camera capture was the P0 priority at the time. That shell is
task-shaped by design (§7.4: "not a full clone of the web app"), and Home is a genuinely different
thing from any of its four tabs: a summary a person reads before deciding which task to do, not a
fifth task.

Two of the four figures MOB-006 asks for depend on backend capability that is only partially
built. Confirmed before writing anything:

- FR-BNK (bank feeds/reconciliation) is P1 and not built — there is no `bank_transaction` table,
  no reconciliation endpoint, and no payment/settlement concept anywhere against `sales_invoice`
  (checked `api.invoicing.model`/`.repository`: the columns are `status`, `due_date`,
  `credits_invoice_id` and nothing that records a payment received).
- FR-VAT-001's full return-preparation engine (rubrieken mapping, pre-filing validation) is P1 and
  not built — `api.vat.rules` (checked in full) is CMP-014's effective-dated rate/rubriek
  *reference data* reader, not a return calculator; there is no "current period net VAT position"
  helper anywhere to reuse.

Neither gap makes `cash_position` or `vat_estimate` unbuildable. FR-GL (the ledger engine) is
complete, so a GL-based cash figure and an invoices-minus-expenses VAT estimate are both real,
computable numbers today — they are simply not the live-bank-synced figure and the filing-ready
figure MOB-006's final, fuller form will eventually mean. This task computes both for real, from
real posted data, and says exactly what each excludes.

## Decision

### 1. Home becomes a fifth section and the new default tab

`MobileTab` gains a `"home"` member; `MobileShell`'s `useState<MobileTab>` initialises to
`"home"` instead of `"capture"`. The bottom tab bar gains a fifth button, first in the list. This
is the one change to `MobileShell.tsx` outside adding the tab itself — CaptureScreen, ApproveList,
ViewList and SendInvoiceForm are unmodified, mounted exactly as ADR-046 left them.

`HomeScreen` receives an `onNavigate: (tab: MobileTab) => void` prop — in practice `setTab` passed
straight through — so tapping a prioritised item can switch to whichever of the four task tabs is
relevant, without `HomeScreen` needing to know `MobileShell`'s internals beyond the `MobileTab`
type.

### 2. Each figure, defined exactly and honestly

**`cash_position`** — the summed balance of the administration's liquid-means GL accounts, read
from `LedgerService.trial_balance()`. "Liquid means" is identified by RGS code prefix `BLim`
("Liquide middelen"), verified against this codebase's own seed data
(`apps/api/data/rgs/rgs-3.8-mkb.json`) rather than assumed — the profile's postable leaves are
`BLimKas` (1000, Kas), `BLimBan` (1100, Bank) and `BLimKrp` (2000, Kruisposten); matched by prefix
so a fourth `BLim*` leaf a future RGS version or a customer's own chart extension adds is picked
up without a code change. `TrialBalanceRow` carries no `rgs_code` (RGS mapping belongs to the
chart-of-accounts bounded context, not the ledger's posting reports), so `api.dashboard.model`
joins trial-balance rows against `ChartOfAccountsService.chart()` on `account_id` in application
code.

Labelled in the UI (`mobile.home.cash_position_caption`) as reflecting what has been **posted** to
the books — manually entered bank/cash movements — not a live bank sync. This is not a
placeholder for FR-BNK; it is the correct, permanent meaning of a GL-based cash figure, and it
will still be correct once bank feeds exist (they will simply make the postings feeding it more
current).

**`receivables`** — the AR control account's current subledger balance total, across every party,
via `LedgerService.subledger_balance(control_kind=ACCOUNTS_RECEIVABLE)`. Not fiscal-year-scoped:
`ledger.subledger_balance()` takes no fiscal year parameter, and a receivable does not become
uncollectable at a calendar boundary.

**`vat_estimate`** — output VAT recorded on issued invoices minus input VAT recorded on posted
expenses, for the fiscal year to date (`fiscal_year.start_date` through `min(today,
fiscal_year.end_date)`). Output VAT sums `sales_invoice_vat_total.vat_amount` (frozen at issue,
ADR-037) for issued invoices in range; a credit note's own totals are negative (its lines carry
negative quantities), so the SUM already nets a credited sale's VAT back out with no special
case. Input VAT sums `expense.vat_amount` for **POSTED** expenses in range — not `ready`, because
`cash_position` promises "reflects what has been posted" and using a different population for VAT
would make the two figures disagree about what "in the books" means.

Fiscal-year-to-date rather than the current VAT period specifically: it is simplest, defensible
for a first version, and the endpoint already takes `fiscal_year_id` (the same shape
`ledger.trial_balance()` takes it) rather than inventing a "which VAT period is current" concept
this codebase does not otherwise have. Labelled in the UI (`mobile.home.vat_estimate_caption`) as
a rough estimate, excluding anything FR-VAT-002's pre-filing validation would catch — unposted
documents, unreconciled items — because that engine does not exist. The date range is shown
plainly (`mobile.home.vat_estimate_period`, "1 Jan – today") rather than left implicit.

**`items_needing_action`** — FR-UX-005's prioritised list, built by a pure function
(`api.dashboard.model.build_action_items`) from three sources, in this order:

1. **Overdue issued invoices** — most urgent, sorted **most-overdue-first**. "Overdue" is FR-BNK's
   honest definition, not an invented one: an issued invoice past its `due_date` and not credited.
   No payment/settlement concept exists to check instead (see Context above), and a credit note is
   the only way an issued invoice's claim is withdrawn today, so a credited invoice is excluded
   rather than left looking permanently overdue. A credit note itself is never flagged, whatever
   its own `due_date` happens to be — it carries no due obligation of its own.
2. **Draft expenses** awaiting completion, in the order `SqlCaptureRepository.list_by_status`
   already returns them (newest-first) — not re-sorted.
3. **Draft sales invoices** not yet issued, same repository ordering as (2).

Tested directly and specifically: `test_a_more_overdue_invoice_ranks_above_a_less_overdue_one`
proves the within-group ordering: `test_overdue_invoices_rank_before_draft_expenses_and_invoices`
proves the cross-group ordering (`apps/api/tests/dashboard/test_model.py`).

Each item carries `kind` (`overdue_invoice` / `draft_expense` / `draft_invoice`), `id`, and a
`description` built server-side from the item's own facts via the shared message catalogue —
`"Invoice INV-2024-003 to Jansen BV, 12 days overdue"`, not `"1 overdue invoice"` — the same D5
discipline `_view_json`'s `statutory_failures` already follow via `describe()`. Translation, not
the pure aggregator, is where the description is built (`api.dashboard.routes._action_item_json`),
so `api.dashboard.model` stays free of language and is unit-testable without a catalogue.

### 3. One aggregation endpoint, `GET /v1/administrations/{id}/dashboard?fiscal_year_id=...`

Reuses **"View reports"** (`("view", "report")`), not "Prepare VAT return". A dashboard is
read-only and asserts nothing to the tax authority — it is a report, not a filing action — and
"View reports" is also the wider, correct-audience grant: Owner/Accountant/Bookkeeper (F),
Approver (C), Viewer (R) all have legitimate reason to see cash position and receivables, and
"Prepare VAT return" (Owner/Accountant/Bookkeeper only) would deny an Approver or Viewer for no
reason MOB-006 asks for. Reused via the identical `require_permission("view", "report", ...)`
call `get_administration` in `api.main` already makes for the same capability — no permission is
invented (ADR-012).

Composition inside `DashboardService` is deliberate about *which* layer of each bounded context it
calls, checked against `api.authz.matrix.MATRIX` before writing any of it:

- `LedgerService.trial_balance()`/`subledger_balance()` — called directly. The ledger's read
  methods have never done their own authorization (see their own docstrings); the route's one
  "View reports" check is what gates them.
- `ChartOfAccountsService.chart()` — called through the real service (via a new
  `api.ledger.chart.build_chart_service`, mirroring `build_ledger_service`), because its "View
  chart of accounts" permission check IS coextensive with "View reports": every role holding "View
  reports" also holds "View chart of accounts" at the same or a wider, unconditional level
  (Owner/Accountant/Bookkeeper F/F, Approver C/R, Viewer R/R). `tests/ledger/
  test_bounded_context.py` keeps `api.ledger.chart_repository` internal to its own service, so
  this is the "widen the API deliberately" fix that test's own failure message asks for, not a
  reach past it.
- `SqlInvoiceRepository`/`SqlCaptureRepository` — called directly, **not** through
  `InvoicingService`/`ExpenseFormService`. Those services check `create sales_invoice`/`submit
  expense`, neither of which Appendix A makes coextensive with "View reports" (a Viewer holds
  "View reports" but neither of those). Composing through them would deny a Viewer's own dashboard
  for lacking a permission the dashboard was never supposed to need. Their repositories do no
  authorization of their own — no repository in this codebase does — which is exactly the shape
  needed once the route's own check has already gated the whole aggregation.

`api.dashboard.model.summarize()` is the pure aggregation logic — trial-balance rows, chart
accounts, subledger rows, two VAT sums, invoices, expenses and "today" in; a `DashboardSummary`
out, no database, no tenant context — mirroring how `api.templates.compliance.check()` keeps
business logic testable without one. `api.dashboard.service.DashboardService` is the thin
fetch-and-hand-off half; `api.dashboard.repository.DashboardRepository` is two new SQL reads
(the fiscal year's own date range; the two VAT sums) that no existing repository already exposed.

Every amount crosses the wire as a Decimal-shaped **string** (`str(summary.cash_position)`
etc.) — the same convention `api.customers.routes`'s `credit_limit` uses — and nothing in the
calculation path (`api.dashboard.model`, `api.dashboard.repository`) touches a float; both are
`Decimal` throughout and tested for it (`test_no_floats_anywhere_in_the_money_path`).

### 4. Frontend: `DashboardApi`, `HomeScreen`, no re-sorting

`apps/web/src/home/api.ts`'s `DashboardApi` mirrors `SalesInvoiceApi`/`CaptureApi`'s conventions
exactly (`ApiOptions`, `ApiError`, `OfflineError`, `languageHeaders`) — one GET call, no
idempotency key needed since nothing mutates.

`HomeScreen.tsx` renders the three figures via `useI18n()`'s `money()`/`date()` formatters
(matching `ApproveList`/`ViewList`'s own usage), each with its honesty caption, and the
prioritised list exactly in the order the API returned it — the component does not re-sort;
`build_action_items`' ordering is the backend's job and is tested there, not duplicated
client-side. A zero-item list renders `mobile.home.items_empty`'s positive statement (FR-UX-004)
rather than an empty `<ul>` with no explanation.

Tapping an item calls `onNavigate(tabFor(item))`: `overdue_invoice`/`draft_invoice` → `"view"`
(ViewList's invoices list includes both issued and draft invoices already — MOB-005); `draft_expense`
→ `"approve"` (ApproveList is exactly "draft expenses awaiting completion"). Neither destination
opens directly to the tapped record — see Known gaps.

## Alternatives considered

| Option | Rejected because |
|---|---|
| "Prepare VAT return" as the permission | Narrower audience (Owner/Accountant/Bookkeeper only) than a read-only summary dashboard needs, and it is a filing-action capability — a dashboard asserts nothing to the tax authority. |
| Compose through `ChartOfAccountsService`, `InvoicingService` *and* `ExpenseFormService` uniformly | Checked against `api.authz.matrix.MATRIX`: only the chart service's permission is coextensive with "View reports". Composing through all three would deny a Viewer's own dashboard for lacking `submit expense`/`create sales_invoice`, which the dashboard never needed. |
| Import `api.ledger.chart_repository` directly from `api.dashboard` | `tests/ledger/test_bounded_context.py` fails the build: that module is internal to `ChartOfAccountsService` alone. `build_chart_service` (mirroring `build_ledger_service`) is the deliberate widening its own failure message asks for. |
| Add `rgs_code` to `TrialBalanceRow`/`ledger.trial_balance()` | Touches the ledger's core reporting SQL function for one caller's convenience; joining chart accounts to trial-balance rows on `account_id` in application code is a smaller, more reversible change that widens nothing about the ledger's narrow API. |
| The current VAT *period* (month/quarter) instead of fiscal-year-to-date | This codebase has no "which period is current" concept outside a specific fiscal year's own periods, and inventing one for a dashboard estimate is more machinery than a first version needs. Fiscal-year-to-date is simplest, uses the `fiscal_year_id` the endpoint already needs for `trial_balance()`, and is stated plainly in the response so nobody mistakes it for a filing period. |
| Include `ready` (not just `posted`) expenses in the input-VAT sum | Would make `vat_estimate` count VAT on claims not yet in the books, while `cash_position` counts only posted movements — two figures on one screen silently using different definitions of "in the books". |
| A payment/settlement concept invented to define "overdue" more precisely | FR-BNK's reconciliation is P1 and not built; inventing a payment-tracking concept that does not exist in this codebase would be exactly the kind of placeholder this task's honesty constraint forbids. "Issued, past due, not credited" is the definition this codebase can actually support today. |
| Deep-linking `ApproveList`/`ViewList` to open a specific record | Neither list supports it today, and building that support is materially more work than this task's scope (a dashboard, not a rework of two existing task screens) — recorded as a known gap instead. |
| Building `items_needing_action`'s descriptions client-side from raw fields | `t()` supports interpolation, but every other D5-specific, multi-fact sentence in this codebase (`statutory_failures`, `duplicate_warnings`) is built server-side in the request's own language via the shared catalogue; keeping descriptions there is consistency, not necessity. |

## Consequences

**Easier.** A future native app (MOB-001, P2) gets the same dashboard endpoint, the same
`DashboardView`/`DashboardActionItemView` shapes (added to `@ledgr/shared-types` field-for-field
against the route's own JSON, the same rule `ExpenseView`/`SalesInvoiceView` follow), and the same
permission reuse — nothing dashboard-specific to re-derive server-side. `build_chart_service`
becomes the second public constructor into the chart-of-accounts bounded context and is available
to any future caller with the same "View reports"-shaped need.

**Harder.** `DashboardService` now composes four different sources with three different
authorization postures (two ledger reads with none, one chart-service read with its own check
proven-coextensive, two repository reads with none) — a future change to any of those four
services' own permission has to be re-checked against `api.authz.matrix.MATRIX` for continued
coextensiveness with "View reports", or this endpoint silently starts denying (or over-granting)
a role it did not before. The reasoning is written down here and in `api.dashboard.service`'s own
module docstring specifically so that re-check is possible without re-deriving it from scratch.

**Known gaps.**

- **No deep-linking to a specific record.** Tapping an item switches to the right tab; neither
  `ApproveList` nor `ViewList` opens directly to the tapped invoice or expense. A person still has
  to find it again in the list they land on. Building that support into either screen was
  explicitly out of scope for this task.
- **Cash position is GL-based, not live-bank-synced.** It reflects what has been posted, honestly
  labelled as such. It will read differently from a bank's own current balance whenever a real
  transaction has not yet been captured and posted — which is every transaction, until FR-BNK
  exists.
- **The VAT estimate is fiscal-year-to-date, not the current VAT filing period.** A business filing
  quarterly sees a number covering more than their next return; the caption and the shown date
  range make this explicit rather than implying the figure is filing-scoped.
- **No pagination on the underlying invoice/expense fetches.** `DashboardService` fetches up to
  1000 invoices and 1000 expenses per call (via the existing, unpaginated `list_invoices`/
  `list_by_status`) to build the prioritised list and the VAT sums' population correctly; an
  administration with more open items than that would see a silently incomplete list. The same
  "nothing paginates yet" gap ADR-046 already recorded for the two list endpoints it added.
- **The overdue-invoice definition has no payment tracking behind it.** "Issued, past due date, not
  credited" is honest given FR-BNK does not exist, but it will report an invoice as overdue even
  if the customer paid by bank transfer yesterday and nobody has reconciled it yet. This is a
  known, documented limitation of "overdue" until reconciliation exists — not a bug in this task.
