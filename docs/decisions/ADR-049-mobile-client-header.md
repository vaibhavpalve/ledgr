# ADR-049: The persistent client header on mobile

- **Status**: Accepted
- **Date**: 2026-09-12
- **Implements**: FR-FRM-000a (the active client is unmistakable at all times — persistent name and
  colour marker in the header on every screen, on web and mobile; posting to the wrong client is
  named as the single worst usability failure in this product)
- **Serves**: FR-LOC-001 (every user-facing string translated; the client's own name and initials
  are the documented exception), CLAUDE.md rule 1 (tenant context — this header is a rendering of
  the session's own active administration, never a value a screen could substitute its own for)
- **Constrained by**: ADR-046 (the five-tab mobile shell this task adds a header above, unchanged
  otherwise), the user's explicit scope for this task (mobile only — see Known gaps)
- **Related**: [ADR-046](ADR-046-mobile-pwa.md) — the shell this header is hoisted into;
  [ADR-048](ADR-048-mobile-home-dashboard.md) — the most recent prior addition to the same shell,
  whose "one aggregation endpoint, fetched once at the composition root" shape this task's badge
  fetch mirrors exactly

## Context

    FR-FRM-000a  The active client is unmistakable at all times — persistent name and colour
                 marker in the header on every screen, on web and mobile. Posting to the wrong
                 client is the single worst usability failure in this product. M (P0)

Three pieces of this requirement already existed before this task, confirmed by reading each:

- `apps/web/src/client/ClientHeader.tsx` — the component. Renders a client's name, initials-in-a
  -coloured-marker, legal name (when it differs from the trade name) and an ambiguity warning, from
  a `ClientBadge`. Not rendered anywhere in the app.
- `packages/shared-types/src/index.ts`'s `ClientBadge`/`ClientColour`/`CLIENT_COLOURS` — the shared
  shape, already correct, already used by `ClientHeader` and `ClientSwitcher`.
- `apps/api/src/api/main.py`'s `GET /v1/switcher/active` (`_entry_json`) — already built, already
  correct, returning `null` when the session holds no active administration. Not called from
  anywhere in the frontend.

Nothing wrote to `MobileShell`'s render tree to use any of this: the five-tab mobile shell (ADR-046
onward) had a bottom tab bar and nothing else — no persistent identity signal at all, on the one
surface this requirement calls out by name (P0, "the single worst usability failure"). This task
closes that gap for mobile, which is the scope explicitly asked for.

## Decision

### 1. `ClientApi`, a new small client — `apps/web/src/client/api.ts`

One method, `fetchActiveBadge(): Promise<ClientBadge | null>`, following `capture/api.ts`/
`home/api.ts`'s exact conventions (`ApiOptions`, `ApiError`, `OfflineError`, `languageHeaders`).

The one thing genuinely new here rather than copied: `_entry_json`'s wire response is snake_case
(`administration_id`, `display_name`, ...) but `ClientBadge` is camelCase, because it is consumed
by `ClientHeader`/`ClientSwitcher` in idiomatic TS shape rather than placed on screen as raw wire
data the way `ExpenseView`/`SalesInvoiceView` are. `toBadge()` is the one place that rename happens;
everything downstream only ever sees `ClientBadge`.

### 2. Three states, not two: `ClientBadge | null | undefined`

This is the one genuinely novel design decision in this task.

`ClientHeader` already had a two-state contract: `ClientBadge` (an active client) or `null`
("confirmed: no active client", rendered as "No client selected"). Wiring a real fetch in front of
it introduces a THIRD, real state that the two-state contract has no room for: the moment between
mount and the fetch answering, when the truth is simply "not yet known."

Collapsing that moment into `null` — the obvious shortcut, since `useState<ClientBadge | null>`
already defaults to something falsy — would render "No client selected" on every cold start and
every reload, for as long as the network round trip takes. That is not a cosmetic flicker. It is a
header making a confident, false claim on the one screen this requirement singles out as the
highest-stakes place to get identity wrong: a person glancing at the header during that window sees
exactly the "no client" state that should mean "stop, don't post anything yet" when the true state
is "a client is very likely about to appear." FR-FRM-000a's own wording — "unmistakable at ALL
times" — rules out a window where the header is mistaken by construction.

