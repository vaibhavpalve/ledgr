# ADR-086: Journals and posting-account mappings are created at onboarding

- **Status**: Accepted
- **Date**: 2026-09-24
- **Implements**: FR-ONB-005 (a usable chart, seeded), FR-AR-001, FR-EXP-001e, FR-GL-001
- **Builds on**: [ADR-059](ADR-059-onboarding-transaction-and-founding-firm-grant.md) (the
  onboarding transaction), [ADR-033](ADR-033-expense-posting.md) and
  [ADR-039](ADR-039-invoice-posting.md) (the two mapping tables)

## Context

`POST /v1/administrations` seeded the chart of accounts and opened the fiscal year, then
stopped. Every posting path looks up configuration that nothing wrote:

| Path | Needs |
|---|---|
| Issue a sales invoice | one active `sales` journal; `sales_posting_account` rows for revenue and output VAT |
| Post an expense | one active `purchase` journal; `expense_posting_account` rows for the category, input VAT and the payment method |
| Reconcile a bank line | one active `bank` journal |
| Manual posting, depreciation | a `memorial` journal |

`scripts/golden_path.py` found the break by driving a real signup over HTTP. A customer signs up,
onboards, drafts an invoice and is refused on issue: "This administration has no single active
sales journal". No test caught it, because every integration test seeds its own journals and
mappings. There was also no screen where a customer could have entered this configuration.

## Decision

One idempotent SQL function, `app.ensure_posting_defaults(administration_id)` (migration 0067),
derives the journals and mappings from the administration's own seeded chart:

- **Journals**: sales (VK), purchase (IK), bank (BNK), cash (KAS) and memorial (MEM), unless an
  active journal of that type already exists.
- **Sales**: a revenue row for each VAT treatment that a revenue account names as its
  `default_vat_code` (8000 → btw_21, 8010 → btw_9, 8020 → verlegd, …), a revenue fallback
  (8000), and one output-VAT fallback (1700 / RGS `BSchObe`).
- **Expenses**: input VAT (1720 / `BVorObe`); `business_account` and `business_card` to the bank
  (1100); `reimbursement_liability` to the director's current account (1500, for a BV), private
  contributions (0560, for a sole trader) or other payables (1790); each capture category to an
  expense account (office supplies → 4100 Kantoorkosten, travel → 4200 Verkoopkosten, …); and a
  fallback (4400 Algemene kosten).

Accounts are found by code first and RGS code second, so a renamed account still resolves. The
function **only fills gaps**. It never changes an existing journal or mapping, so calling it
twice is a no-op. It is `SECURITY INVOKER`: called by the API as `ledgr_app`, it runs under the
caller's tenant context and RLS like any other insert. Journals go through
`ledger.create_journal`, the ledger's narrow API (non-negotiable #1).

Onboarding calls it right after the seed, in the same transaction. For a firm-created client it
runs under the client's tenant context, exactly as the seed does. The migration calls it once for
every existing administration that has a chart, which repairs the ones already in production.

## Alternatives considered

| Option | Rejected because |
|---|---|
| A settings screen where the owner maps accounts before the first posting | The owner of a small business does not know which ledger account a sale "should" post to, and should not have to. It blocks the first invoice behind accounting configuration. The mapping remains editable data for an accountant who wants a different one. |
| Implement it in Python in the onboarding route | Existing administrations would then need a separate backfill script run by hand in production. One SQL function serves both the route and the migration. |
| Derive the account at posting time from the chart (no mapping rows) | Mapping tables exist so that revenue in the wrong account (and so the wrong rubriek) is a configuration fact that can be read and changed, not an inference buried in the posting code (0040's header). |
| Overwrite mappings that differ from the defaults | Destroys a deliberate choice an accountant made. Filling gaps is the only safe idempotent behaviour. |

## Consequences

**Easier.** A new customer can issue an invoice, post a receipt and reconcile a bank line
immediately after onboarding. `scripts/golden_path.py` proves it end to end over HTTP.

**Harder.** The category list now lives in two places: `api.expenses.categories` and 0067's
`values` list. `tests/integration/test_posting_defaults.py` fails if a category is added to
the Python list without a default mapping. A new category needs a new migration that re-creates
the function with the extra row. Calling it again for existing administrations is safe because
it only fills gaps.

**Known gaps.** The mapping choices (for example, travel to Verkoopkosten, insurance to Algemene
kosten) are reasonable defaults for a Dutch SMB, not a statement of what every business should
use. There is no screen to change them yet. An accountant can change them today only in the
database.
