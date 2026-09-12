# ADR-047: One tap, not two — auto-starting a sitting and the race that made possible

- **Status**: Accepted
- **Date**: 2026-09-12
- **Implements**: FR-EXP-001 ("single tap from the home screen"), MOB-002 (capture via installable
  PWA), MOB-013 (44pt/48dp minimum touch targets, sufficient contrast)
- **Serves**: FR-EXP-001c (the product never blocks), FR-EXP-001f/MOB-003 (capture works offline)
- **Constrained by**: ADR-036 (the capture screen and `useSitting`'s existing shape — this is a
  focused refactor of that code, not a rebuild)
- **Related**: [ADR-036](ADR-036-capture-screens.md) — the capture screen and the receipt
  reference this task reuses unmodified; [ADR-046](ADR-046-mobile-pwa.md) — the mobile shell
  `CaptureScreen` is mounted inside, also unmodified by this task

## Context

    FR-EXP-001  Receipt capture by CAMERA (single tap from the home screen, multi-page, auto
                edge detection, deskew, glare and blur warning with retake prompt) ...
    MOB-013     Accessibility: VoiceOver and TalkBack labels, Dynamic Type, minimum 44pt/48dp
                touch targets, sufficient contrast.

`apps/web`'s mobile shell already opens on the capture tab (ADR-046). What it asks of the person
holding a receipt, standing in a car park, is two taps before the native camera even opens:

1. Tap "Start" (`sitting.phase === "idle"`) — a `POST` that opens a capture session on the server.
2. Only once that session is open does "Take Photo" appear; tapping it clicks a hidden
   `<input type="file" capture="environment">`, which is what actually opens the camera.

That is not "single tap from the home screen." The fix is to start the session automatically, in
the background, on mount, and show the shutter button immediately — so the only tap left is the
one that opens the camera.

Read in full before writing anything: `CaptureScreen.tsx`, `useSitting.ts`, `MobileShell.tsx`, and
ADR-036 (which already built this screen and already recorded, as a known gap, that "starting a
sitting needs a connection" — the session id is server-allocated and there is no way to ask for one
offline). This task does not touch that constraint; it is a real one and stays one.

## Decision

### 1. `CaptureScreen` starts the sitting itself, on mount

A `useEffect`, guarded by a ref (`autoStarted`) rather than by `sitting.phase` alone — the effect's
dependency is the whole `sitting` object, which gets a new identity on every phase transition, and
the ref is what stops a re-render from calling `start()` again:

```tsx
const autoStarted = useRef(false);
useEffect(() => {
  if (!autoStarted.current && sitting.phase === "idle") {
    autoStarted.current = true;
    void sitting.start();
  }
}, [sitting]);
```

The separate idle/opening screen — the one that showed only a "Start" button and nothing else — is
gone. The main capture UI (shutter, drop zone, queue status, review list) renders unconditionally
except in the `finalised` phase, which is unaffected. `SittingProblemNotice` stays visible in every
remaining phase, so a genuine start failure (offline) is still reported — the gate screen is gone,
not the error reporting.

No manual retry button was added for a failed auto-start. The existing `SittingProblemNotice`
already names the problem (`offline_cannot_start`) and says what still works (queued receipts still
upload); the shutter button stays on screen, visible but unable to hand off a captured page while
`capture()`'s wait (below) genuinely comes back empty. Adding a redundant "retry" affordance next to
an auto-starting sitting risks reintroducing exactly the "tap something first" step this task
removes, for a case (genuinely offline at the moment of opening) FR-EXP-001c already treats as
"capture still queues; only the sitting can't be opened yet."

### 2. `useSitting.capture()` waits out an in-flight `start()` instead of reading it as "no session"

This is the change that makes (1) safe rather than lossy. Before this task, `capture()` was:

```ts
if (sessionId === null) return null;
```

That was correct when a person had to tap "Start" and wait for it before "Take Photo" ever
appeared — nobody could tap a button that was not there yet. It stops being correct the moment the
screen shows the shutter immediately and starts the session on its own: a tap landing in the gap
between `start()` being called and its promise settling would read as "no session" and the
photograph would be silently dropped, with no trace of it anywhere. On the slow, unreliable
connection a car park is a byword for, that gap is not a hypothetical.