So `badge` is `ClientBadge | null | undefined`, with `undefined` reserved for "not yet known" and
`null` reserved for what the API actually told us:

| Value | Meaning | Rendered as |
|---|---|---|
| `ClientBadge` | An active client. | Name, initials-in-marker, colour, legal name if it differs, ambiguity warning if set. |
| `null` | CONFIRMED by `GET /v1/switcher/active`: no active administration. | "No client selected" (`client.header.none_selected`). |
| `undefined` | NOT YET KNOWN: the fetch hasn't answered (or hasn't been retried after failing). | A neutral loading state (`client.header.loading`, new catalogue key). |

`AuthenticatedMobileShell` (`App.tsx`) is where this lives: `useState<ClientBadge | null |
undefined>(undefined)`, fetched once in a `useEffect` on mount, passed down as a prop —
`MobileShell` and `ClientHeader` only ever mirror whatever state they're given, never fetch it
themselves. This is the same seam `administrationId`/`fiscalYearId` already are on `MobileShell`
(ADR-046's own docstring: a shell that fetched its own tenant context could be the one place that
gets it wrong), extended to the badge for the same reason.

A **failed** fetch (offline, a refusal) is caught and left at `undefined` rather than advanced to
`null` or a stale previous badge. An unanswered question is not the same fact as "confirmed no
client," and — consistent with CLAUDE.md's default-deny posture applied to a UI signal rather than
an authorization check — the safe failure mode for a component whose only job is to never assert
something false is to keep saying "not yet known" rather than commit to either wrong answer. This
does mean a sustained outage shows the loading state indefinitely rather than a dedicated error
message; see Known gaps.

**Test proof** (both new):

- `ClientHeader.test.tsx` — `badge={undefined}` renders `data-state="loading"` and does NOT
  contain "No client selected"; `badge={null}` renders `data-state="none"` and does; a resolved
  badge renders `data-state="active"`.
- `App.test.tsx` — a stubbed `fetch` that returns a controllable, not-yet-resolved `Promise`
  proves the ordering directly: immediately after mount (before the promise settles) the header is
  `data-state="loading"` and "Geen klant geselecteerd" is absent; only after the promise resolves
  to a JSON `null` does the header become `data-state="none"` and show that text. The two
  assertions being in the same test is what proves the sequencing, not just that each state can be
  reached independently.

### 3. Rendered once, hoisted above the tab switch, sticky

`<ClientHeader badge={badge} />` is rendered exactly once in `MobileShell.tsx`, above the per-tab
`{tab === "..." ? ... : null}` conditional and before the bottom tab bar in DOM order — so it is
structurally impossible for a tab to render without it, and a future sixth tab inherits it for
free rather than needing to remember it.

