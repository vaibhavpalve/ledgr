# ADR-030: The document archive

- **Status**: Accepted
- **Date**: 2026-09-04
- **Implements**: FR-DOC-001, FR-DOC-002, FR-DOC-003, FR-DOC-004, FR-DOC-005 (PRD §6.10);
  SEC-005 (§9)
- **Serves**: FR-EXP-001d (the captured receipt is the source document), PRIV-023 (restricted
  rather than erased), PRIV-030 (retention enforced by job, not by process)
- **Constrained by**: IAM-004 / SEC-022 (per-tenant keys), IAM-005 (tenant isolation),
  IAM-090 (audit), CLAUDE.md non-negotiable #4 (integrations behind adapters), NFR-044
- **Related**: [ADR-022](ADR-022-ledger-bounded-context.md) — why the link is a table and not a
  column; [ADR-028](ADR-028-fiscal-year-definition.md) — the same one-table-two-implementations
  device, used there for period derivation; [ADR-012](ADR-012-appendix-a-as-source-of-truth.md) —
  why deletion has no HTTP surface

## Context

> **FR-DOC-001.** Every document is stored in original form, unaltered, alongside any derived text
> and extracted fields.
>
> **FR-DOC-005.** Storage is write-once for the retention period; deletion before expiry requires
> a documented legal basis and privileged approval.
>
> **SEC-005.** File uploads: type verified by content not extension, size-capped, malware-scanned,
> stored outside the web root, served from a separate origin with `Content-Disposition: attachment`.

A source document is the evidence a posting rests on. Nothing else in the product has that
property: a ledger entry can be reversed and a report can be re-run, but a receipt that was
altered, lost or quietly deleted cannot be reconstructed from anything the system still holds.

## Decision

### 1. The table is immutable in its identity and mutable in its derivation

FR-DOC-001 is one sentence carrying two opposite requirements. The **original** must never change
— its bytes, hash, size, verified type, tenant. The **derived** material must be writable after the
fact, because OCR runs after upload (FR-EXP-001c is explicit that the product never blocks on
extraction) and is re-run when the pipeline improves.

So `document` is neither append-only nor freely mutable. `document_original_immutable()` is the
line between them, the same shape `ledger_account_identity_immutable()` has in `0020`: "append-only"
is too blunt when a row legitimately learns things about itself later.

The bytes are not in the table. They are in Azure Blob, encrypted under the administration's own
DEK, and `content_hash` is what makes "unaltered" checkable rather than asserted — verified on
every read, and a mismatch is a 404 rather than a warning, because a document that is not what it
was is not evidence of anything.

### 2. Retention is derived, and a caller-supplied value is discarded

FR-DOC-002 anchors to the **fiscal year's end**, not the upload date: a receipt uploaded in March
2026 for FY2025 is kept until end-2032, and two receipts from one year expire together. A trigger
computes it and throws away whatever was sent, the stance `0020` takes on `journal_entry.
entry_number`. A retention date the uploader can choose is not a policy, and "not deletable by
users" has to be true of the service too.

It may be **extended and never shortened** — reclassifying to immovable property turns 7 years into
10; nothing turns 10 back into 7. Shortening is the deletion FR-DOC-002 forbids, arriving by a
quieter route than `DELETE`.

The rule exists twice — `documents.retention_until()` and `api.documents.retention` — because a
screen shows a retention date before the row exists and the database derives one inside the
transaction that creates it. `tests/documents/retention_cases.py` runs one table against both, the
device ADR-028 uses for period derivation. **Writing the second implementation immediately found a
real off-by-one**: the SQL guard permitted deletion *on* `retention_until` while the Python treated
it as inclusive. Inclusive is right, and the day the two disagreed about is the last day of the
seventh year — the day a deadline-dated inspection asks for.

### 3. The document–posting link is its own table

Forced twice over. `journal_entry` is append-only with no `UPDATE` grant (ADR-022), so a document
attached to an existing posting — the ordinary case, since an invoice is coded after it arrives —
could not be recorded on the entry. And the relation is genuinely many-to-many: one invoice can
support several entries, one entry can rest on an invoice plus a delivery note.
`journal_entry.document_reference` stays what FR-GL-004 made it: a human's free-text note.

Links are **detached, not deleted**, following `0013`'s convention for profile assignments — a link
once asserted is evidence about what somebody believed, and the correction is a new fact.

### 4. The upload pipeline's order *is* the security control

    authorize → cap size → sniff type → hash → scan → store → record → audit

Several steps would be wrong one position later. The cap precedes the sniff, so an absurd body is
refused unexamined. The hash is of the **plaintext**, because hashing ciphertext produces a value
that changes when the key rotates — making "unaltered" unverifiable across exactly the operation
most likely to be blamed for altering something. The scan precedes the store, because
store-then-scan leaves a window where the object exists and nothing has judged it, and the
compensating delete is what FR-DOC-005 forbids. The row is written **last**, so a failure leaves an
orphan blob (which a sweep collects) rather than a row pointing at nothing (a lost original).

