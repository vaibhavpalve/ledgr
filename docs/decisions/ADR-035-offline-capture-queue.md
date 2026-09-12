# ADR-035: The offline capture queue, and what it refuses to lose

- **Status**: Accepted
- **Date**: 2026-09-06
- **Implements**: FR-EXP-001f, MOB-003, MOB-009 (PRD §6.7, §11)
- **Serves**: NFR-032 (retries never double-post), FR-UX-007 and FR-LOC-001 (every string
  translated), MOB-008 (the queue is visible before the device is unlocked)
- **Constrained by**: IAM-001–005 (every request carries tenant context), IAM-030 (authorization
  against current state), SEC-005 (the archive owns the format allowlist)
- **Related**: [ADR-031](ADR-031-receipt-capture.md) — the endpoint this drains into, and the
  gap it left open; [ADR-030](ADR-030-document-storage.md) — where a delivered capture lands;
  [ADR-024](ADR-024-idempotency.md) — the key that makes a retry safe

## Context

> **FR-EXP-001f.** Capture works with no connectivity: the image is queued encrypted on-device and
> uploads automatically, with visible queue state (see MOB-003).
>
> **MOB-003.** Offline capture queue — documents taken without connectivity are stored encrypted
> on-device and uploaded when connectivity returns, with visible queue state.
>
> **MOB-009.** No financial data written to unencrypted device storage; on-device cache is
> encrypted, size-capped and purged on logout, role change or remote wipe.

ADR-031 built the server side and named this as a known gap: *"FR-EXP-001f's offline queue is
client-side (MOB-003). The server side it needs is this endpoint being idempotent-keyed and
order-independent, which it is."* This is that client side.

MOB-002 puts capture in P0 **via the installable PWA**, with the native camera path in P2, so the
first implementation is a browser one — and the second, when the React Native app lands (MOB-001),
must not be a second implementation of the same rules.

The situation this is written for is specific and worth stating, because most of the decisions
below only make sense against it: somebody photographs six receipts in an underground car park.
The photographs were taken *by the app*. They are not in the camera roll. The paper may be in a
bin by the time anyone notices. **For as long as the queue holds one of these, it holds the only
copy.**

## Decision

### 1. The policy is a platform-free package; only the I/O is per-platform

`packages/offline-queue` holds the queue, the uploader, the cap, the backoff, the purge rules and
the request builder. It has no `crypto`, no `fetch`, no `navigator`, no `IndexedDB` and no timers,
and its tsconfig omits the DOM lib so that stays true rather than being remembered. Five ports —
`QueueStore`, `QueueCipher`, `Connectivity`, `Transport`, `Scheduler` — are what a platform
supplies. The web adapters are `apps/web/src/capture`.

This mirrors `@ledgr/i18n`, which is deliberately free of `navigator` and `localStorage` for the
same reason. The alternative is that the mobile app re-derives the size cap, the purge triggers and
the retry classification from scratch, and the second one written is the one that forgets to purge
on a role change. It is the argument CLAUDE.md makes about the authorization library, applied to a
smaller thing.

### 2. Everything is queued, always — there is no online fast path

`enqueue` is the only way in, whether or not there is a connection; the uploader drains, and drains
immediately when it can. A "send it now if we're online, queue it otherwise" branch would be two
delivery paths with two retry stories, and the one exercised least often — the queued one — is
exactly the one that runs when somebody is in a tunnel.

### 3. When the cap is reached, the NEW capture is refused; nothing is evicted

This is the decision the whole module turns on, and it is the opposite of what a cache does.

A queued receipt is not a cached copy of something that exists elsewhere. Evicting the oldest to
make room destroys a document its owner believes is saved, silently, at the moment they are least
able to notice — the state FR-DOC-003's completeness report exists to find months later. Refusing
is loud, it lands while the person is still holding the receipt, and the two remedies (get online;
discard something you can see) are both theirs.

Two refusals, not one, because they need opposite advice: `cap_exceeded` ("upload what is waiting")
and `larger_than_cap` ("this file will never fit, whatever you clear").

Blocked captures keep their bytes and keep counting against the cap, for the same reason. The queue
does not free space by discarding evidence.

### 4. The purge destroys the key first, and it really does destroy work

