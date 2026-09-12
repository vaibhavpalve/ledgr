# ADR-039: Issuing an invoice is one act — number, books, and the PDF as issued

- **Status**: Accepted
- **Date**: 2026-09-09
- **Implements**: FR-GL-006 (AR sub-ledger), FR-TPL-017 (sent invoices are immutable), and the
  ledger half of FR-AR-001 (PRD §6.2, §6.3, §6.4)
- **Serves**: NFR-031 (no floating point), NFR-032 (idempotency), FR-GL-001/003/004 (balance,
  immutability, attribution), FR-VAT-001 (the rubriek reads the ledger), CMP-001 (seven-year
  retention), FR-TPL-013 (the recipient's language), FR-TPL-016 (selectable text, 9pt minimum)
- **Constrained by**: CLAUDE.md non-negotiable #1 (the ledger's narrow API), ADR-012 (no invented
  permissions), Appendix A (the Invoicer may send but not post)
- **Related**: [ADR-037](ADR-037-sales-invoices.md) — the gap this closes;
  [ADR-033](ADR-033-expense-posting.md) — the purchase side, and the split this deliberately does
  not make; [ADR-038](ADR-038-customer-master.md) — where the debtor comes from;
  [ADR-022](ADR-022-ledger-bounded-context.md) — why nothing here writes a posting

## Context

ADR-037 recorded its own largest functional gap plainly:

> **Nothing is posted to the ledger.** Issuing an invoice creates no journal entry, so AR does not
> yet exist in the books and FR-AR-012's aged receivables has nothing to read. […] the posting goes
> through `LedgerService` when it lands (ADR-022), never around it.

ADR-038 inherited it — the customer master's credit limit is enforced by nothing, because there is
no receivables balance to check against. This closes both, and adds FR-TPL-017's stored rendering,
which has the same shape of requirement: a fact that must be established at issue and never
recomputed.

Two foundations were already there. `subledger_party`, `control_kind` and
`journal_line_validate()`'s biconditional (0020) are the whole AR sub-ledger mechanism; and the
document archive (0031) already gives write-once storage, per-tenant encryption, hash verification
and seven-year retention. Neither needed extending.

## Decision

### 1. Issuing and posting are one transaction

    1. FR-AR-003's statutory gate                    can fail
    2. resolve the posting: accounts, journal, period, debtor   can fail
    3. allocate FR-AR-004's number                   CANNOT BE UNDONE
    4. freeze the VAT totals
    5. post the entry, render the PDF, store and link it

There is no state in which an invoice is issued and the books do not know about it. An issued
invoice missing from the ledger is a receivable nobody chases and turnover missing from the
aangifte, and the drift is invisible — which is why `invoicing.unposted_invoices()` exists and
should always return nothing.

**Step 2 is the new one, and it is placed there deliberately.** ADR-037 established that everything
which can fail happens before the number is allocated, because FR-AR-004 wants the series gapless.
Configuration failures join that list: an administration with no revenue account mapped must not
discover it after taking a number. The rollback would in fact take the number back — 0037 allocates
from a table rather than a `sequence` precisely so it does — but a refusal that never touches the
allocator beats one that relies on undoing it.

**This is not the split the expense side makes.** ADR-031/ADR-033 separate capture from posting
because an Expense Submitter may capture and may not post: a real segregation boundary. §2 below is
why no such boundary exists here, so the split would buy nothing and cost the guarantee.

### 2. `send sales_invoice` authorises the posting — not `post journal_entry`

Appendix A grades the two capabilities differently, and the Invoicer is the row where they part:

| Capability | Owner | Accountant | Bookkeeper | Invoicer |
|---|---|---|---|---|
| Send sales invoices | F | F | F | **F** |
| Post journal entries | F | F | C | **—** |

Requiring `post journal_entry` here would mean an Invoicer cannot issue an invoice, contradicting
the cell that says they can. So the entry is not a second discretionary act: issuing an invoice **is**
the accounting event — revenue is recognised at the invoice date and the customer owes the money
from that moment — and the posting is the mechanical consequence of an authority the actor already
holds.

The actor is still recorded. The entry names the person who issued the invoice (FR-GL-004,
IAM-060), because we know exactly who caused it; this is not an `ActorType.SYSTEM` posting like a
bank import, and pretending it were would make "who did this" unanswerable.

### 3. The entry is per VAT group, and a credit note exchanges the sides

    Dr  Debiteuren (AR control)      gross     <- carries the sub-ledger party
    Cr  Omzet <treatment>            taxable   } one pair per VAT group
    Cr  Te betalen omzetbelasting    vat       }

Per group rather than per line, for the reason 0037 gives about `sales_invoice_vat_total`: Art. 226
asks an invoice to show the taxable amount and the VAT **per rate**, those are the figures frozen at
issue, and posting a different decomposition would put one set of numbers on the document and
another in the books — which FR-VAT-001 would then inherit, because the rubriek reads the ledger.