Scan verdict is a **state on the row**, not a boolean. `pending` and `failed` refuse the download
alongside `infected`: an unscanned file and an infected one are the same risk to whoever opens
them, and the difference is only time. An infected file is **kept** — it is inside its retention
period, and it is evidence about an incident.

### 5. Deletion exists in the database and has no HTTP surface

`documents.delete_with_approval` is the only path that removes a row inside retention, it requires
an approved `document_deletion_request` (legal basis ≥ 20 characters, approver ≠ requester), and it
is granted to `ledgr_ops` alone. `ledgr_app` — the role every request runs as — has no `DELETE`
grant at all.

No endpoint, deliberately: Appendix A has no capability for deleting a document, so choosing one
would be inventing a permission (ADR-012). The mechanism is built and the surface waits for a
requirement that names who may use it.

## Alternatives considered

| Option | Rejected because |
|---|---|
| `document_id` column on `journal_entry` | That table is append-only with no UPDATE grant, so a document attached after posting could not be recorded — and the relation is many-to-many in both directions. |
| Fully append-only `document` table | FR-DOC-001 requires derived text stored *alongside*, and OCR runs after the upload. Immutability has to be per-column. |
| Store first, scan asynchronously | Leaves a window where the object exists unjudged, and closing it needs the delete FR-DOC-005 forbids. Fails closed instead: no clean verdict, no stored document. |
| Hash the ciphertext | Changes on key rotation (SEC-023), making "unaltered" unverifiable across the one operation most likely to be suspected of altering something. |
| Trust `Content-Type` or the file extension | SEC-005 says the opposite in as many words. The sniffed answer is authoritative, and a client whose declaration disagrees is refused rather than silently corrected — a contradiction resolved quietly is resolved in the attacker's favour. |
| `tsvector` for FR-DOC-004 | Needs a stemmer chosen per document, and this corpus is Dutch and English mixed with no reliable way to tell which a scanned receipt is. `pg_trgm` (CLAUDE.md's stated search choice) needs no language. |
| Multipart upload | Needs a parser running over attacker-controlled bytes before anything has decided to accept them. The raw body with `Content-Type` is how object stores work, costs a client nothing (a `File` is a `Blob`), and avoids a parser SEC-006's reasoning would otherwise want sandboxed. It also avoids adding `python-multipart`, which could not be locked here. |
| `include_router` for the endpoints | **This one nearly shipped.** See below. |
| Provider-side (Azure) encryption | Would make IAM-004's key isolation a property of a console setting rather than of this code path. `EncryptedBlobStore` wraps any store, so the bytes are ciphertext before the provider sees them. |

## Consequences

**A near-miss worth recording.** The routes were first added with `app.include_router()`. This
FastAPI version does not flatten an included router into `app.routes` — it appends one opaque
`_IncludedRouter` wrapper. Everything that makes a route safe here walks `app.routes` and reads each
entry's `dependant`: the authorization enforcement middleware, and the coverage checks for
CLAUDE.md rule three, IAM-005, IAM-090 and NFR-032. Three endpoints touching financial evidence
would have been invisible to all five, and **every check would have passed by never seeing them**.
`api.documents.routes.register` uses `add_api_route` and says why at length. Any future router must
do the same.

**Easier.** A new document type is a signature in `content_type.py` plus a size cap. A real malware
scanner is one `build_scanner` branch. Azure Blob is one `BlobStore` implementation — the Protocol
deliberately offers no `url_for` or `path_for`, so nothing can serve a blob around the download
endpoint's scan gate, audit entry and response headers.

**Harder.** Two implementations of the retention rule to keep in step (the shared table is what
makes that safe). The upload path holds the whole file in memory — bounded by the 50 MB cap, and a
streaming path would have to re-solve hashing and scanning over the same bytes.

**Not built, and each has a trigger.**

- **FR-DOC-006** (bulk export with manifest and checksums) — not in the requested scope.
- **The deletion endpoint** — waits on a capability that names who may delete.
- **OCR / extraction** — `derived_text` and `extracted_fields` are the columns it will write; the
  pipeline itself is SEC-006's sandbox and FR-AP-002's confidence scoring.
- **Row-level "own submissions" scoping** — `matrix.py`'s note asks whoever builds FR-DOC to add
  it. `uploaded_by_user_id` and its index are here, which is what makes it possible; wiring it into
  the authorization library is a change to that library, not to this schema. Until then an Expense
  Submitter granted `view document` still sees more than §8.4 intends.
- **The Azure Blob adapter** — `InMemoryBlobStore` is wired in `routes.py` and named for what it is,
  the posture `api.crypto.kms` and `api.auth.breach_check` take for their own local stand-ins.
