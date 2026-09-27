# ADR-095: Invoice reading tells its failures apart, retries what is transient, and can be run again

- **Status**: Accepted
- **Date**: 2026-09-28
- **Serves**: FR-EXP-001c (never blocks), FR-AP-002 (extraction), IAM-005 (tenant isolation of the
  new endpoint)
- **Builds on**: [ADR-081](ADR-081-automatic-invoice-reading.md) (the reader and its port),
  [ADR-094](ADR-094-mistral-ai-replaces-vertex-for-invoice-reading.md) (Mistral as the provider)

## Context

After ADR-094 switched reading to Mistral, every captured invoice in production came back
`failed` with one reason, `provider_refused`, and nothing else to go on. Finding the cause took a
browser's network panel, the expense-detail JSON and a hand-written call to Mistral from a
terminal. There turned out to be two causes, one after the other:

1. **A Claude model id was sent to Mistral.** `EXTRACTION_MODEL` had a single default,
   `claude-haiku-4-5@20251001`. Switching `EXTRACTION_PROVIDER` without also setting the model
   pointed Mistral at a model it does not have, and it answered 400.
2. **Mistral then answered 429 `rate_limited` (code 1300)**, even to a single isolated call.
   That is a limit on the Mistral account, not a code defect. But the reader gave up on the
   first 429, and the only way to try again was to upload the same invoice a second time.

The design was right to keep the provider's error *body* out of the record and the logs, because
it can echo the invoice. But it also threw away the HTTP status and the provider's own error code,
which carry no document content and are the only things that tell a wrong key from a busy
provider.

## Decision

1. **Refusals are named by HTTP status** (`api.expenses.extraction.transport`):
   `provider_auth_failed` (401/403), `provider_rate_limited` (429), `provider_rejected_request`
   (400/404/422, which is nearly always configuration), `provider_error` (5xx). The HTTP status and
   the provider's short error code (`rate_limited`, `PERMISSION_DENIED`) are stored on
   `expense.extraction`, added to the audit detail, and logged in one warning line. A provider code
   is kept only when it matches `^[A-Za-z0-9_.-]{1,40}$`. That is a machine token, not somewhere a
   document's text can travel. `message` is never read.
2. **Transient refusals are retried.** A 429 or 5xx is tried again at most twice. The wait is what
   `Retry-After` asks for, capped at 5 s, or else 0.5 s then 1 s. Nothing else is retried. The
   service's existing overall timeout still bounds the whole reading.
3. **Each provider has its own default model.** `EXTRACTION_MODEL` is optional; unset means
   `mistral-small-latest` for `mistral` and `claude-haiku-4-5@20251001` for `vertex-claude`.
   Changing the provider can no longer leave the other provider's model behind.
4. **A draft can be read again**:
   `POST /v1/administrations/{administration_id}/expenses/{expense_id}/extraction`. It uses the
   same `submit expense` permission as the form. It reads the *stored* first page through
   `DocumentService.original`, which checks the hash and the scan, and runs the same reading.
   It answers 409 `extraction_unavailable` when reading is off, and 409 for a claim past draft.
5. **A reading only ever fills empty fields.** On a fresh capture every field is empty, so nothing
   changes there. On a re-read, whatever the person has already typed is kept.
6. **The form says which kind of failure it was.** A busy provider says to try again shortly and
   offers "Read again". A refused key or model says an administrator has to check the settings.
   Any other reason keeps the general sentence.

## Consequences

- One new endpoint. It has an IAM-005 isolation test
  (`test_another_tenants_expense_cannot_be_read_again`), run against a real Postgres with RLS when
  this was written. It needs no idempotency handling of its own: the middleware requires the key
  on every mutating request, and a repeated call only fills what is still empty.
- A re-read, like a capture, keeps the database transaction open for the model call (ADR-081's
  consequence, unchanged). With retries the worst case is still the service's timeout.
- Rate limits on the Mistral account are not fixed by this, only survived and named. If
  `provider_rate_limited` keeps appearing, the fix is on the account: Mistral's tiers go up
  with cumulative billed usage, not with adding a card, and the Limits page in the admin console
  shows the current ceiling.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Keep the error body, truncated | Truncating does not make an echo of the invoice safe. The status and a validated token are enough to diagnose, and neither can hold the document. |
| Retry in a background job instead of inline | Right once reading moves off the request (ADR-081's deferred option). Until then, two short retries inside the existing timeout cover a per-second limit without new infrastructure. |
| Re-read overwrites every field | A person's corrections would be replaced by the same model's guess. Filling only what is empty makes the action safe to press. |