`CaptureQueue.purge` destroys the encryption key and *then* deletes the records. The ordering is
the design, not tidiness: deleting records is a loop over a database that can be interrupted — the
tab closes, the process is killed, the OS reclaims the app mid-wipe — and an interrupted delete
leaves readable ciphertext behind. Destroying the single key first makes everything that remains
permanently unreadable in one operation, so the loop afterwards is housekeeping rather than the
security boundary.

The honest cost, stated because it is not small: **a purge destroys captures that never uploaded.**
Somebody signing out with four receipts waiting loses four receipts. MOB-009 requires this and the
requirement is not negotiable, so what the design owes is that it not be a surprise —
`capturesAtRisk()` and `CaptureQueuePurgeWarning` exist so a sign-out flow says the sentence before
it offers the button.

`role_change` is a purge and not a pause because IAM-030 evaluates authorization against current
state: uploading afterwards would submit claims under an authority the person no longer holds.

### 5. Metadata is sealed with the image, and the queue still renders without the key

One AES-GCM envelope per capture, framed as `[length][JSON header][image bytes]`, with the record
id bound in as additional authenticated data. The tenant context, the file name and the idempotency
key are all *inside* it: MOB-009 draws its line at device storage, and an administration id in the
clear is a record of whose books this device has been working on. Sealing them together also means
an image cannot be paired with another record's routing information — the failure that would upload
a receipt into the wrong administration.

What stays in the clear is only what the store must index without a key: id, state, byte count,
timestamps, and a blocked reason. None of it is financial. That split buys a property worth having
on its own — a device that has auto-locked (MOB-008) can still show *"4 receipts waiting to
upload"*, which is the answer somebody wants before they unlock rather than after.

On the web the key is a non-extractable `CryptoKey` held in IndexedDB. `exportKey` refuses it, for
this code and for anything else on the origin, so the raw bytes are never a JavaScript value at any
point in its life. That is the browser's nearest equivalent to the platform keystore MOB-008 names.

### 6. Refusals divide into "wait" and "a person must look", and waiting has no limit

The dividing line is not *did it work* but *could offering it again ever work*.

| | Examples | Behaviour |
|---|---|---|
| `retry` | no network, 5xx, 429, 401, scanner unavailable, idempotency in-flight | exponential backoff, 5s → 15min, ±20% jitter, **no attempt limit** |
| `blocked` | 415, infected, too large, session finalised, 403, 404 | stops, names a reason, offers retry and discard |

No attempt limit, because every retryable condition is one that ends: a queue that gave up after
ten tries would discard a receipt because a server was down over a weekend. What a stuck capture
gets instead is visibility — the attempt count and the next attempt time are in the snapshot, so a
queue that is not draining looks like one that is not draining.

Classification lives beside the request builder, in the shared package, because what a 415 means is
API knowledge. Two transports classifying independently is two chances for one of them to retry
forever something that will never succeed. It reads `problem()`'s machine-readable `reason` and
never the `message` — which is how the two different 409s are told apart: a finalised capture
session (permanent) from the idempotency middleware's "a request with this key is executing right
now" (transient, and asking for exactly the retry it gets).

### 7. One idempotency key per capture, minted at capture and reused on every attempt

NFR-032, and the property the whole retry story rests on. A key regenerated per attempt would
double-post precisely the receipt whose first response was lost on a flapping connection — the case
this queue exists for.

### 8. Sequential, oldest-first, and the tenant context comes from the record

Oldest first because `capture_page` allocates position as max + 1 (ADR-031), so send order is the
order a batch is numbered in, and the pile of paper somebody checks it against is in the order it
was photographed. Sequential rather than parallel for the same reason, and because it is the kinder
thing to do to a connection that has just come back.