`position: sticky; top: 0` (not `fixed`) in `MobileShell.css`, with `z-index: 20` — higher than the
tab bar's `z-index: 10`, since a signal this task makes newly persistent must never end up
underneath something else. `sticky` was chosen over `fixed` specifically so the header's own height
is never assumed: it sits in normal document flow, so the ambiguity warning wrapping onto a second
line, or a long legal name wrapping, pushes the content below it down automatically. `fixed` would
have needed a matching `padding-top` on `.mobile-shell__content` sized to the header's rendered
height — exactly the kind of manual, easy-to-drift-from-content synchronization `sticky` avoids
(the bottom tab bar already needs this trick for its OWN fixed height, which is a fixed 56px and
therefore safe to hard-code — the header's height is not fixed in the same way).

### 4. The `ApiError`/`OfflineError` duplication, noted and left alone

Confirmed while writing `client/api.ts`: `ApiError` and `OfflineError` are now defined, verbatim,
in **four** places — `capture/api.ts`, `home/api.ts`, `invoicing/api.ts`, `templates/api.ts` — and
this task's `client/api.ts` follows the same pattern rather than being the one file that
introduces a shared module nothing else expects yet. Recorded here as an observation the codebase
should revisit, not fixed as part of this task (out of scope, and a shared-module refactor touching
four existing files carries more risk than this task's mandate).

## Alternatives considered

| Option | Rejected because |
|---|---|
| `badge: ClientBadge \| null`, defaulting the `useState` to `null` | The two-state shortcut this whole ADR exists to reject — see Decision §2. |
| A wrapper component around `ClientHeader` that renders a separate `<LoadingHeader>` until the fetch resolves, instead of teaching `ClientHeader` a third state | Two header markups (loading vs. resolved) to keep visually consistent instead of one, for no benefit — `ClientHeader` already owns "what the header looks like" and is the natural place for all of its states, not just two of three. |
| `position: fixed` for the sticky header, with a hard-coded `padding-top` on `.mobile-shell__content` | Requires the padding to track the header's actual rendered height, which varies (ambiguity warning, legal-name wrapping) — a manual number that drifts is worse than `sticky`'s self-sizing behaviour. |
| Fetching the badge inside `MobileShell` itself | Contradicts the seam `MobileShell`'s own docstring already establishes for `administrationId`/`fiscalYearId`: this shell renders what it's given, and is not one of potentially several places that could each independently get "the active client" wrong. |
| On a failed fetch, fall back to `null` ("no client selected") so the header always shows a definite state | Would render a false "no client" claim during a transient outage — worse than an honest, indefinite "not yet known," given what this requirement exists to prevent. |
| De-duplicating `ApiError`/`OfflineError` into a shared module as part of this task | Explicitly out of scope per the task's own ground rules; a four-file refactor is a separate, deliberate piece of work, not a side effect of wiring one new client. |

## Consequences

**Easier.** A future native app (MOB-001, P2) gets the same `GET /v1/switcher/active` endpoint,
the same `ClientBadge` shape, and the same three-state loading discipline to copy rather than
re-derive — the reasoning for why `undefined` and `null` must stay distinct is written down here
rather than only living in one engineer's head.

**Harder.** `ClientHeader` now has three render branches instead of two, and any future consumer of
`badge` (not just `MobileShell`) has to remember the tri-state contract rather than treating
`ClientBadge | null` as the whole story — a plain TypeScript union does not stop someone from
writing `badge === null` and quietly mishandling `undefined` as falsy-and-therefore-fine. The
`data-state` attribute exists partly so a future test can catch that class of regression directly.

**Known gaps.**

- **The plain web `Shell` (the non-mobile branch of `App.tsx`) has the identical gap and is
  untouched.** `ClientHeader` is not rendered there either, and `GET /v1/switcher/active` is not
  called from it. This was the user's explicit scope for this task ("mobile only... do not also
  wire this into the plain web Shell"), not an oversight, but it means FR-FRM-000a is still
  unmet on desktop web after this task ships.
- **No per-token colour CSS exists anywhere in the frontend yet**, for `ClientHeader` or for
  `ClientSwitcher`'s identical marker, on mobile or web. `CLIENT_COLOURS`' ten tokens
  (`indigo`, `amber`, ...) are attached as `data-colour`/`className` but nothing maps a token to an
  actual rendered colour — the marker is legible today only via its initials. This predates this
  task (confirmed: no `.client-marker` rule existed anywhere before it), and this task's explicit
  scope was sticky positioning, not the marker's visual design — a generic grey circle was added
  in `MobileShell.css` only so the marker is not literally invisible in the meantime.
  FR-FRM-000a's colour signal is not yet load-bearing until this is addressed.
- **A sustained fetch failure shows the loading state indefinitely, not a dedicated error state.**
  `AuthenticatedMobileShell` does not retry `fetchActiveBadge()` and has no separate "couldn't load
  the client" message distinct from "still loading" — both look identical. This is the deliberately
  safe choice (see Decision §2) but it is also a real UX gap for anyone on a genuinely broken
  connection: the header will look like it is loading forever rather than saying so.
- **`ApiError`/`OfflineError` are now duplicated across four API-client modules**
  (`capture/api.ts`, `home/api.ts`, `invoicing/api.ts`, `templates/api.ts`, and now
  `client/api.ts`). Worth a deliberate de-duplication pass; not undertaken here (see Decision §4).
