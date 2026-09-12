# ADR-036: The capture screens, and the receipt reference that makes them work offline

- **Status**: Accepted
- **Date**: 2026-09-09
- **Implements**: FR-EXP-001, FR-EXP-001a, FR-EXP-001b, FR-EXP-001e (PRD §6.7) — the client side
- **Serves**: FR-EXP-001c (never blocks), FR-EXP-001f/MOB-003 (capture works offline),
  FR-EXP-001g (duplicate warnings), NFR-031 (no floating point), NFR-032 (idempotency),
  FR-LOC-001/FR-UX-007 (every string translated)
- **Constrained by**: IAM-001–005 (tenant context on every request), SEC-005 (the archive owns
  the format allowlist), MOB-002 (PWA in P0, native camera in P2)
- **Related**: [ADR-031](ADR-031-receipt-capture.md) — the endpoints and the schema these drive;
  [ADR-032](ADR-032-expense-form.md) — the form's server side;
  [ADR-035](ADR-035-offline-capture-queue.md) — the queue every capture goes through

## Context

ADR-031 and ADR-032 built the server side of receipt capture and the expense form; ADR-035 built
the offline queue and recorded that nothing called `enqueue` yet, because there was no capture
screen. This is that screen, the review list beside it, and the form after it.

MOB-002 puts capture in P0 **through the installable PWA**, with the native camera path in P2, so
the first implementation is a browser one.

Building it surfaced a defect in the queue that no amount of client work could have papered over,
and §1 is about that rather than about interface.

## Decision

### 1. A capture carries a client-minted RECEIPT REFERENCE, not a server item id

This is the load-bearing change, and it is a correction.

ADR-031 §3 distinguishes FR-EXP-001's multi-page from FR-EXP-001a's batch with one parameter:

    ?item=<uuid>   absent  → a NEW receipt: new item, new draft expense
                   present → ANOTHER original for a receipt already captured

That uuid is allocated by the **server**, when a receipt's first page lands. ADR-035 stored it on
the queued capture as `itemId` and assumed the caller would know it. Offline, the caller cannot:
page two of an invoice is photographed long before page one has been anywhere. Every page captured
without a connection would have arrived as a new receipt, and **a three-page invoice would have
been claimed three times** — silently, in the direction that multiplies somebody's money, which is
the precise failure ADR-031's three-level schema was shaped to make impossible.

So a capture now carries `receiptRef` (client-local, minted by the screen) and `pageIndex`, and the
queue resolves the reference to the server's item id when page 0 is delivered:

| | |
|---|---|
| `pageIndex === 0` | sent with no `item`; **creates** the receipt; the response carries `item_id` |
| `pageIndex > 0` | held by `policy.due` until the reference resolves, then sent with that `item` |

Three consequences follow, and each is a rule rather than a nicety:

- **A later page is never offered while its first page is unresolved.** Sequential oldest-first
  ordering makes this moot most of the time; it is not moot when page 0 is waiting out a backoff or
  was refused, and those are exactly the cases where skipping ahead would do the damage.
- **A refused first page blocks the rest of its receipt**, with its own reason
  (`first_page_blocked`) rather than the one page 0 got. They could never resolve, and left queued
  they would sit forever with nothing to explain themselves. Retrying page 0 successfully releases
  them again.
- **Discarding a first page discards the whole receipt.** Pages 2 and 3 with no page 1 could never
  be sent, and promoting one of them to first page would submit a claim evidenced by the middle of
  a document.

`resolvedItemId` is stored in the clear, unlike the rest of the envelope (ADR-035 §5), because it
is written *after* the envelope is sealed and re-sealing every waiting sibling would mean
decrypting each one to change a single field. The tamper case that opens is bounded: someone with
write access to the device store could attach a page to the wrong receipt, but only within the same
capture session, because the server verifies the item belongs to the session it was sent with.
Cross-tenant is not reachable from there.

### 2. The two "several" are two buttons, and neither is ever inferred

    "Another page of this receipt"  → joins the current receipt
    "Next receipt"                  → starts a new one

ADR-031 refused to infer this server-side. The screen refuses too, for the same reason: nothing in
an image says whether it is the second page of the last receipt or the first page of the next one,
and a heuristic on elapsed time or visual similarity would be wrong *silently*, either multiplying
or merging a claim. "Another page" arms for one capture and then disarms — the sticky version is
how somebody ends up with a six-page receipt they meant to be six claims.

The screen states what finalising would produce (*"this makes 2 expenses"*) before anybody commits,
which is FR-EXP-001a's review list doing the job it exists for.

### 3. Blur and glare are measured before the shutter closes; edge detection is not built

FR-EXP-001 names four camera-side operations. Two are here and two are not, and the split is
deliberate rather than a stopping point.

**Built:** blur, as the variance of a Laplacian over the luminance channel, and glare, as the share
of blown-out pixels. Both are cheap single passes on a downscaled greyscale copy. They **warn and
never refuse** — "take it again" first, "use it anyway" always available — because a blurred
photograph of a receipt is worth more than no photograph of it, because FR-EXP-001c is explicit
that the product never blocks, and because these heuristics are wrong often enough that a hard
refusal would eventually throw away the only copy of something.

**Not built:** auto edge detection and deskew. They need corner detection and a perspective
transform — image-processing work rather than interface work — and MOB-002 puts the native camera
path in P2, which is where a real document scanner belongs. What is here is the part that changes
whether somebody re-takes a photograph while the receipt is still in their hand.

