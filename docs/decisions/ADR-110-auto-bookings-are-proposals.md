# ADR-110: Auto-bookings are proposals outside the ledger, approved through the bank's own path

- **Status**: Accepted
- **Date**: 2026-10-08
- **Builds on**: ADR-091 (matching confidence), ADR-092 (receipts settled by bank lines), ADR-108
  (PSD2 feed through the import's write path), ADR-109 (firm cross-client reads);
  docs/firm-home/contract.md decision 2

## Context

FR-BNK-004 asks for "auto-post above the high threshold, propose between thresholds, queue for
manual handling below". The firm home (FR-FRM-002) wants an accountant with 200 clients to clear a
week of certain matches in one sitting: "KPN -> 4500 Telefoon - 12 bookings - 9 clients - approve".

Posting automatically would put entries in the ledger that nobody looked at. The ledger is
append-only (FR-GL-003, CMP-009): a wrong auto-post can only be corrected with a reversing entry,
and "flag the suspicious ones afterwards" would mean marking or editing posted entries, which
nothing may do. NFR-032 also asks that no retry ever double-posts - and a bulk approve over a
flaky mobile connection is exactly the request that gets retried.

## Decision

- **A proposal is a row, not a posting.** `booking_proposal` (migration 0079) records that one
  unmatched bank line is settled by one open document (a bank-paid receipt or a sales invoice), the
  target account, the amount (`numeric(18,2)`, positive) and a `group_key` (normalised
  counterparty + target account code). It is outside the ledger: RLS on
  `app.has_administration_access`, no DELETE grant, status `pending -> approved | rejected |
  superseded` once and never back (trigger), at most one pending row per bank line (partial
  unique index). Every other column is frozen as proposed.
- **Only certain matches are proposed.** Generation runs the existing `api.bank.matching.suggest`
  and keeps its HIGH results - which `suggest` has already made unambiguous (two documents for one
  line, or one document for two lines, are demoted to MEDIUM). A partial payment is never HIGH, so
  it is never proposed. This is the Bank screen's own "match all certain", not a second scorer.
- **Where generation runs.** One hook in `BankService.import_rows` (the single write path for both
  uploaded statements and the PSD2 sync), one in the expense posting route (a receipt paid from the
  business account is a new candidate document), and a backfill job
  (`python -m api.firm.proposals_job`, ops-reads / app-writes like ADR-108's sync). Generation is
  idempotent: a pending proposal still certain for the same document is kept; one whose line is no
  longer certain, or certain for a different document, is superseded and the new one written.
- **Withdrawn when the line is matched another way.** Every reconciliation path ends in
  `BankService._record_reconciled`, which supersedes the line's pending proposal and any pending
  proposal for the document it just settled.
- **Advisory, never in the way.** The hooks run in a savepoint and log a failure instead of raising,
  so an import or a reconciliation is never lost to proposal generation - including on a deployment
  where 0079 is not applied yet (NFR-044).
- **Approving posts through the bank's own path.** An approval calls
  `BankService.reconcile_with_expense` / `reconcile_with_invoice` - what a click on the Bank screen
  calls - so the entry is made by `LedgerService.post()` or `SalesPaymentService.record()` with
  every check they already make. Rejecting changes only the proposal. Nothing posted is ever
  flagged, edited or deleted.
- **One administration at a time, even in a batch.** `POST /v1/firm/proposals/decide` declares
  `require_portfolio_permission("reconcile", "bank_transaction", audit=POSTING)` (ADR-109's
  shared firm-route dependency: the switcher's live-grant join, then `authorize()` per
  administration). A proposal whose administration is not in that portfolio is refused. Every
  other proposal is authorized AGAIN against its own administration through the shared library,
  against current state, then runs in its own savepoint, is audited individually (`approve_` /
  `reject_booking_proposal`, plus the reconciliation's own entry, beside the request-level POSTING
  entry the declaration produces), and refusals are reported in `failed[]` with the Bank screen's
  own reason codes. A batch is never one 500.
- **The review sheet reads only the portfolio.** `GET /v1/firm/proposals` declares
  `require_portfolio_permission("view", "bank_transaction")` and reads pending proposals in exactly
  those administrations (`administration_ids` can narrow, never widen). The per-client
  `GET /v1/administrations/{id}/proposals` is an ordinary `require_permission` on the path.
- **Never twice.** The proposal is claimed (`UPDATE ... WHERE status = 'pending'`) before the
  posting, in the same savepoint; the row lock serialises concurrent decisions and a failed posting
  rolls the claim back. A retried decision that already took effect is answered as that decision
  again without posting; the idempotency middleware replays a same-key retry on top.
- **Cross-client by design.** The two `/v1/firm/proposals` routes name no administration; they are
  not in `AUTHORIZATION_EXEMPT_PATHS` - the portfolio declaration is their requirement, seen by the
  enforcement middleware and the authz/audit coverage tests like any other. `decide` is not
  subject to the active-client guard (FR-FRM-000a): every row of
  the review sheet names its client and every decision names one proposal, so there is no "header
  says one client, request writes to another" to refuse - the switcher's `allows_cross_client`
  case. A session has no way back to "no active client" today, so applying the guard would refuse
  the firm home to anyone who ever opened a client.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Auto-post HIGH matches and let the accountant reverse mistakes | Puts unreviewed entries in an append-only ledger; every mistake becomes two more entries, and "flag" would mean marking posted rows. |
| A `proposed` status on `bank_transaction` | Mixes a suggestion into the statement line's own lifecycle (0065's trigger) and cannot carry the target account, the group key or the decision history. |
| A dedicated posting path for approvals | A second write path into the ledger beside the Bank screen's, with its own checks to keep in step - non-negotiable #1. |
| Re-scoring proposals with a separate, firm-level confidence model | Two answers to "is this certain" that could disagree; the Bank screen and the firm home must show the same thing. |
| One authorization check for the whole batch | A batch spans clients; the check has to be the one each client's books would apply. |
| Exempting the firm routes from route-level authorization and authorizing only inside | Invisible to the enforcement middleware and coverage tests; ADR-109's portfolio dependency declares the same per-administration check where they can see it. |
| Store the proposal's amount signed, like the bank line | The review sheet shows what each booking amounts to; direction is carried by the document kind. |

## Consequences

- A sales invoice issued after its payment was imported is not proposed until the next import,
  feed sync or backfill run - issuing has many entry points (single, batch, recurring, approval)
  and none is hooked. Running the backfill on a schedule closes the gap.
- `booking_proposal.document_kind` is an addition to the contract's schema: approving needs to know
  which reconciliation path a `document_id` belongs to.
- Proposals for medium-confidence matches are allowed by the schema (`confidence` admits
  `medium`) but none are generated; doing so is a policy change here, not a schema change.
- "Approve as rule" (wave 2) would be a rule that approves future proposals in a group; it must
  still decide through this path, one administration at a time.
