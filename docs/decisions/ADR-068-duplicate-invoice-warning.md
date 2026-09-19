# ADR-068: Duplicate-invoice warning on sales-invoice drafts

- **Status**: Accepted
- **Date**: 2026-09-19
- **Implements**: SI-12 (TRD, "Differentiators beyond the PRD") - warn when creating an invoice
  very similar to a recent one for the same customer
- **Extends**: [ADR-034](ADR-034-duplicate-detection.md) - the same warn-never-block pattern,
  applied to sales invoices
- **Constrained by**: FR-AR-003 (the statutory gate must stay the only thing that blocks an
  issue), IAM-001..005 (tenant predicate on every query), NFR-031 (no floating point)

## Context

A bookkeeper who is interrupted mid-invoice, or who copies last month's invoice to start this
month's, can bill the same customer for the same work twice. The second document is a valid
invoice: it passes the statutory gate, takes the next number from the gapless series
(FR-AR-004) and reaches the ledger. The mistake is then corrected by a credit note, which is
correct but leaves both documents in the customer's hands.

## Decision

### 1. Warn, never block

Exactly ADR-034's position, for the same reason. A retainer billed on the 1st and the 15th, or
two identical call-outs, are ordinary. Nothing here raises; `InvoiceView.can_be_issued` does not
consult the warnings and a test asserts it, at the service layer and on the view itself.

### 2. What "very similar" means

Same customer, invoice dates within 30 days of each other in either direction, and then:

| Strength | Rule |
|---|---|
| `identical` | the same lines - description (case- and whitespace-insensitive), quantity, unit price, discount - in any order |
| `same_total` | different lines, equal net total |

The `same_total` tier exists for the re-typed invoice whose wording drifted. It is worded as a
possibility rather than a finding, since two different jobs can cost the same.

The **net** total is compared, not the gross. It needs no VAT-rate lookup, cannot disagree with
itself across a rate change, and two invoices with equal lines always have equal nets. All
amounts are `Decimal`.

The same customer means the same `customer_id` when both invoices carry one, or else the same
name normalised by ADR-034's rule. Both are checked because a one-off customer has no id, and an
invoice typed out by hand for somebody later added to the customer master is still the same
customer. A name that normalises to nothing matches nothing (ADR-034 section 5).

### 3. What is and is not compared

- **Drafts only, and only invoices.** The question is "should I issue this?". An issued invoice
  and a credit note are not checked, which also means `issue` pays no extra query.
- **An invoice with no lines is not checked**: two empty drafts are two forms, not two claims.
- **Candidates include other drafts.** The duplicate is often the half-finished draft somebody
  forgot they had started. Credit notes are excluded as candidates - matching one against the
  invoice it reverses is correct behaviour, not a duplicate.
- Results are ordered strongest-then-nearest and capped at five.

### 4. Where it lives, and no new endpoint

The warnings ride on the existing `GET .../sales-invoices/{id}` view (and so also on the create
and edit responses, which return through the same `view()`), as `duplicate_warnings[]`. There is
no new route, so there is no new surface to authorize: the existing `create sales_invoice`
check governs it.

The rule is a pure module, `api.invoicing.duplicates`, over an `InvoiceFingerprint` value (the
analogue of ADR-034's `ExpenseTriple`). The database is asked for an over-approximate pool and
Python applies the exact rule, so there is one definition of "similar" and no SQL twin to keep
in step. The pool query filters on `administration_id`, a date window and a customer match, and
is bounded by `LIMIT 50`; it is served by the existing `sales_invoice_administration_idx`, so
**no migration is needed**.

### 5. Tenancy

The pool query carries the administration predicate in the WHERE clause and runs under RLS. The
warning carries only the other invoice's own facts (id, reference, date, net, status) - all from
the same administration and the same customer. There is nothing in it about a person, so
unlike ADR-034 there is no "does not name the other submitter" question here.
`tests/integration/test_invoice_duplicate_isolation.py` seeds an identical invoice in each of
two organizations and asserts neither pool contains the other's.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Refuse to issue a duplicate | Refuses legitimate repeats and teaches people to work around it. |
| A unique constraint on customer, date and total | Same, and enforced where it cannot be overridden. |
| Compare gross totals | Needs a rate lookup for every candidate and is no more precise: equal lines give equal nets. |
| Trigram similarity on line descriptions | A tunable guess in SQL that Python cannot reproduce; exact matching on the description plus the total tier covers the case that matters. |
| Check again at issue time and in the list | Issue is where a warning is least useful (the person already chose) and the list has no draft-level context. |
| A separate `GET .../duplicates` endpoint | Another route to authorize and test, for data the view already has every reason to carry. |

## Consequences

**Easier.** A client shows `duplicate_warnings` beside the draft with one field; the message is
already translated (FR-UX-007) and nothing gates the issue button on it.

**Harder.** Opening a draft costs one bounded query plus one for its candidates' lines.

**Known gaps.**

- **Verified against the native local Postgres** (`scripts/dev-stack.ps1`, no Docker), not only
  unit-tested: `tests/integration/test_invoice_duplicate_isolation.py` runs the pool query as
  `ledgr_app` under RLS - two tenants holding an identical invoice never see each other's, the
  positive case returns lines and a net total summed from the generated `line_net`, and the
  case-insensitive customer match works. Running it caught a bug in the test's own helper (a
  parameter name clash) that a fake could not have.
- **The pool's customer-name match is `lower(btrim(name))`**, narrower than the Python
  normalisation, which also strips edge punctuation. A name differing from an earlier invoice only
  by a trailing full stop is not fetched, so it is not warned about. The customer id path is exact.
- **No UI.** `packages/shared-types` carries the type; rendering it is client work.
- **The window (30 days) and cap (5) are constants**, with no per-administration setting, for the
  same reason as ADR-034's similarity threshold.