A credit note posts the same lines with debit and credit **exchanged**, not with negative amounts.
`journal_line` refuses negatives (0020: "amounts are unsigned; direction is carried by which of
debit/credit the amount is on"), and a negative debit would balance while breaking every report that
sums a column. `_side()` exists as one helper rather than three conditionals because getting one of
them backwards produces an entry that still balances — two flipped lines cancel — and reverses the
sign of a customer's balance.

A group carrying no VAT writes no VAT line. Five of FR-AR-002's eight treatments charge the customer
nothing, and a `0.00` movement on the output-VAT account is noise in every report that lists it.

### 4. The AR control account is found, not configured; revenue is configured, not inferred

`expense_posting_account` (0034) maps five purposes because an expense has a free-text *category*
and the chart has no idea what "Kantoorbenodigdheden" is. The receivable has no such gap: FR-GL-006
already designates the AR control account and `ledger_account_one_control_per_kind_idx` already makes
it unique per administration. A mapping row pointing at the account the schema has singled out would
be a second answer to a question that has one, and the two could disagree.

Revenue goes the other way. It was tempting to read it off `ledger_account.default_vat_code` — the
shipped RGS chart already carries "Omzet hoog tarief" → `btw_21`, "Omzet EU" → `btw_icp` — and it is
the wrong direction. 0020 defines that column as *"the VAT default applied when this account is
selected"*: account → treatment, a suggestion to a bookkeeper who has already chosen the account.
Inverting it assumes the relation is a function, which nothing enforces ("Inkoopwaarde van de omzet"
is also `btw_21`), so the inversion is unambiguous only if no administration ever splits revenue by
product line — ordinary bookkeeping, so the hope is misplaced. The failure would be silent and
expensive: **revenue in the wrong account is revenue in the wrong rubriek**, and the trial balance
still balances.

So `sales_posting_account` maps `revenue` and `vat_output` per treatment, with a nullable-key
fallback row, and 0040's trigger additionally refuses a revenue mapping onto a non-revenue account —
which would understate turnover and overstate costs by the same amount.

### 5. A customer is a sub-ledger party, and the pointer lives on the customer

0039's `customer` and 0020's `subledger_party` are two tables about one counterparty.
`customer.subledger_party_id` links them, set once on first posting.

The direction matters: `api.customers` may know the ledger exists, and the ledger may not know a
customer master does. A `customer_id` on `subledger_party` would make the ledger's schema depend on a
bounded context outside it — the coupling CLAUDE.md's first non-negotiable exists to prevent.

A **one-off** customer (no master record) still gets a party, named from the invoice's own snapshot.
Somebody does owe the money and the sub-ledger has to say who. The cost is honest: invoice the same
one-off name twice and aged receivables shows two debtors. The fix is to make them a customer, which
is exactly the trade FR-AR-006 exists to offer.

### 6. FR-TPL-017 is a claim about storage, not about drawing

> Sent invoices are immutable. Editing a template never alters the appearance of an already-issued
> invoice; the rendered PDF is stored as issued.

Almost all of that is about *when* rendering happens and what becomes of the result. So:

- rendered **once**, at issue;
- stored in the document archive, inheriting FR-DOC-001's hash verification, per-tenant encryption
  and CMP-001's retention — re-implementing any of that would be a second archive with none of it;
- linked to the posting (FR-DOC-003), in the one case where the "source document" is one this system
  produced rather than received;
- `sales_invoice.document_id` set once, with a trigger refusing to move it;
- and **there is no re-render path** — not lazy, not a cache, not an admin tool. The absence of the
  path is what makes the requirement true rather than merely intended.

The renderer output is **deterministic** (no clock, no random source, no `CreationDate`), so the
archive's content hash is a property of the invoice rather than of the moment. That is a small
independent check on the whole claim.

Storing through `DocumentService.upload` authorises as `upload document`, which every role holding
`send sales_invoice` already has. That is a **coincidence rather than a design**, so
`tests/invoicing/test_posting.py` asserts it rather than relying on it — and if it ever fails, the
fix is not to bypass the authorization check but to decide whether that role should be issuing at
all.

### 7. `MinimalPdfRenderer` is a complete invoice and an incomplete FR-TPL

Stated plainly because the gap between "a PDF comes out" and "FR-TPL is built" is easy to lose sight
of. It renders every field Art. 226 requires, paginates with repeating headers and page numbers, puts
the words in the recipient's language (FR-TPL-013) and the figures in the administration's locale
(FR-LOC-002), and prints the Art. 226(11) legal wording. It is **not** a template system
(FR-TPL-001..014), **not** PDF/A-3 (FR-TPL-015), and **not** tagged (FR-TPL-016) — though it is real
selectable text at no less than 9pt, which `Text.__post_init__` enforces rather than trusts.

It is hand-written rather than a library because every library that could produce this page brings a
rendering engine, font subsetting and an image pipeline for one page of text in one font — and these
are bytes a customer receives and CMP-001 keeps for seven years, so a surface small enough to read in
one sitting is worth more than usual.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Post as a separate step after issuing | Reintroduces the drift `unposted_invoices()` exists to detect, for an SoD boundary that does not exist here (§2). |
| Require `post journal_entry` to issue | Contradicts Appendix A's Invoicer, who may send invoices and may not post. |
| Post as `ActorType.SYSTEM` | We know exactly who caused it. Pretending otherwise makes IAM-060 unable to ask "did the same person do both". |
| Post one line per invoice line | Art. 226 asks for per-rate figures, and 0037 froze those. Two decompositions of one invoice, and FR-VAT-001 reads the wrong one. |
| Post a credit note with negative amounts | `journal_line` refuses them, and a negative debit breaks every report that sums a column. |
| Derive the revenue account from `default_vat_code` | Inverts a relation nothing enforces to be a function. Silent, and lands turnover in the wrong rubriek (§4). |
| Map the AR control account in `sales_posting_account` | A second answer to a question FR-GL-006 already answers uniquely. |
| Refuse to invoice a customer with no master record | 0039 keeps the one-off path permanently, and a receivable from a one-off customer is still owed by somebody. |
| Put `customer_id` on `subledger_party` | Makes the ledger's schema depend on a context outside it. |
| Render the PDF lazily on first download | A template edit between issue and download would change a document already sent. That is precisely FR-TPL-017. |
| Cache the rendering and re-render on miss | Same failure, harder to see. |
| Store the PDF outside the document archive | A second archive with no hash verification, no per-tenant encryption and no retention. |
| Use a PDF library | A rendering engine, font subsetting and an image pipeline for one page of text. |
| Put `CreationDate` in the PDF | The only non-deterministic bytes in the file; the content hash would become a property of the moment rather than of the invoice. |
| Print `wording_is_provisional` on the invoice | It is an internal warning that no tax adviser has signed the sentence off. On a customer's invoice it would be worse than the risk it describes. |

## Consequences

**Easier.** FR-AR-012's aged receivables and per-customer statement are `ledger.subledger_balance`
over one party — already reconciled to the control account by construction. FR-VAT-001's rubrieken
read `journal_line.vat_treatment`, which every revenue and VAT line now carries. ADR-038's credit
limit finally has a balance to check against. FR-AR-005's send has bytes to attach.

**Harder, and this is the real cost.** **An administration with no revenue account mapped cannot
issue an invoice at all.** Issuing now depends on chart configuration that previously did not matter,
and on a firm-managed client the person who hits the refusal is often not the person who can fix it —
which is why `errors.sales_invoice_posting_unconfigured` is worded at the bookkeeper rather than at
whoever pressed the button. FR-ONB should seed `sales_posting_account` when it seeds the chart;
`ledger_account.default_vat_code` makes that mechanical (§4 — seeding is the direction that column
legitimately supports).

**Known gaps, each with a trigger.**

- **A pre-existing bug was found and fixed on the way.** `api.expenses.repository.purchase_journal`
  queried `ledger_journal.type`; the column is `journal_type` and never was anything else, so
  **every attempt to post an expense would have raised** `column "type" does not exist`. Undetected
  because the DB-backed suite is skipped without Postgres and `tests/expenses/test_posting.py`
  drives a fake repository. Fixed here with a comment saying so. A sweep of the other application
  SQL against the migrations found no second instance — but the sweep was a heuristic script, not a
  substitute for running the queries.
- **Nothing consults the credit limit yet.** The balance now exists; the check does not. It belongs
  in `prepare()`, where a refusal is still free, and it needs a policy decision this ADR does not
  make — whether an over-limit invoice is refused or flagged.
- **Payments are not built.** The receivable is created and nothing clears it, so every debtor
  balance grows forever. FR-AR-009's payment matching and the bank reconciliation are what close it.
- **No `sales_posting_account` API.** The table is written by migration and by hand. FR-ONB's chart
  seeding is where the UI for it belongs, and inventing an endpoint here would have meant a
  permission Appendix A does not grade.
- **The PDF has never been opened by a real reader.** No PDF library is available in this
  environment, so the tests validate the format structurally — xref offsets against object
  positions, the object count against the trailer, the escaping, the encoding. That is what a
  parser would check, done by hand. It is not the same as a viewer opening it, and somebody should
  open one before this reaches a customer.
- **The database-backed tests have never run.** `tests/integration/test_sales_invoice_posting.py`
  covers the privilege boundary, the control-account biconditional, the reconciliation, both
  set-once triggers and the two reports — and, like every migration in this repo, **0040 has never
  been executed**: there is no Postgres and no Docker on the machine this was written on. Two SQL
  errors in the test helpers were caught by reading the migrations (`post_entry`'s argument order
  and `ledger.control_account_reconciliation`'s name); there may be more that only running would
  find. This is the first thing to run when a database is available.
