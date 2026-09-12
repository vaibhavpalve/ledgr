# ADR-037: Sales invoices — the number, the groups, and the words

- **Status**: Accepted
- **Date**: 2026-09-09
- **Implements**: FR-AR-001, FR-AR-002, FR-AR-003, FR-AR-004 (PRD §6.3)
- **Serves**: NFR-031 (no floating point), NFR-032 (idempotency), IAM-001–005 (tenancy),
  IAM-090 (audit), FR-LOC-001/FR-TPL-013 (both languages, recipient's language on the document)
- **Constrained by**: CMP-014 (effective-dated rules), ADR-012 (no invented permissions)
- **Related**: [ADR-027](ADR-027-effective-dated-tax-rules.md) — the treatments and rates this
  reads; [ADR-022](ADR-022-ledger-bounded-context.md) — why issuing posts nothing;
  [ADR-032](ADR-032-expense-form.md) — the purchase side's opposite VAT direction

## Context

> **FR-AR-001.** Create, edit, send and credit sales invoices with line items, quantities, unit
> prices, discounts and per-line VAT treatment.
>
> **FR-AR-002.** VAT handling: 21%, 9%, 0%, exempt, reverse charge domestic, intra-Community
> supply, export, margin scheme. Correct legal wording rendered per treatment.
>
> **FR-AR-003.** Invoices comply with Dutch statutory invoice content requirements; the system
> blocks sending if a mandatory field is absent.
>
> **FR-AR-004.** Sequential, gapless invoice numbering per year, configurable prefix; issued
> numbers cannot be reused.

Two foundations were already in place and neither needed extending. `vat_treatment` (migration
0028) already holds exactly FR-AR-002's eight treatments — its own comment says so — including
`btw_marge`. And Appendix A already carries `create sales_invoice` and `send sales_invoice`, so
ADR-012's rule against invented permissions cost nothing here.

## Decision

### 1. A draft is a document; an issued invoice is a statutory record

    draft ──edit──▶ draft ──issue──▶ issued ──credit──▶ a NEW issued invoice
                                                        pointing at the first

A draft is freely editable, carries no number, and may be deleted. Issuing freezes it: the content
cannot change, the row cannot be deleted, and the number can never be reused. A mistake is
corrected by a credit note.

That is CLAUDE.md's second rule — corrections via reversing entries, never mutation — applied one
layer above the ledger, and for the same reason. An invoice is a claim made to somebody outside the
business; once it has been made, the record of what was claimed has to survive the correction of
it. Migration 0037 enforces it in triggers rather than in the service, so a second write path
cannot get it wrong either.

There is deliberately no `cancelled` status. A cancelled invoice is an issued invoice with a credit
note against it, because FR-AR-004 says the number cannot be given back.

### 2. The number is allocated at issue, not at creation

FR-AR-004 wants the series gapless. A number handed out when a draft is created disappears when the
draft is abandoned, and every abandoned draft is then a hole somebody has to explain to an
inspector. So `invoice_number` is NULL for a draft and allocated in the transition.

The allocation is `update invoice_sequence ... returning`, the same shape `journal_entry_validate`
uses for FR-GL-013, and the two reasons carry over exactly:

- it takes a row lock, so concurrent issues serialise instead of colliding on the unique constraint;
- it is a **table update, not a sequence**. A rolled-back transaction takes the increment back with
  it. `nextval` would not, and that is precisely how a gapless series acquires gaps.

The cost is honest: issuing serialises per administration per year. That is the right trade —
gaplessness *is* the requirement, and issuing an invoice is not a hot path.

`invoice_number_gaps()` reports holes that cannot exist. Kept for the reason 0020 gives about its
own gap report: a check that can only ever be empty is the check on the thing that makes it empty,
and an inspector asking "prove it" gets an answer rather than an assurance.

**The order inside `issue` is load-bearing.** Rates are resolved, then FR-AR-003's gate runs, then
the number is allocated, then the VAT totals are frozen. Both things that can fail happen before the
one that cannot be undone — a refused invoice must not have burned a number.

### 3. VAT is computed per treatment group, never per line

This is the decision with money in it.

Twelve lines of €0.05 at 21% are twelve lots of €0.0105. Rounded per line that is 12 × €0.01 =
€0.12; rounded on the group total it is round(€0.63) = €0.13. Neither is a rounding error — they are
different answers to different questions.

EU VAT Directive Art. 226 settles which question an invoice asks: it must show, **per rate**, the
taxable amount and the VAT on it. So the group is the answer, and it is also the figure that reaches
the aangifte (FR-VAT-001) — a per-line total would put one number on the document and a different
one on the return.

The consequence is stated in the code and in the API: **a line has a net amount and deliberately no
VAT amount.** Nothing returns one, so nothing downstream can sum a per-line column and disagree with
the invoice.

Line *net* is still rounded at the line, once, because that is the figure printed on the line and
added up by hand. Carrying the unrounded product into the group would make the printed lines
disagree with the printed subtotal by a cent, which is the single most common reason somebody
telephones about an invoice.

### 4. Zero is not one thing, and the margin scheme is not zero either

Six of the eight treatments charge the customer nothing, and they are not interchangeable:

| Treatment | Why no VAT | Consequence |
|---|---|---|
| `btw_0` | a rate that is 0% | taxable, at nothing |
| `btw_vrijgesteld` | outside the VAT system | different rubriek |
| `btw_verlegd` | the **buyer** accounts for it | customer VAT number mandatory |
| `btw_icp` | taxable in the buyer's member state | customer VAT number mandatory |
| `btw_export` | outside the EU | — |
| `btw_marge` | VAT is due on the **margin** | stating a rate is unlawful |

Same number, six legal meanings, different wording and different rubrieken. Collapsing them into
"rate 0" is what makes a return wrong in a way the ledger cannot see, so the treatment travels with
the amount everywhere in `api.invoicing.vat`.

The margin scheme carries `rate = None`, not `Decimal(0)`. Under the margeregeling VAT is due on the
margin rather than the sale price, and stating that VAT separately on the invoice is not permitted —
"there is no rate on the sale price to state" is a different fact from "the rate is nought", and
`VatGroup` refuses to be constructed the other way.

This is also why `api.expenses.model.VatTreatment` **excludes** `btw_marge` and this module includes
it. On the purchase side, extracting VAT from a gross receipt under the scheme would compute the tax
on the whole amount and overstate it (migration 0033 refuses it with a CHECK). On the sales side the
scheme is the seller's own and the invoice simply shows no VAT. Same code, opposite correct
handling, which is why neither module reaches into the other.

### 5. The wording is the legal content, and it is in the recipient's language

Art. 226(11) requires an invoice to **state** the reason no VAT has been charged. An invoice showing
0.00 with no explanation is not compliant, and the customer cannot use it: a reverse-charged supply
obliges the buyer to account for the VAT, and they can only know because the invoice says so.

The words come from the shared catalogue in the **recipient's** language (FR-TPL-013), not the
caller's — a Dutch bookkeeper invoicing a German customer in English gets English wording on the
document while their own screen stays Dutch.

"BTW verlegd" is carried verbatim into the English version rather than translated away, with a
gloss. That follows FR-LOC-001c's argument about BTW generally, and it matters more here than in the
UI: a foreign customer's own tax authority looks for the Dutch phrase.

### 6. The customer is snapshotted onto the invoice

Name, address, country and VAT number are columns on `sales_invoice`, not a join to a customer
master. FR-AR-006's master is not built, but this would be the right shape even when it is: an
invoice is a statutory record of what was asserted on a date, and a customer who moves next year
must not silently rewrite the address on a document already filed. FR-AR-006 will **populate** these
columns; it does not replace them.

### 7. Discounts are a percentage per line

`discount_percent numeric(9,4)`, applied to quantity × unit price. A percentage is what appears on
an invoice and what "discounts" on a line item usually means, and it composes cleanly with the other
two factors. A fixed-amount discount is expressible as a line of its own; supporting both fields
would mean deciding what they mean together, which is a rule nobody would remember.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Allocate the number when the draft is created | Every abandoned draft becomes a gap in a series FR-AR-004 requires to be gapless. |
| Use a Postgres `sequence` for the number | `nextval` does not roll back. That is exactly how a gapless series acquires gaps. |
| Let the caller supply the number | It could fill a hole, restart the series, or duplicate a number in another year. The unique constraint catches only the last. |
| Allow an issued invoice to return to draft | Frees a number that has been quoted to somebody (FR-AR-004). |
| Edit an issued invoice | It changes what the customer was told without changing what they hold. FR-AR-001's correction path is a credit note. |
| Sum VAT from a per-line column | Differs from the group total by cents, and Art. 226 asks for the per-rate figure. Two numbers, one document. |
| Round the line net only at the group | The printed lines would not add up to the printed subtotal. |
| Treat the six no-VAT treatments as "rate 0" | Same number, six legal meanings, different rubrieken. A return wrong in a way the ledger cannot see. |
| Give the margin scheme `rate = 0` | Conflates "no rate may be stated" with "the rate is nought". |
| Reuse `api.expenses.vat` with a direction flag | The flag would eventually be wrong somewhere. They share the rounding rule and nothing else. |
| Join the customer from a master record | An invoice already sent would silently restate when the customer moved. |
| Check statutory content at send rather than issue | FR-AR-005 has three delivery channels; that would be three checks. Issue is one, and it is the point after which editing is impossible anyway. |
| Post to the ledger on issue | ADR-022's bounded context, and the expense side's precedent (ADR-033 is a separate decision from ADR-032). Out of FR-AR-001..004's range; see the gaps. |

## Consequences

**Easier.** FR-AR-005's PDF and Peppol renderers read `InvoiceView` and need no arithmetic of their
own — the groups, the wording and the totals are already computed and frozen. FR-AR-006's customer
master populates existing columns. FR-VAT-001's return reads `sales_invoice_vat_total`, which is
already grouped the way a rubriek is.

**Harder.** Issuing serialises per administration per year, and an issued invoice cannot be touched.
Both are the requirements rather than side effects, but they are real constraints on anything built
on top.

**Known gaps, each with a trigger.**

- **THE LEGAL WORDING STILL NEEDS A DUTCH TAX ADVISER — and this cannot be closed from inside the
  codebase.** Nothing in this system can check that a statement is the one the statute requires;
  only a person qualified in Dutch VAT can. What was built instead is everything that makes that
  review a short act rather than a project: `data/invoicing/wording-review.json` records, per
  treatment, the statutory provision the words answer to, a status, a confidence and the open
  questions — the same shape the glossary uses for FR-LOC-001c — and
  `make review-invoice-wording` renders it as a sheet, least settled first. Signing one off is an
  edit to that file (status, name, date) taking effect on the next restart with no code change;
  `REVIEWED` is derived from it, and "reviewed" requires a **named person on a date**, so a status
  alone cannot manufacture an assurance. Until then every invoice reports
  `wording_is_provisional: true`.

  Three entries are `flagged` with substantive questions, and two of them may change the model
  rather than the words: an exemption may have to cite the specific Wet OB art. 11 sub-paragraph
  (per-supply data, not a per-treatment string), and art. 226(14) names **three** margin schemes
  the invoice must distinguish where `btw_marge` is currently one treatment. Both are ruleset
  questions for migration 0028, not wording ones.

  This deliberately does **not** block issuing. `check_translations.py` makes the argument at
  length about the glossary: a gate on a human process nobody has scheduled gets disabled, and the
  checks worth having go with it.
- ~~**The supplier's address has nowhere to come from.**~~ **Closed 2026-09-09 by migration 0038**,
  which adds `address_line1`, `address_line2`, `postal_code`, `city` and `country` to
  `administration`. Structured rather than one text field, unlike the customer's snapshotted
  address, because EN 16931 wants the parts separately for FR-AR-005's Peppol (P2) and because "is
  the address blank" is a question a single space answers while "are the street, postcode and city
  all present" is the one art. 35a(1)(e) asks. The columns are nullable and FR-AR-003's gate is
  what refuses to issue until they are filled — a backfilled placeholder would have put a
  fabricated address on a statutory document, which is worse than having none.
- ~~**Nothing is posted to the ledger.**~~ **Closed 2026-09-09 by migration 0040 and
  [ADR-039](ADR-039-invoice-posting.md).** Issuing now posts the entry and stores the rendered PDF
  in the same transaction, through `LedgerService` as predicted. Two things this ADR expected turned
  out differently and are argued there: the expense side's capture/post split was **not** reused —
  it exists for an SoD boundary that has no equivalent on the sales side — and the posting is
  authorised by `send sales_invoice` rather than `post journal_entry`, because Appendix A gives the
  Invoicer the first and not the second.
- ~~**Sending is not built.**~~ **Closed 2026-09-09 by migration 0041 and
  [ADR-040](ADR-040-invoice-delivery.md)** for the e-mail half. Delivery is a separate act from
  issuing — deliberately the opposite call from ADR-039's atomic posting, because an e-mail cannot
  be rolled back — with per-channel status on `invoice_delivery` and Peppol behind an adapter seam.
  Peppol itself remains P2.
- **Credit notes are whole-invoice only.** A partial credit — three lines of eight — is expressible
  by editing the draft credit note before issuing it, but the service creates it with every line
  negated. A partial-credit API belongs with FR-AR-013's bad-debt write-off.
- **The database-backed tests are skipped without Postgres.** The isolation suite
  (`tests/integration/test_sales_invoice_isolation.py`) is written and registered against all six
  routes, so `test_isolation_coverage.py` passes, but it only *executes* under
  `TENANT_ISOLATION_TESTS_ENABLED=1`. The numbering and immutability triggers are therefore
  **unexercised** in this environment: nothing here has yet observed 0037 allocate a number or refuse
  an edit. That is the single largest untested surface in this change.