The measured bytes are never the queued bytes. FR-DOC-001 requires the original unaltered, and a
JPEG round-tripped through a canvas is a different file with a different hash — which would also
defeat the archive's byte-identical duplicate detection. The canvas measures; the original goes to
the queue.

### 4. The screen queues and never uploads

Every path — camera, file picker, drag-and-drop — ends in one function, and that function calls
`queue.enqueue`. There is no online fast path (ADR-035 §2), so the capture screen needs no
transport at all, and its tests need no network.

Everything that genuinely requires a server — opening a sitting, reading the review list back,
saving the form, finalising — is a plain call that fails cleanly when there is no connection, and
the screen says which of the two happened rather than showing one error for both.

### 5. Money is a string from the keystroke to the wire

`gross_amount` is an `inputMode="decimal"` text input, held as the typed string, and sent as a
string. Never `type="number"`, which hands back a value the browser has already parsed as a double,
and never `Number()`. NFR-031 says "everywhere in the calculation path", and the client is on it.

VAT, net and the rate have no inputs. They are computed from the treatment and the date (CMP-014)
and shown read-only — accepting any of them would let the screen assert a figure the database is
about to disagree with.

### 6. Warnings do not become gates

`missing_fields` is shown as information about what is still needed, not as validation errors on a
form somebody has only started (FR-EXP-001c). FR-EXP-001g's duplicate warnings arrive alongside
`can_be_marked_ready: true` on purpose, and the submit button is gated on the server's answer
alone — two identical receipts can be legitimate, and a client that refused them would refuse a
claim the server is willing to accept.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Keep the server's `itemId` on a queued capture | Unknowable offline. Every page becomes a new receipt, and a three-page invoice is claimed three times, silently. |
| Let a later page upload with no item when its first page is stuck | The same failure, reached by a different route. Holding it back is recoverable; a duplicate claim is not. |
| Send a receipt's pages in parallel once resolved | `position` is allocated per item as max + 1 (ADR-031); parallel sends would number a batch in response order, against a pile of paper somebody is checking it against. |
| Infer multi-page from timing or image similarity | ADR-031's argument, unchanged: wrong silently, in the direction that multiplies or merges a claim. |
| Refuse a blurred or glared photograph | The checks are heuristics, and the queue may hold the only copy. FR-EXP-001c says the product never blocks. |
| Build edge detection and deskew now | Corner detection plus a perspective transform is image-processing work; MOB-002 puts the native camera path in P2. |
| Re-encode the image through a canvas before queueing | FR-DOC-001 requires the original unaltered, and the re-encoded bytes would have a different hash — defeating byte-identical duplicate detection. |
| Repeat SEC-005's format allowlist in the screen | ADR-031 §4's second allowlist. The file input's `accept` is a picker convenience, not a control. |
| `type="number"` for the amount | The browser parses it to a double before any code here sees it (NFR-031). |
| Gate submit on the duplicate warnings being empty | They warn and never block. The API sends them beside `can_be_marked_ready: true` for exactly this reason. |
| Persist the sitting across a reload | It would mean writing an administration id to unencrypted device storage — what ADR-035 §5 sealed the queue's metadata to avoid — to recover a list, while losing no receipts either way. |
| Read the "currently open client" inside the capture hook | The client-side half of CLAUDE.md's first rule. The context is passed in once and never re-derived, or a receipt eventually uploads into the wrong administration. |

## Consequences

**Easier.** FR-EXP-001h's e-mail and share-sheet paths are new `CaptureSource` values arriving at
the same `accept` function. The React Native app (MOB-001) reuses every rule in
`@ledgr/offline-queue` — including §1's resolution, which is where the subtle failure lives — and
supplies its own camera. Extraction (FR-EXP-001c/P1) fills the form's fields with no change to the
form.

**Harder.** A receipt is now a two-level thing on the client as well as the server, and the page
numbering has to be right at capture time. `useSitting` keeps its page counter in a ref rather than
in rendered state for exactly this reason: React batches updates, so four files dropped at once
would otherwise all be numbered page zero — and page zero is the number that opens a receipt.

**Known gaps, each with a trigger.**

- **Starting a sitting needs a connection.** The session id is allocated by the server and there is
  no way to ask for one offline, so FR-EXP-001f is whole for every capture *after* the first
  sitting is open and not for a cold start in a car park. The fix is a client-supplied session id —
  `PUT .../capture-sessions/{id}`, idempotent by construction — which also needs the uniqueness to
  be per administration so that a supplied id cannot probe for another tenant's. That is an API
  change with a migration and it is deliberately not smuggled in here. The screen says plainly what
  it cannot do meanwhile.
- **The sitting does not survive a reload.** No receipts are lost — every page is in the queue with
  its tenant context sealed alongside it, and the uploader delivers them regardless — but the
  in-progress review list resets. Recovering it is what the previous point's session id would also
  buy.
- **`browserDecode` has no unit tests.** jsdom has no `createImageBitmap` and no canvas. The
  measures it calls are tested directly against synthetic images, and the screen's behaviour is
  tested against an injected decoder; what is untested is the twelve lines that drive the canvas.
- **The screens have no route and no session to run in.** `SittingContext` is a prop, because there
  is no sign-in flow (`auth/SignInPending`) and no active-administration endpoint wired. It stays a
  prop when there is one: a hook that reached for the currently open client itself is the one that
  uploads a receipt into the wrong one.
- **The review list shown during a sitting is the client's own**, not the server's. Offline the
  server has not seen any of it, and a list that stayed empty until the pages uploaded would be
  empty exactly when somebody most wants to check they got all six. The server's list is what
  `reviewSession` reads back afterwards.