`buildRequest` reads the administration id out of the sealed payload and takes no "current
administration" parameter. A receipt photographed for one client and uploaded twenty minutes later,
after the person has switched to another, must post to the client it was photographed for.
CLAUDE.md's first rule is usually read as a server-side obligation; this is the client-side half,
and it is the one place a queue can silently get it wrong.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Build the queue inside `apps/web` | MOB-003 and MOB-009 are mobile requirements. A web-only queue guarantees the mobile app re-derives the cap, the purge triggers and the retry classification — a second implementation of exactly the rules that must not differ. |
| Evict the oldest capture when the cap is reached | Destroys the only copy of a photograph its owner believes is saved, silently. The queue is not a cache. |
| Give up after N failed attempts | Discards a receipt because a server was down over a weekend. Every retryable condition ends; permanent refusals are already `blocked`. |
| Send straight out when online, queue only when offline | Two delivery paths. The one exercised least often is the one that runs in a tunnel. |
| Encrypt the image and leave the metadata in a cleartext row | An administration id on the device is a record of whose books it has worked on, and a separable envelope lets an image be paired with another record's routing information. |
| Key in `localStorage` as base64 | A key in cleartext beside the ciphertext it opens. No encryption at all. |
| Key derived from the password, held only in memory | Would make the queue unreadable after every auto-lock (MOB-008) — including for the background drain, which is the thing that has to work while nobody is looking. |
| Delete the records first, then the key | An interrupted delete leaves readable ciphertext. The key is one operation; the records are a loop. |
| Keep the queue across logout, encrypted | MOB-009 says purged on logout, in as many words. |
| A fresh idempotency key per attempt | Double-posts the receipt whose first response was lost — the exact case this queue creates. |
| Parallel uploads | Numbers a batch in response order, against a pile of paper somebody is checking it against. |
| Background Sync API / a service worker drain | Chromium-only, and it would be a second drain path to keep in step with the in-page one. Deferred rather than dismissed: it changes no data shape, so it is an enhancement over this design rather than a replacement for it. |
| Re-check the format allowlist and per-type size limits client-side | ADR-031 §4's second allowlist, and the one that drifted would be the one a receipt was wrongly refused by. The queue enforces its own device budget and nothing else about size. |
| Trust `navigator.onLine` | It reports an interface being up, not the API being reachable. A captive portal satisfies it. Used as a hint that starts an attempt, never as a fact. |

## Consequences

**Easier.** The capture screen, when it is built, calls `enqueue` and is done — it needs no notion
of connectivity, retry or upload state. FR-EXP-001h's e-mail and share-sheet paths are new
`CaptureSource` values and nothing else. The React Native app supplies five adapters (SQLCipher,
the keystore, NetInfo, fetch, `setTimeout`) and inherits every rule above; nothing in
`packages/offline-queue` changes.

**Harder.** There is now a place where a person's document lives outside the archive, subject to a
cap and to a purge. Anything that ends a session has to consider the queue, which is why
`purgeCaptureQueue` is one function with three named reasons rather than three call sites clearing
a store.

**Foreclosed.** A capture cannot be edited while queued. The envelope is sealed at capture and its
metadata authenticated with the image, so changing which receipt a page belongs to means discarding
and re-photographing. That is the right trade while nothing but routing information is in there;
it would be the wrong one if the expense form's fields ever moved into the envelope, and that is
the change that would require revisiting this.

**Known gaps, each with a trigger.**

- ~~**Nothing calls `enqueue` yet.** There is no capture screen in the web app — FR-EXP-001's
  client side is not built.~~ **Closed 2026-09-09 by
  [ADR-036](ADR-036-capture-screens.md)**, which also **corrects §1 of this ADR**: a queued capture
  carried the server's `itemId`, which is unknowable offline, so every page captured without a
  connection would have opened a new receipt and a three-page invoice would have been claimed three
  times. It now carries a client-minted `receiptRef` and a `pageIndex`, resolved to the server's
  item id when page 0 is delivered. See ADR-036 §1.
- **Only one of MOB-009's three purge triggers can fire.** `logout` has no sign-out flow to call it
  (see `auth/SignInPending`), `role_change` has no endpoint that reports a changed grant, and
  `remote_wipe` has no signal. All three are named in `PurgeReason` and go through one function, so
  wiring each is a call rather than a new purge that might leave the key behind.
- **The IndexedDB adapter has no unit tests.** jsdom has no IndexedDB, and the only fake
  implementations are external dependencies. The adapter is deliberately five one-line wrappers
  with every decision moved behind the `QueueStore` port, which is tested against an in-memory
  store; what is untested is the wrapping, and the mitigation is that there is almost none of it.
- **Durable storage is requested, not guaranteed.** `navigator.storage.persist()` is asked for
  once at startup and the browser may refuse. A refusal means the origin's storage is best-effort
  and the browser may clear it under pressure with no event — the one way a queued capture can
  disappear without a purge. MOB-009's cap is what keeps the ask small enough to be granted.
- **The uploader runs only while a tab is open.** A phone that locks mid-drain resumes on the next
  visibility change, which `BrowserConnectivity` listens for precisely because the `online` event
  often never fires for a frozen tab. Background Sync would remove the caveat and is above.
