# ADR-040: Delivery is a channel behind an adapter, and sending is not issuing

- **Status**: Accepted
- **Date**: 2026-09-09
- **Implements**: FR-AR-005 (PDF by e-mail, delivery status per channel), FR-LOC-003 (e-mail
  localised per recipient)
- **Serves**: NFR-026 (graceful degradation), IAM-090 (audit), FR-TPL-017 (the stored rendering is
  what goes out), CMP-009 ("we never sent that"), SEC-020 (TLS), PRIV-010/011 (EU sub-processors)
- **Constrained by**: CLAUDE.md non-negotiable #4 (integrations behind adapters), ADR-012 (no
  invented permissions)
- **Related**: [ADR-039](ADR-039-invoice-posting.md) — why *that* one is atomic with issuing and
  this one is not; [ADR-038](ADR-038-customer-master.md) — where the address and the channel
  preference come from; [ADR-037](ADR-037-sales-invoices.md) — the gap this closes

## Context

> **FR-AR-005.** Send as PDF by email in P0; structured e-invoice over Peppol (BIS Billing 3.0 /
> NLCIUS, EN 16931) added in P2. **Delivery status tracked per channel.**

ADR-037 recorded "sending is not built" as a gap. Everything it needs now exists: the PDF is stored
at issue (ADR-039, FR-TPL-017), and the customer master holds a preferred channel and an invoice
address (ADR-038). What did not exist was any way to send mail at all — this is the product's first
outbound e-mail.

The brief was explicit about the shape as well as the feature: *design the delivery interface so
adding a channel later doesn't touch invoice logic*.

## Decision

### 1. Sending is its own act — the opposite call from ADR-039

ADR-039 made posting **atomic** with issuing. This makes delivery **separate**, and the difference
is not taste:

| | posting | delivery |
|---|---|---|
| what it is | a database write | an act by somebody else's server |
| on rollback | undone | **already happened** |
| consequence of coupling | none | a customer holds an invoice that does not exist |

Sending inside the issue transaction would hold a write transaction open across an SMTP round trip,
and — worse — a rollback after the provider accepted the message would leave a customer holding a
document this system has no record of. So delivery is its own endpoint, its own row, and its own
decision.

The cost is real and is the point of `invoicing.undelivered_invoices()`: an invoice can be issued
and not sent. Unlike `unposted_invoices()`, that report is **not** structurally empty — it is a work
list, because an invoice nobody sent is an invoice nobody will pay.

### 2. Three things differ per channel, and all three live on the adapter

This is the answer to the brief. Peppol is a different protocol carrying a different artifact to a
different kind of address, so the `DeliveryChannelAdapter` Protocol has exactly those three members
plus the channel it serves:

```python
channel: DeliveryChannel
required_artifacts: frozenset[ArtifactKind]
def address_of(recipient, *, override=None) -> str | None
async def deliver(request) -> DeliveryOutcome
```

`InvoiceDeliveryService` contains no `if channel is EMAIL` and never mentions an e-mail address.
Adding Peppol in P2 is: write a `PeppolInvoiceChannel`, register it in
`api.invoicing.routes.get_delivery_service`, teach something to produce `ArtifactKind.UBL`.

**The claim is executed, not asserted.**
`test_a_channel_this_package_has_never_heard_of_can_be_dispatched_over` defines a courier channel
*in the test file*, addressed by postal address, and drives a full dispatch over it with nothing in
`api/invoicing` changed.

Two sub-decisions fell out of writing it:

- **`Recipient` carries every address, not one string.** The tempting version has the service read
  `customer.invoice_email` and pass it. That works until the second channel, whose address lives in
  a different column and is validated by a different rule — at which point the service grows the
  branch this design exists to avoid.
- **The override goes to `address_of`, not into the `Recipient`.** The first draft substituted the
  override in the service via `dataclasses.replace` on a channel-keyed field map. mypy rejected the
  unpacking, which was lucky: that map *was* the channel branch, wearing a dict. Validating an
  address is channel-specific, so the override is now an argument to the adapter's own resolver —
  and a bad override returns `None` rather than falling back to the customer, because quietly
  sending somewhere the sender did not intend while reporting success is the worst outcome
  available.