The fix: `start()`'s in-flight promise is stashed in a ref (`startPromise`, `useRef<Promise<void> |
null>`) — set synchronously before `start()`'s own first `await`, so it is already there for a
`capture()` call in the same tick, and cleared in a `finally` once it settles. `capture()` checks
for it:

```ts
if (phase === "opening" && startPromise.current !== null) {
  try {
    await startPromise.current;
  } catch {
    // start() catches its own failure and records it as `problem`; capture()
    // only needs to know whether a session id came out of the wait.
  }
}
const activeSessionId = sessionIdRef.current;
if (activeSessionId === null) return null;
```

`sessionIdRef` (a second ref, written synchronously alongside `setSessionId`) is what makes the
"read fresh" part actually work: `capture()`'s own `sessionId` parameter is whatever the render
that produced this closure held, which is stale — still `null` — after the `await` above, no matter
what `start()` did while it was waiting. The ref is not.

The result: `capture()` returns `null` in exactly one case now — a `start()` that genuinely never
produces a session id, which is ADR-036's already-accepted, unavoidable "no connection at all"
case, not a timing accident. A capture that arrives while a session is merely still opening now
waits and is enqueued once it opens, rather than vanishing.

### 3. The first stylesheet in this codebase — one shutter button, sized and placed for one hand

Confirmed before writing anything: no `.css` file exists anywhere in `apps/web/src`, and no styling
library is a dependency (`package.json` checked). `CaptureScreen.css` and `MobileShell.css` are
added, imported directly from their `.tsx` files (`import "./CaptureScreen.css"` — Vite's own
convention; there was no prior import to follow since nothing imported CSS before). Plain CSS, no
framework, scoped narrowly to this task:

- `.capture__shutter`: the take-photo button, fixed near the bottom of the viewport, horizontally
  centered, 88×88px — comfortably past MOB-013's 44×44pt/48×48dp floor — for a cold or gloved hand
  that is not looking closely.
- `.capture__secondary`: every other action (choose files, add a page, next receipt, finalise) —
  still a full 48px touch target, but visually smaller and lower-contrast than the shutter, so nothing
  competes with the one action this screen exists to make effortless.
- `.mobile-shell__tabs button`: the bottom tab bar had no sizing at all before this task (checked);
  it now gets the same 48px floor, for the same MOB-013 reason.

No palette, no tokens, no dark-mode handling — one accent colour and generous sizing, matching this
codebase's "hand-roll, minimal dependencies" ethos and the narrow scope of this task.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Keep the "Start" button but make it auto-click itself once, on mount | Same tap count as leaving it out and calling `start()` directly; the button becomes dead weight that a screen reader would still announce. |
| Disable the shutter button until `phase === "capturing"` | Reintroduces a wait before the one tap that matters, on exactly the slow connection this task is for — the opposite of the fix. |
| Have `capture()` retry `start()` itself when `sessionId` is `null`, instead of waiting on the SAME in-flight attempt | Two concurrent `start()` calls racing the server for two session ids, one of them silently abandoned — worse than the bug being fixed, for a problem the existing in-flight promise already solves by being awaited rather than duplicated. |
| Buffer a capture taken with no session at all (truly offline) and replay it once a session opens later | This is ADR-036's already-accepted, unavoidable constraint, not this task's to solve: a receipt reference and page number are minted against a SPECIFIC session (ADR-036 §1), and there is no session to mint against yet. Solving it needs a client-supplied, idempotent session id — an API change with a migration, explicitly deferred by ADR-036 itself. |
| A CSS framework or component library for the shutter button | Not a dependency today (confirmed via `package.json`); one button and a tab bar do not justify adding one, and this codebase's convention is to hand-roll. |
| A manual "retry" button next to the shutter for a failed auto-start | Trivial to add but not clearly matching this file's existing conventions, and it risks reintroducing a "tap something first" step for the overwhelmingly common case (successful auto-start) if built carelessly. `SittingProblemNotice` already reports the failure; left out per the task's own guidance. |

## Consequences

**Easier.** Any future capture entry point (e.g. FR-EXP-001h's share-sheet/e-mail paths, already
anticipated by ADR-036 §4's "one function, `accept`, that everything ends in") gets the same
auto-start and race protection for free — nothing about `useSitting`'s public shape changed.

**Harder.** `useSitting` now carries two refs (`startPromise`, `sessionIdRef`) alongside its
existing `nextPage` ref, purely to make a fast tap safe against a slow network. Anyone changing
`start()` or `capture()` again has to keep both in sync with `setSessionId`/`setPhase`, or the race
this task closes reopens quietly.

**Known gaps — cannot be verified without a physical device.** This was built and tested entirely
in jsdom, with no physical phone, no iOS/Android simulator, no ADB, and no browser-automation tool
capable of driving a real touch screen. Nothing below was, or could be, verified here:

- **Whether `capture="environment"` actually opens the native camera app** — versus a photo-library
  picker — on real iOS Safari and real Android Chrome, *including specifically when the app is
  running in installed/standalone PWA mode*. This is documented, real-world browser inconsistency
  that jsdom cannot represent at all; this task did not change the mechanism (per its own scope),
  only what happens before it fires.
- **Whether the resulting tap sequence genuinely feels like one tap in practice** — real camera-app
  launch latency, and whatever confirm/use-photo step the OS's own native camera UI imposes after
  the shutter, which this app has never controlled and still does not.
- **Whether the shutter's sizing and placement are actually comfortable one-handed** across real
  phone sizes and real hands. CSS can target MOB-013's documented minimum and a conventional
  thumb-reach zone; genuine ergonomic comfort needs a real hand on a real device.
- **Whether auto-starting has a visible real-world latency side effect** — e.g. a moment of
  "nothing happening yet" on a slow connection — that automated tests running against a fake,
  either instantly-resolving or manually-controlled promise cannot surface.
- **Whether real outdoor/sunlight/glare conditions are handled well by the existing glare/blur
  detection in `quality.ts`**, which this task did not modify. That logic was built and tested
  against synthetic image data, never a real car-park photograph, and nothing here changes that.
- **Whether service-worker-cached app-shell loading behaves correctly for a fast reopen from a
  home-screen icon on a real device.** jsdom does not implement service workers at all — confirmed
  by ADR-046's own prior PWA task — and this task did not touch `sw.js` or exercise it.
