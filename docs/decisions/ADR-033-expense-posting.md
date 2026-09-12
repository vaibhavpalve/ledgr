# ADR-033: Confirming an expense into the ledger

- **Status**: Accepted
- **Date**: 2026-09-04
- **Implements**: FR-EXP-001d, FR-EXP-001e (PRD §6.7)
- **Serves**: FR-EXP-001c (manual entry is a complete path)
- **Constrained by**: CLAUDE.md non-negotiable #1 (the ledger's narrow API), FR-GL-001 (balance),
  FR-GL-003 (append-only), FR-GL-007 (period locks), NFR-031, ADR-012
- **Related**: [ADR-032](ADR-032-expense-form.md) — where the amounts come from;
  [ADR-030](ADR-030-document-storage.md) — the link this completes;
  [ADR-022](ADR-022-ledger-bounded-context.md)

## Context

> **FR-EXP-001d.** The image is retained as the source document under §6.10, **linked
> bidirectionally to the resulting posting** …
>
> **FR-EXP-001e.** Payment method is captured at entry — paid by business account, business card,
> or personally (reimbursable) — **because it determines the posting** and cannot be reliably
> inferred later.

## Decision

### 1. Through the ledger's API, and the check caught me reaching past it

`api.expenses.posting` builds an `EntryInput` and calls `LedgerService.post`. It holds no SQL
against `journal_entry` or `journal_line`, and `ledgr_app` has no grant that would let it.

The first version imported `SqlLedgerRepository` to construct the service —
`tests/ledger/test_bounded_context.py` failed, correctly, and its message says what to do: *"the
fix is to widen their API deliberately, not to reach past it."* So `api.ledger.service` gained
`build_ledger_service(session, audit_log)`. One function on a public module; the repository import
is function-local so the only line naming it stays inside the context.

This is the check working as designed. A caller reaching for the repository to build the service
has already reached past the narrow API, and the *next* caller reaches for a query on it.

### 2. The payment method chooses the credit — literally

| Method | Credit | Why it differs |
|---|---|---|
| `business_account` | the bank account | reconciles against a bank transaction |
| `business_card` | the card liability | settles later |
| `personal_reimbursable` | **the reimbursement liability** | the business owes this person money |

The debits never move; only the credit does. That is what FR-EXP-001e means by "determines the
posting", and why the field cannot be filled in afterwards: **a card slip looks the same whoever's
card it was**, and only the person standing there knows whether they are owed the money.

**The reimbursement liability is not accounts payable**, for a mechanical reason and an accounting
one. Mechanically, FR-GL-006 makes a control account postable only through its sub-ledger with a
party, so every claimant would need a `subledger_party` — putting employees in the trade creditors
ledger. In accounting terms they do not belong there: money owed to staff is its own position
("Te betalen declaraties"), and a Dutch bookkeeper reading a creditors list does not expect to find
colleagues in it.

### 3. The entry

    Dr  expense account   net
    Dr  input VAT         vat      (omitted when the treatment carries none)
    Cr  funding account   gross

It balances because `net + vat == gross` is an *identity* in the schema — ADR-032 generates net as
`gross − vat` — not an arithmetic hope. The ledger re-derives the sum at COMMIT anyway, which is
where the guarantee lives.

### 4. The VAT treatment goes on the line

`journal_line` had no `vat_code`, though the PRD's data model (§12) lists one. Migration 0035 adds
it, because a VAT return built by joining back to whatever produced each entry would have to know
about expenses, sales invoices, purchase invoices and manual journals separately. One column means
FR-VAT-001 reads the **ledger**, which is the only place every source has already agreed.

It is on the **cost** line as well as the VAT line: a rubriek needs the turnover, which is the net
amount and not the tax. The funding line carries none — money moving is not turnover.

`ledger.post_entry` was re-created with two lines added. It was extracted from 0020
**mechanically**, not retyped: a hand-copied 128-line function is a transcription error waiting to
be found by an unbalanced entry. The signature is unchanged, so every grant still names it.

### 5. Accounts are configuration, not a guess