`ArtifactKind.UBL` is declared and produced by nothing. That is the one-line difference between
"e-mail sends the PDF" and "each channel declares what it needs", and asking for it raises
`ArtifactNotAvailable` — a clear refusal rather than an e-invoice with no payload.

### 3. One row per dispatch, and `sent` is not `delivered`

FR-AR-005's "tracked per channel" rules out a status column on `sales_invoice`. Three shapes were
possible:

| shape | rejected because |
|---|---|
| per invoice+channel, updated | a bounce that was then resent shows only "sent". The bounce is the interesting event and it is gone. |
| per transport attempt | complete and unreadable; "has this been sent" becomes an aggregate. |
| **per dispatch** | one row per *human decision*. Transport retries increment `attempts`; a resend is a new row. |

So the current state of a channel is the latest row for it (`invoicing.delivery_state_of`), and the
bounce behind it survives.

The five states separate two facts that are easy to conflate:

    queued → sent → delivered
                 ↘ bounced

`sent` means the provider **accepted** it. `delivered` means it **arrived**. Collapsing them is how
somebody tells a customer "we sent it" about an invoice that bounced an hour ago.
`DeliveryStatus.reached_the_customer` is `is DELIVERED` and nothing else, written as a property
because `!= BOUNCED` is the tempting and wrong way to ask.

### 4. An outage queues; a rejection does not

NFR-026: a mail provider being down is not the caller's mistake. A failed hand-over returns
`DeliveryOutcome(accepted=False, retryable=True)`, the dispatch stays `queued` with a
`next_attempt_at`, and the endpoint answers **200 with a status** rather than 502 — which a user
would read as "the invoice is broken".

A **rejected recipient** is different: it carries a provider reference, so the conversation
succeeded and the address failed. That is `retryable=False` → `failed`, because retrying it produces
the same result forever while occupying the queue and looking like an outage. `UnsendableMessage`
(no recipient, no subject) is the same category — a defect, not weather.

`RETRY_BACKOFF` is four explicit intervals running out at about a working day. Past that, an invoice
that has not gone out is something a person should look at, not something a queue should keep
retrying quietly.

### 5. The transport is its own package

`api.mail` knows about messages and providers and not about invoices. FR-AR-010's dunning ladder,
FR-RPT-010's scheduled reports and FR-NTF-002's notification channel are the same act with a
different body; putting SMTP under `api.invoicing` would mean the second of those either imports
from invoicing or writes its own client — and the second client is where the TLS settings drift.

SMTP rather than a provider's HTTP API, because every EU provider speaks it and PRIV-010/011 make
choosing an EU sub-processor a compliance decision. There is **no plaintext option**: STARTTLS is
performed before authentication and a server that cannot do it is refused (SEC-020). There is also
no default host — a default would be that decision made by whoever wrote the line rather than by
whoever is accountable for it.

### 6. The e-mail is short, plain text, and in the recipient's language

FR-LOC-003. The language is the frozen `customer_language` snapshot (ADR-038), not the sender's UI
language and not the language of whoever pressed send.

Plain text with no HTML alternative: it renders identically everywhere, has no tracking pixel to
leak (PRIV-012), and offers no rendering surface to attack the reader through. The body says what is
attached, from whom, for how much and by when — the authoritative document is the PDF beside it, and
a body restating the invoice would be a second document to keep in step with the first.

The **From name is the supplier**, not LEDGR: a customer is receiving an invoice from their
supplier, and a From line naming the bookkeeping software is how an invoice reaches a spam folder.

### 7. The attached PDF is read, never re-rendered

