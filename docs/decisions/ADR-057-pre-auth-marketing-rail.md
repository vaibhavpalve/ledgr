# ADR-057: A marketing rail on the login/signup/MFA screen

- **Status**: Accepted
- **Date**: 2026-09-14

## Context

The pre-authentication frame (`PreAuthScreen.tsx`) rendered a single centred card on a plain
ground — wordmark, heading, form, nothing else. It was styled (ADR-055 gave it real type and a
card), but structurally it read as a support-tool login screen: correct, but with no claim about
what the product does, shown to someone who by definition doesn't know yet. Direct user feedback
on a live build called it "dull, like a noob made it," alongside two reference screenshots — both
competitor accounting/booking products, and both, on inspection, the same pattern: a dark
marketing panel with a headline and a short feature list beside the actual sign-in form. Neither
reference has a separate marketing homepage; the login screen **is** their front page. That
reframed "also make the front page with all the features" as the same request as the layout
complaint, not a second, separate page to build.

## Decision

**Split the frame into two regions**: `.pre-auth__panel` (the existing card, unchanged in content)
and `.pre-auth__marketing` (new) — a fixed-dark rail with the wordmark, a headline stating the
product's own thesis, and six feature entries, each traced to a real, shipped requirement rather
than an invented claim:

| Feature copy | Requirement |
|---|---|
| "Snap a photo, we book it" | FR-EXP-001 / MOB-002 — offline-capable capture |
| "Nothing is ever quietly changed" | FR-GL-003 — the append-only ledger |
| "Never post to the wrong client" | FR-FRM-000a — the client switcher's three redundant signals |
| "Down to the cent, always" | NFR-031 — no floating point in the calculation path |
| "Know what needs you first" | FR-UX-005 — the prioritised home screen |
| "Sign in without a password" | IAM-012 — passkey satisfies sign-in and MFA together |

**Layout takes structure from the two references, not their palette.** Every colour in the rail is
LEDGR's own — the same hue-80 dark ramp and teal accent ADR-055 already established, used here as
fixed literals rather than swappable tokens (see Consequences). Copying a competitor's green or
their exact type treatment was never the ask; the ask was that this screen look like it was
designed at all.

**DOM order is not visual order.** `.pre-auth__panel` (the form) is first in the markup;
`.pre-auth__marketing` is second. A CSS grid (`grid-template-areas`, `min-width: 64rem`) places the
rail visually on the left. This means a keyboard or screen-reader user reaches the actual task —
signing in — before the pitch, the same reasoning already applied to where the language control
sits in this same file. The rail is a real `<aside>` landmark with an accessible name, not
`aria-hidden` — it's content someone can genuinely want to read, just not before the form.

**The rail is fixed-dark, not theme-following.** `#15120e` background, `#f2ece3` text, `#4fbcc0`
accent — literals, not `var(--ledgr-*)`. This mirrors the reasoning `tokens.css` already documents
for client marker colours: a brand statement, not a themed surface, and it must look identical
whether the viewer's own preference is light or dark. The task panel beside it keeps following the
theme exactly as before.

**Below 64rem the rail drops out of the grid entirely**, leaving the single centred card — which is
what one of the two references already shows at that width, not a compromise invented for this
change. A marketing pitch competing with the actual form for a 390px phone screen is a worse
screen, not a smaller version of a better one.

**One wordmark per view, not two.** The panel keeps its own wordmark as the narrow-viewport
fallback (shown when the rail is hidden) but hides it once the rail appears at 64rem, where the
rail's copy is the only "LEDGR" on screen — two of them on one wide viewport read as a mistake, not
confidence.

**The icon-wrapper bug this surfaced.** The six feature icons were first built with padding and a
background colour applied directly to the `<svg>` element. `app.css`'s global `box-sizing:
border-box` reset applies to `svg` too, and padding on an SVG with explicit `width`/`height`
attributes shrinks its CONTENT box by the padding amount rather than growing the element around
it — every icon rendered as a nearly invisible sliver inside an otherwise-empty tinted circle,
caught by actually looking at a screenshot rather than trusting the code. Fixed by moving
padding/background/radius onto a wrapping `<span>`, the same shape `HomeScreen.tsx`'s
`.home__item-icon` already uses for exactly this reason — this component just hadn't followed it.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Build a genuinely separate marketing homepage, distinct from the login screen | Neither reference the request pointed at has one — both combine marketing and login into one page, and building a second, separately-routed page (this app has no router by design) is a materially larger, differently-scoped change than "the login page looks dull." |
| Copy the references' colour palette (dark green, or black/white) | Would undo ADR-055's palette work, which was itself a deliberate, reviewed choice distinct from what every competitor in this market uses. Taking layout structure from a reference and inventing new brand colours to match it are two different kinds of "inspiration," and only the first was actually being asked for. |
| Let the marketing rail follow the light/dark toggle like the rest of the page | The rail is a brand statement (headline, product thesis) more than it is a themed surface — the same category client marker colours already fall into. A rail that flips between a light and dark treatment mid-onboarding reads as inconsistent branding, not as respecting a preference. |
| Vary the rail's copy by screen (login vs. signup vs. MFA) | A pitch that changes or disappears the moment someone commits to using the product (MFA enrolment is post-signup) reads as the product losing confidence in its own claims right when they matter least. One stable rail is also one set of strings to keep correct in two languages instead of three. |

## Consequences

**The login screen is now the closest thing LEDGR has to a front page**, and any future change to
the product's headline claim or feature list belongs here, in `PreAuthScreen.tsx`'s `FEATURES`
array and `auth.marketing.*` catalogue keys — not a second copy maintained somewhere else.

**Twelve new i18n keys** (`auth.marketing.*`), all Dutch/English complete; two required the
`grootboek`/`BTW` glossary terms rather than "ledger"/"VAT" in the English copy — FR-LOC-001c
caught this automatically via `check_translations.py`, exactly as designed.

**The rail's literal colours are a second place LEDGR's dark palette values now live** (alongside
`tokens.css`), the same trade-off client marker colours already accepted: a value that needs to
change in both places if the underlying dark-ramp hex values are ever revised. No test currently
guards that these stay in sync (unlike `tokens.css`/`tokens.ts`, which `tokens.test.ts` checks
automatically) — a reasonable follow-up if the palette changes again, not before.

**What this does not do.** No pricing, testimonials, footer links, or a genuinely separate
`/` marketing route — this is the login screen's marketing rail, not a full site. If a distinct,
separately-linkable homepage is wanted later, it is new scope, not a natural extension of this
change.
