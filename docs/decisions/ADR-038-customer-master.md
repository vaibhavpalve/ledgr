# ADR-038: The customer master — a default, a verdict, and a stub that refuses to guess

- **Status**: Accepted
- **Date**: 2026-09-09
- **Implements**: FR-AR-006 (PRD §6.3), FR-ONB-003's VIES half (PRD §6.1)
- **Serves**: NFR-031 (no floating point), NFR-026 (graceful degradation), IAM-001–005 (tenancy),
  IAM-090 (audit), FR-TPL-013 (the recipient's language), FR-AR-003 (statutory invoice content)
- **Constrained by**: ADR-012 (no invented permissions), CMP-001 (seven-year retention)
- **Related**: [ADR-037](ADR-037-sales-invoices.md) — the snapshot columns this populates and the
  promise this keeps; [ADR-030](ADR-030-document-storage.md) — the same adapter shape for an
  external service; [ADR-019](ADR-019-client-switcher.md) — the search ranking reused here

## Context

> **FR-AR-006.** Customer master with KvK number, VAT number, Peppol participant ID discovery,
> payment terms, credit limit, preferred delivery channel and language.
>
> **FR-ONB-003.** Capture and validate BTW-identificatienummer and OB-nummer; validate EU VAT
> numbers via VIES.

ADR-037 §6 left an explicit promise: the customer's name, address, country and VAT number are
snapshotted onto `sales_invoice`, and "FR-AR-006 will **populate** these columns; it does not
replace them." This change keeps that promise rather than revisiting it.

Two foundations were already in place. `api.authz.matrix.MANAGE_CUSTOMER` already declares
`manage customer` — Appendix A has no row for customers, so it lives in `EXTENSION_CAPABILITIES`
with its reasoning attached, and ADR-012's rule against invented permissions cost nothing here.
And `pg_trgm` (0018, 0031) already serves the switcher's search, so the customer picker needed no
new infrastructure.

## Decision

### 1. A customer record is a default; the invoice is the record

    customer          what we believe about this counterparty TODAY
    sales_invoice.*   what was asserted to them ON A DATE, frozen at issue

`sales_invoice.customer_id` is added **beside** the snapshot, not instead of it, and is nullable
permanently. The pointer is provenance — "every invoice to this customer" — and the snapshot is
what the document says.

This is the decision everything else follows from. A customer who moves next year must not silently
rewrite the address on a document already filed with the Belastingdienst, so nothing ever joins
through `customer_id` to render an invoice. Migration 0039 extends 0037's freeze to `customer_id`
itself: re-pointing an issued invoice at a different customer would rewrite its provenance while
every visible field stayed identical, which is the quietest possible corruption of a statutory
record.

**A one-off customer stays invoiceable.** `customer_id` is optional on the create-draft path and the
typed-in fields still work. The master is a convenience that fills the document in, never a
prerequisite for invoicing somebody once. Supplying *both* is refused (`CustomerDetailsConflict`,
422) rather than resolved: either resolution is defensible, which is exactly the problem — whichever
the code picked silently would be wrong for somebody, on a document that outlives the request.

### 2. `customer_language` is snapshotted too, and FR-TPL-013 becomes true

FR-AR-006 lists "language" and FR-TPL-013 says the invoice's legal wording is rendered in the
**recipient's** language. Article 226(11)'s statement of *why* no VAT was charged is legal content,
not presentation — a reverse-charged supply obliges the buyer to account for the VAT and they can
only know because the invoice says so.

So the language joins the snapshot block and is frozen with it. A customer switching to English next
year must not restate an invoice already in their hands.

This also **corrected an existing defect**. `InvoicingService._build_view` was rendering the wording
in the language of whoever was looking, while `_view_json`'s docstring already claimed the opposite
("`legal_wording` follows the customer (FR-TPL-013) and is resolved upstream in the service"). The
`language` parameter is now gone from `issue`, `credit` and `view` entirely: the document carries
its own, and nobody clicking a button decides what language a customer's invoice is in.

### 3. VIES returns a verdict, not a boolean — and `unavailable` is one of them

This is the decision with an assessment attached to it.

VIES is not one service. It is a thin federating layer in front of 27 national registers and it
fails **per member state**, without notice, routinely. NFR-026 settles what that has to mean:

| Status | Meaning |
|---|---|
| `unchecked` | nobody has asked. Not a judgment. |
| `syntax_invalid` | cannot be a VAT number; no call was made |
| `valid` | the register confirmed it |
| `invalid` | the register denied it |
| `unavailable` | VIES could not answer. **Says nothing about the number.** |

A VIES outage never fails a save. The customer is stored and the outage is a column. The alternative
makes an unrelated failure in Brussels look like the user's mistake, and their only recourse is to
clear a field that was correct.

The corollary matters more: **`unavailable` must never become `valid`.** Every failure path in
`RestViesValidator` — timeout, 5xx, an error page served with a 200, a member state reporting itself
down inside an otherwise successful body, a response with no validity flag — resolves to
`UNAVAILABLE`. `valid` is returned only on an explicit `isValid: true`. `ViesStatus.permits_zero_rating`
is a property rather than a comparison each caller writes, because `!= INVALID` is the tempting and
wrong way to ask, and it would treat both `unchecked` and `unavailable` as good enough to zero-rate
an intra-Community supply.

**The check is stored, not recomputed.** For `btw_icp`, zero-rating depends on the customer holding a
valid number, and the supplier's defence is the evidence that they checked — on a date, with a
result, and with VIES's own consultation number where one was issued. That is a historical fact
about a moment, the same argument CMP-014 makes for effective-dated rates and 0037 for frozen VAT
totals. Migration 0039 enforces the pairing: a verdict without a date is refused, and so is a verdict
about a number that is not there.

**No checksums.** Several member states' numbers carry a check digit and the Dutch one famously used
to — the old BTW-identificatienummer was a BSN plus a modulus-11 digit. Implementing that would be
actively wrong: since 2020 the number issued to a sole trader is *random*, precisely so it no longer
discloses a BSN, and it does not satisfy the eleven-proef. A checksum would reject the numbers the
Belastingdienst has been issuing for years. Format only; the register is the authority on existence.

`EL` is Greece, not `GR`, and there is no silent alias — a lookup that quietly rewrote the country
would hide the one case where it mattered. `GB` is out and `XI` is in (Windsor Framework).

### 4. The Peppol stub says `not_configured`, and that is not `not_registered`

FR-AR-006 asks for participant ID *discovery*; FR-AR-005 puts Peppol in P2. The stub therefore
performs no lookup — and the only interesting decision is what it is allowed to *say*.

The tempting stub returns "not registered" and lets everything downstream carry on. It is wrong, and
invisibly so. "This customer is not on the Peppol network" correctly routes an invoice to email;
"this deployment has not been configured to look" routes it to email while the customer waits for it
on Peppol — and once ViDA lands (PRD §2.1), while a structured invoice was legally required. So
`NOT_CONFIGURED` is its own outcome, `DiscoveryStatus.is_deliverable` is true only for `REGISTERED`,
and nothing collapses them.

Two things are already built and tested because they are judgments rather than network code:

- `candidate_identifiers` — KvK under scheme `0106` first, then the VAT number under `9944`. KvK
  first because that is how a Dutch business is ordinarily registered, and because a Dutch VAT number
  identifies a *fiscal* entity that may cover several registered businesses, making a `9944` match
  the weaker evidence. An unparseable VAT number is not offered at all: it would be a lookup
  guaranteed to miss, recorded as absence from the network.
- `supports_document_type` in the interface, because a participant registered to receive orders but
  not invoices is real and common, and treating "found in the SML" as "can be sent an invoice" is the
  standard way to have a delivery rejected by the receiving access point.

The identifier is still writable directly. Discovery is not the only way one arrives — a customer can
simply say what theirs is — so the stub limits discovery, not the field.

### 5. The address is structured, and rendering is one-way

0039 stores street, line 2, postcode, city and country separately, and
`api.customers.address.format_address` renders them into the single text column 0037 snapshots. The
rendered string is never parsed back.

The same argument 0038 made for the supplier's own address: EN 16931 wants the parts separately for
P2's Peppol and does not accept a blob, and "is the address blank" is satisfied by a single space
while "are street, postcode and city all present" is the question Wet OB art. 35a(1)(e) asks.

An address is **not** required to save a customer — FR-AR-006 does not ask for one and a half-known
customer is a legitimate record. The refusal lands where the document is made
(`CustomerAddressIncomplete`, 422), which is where FR-AR-003 actually bites.

### 6. Payment terms produce a date; the credit limit produces nothing yet

`payment_terms_days` becomes the invoice's `due_date` at draft creation — calendar days, which is
what "30 dagen netto" means and what BW art. 6:119a counts. An explicit `due_date` on the request
still wins: a one-off arrangement on a single invoice is ordinary, and the alternative is editing the
customer, raising the invoice and editing the customer back.

The CHECK is `0..365`, deliberately not the statutory 60. A B2B term beyond 60 days is lawful where
expressly agreed and not "kennelijk onbillijk" — a judgment about a contract this system has not
seen. Encoding 60 would refuse legal terms and push people to record something false.

**`credit_limit` is stored and enforced by nothing.** Said plainly rather than left to be discovered:
there is no AR balance to compare it against, because ADR-037 §"Known gaps" records that issuing an
invoice posts nothing to the ledger. A limit checked against a receivables figure that does not exist
would be a control in name only. It is `numeric(19,2)` (NFR-031), and `NULL` means "no limit set" —
which is emphatically not `0`, a customer who may have no credit at all. A screen showing the two
alike would put a trading halt on everybody nobody has assessed. See the gaps for the trigger.

### 7. A customer is archived, never deleted

No DELETE grant, no delete policy, no route. A customer row is pointed at by statutory documents
CMP-001 keeps for seven years, so "who was this invoice made out to" has to stay answerable for all
of them. This is 0001's own posture in its own words — "the privilege to attempt one does not exist
for `ledgr_app`" — applied one table further out.

An archived customer is still readable by id and still findable with `include_archived`, but is out
of the default picker and cannot be edited or invoiced. Restoring is its own route rather than a flag
on the update body, so resuming trade with a counterparty somebody deliberately archived is an act
that happened, and reads as one in the audit log.

### 8. Reads require `manage customer`, and that has a consequence

There is no `view customer` permission and none was invented (ADR-012). Appendix A grades neither,
and `MANAGE_CUSTOMER` declares only the full permission.

**So a Viewer cannot list customers.** That is stated here rather than papered over, because the
honest fix is a PRD change to Appendix A — a row for customers, graded per role — not a permission
minted in this package. The conformance test (`test_appendix_a_conformance.py`) exists precisely to
stop that drift, and routing around it would make the matrix a description of the code rather than of
the document.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Join the customer onto the invoice instead of snapshotting | ADR-037 §6. A customer who moves would silently restate a document already filed. |
| Make `customer_id` required on a sales invoice | A one-off customer would need a master record first. 0037's snapshot columns are NOT NULL and this one is not — the correct way round. |
| Let `customer_id` and typed-in details both be sent, master wins | Silently wrong for somebody on a statutory document. Refusing is the only answer that cannot be quietly incorrect. |
| Leave `customer_id` editable on an issued invoice | Rewrites provenance while every visible field stays identical. |
| Read the recipient's language from `customer.language` at render time | A customer switching language would restate the legal wording on an invoice already in their hands. |
| A boolean `vat_number_valid` | Collapses "nobody asked", "the register said no" and "the register did not answer" into one bit, and the third silently reads as the second. |
| Fail the save when VIES is down | NFR-026. An outage in another member state becomes the user's problem, and their recourse is to clear a correct field. |
| Recompute VIES validity on demand rather than storing it | The evidence for an intra-Community supply is a check on a date, not a property recomputable later. |
| Implement per-country VAT checksums | The modern Dutch number is random by design and fails the eleven-proef. It would reject numbers the Belastingdienst issues. |
| Alias `GR` to `EL` silently | Hides the one case where the difference matters. |
| Have the Peppol stub return `not_registered` | Routes invoices to email while the customer waits on Peppol, and after ViDA while a structured invoice was legally required. |
| Return 501 from the discovery route | The route works; "no directory is configured" is information the client needs to fall back deliberately. |
| A `view customer` permission so Viewers can read | ADR-012. Appendix A grades no such row, and inventing one is the drift the conformance test exists to catch. |
| Unique index on `kvk_number` per administration | Two branches of one customer share a KvK number — each vestiging has its own vestigingsnummer but the register number is the legal entity's. It would refuse a legitimate second delivery address. |
| Hard DELETE for a customer with no invoices | The condition is true until it isn't, and a row that can be deleted today is a row somebody deletes the day after it is referenced. |
| Store the credit limit as a float | NFR-031, in one cast. |

## Consequences

**Easier.** FR-AR-005's PDF and Peppol senders read `delivery_channel`, `invoice_email` and
`peppol_participant_id` from one place, and `is_deliverable_over_peppol` answers "can this actually
be honoured" without reasoning about it at the send site. FR-VAT-006's ICP declaration has
`vat_number_status` and a dated consultation number already on the row. P2's Peppol work is a
`PeppolDirectory` implementation and a config value — no new route, permission or audit action.

**Harder.** A Viewer cannot read customers (§8). Editing a customer is deliberately inert with
respect to documents already issued, which is correct and will surprise somebody at least once.

**Known gaps, each with a trigger.**

- **The credit limit enforces nothing.** ~~There is no receivables balance to check it against~~ —
  **there is one as of 2026-09-09** ([ADR-039](ADR-039-invoice-posting.md)): issuing posts to the AR
  sub-ledger, so `ledger.subledger_balance` now answers what this customer owes. The *check* is
  still not built. It belongs in `SalesPostingService.prepare()`, where a refusal is still free, and
  it needs a policy decision nobody has made — whether an over-limit invoice is refused outright or
  issued with a flag. Until then the field is recorded and reported, and this remains the record
  that it is not a control.
- **Nothing consults `vat_number_status` before zero-rating.** FR-AR-003's gate still requires only
  that a customer VAT number be *present* for `btw_verlegd` and `btw_icp`, not that VIES confirmed
  it. Tightening that is FR-VAT-006's ICP work (S, P3) and is a change to
  `api.invoicing.statutory`, not here. `permits_zero_rating` exists and is unused on purpose — it is
  the seam, written where the knowledge is.
- **No re-validation schedule.** A number confirmed today can be deregistered tomorrow, and nothing
  re-checks. `vat_number_checked_at` is what a periodic job would read; the job is not built, and an
  `unavailable` verdict is retried only when somebody asks.
- **VIES rate limits are not handled.** The Commission throttles per caller and this makes one
  request per customer save. A bulk import can pass `validate_vat_number=False`, which is the only
  mitigation present; a queue with backoff belongs with the import feature that needs it.
- **No web UI and no shared type.** `packages/shared-types` gains no `CustomerView`, matching what
  ADR-037 did for invoices: a type nothing imports is a second shape to keep in step for no reader.
  It should be added with the screen that consumes it.
- **The database-backed tests are skipped in this environment.** `tests/integration/test_customer_master.py`
  is written against 0039's constraints, the no-DELETE grant, the `updated_at` trigger, the tenant
  coherence trigger and the extended freeze — and `tests/integration/test_customer_isolation.py`
  covers all eight routes for IAM-005 — but both only *execute* under
  `TENANT_ISOLATION_TESTS_ENABLED=1`, and no Postgres was available here. **Migration 0039 has
  therefore never been applied**, which is a stronger statement than ADR-037's equivalent gap: its
  SQL is unexecuted, not merely its triggers unexercised. The in-memory
  `InMemoryCustomerRepository` reimplements the constraints so the service tests are not vacuous,
  but the two implementations have not yet been checked against each other. That is the single
  largest untested surface in this change and the first thing to run when a database is available.