FR-TPL-017. `DocumentService.original` verifies the hash on the way out, so what is attached is
provably the bytes stored when the invoice was issued. Sending does not go near the renderer, and an
invoice with no stored PDF (issued before ADR-039) is **refused** rather than rendered now —
producing one today for a document numbered last year would be a different document under the same
number.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Send inside `issue`, like posting | An e-mail cannot be rolled back; a failed transaction would leave a customer holding an invoice that does not exist. |
| One delivery status column on `sales_invoice` | FR-AR-005 says per channel, and one column cannot hold two. |
| One row per invoice+channel, updated in place | Loses the bounce, which is the event worth keeping. |
| One row per transport attempt | Complete history, unreadable answer to "has this been sent". |
| One `sent` state | "We sent it" about an invoice that bounced. |
| Service reads `customer.invoice_email` and passes a string | Works until the second channel, then becomes the branch this design exists to remove. |
| Override substituted into `Recipient` by the service | A channel-keyed field map is a channel branch wearing a dict — and mypy caught it. |
| Bad override falls back to the customer's address | Sends somewhere the sender did not intend and reports success. |
| 502 when the provider is down | NFR-026, and a user reads it as "the invoice is broken". |
| Retry a rejected recipient | Retries a mistake forever while looking like an outage. |
| HTML e-mail | A tracking-pixel surface (PRIV-012) and a rendering surface, for a message that says four things. |
| From name = LEDGR | The customer is being invoiced by their supplier, not by us. |
| Re-render the PDF at send time | Precisely FR-TPL-017's failure. |
| A `deliver invoice` permission | Appendix A's "Send sales invoices" *is* this act (ADR-012). |
| SMTP client in `api.invoicing` | The next caller writes a second one. |

## Consequences

**Easier.** FR-AR-010's dunning reminders and FR-RPT-010's scheduled reports have a transport.
FR-AR-005's P2 half is an adapter and a registration. A screen can show per-channel status and grey
out channels this deployment lacks, because `available_channels` is returned beside the state.

**Harder.** There is now a state — issued but not sent — that somebody has to watch.
`undelivered_invoices()` is that watch, and nothing surfaces it yet.

**Known gaps, each with a trigger.**

- **Nothing drains the queue.** A dispatch that fails becomes `queued` with a `next_attempt_at`, and
  no worker retries it — so today "queued" means "somebody must press send again". The table, the
  index (`invoice_delivery_due_idx`) and the backoff schedule are the shape a worker needs; what is
  missing is the worker and, harder, its actor: `DocumentService.original` authorises as a user, and
  a background sender has none. Resolving that is the same question FR-NTF's queue will ask, and it
  should be answered once for both rather than twice.
- **`delivered` and `bounced` are never written.** They are provider-webhook states and there is no
  webhook endpoint: it needs per-provider signature verification and a public route, which is its
  own security surface. `provider_reference` is recorded on every dispatch specifically so that
  webhook can match one when it lands. Until then the honest ceiling is `sent`, and the API says
  `reached_the_customer: false` rather than implying otherwise.
- **No reply-to address.** `administration` has no e-mail column, so replies go nowhere. A customer
  replying to their invoice is entirely normal, so this wants an `administration.invoice_email` and
  is a one-column migration whenever somebody adds the administration settings screen.
- **The e-mail body formats amounts in the product default locale**, not the administration's — the
  renderer takes the locale as a parameter and the covering message does not. Invisible today
  because `nl-NL` is the only locale (FR-LOC-005 is where a second arrives), and it should be fixed
  with that one rather than left to be discovered.
- **The database-backed tests have never run, and 0041 has never been executed.** No Postgres and no
  Docker on this machine — the same gap ADR-037 through ADR-039 record. `tests/integration/
  test_invoice_delivery.py` covers the draft refusal, tenant coherence, every state transition, the
  no-delete grant and both reports. One helper bug was caught by reading rather than running (a
  keyword named `org` was being swallowed by the helper's own signature instead of reaching the
  query); there may be more that only running would find.
- **No real SMTP conversation has happened.** `CollectingEmailSender` is what the tests drive;
  `SmtpEmailSender` has never opened a socket. The MIME assembly is exercised (the collecting sender
  builds it too, deliberately), but STARTTLS, auth and a real provider's refusal codes are unproven.