An expense has a free-text `category`, which is not an account. Matching category names against the
chart of accounts is the kind of cleverness that puts a lunch in "Loonheffingen" on a Friday
afternoon. So `expense_posting_account` maps them per administration, and an unmapped one is a
**refusal naming what is missing**. Configuration that has not been done is visible; a wrong guess
is not, and is found by an accountant months later.

Having the client send account ids is worse twice over: it puts accounting knowledge in the client,
which CLAUDE.md rules out, and it lets a caller choose which account their lunch lands in.

### 6. Posting takes the ledger's permission, not the submitter's

Appendix A's **"Post journal entries"**, not "Submit expenses". §8.4 lets an Expense Submitter
submit a claim and *not* post one, and keeping that separation is the point — somebody who can only
capture receipts must not be able to put them in the books.

### 7. FR-EXP-001c: manual entry, proven complete

Extraction is not built, which makes the requirement easy to satisfy *and easy to lose*: the first
commit adding an OCR call between capture and posting would break it without breaking a single
existing test, because every existing test would still exercise a system where extraction happened
to succeed.

So `tests/expenses/test_manual_path.py` asserts two things that survive extraction arriving:

- the **whole path** — capture → form → confirm → post — runs end to end with nothing pre-filling
  anything, every field typed;
- **no module in `api.expenses` imports or calls anything extraction-shaped**, checked over the
  import graph and the identifiers rather than the prose (several modules mention OCR precisely to
  say it is absent).

When extraction lands it belongs behind a switch that is off here, and that test is what will say
so.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Import `SqlLedgerRepository` to build the service | Reaches past the narrow API. The bounded-context check caught it; the fix was to widen the API deliberately. |
| Write the posting rows directly | `ledgr_app` has no INSERT grant — it would fail at runtime anyway, having already ignored non-negotiable #1. |
| Credit accounts payable for a reimbursement | FR-GL-006 would need a sub-ledger party per employee, putting colleagues in the trade creditors ledger. |
| Derive the expense account from the category name | Puts a lunch in "Loonheffingen". |
| Let the client send account ids | Accounting logic in the client, and a caller choosing where their own costs land. |
| Post into an open period when the expense's own is locked | Puts the cost in the wrong month to avoid an inconvenience. Where the period is filed the route is a suppletie (FR-VAT-005). |
| Pick a purchase journal when several are active | FR-GL-013 numbers per journal, so the choice shows up in entry numbers forever. Refused instead. |
| Store the VAT treatment only on the expense | A VAT return would have to know about every source separately rather than reading the ledger. |
| Retype `ledger.post_entry` | 128 lines of hand-copied SQL. Extracted mechanically instead. |

## Consequences

**Easier.** FR-VAT-001 can build a return from the ledger alone. FR-EXP-003's reimbursement run has
a real liability balance to pay down. FR-DOC-003's completeness report sees expense evidence
without knowing what an expense is, because the link goes in the same `document_posting_link` table
a direct upload uses — **which closes the gap ADR-031 recorded**.

**Harder.** An administration must map its posting accounts before any claim can be confirmed.
That is real setup work, and the alternative is guessing.

**Known gaps, each with a trigger.**

- **Reverse charge posts as it was paid.** `btw_verlegd` carries rate 0, so the entry is net =
  gross with no VAT line — correct for the money that changed hands. The notional VAT pair the
  buyer accounts for is FR-VAT-001's, and is derivable later *precisely because* 0035 records the
  treatment on the line.
- **No UI, and no endpoint to manage the account map.** Which role may configure it is not
  something Appendix A answers, so choosing one would be inventing a permission (ADR-012). The
  table and its constraints exist; the surface belongs with administration settings.
- **`expenses.unposted_claims` has no caller.** It is the mirror of FR-DOC-003's completeness
  report from the other side — a `ready` claim with no entry is somebody waiting to be paid — and
  the screen that shows it is not built.
- **One entry per claim, not an accrual then a payment.** A receipt is not a supplier invoice;
  FR-AP's two-step treatment is for documents that arrive before the money moves.
