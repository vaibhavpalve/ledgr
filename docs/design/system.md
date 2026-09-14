# LEDGR design system — the rules a frontend engineer follows without asking

Status: first complete version, 14 September 2026. Written by the product designer for the
frontend team; every value here is either a token in `packages/design-tokens` or a pixel value
measured off an artboard in `.design/`. Where this document and a token disagree, the token wins
and this document has a bug — say so.

Sources of truth, in order:

1. `packages/design-tokens/tokens.css` (+ `src/tokens.ts`, `src/primitives.ts`) — every colour,
   size, radius, duration. Nothing outside `primitives.ts` contains a hex value.
2. `.design/*.dc.html` — the artboards. Each screen section below names its artboard.
3. `apps/web/src/app.css` — the shared component layer that already implements a large part of
   the inventory below (`.button--*`, `.chip--*`, `.panel`, `.alert--*`, `.list__row`,
   `.dialog`, `.skeleton`, `.empty-state`). Extend it; do not fork it per screen.
4. ADR-055 (tokens, three-state theme), ADR-057 (pre-auth rail), ADR-061 (self-hosted fonts and
   layout tokens — this round).

The one-sentence version of the whole system: **warm paper, one teal, a serif for figures and
titles, and colour spent only on state.**

---

## 1. Principles applied (PRD §7.1, D1–D10) — one concrete rule each

| # | Principle | The rule in this codebase |
|---|---|---|
| D1 | One obvious next action | Exactly one `.button--primary` (filled teal) per view. A second filled teal button on the same screen is a bug. Secondary actions are `button` (outlined) or `.button--quiet`. In a dialog the primary is the last button on the right. |
| D2 | Accounting vocabulary is optional | Surface label is plain Dutch (“Te ontvangen van klanten”, “Wat u nog moet doen”); the accounting term appears as the `.caption` under it or in a `<abbr title>` / hover — never as the only label. The grootboek screens are the exception: they are the accountant’s room and use the accounting terms directly. |
| D3 | The system proposes, the user confirms | A pre-filled field carries a `.caption` beneath it saying where the value came from (“Voorgesteld uit het document”, “Vorige keer bij Gamma gebruikt”). Proposed values are shown in the input, never as placeholder text — placeholder text is not a value. |
| D4 | Progressive disclosure | Advanced fields sit behind a `<details>` with a `summary` styled as `.button--quiet` (“Meer opties”), collapsed by default. On the invoice form: cost centre, BTW override, notes. On onboarding: trade name, BTW number. |
| D5 | No dead ends | Every error surface (`.alert--attention`, `.field-error`, error page, toast) has three parts in this order: what happened, why (when known), what to do next — and the “what to do next” is a real control (a button or link), not a sentence. See `Toestanden.dc.html`. |
| D6 | Never lose work | Forms longer than three fields autosave a draft (`Concept opgeslagen 14:32` as a `.caption` next to the title). Leaving with unsaved changes triggers the *one* confirm dialog allowed on the screen (D7). |
| D7 | Reversibility over confirmation | Destructive-but-reversible actions (archive a customer, remove a draft line) act immediately and show a toast with `Ongedaan maken` for 8 s. Only genuinely irreversible actions get a modal dialog: sending an invoice, posting to the ledger, revoking a session, deleting a passkey. The dialog’s title states the irreversibility (“Deze factuur wordt definitief verstuurd”). |
| D8 | Two audiences, one product | Every list and table row is keyboard-reachable (`↑ ↓` to move, `Enter` to open) and the footer of a dense view shows the keycaps (see `LedgerTable.dc.html`, `ClientSwitch.dc.html`). Density toggle (`Ruim` / `Compact`) lives in Instellingen › Weergave, not in the bar. |
| D9 | Show the source | Any monetary figure that derives from a document is a link or a row that opens it. On a ledger detail the `Brondocument` button is always present (disabled with a reason when there is none). |
| D10 | Silence is a feature | Badges use the neutral count chip (`.chip` on `surface-raised`) unless the count is *overdue*; the attention colour is reserved for state that already exists in the data (a late invoice, a failed send), never for “new”, “unread”, or marketing. No toast for successful routine saves — the inline `Opgeslagen` state does that. |

---

## 2. Layout grid and breakpoints

### 2.1 Breakpoints (min-width, rem = 16px)

| Token / value | What changes |
|---|---|
| `< 40rem` (640px) | Phone. Dialogs anchor to the bottom (`.dialog-backdrop` already does this). Tables become card lists. Single column forms. |
| `40rem` | Dialogs centre. Figures grid goes 2-up. |
| `60rem` (960px) | `MobileShell.css` already turns the tab bar into a rail here. **Keep 64rem as the product-wide desktop switch** and treat 60rem as the shell’s internal step only — the pre-auth screen (ADR-057) uses 64rem and new screens use `@media (min-width: 64rem)` for their desktop layout so all desktop layouts switch together. |
| `64rem` (1024px) | Desktop: left rail 240px, client bar, page padding 26px, two-column detail layouts, drawers instead of full-screen forms. |
| `90rem` (1440px) | The artboard width. Content column is capped at `--ledgr-layout-measure` (72rem) so nothing stretches further. |

### 2.2 The desktop shell (`.design/Main.dc.html`, `Emailverificatie.dc.html`)

```
┌──────────┬───────────────────────────────────────────────────────┐
│ rail     │ client bar   (height 4rem, 3px bottom rule in client   │
│ 240px    │              colour)                                  │
│          ├───────────────────────────────────────────────────────┤
│          │ page  padding 26px 26px 0 ; max-width 72rem           │
└──────────┴───────────────────────────────────────────────────────┘
```

- **Rail** `--ledgr-layout-rail` = 15rem (240px). `surface-paper`, `1px border-subtle` on the
  right, padding `22px 14px 18px`. Brand lockup at the top (see §9), then nav groups separated by
  `--ledgr-space-5`. Group eyebrow: `.label` (12px/600/uppercase/0.09em, `text-muted`), padding
  `0 8px 6px`. Nav item: 38px tall (padding `9px 8px`), radius 9px, icon 20px + gap 11px + label
  16px/400. Active item: `accent-wash` background, `accent` text, weight 600. Count badge on an
  item: `.chip--accent` pill, 13px/700. `Instellingen` is pinned to the bottom above a
  `border-subtle` rule.
- **Client bar** `--ledgr-layout-bar` = 4rem (64px). `surface-paper`, bottom border **3px** in
  the active client’s colour (`.client-header[data-colour]` already does this), padding `0 26px`,
  gap 18px. Order left→right: client marker (32px disc, initials 13px/700 white) + legal name
  (16px/600) with `KvK 34281907` beneath (13px, `text-muted`) + chevron; a 1px × 30px divider;
  fiscal-year selector (`Boekjaar 2026` + chevron, 15px); global search (38px tall, max 380px,
  `surface-input`, `1px border-strong`, radius 10px, `Ctrl K` keycap); spacer; user avatar (32px
  disc on `surface-raised`, initials 13px/700 `text-secondary`).
- **Page** padding `26px 26px 0`, title row = `h1` (serif, `type-page`) + date or subtitle in
  15px `text-muted`, `margin-bottom` 20px. Content max width `--ledgr-layout-measure`.
- **Phone** (`< 64rem`): no rail; the client header is sticky (existing `MobileShell.css`), the
  5-tab bar is fixed at the bottom (64px + safe area). Page padding `--ledgr-space-4`.

### 2.3 Widths for the recurring layouts

| Layout | Token | Value | Where |
|---|---|---|---|
| Single-column form | `--ledgr-layout-form` | 40rem (640px) | onboarding, new customer, settings sections |
| Side drawer | `--ledgr-layout-drawer` | 30rem (480px) | customer form beside the list, invoice send panel |
| Detail split | — | `minmax(0, 1fr) 520px` | invoice detail: content left, PDF preview right |
| Dialog | `.dialog` | max 32rem | sign-out, revoke session, send invoice |
| Content measure | `--ledgr-layout-measure` | 72rem | every page |

---

## 3. Spacing scale

A 4px grid. Only these seven values exist as tokens; the artboards use a few off-grid literals
(26px page padding, 22px rail top padding, 9px nav-item padding) which are **deliberate optical
values** and are listed here so they are not “fixed” to the grid.

| Token | px | Use |
|---|---|---|
| `--ledgr-space-1` | 4 | label → input gap; icon → text inside a chip |
| `--ledgr-space-2` | 8 | gap between inline controls; chip padding; row internal gap |
| `--ledgr-space-3` | 12 | list row vertical padding; panel header gap; nav rail padding |
| `--ledgr-space-4` | 16 | panel body padding; form field vertical rhythm; phone page padding |
| `--ledgr-space-5` | 24 | between sections inside a page; dialog padding; between nav groups |
| `--ledgr-space-6` | 32 | empty-state padding; between page title block and first panel on phone |
| `--ledgr-space-7` | 48 | onboarding card padding; top of a wizard page |

Literals allowed off-grid, with the reason:

- Page padding **26px** (desktop). Chosen so the 3px client rule and 1px rail line read as part
  of the frame rather than as a cell border. Use `26px` literally, in one place per screen.
- Nav item padding **9px 8px** → 38px row; keeps eleven items inside 900px with room for the
  settings item.
- Table row height: `--ledgr-row-comfortable` 3rem (48px) / `--ledgr-row-compact` 2.5rem (40px),
  chosen by the density preference (`<html data-density="compact">`).

---

## 4. Type scale

Two families, both self-hosted (ADR-061) via `@font-face` in `tokens.css`:

- `--ledgr-font-sans` Source Sans 3 (variable, wght 200–900; we use 400/500/600/700) — all UI.
- `--ledgr-font-serif` Source Serif 4 (variable, wght 200–900, **opsz 8–60**; we use 400/600/700)
  — page titles (`h1`), monetary figures (`.ledgr-figure`), the wordmark. Because the serif file
  carries an optical-size axis, `font-optical-sizing: auto` (the browser default) picks the
  right master for a 33px figure and a 20px wordmark; do not set `font-variation-settings`
  manually.
- `--ledgr-font-mono` — TOTP secrets, API keys, the dev outbox. Nothing else.

| Token | Size | Weight | Leading | Family | Use |
|---|---|---|---|---|---|
| `--ledgr-type-micro` | 12px | 600, uppercase, 0.09em | tight | sans | eyebrows (`.label`), column heads, keycaps, chip text |
| `--ledgr-type-caption` | 13px | 400 (600 for labels) | snug 1.35 | sans | helper text (`.caption`), field labels, KvK line under a name, timestamps |
| `--ledgr-type-small` | 15px | 400 / 600 | snug | sans | secondary UI: buttons, table meta, filter chips, subtitles, alert body |
| `--ledgr-type-body` | 17px | 400 | normal 1.55 | sans | body, inputs, list rows, form values |
| `--ledgr-type-section` | 20px | 600 | tight 1.2 | sans (`h2`) / serif (`.wordmark`, figure in a card) | section headings, dashboard figure in a card |
| `--ledgr-type-page` | 24→26px fluid | 600 | tight | serif | `h1` — one per screen |
| `--ledgr-type-display` | 28→33px fluid | 600 | tight | serif | hero figures (dashboard cards on desktop: 31px), onboarding step title, pre-auth heading |

Rules:

- Numbers are always tabular lining (`.ledgr-num`, or any `table`/`dd`/`output` — already global).
  A column of amounts that does not line up is a bug.
- Monetary format is `€ 4.235,00` — a thin space after `€`, Dutch separators, always two decimals,
  from the API’s string value. Negative as `−€ 12,50` with a real minus (U+2212), never a hyphen.
- Dates in UI: `14 sep 2026` in prose, `14-09-2026` in table cells (fixed width, sorts by eye).
- `text-wrap: balance` on headings, `pretty` on prose (already global). Max measure for prose
  `46ch` (`.empty-state__body`) to `60ch`.
- Letter-spacing: serif titles `-0.006em`; figures `-0.01em`; eyebrows `+0.09em`; wordmark
  `+0.055em`. Nothing else.

---

## 5. Colour usage rules

The palette is in `primitives.ts`; components only use the semantic tokens. The rules below are
about *when* a token may be used.

### 5.1 Surfaces

| Token | Use |
|---|---|
| `surface-ground` | page background, and nothing else |
| `surface-paper` | rail, bar, panels, table bodies, dialogs, toasts |
| `surface-raised` | table header rows, hover on rows and quiet buttons, neutral chips, keycaps |
| `surface-sunken` | the document pane behind a receipt/PDF preview (`ReviewStack.dc.html`, `Factuur-Detail.dc.html`), skeleton highlight |
| `surface-input` | inputs and the global search field only |
| `surface-scrim` (new) | the backdrop behind a dialog or drawer |

A screen never nests more than two surfaces (ground → paper → raised). Paper-on-paper is a
mistake; use a `border-subtle` rule instead.

### 5.2 Text

`text-primary` for content, `text-secondary` for labels and de-emphasised cells (a
`Tegenrekening` column, a `KvK` line), `text-muted` for helper text, placeholders and eyebrows.
All three clear 4.5:1 on all three grounds (asserted in `contrast.test.ts`); pick by hierarchy,
not by taste.

### 5.3 The accent (teal `--ledgr-accent`)

Allowed: the one primary button per view, links, the active nav item (wash + text), the
selected row (`accent-wash` fill + 3px inset left edge), the focus ring, a count badge, the
`Ruim/Compact` selected segment, the progress bar of a review stack, the wordmark glyph.
Not allowed: as a large background (the marketing rail is the sole exception and uses a
literal), as a text colour for body copy, on icons that are not interactive.

### 5.4 The status colours — when attention may be used

Four families, one lightness, one chroma. Each has a solid (text/icon/edge) and a wash
(background). **A wash is never a text colour; a solid is never a large background.**

| Family | Use | Never |
|---|---|---|
| `positive` | posted / paid / verified / “In balans”; the success toast edge | a button |
| `caution` | draft, concept, incomplete, “nog niet geboekt”, “Verstuurd — nog niet betaald”, the ambiguous-colour warning, the email-verification banner | on more than one element per row |
| `attention` | **state that is already bad in the data**: overdue, failed send, revoked, a reversing entry, a validation error, a destructive-outline button | any *action* (“Herinnering sturen” is teal, `Main.dc.html` row 1), “new”, badges for unread, decoration, a filled button |

Attention is spent on at most three things in one row: the 3px left edge, the icon, the
status word. Never the whole row’s text. Two attention rows in a list are fine; a whole list in
attention wash means the wrong colour was picked for the state.

### 5.5 Client colours

Ten theme-independent markers (`--ledgr-client-*`). Used on: the marker disc, the 3px bar
rule, the 3px left edge of the switcher’s current row, the underline of the “from → to” switch
strip. Never as a text colour, never on a button, never as a tint over a panel. Initials are
`text-on-marker` (pure white). Colour is recognition; initials + name + KvK are identification.

### 5.6 Fixed literals

Only three places may contain a colour that is not a token: the marketing rail (ADR-057), the
white QR panel (`.mfa-totp-qr`), and the rendered invoice PDF / receipt image (document content,
not UI). Each already carries a comment saying why.

---

## 6. Component inventory with states

Every component below is either already in `app.css` (name given) or specified here for the
lead to add to `app.css`. Sizes are desktop; phone differences noted.

### 6.1 Buttons (`button`, `.button--primary`, `.button--quiet`, `.button--danger`)

| Kind | Fill | Border | Text | Use |
|---|---|---|---|---|
| Primary | `accent` | `accent` | `text-on-accent`, 15px/600 | the one action of the view |
| Secondary (default `button`) | `surface-paper` | 1px `border-strong` | `text-primary` | everything else |
| Quiet | transparent | transparent | `text-secondary` | cancel, back, skip, “Meer opties” |
| Danger | transparent | 1px `attention` | `attention` | revoke, delete passkey, archive — never filled |
| Icon-only | as secondary | | 38×38px, icon 20px, `aria-label` required | pagination, zoom, close |

Sizes: min-height `--ledgr-touch-min` (48px) on phone and for any button in page content on
desktop; **42px inside a table detail or drawer footer** and **38px in the bar/filters row** —
these two smaller sizes exist for density only and are set by the container, not per button.
Padding `8px 16px` (secondary) / `8px 22px` (primary with label), radius `--ledgr-radius-control`
(10px; artboards use 9px inside tables — treat as 10). Icon in a button: 18px, gap 9px.

States: hover → `surface-raised` (secondary/quiet) or `accent-hover` (primary), 120ms;
active → same as hover, no scale; focus-visible → the global 3px ring; disabled → `text-muted`
on `surface-raised`, `border-subtle`, `cursor: not-allowed`; **loading** → label replaced by
“Bezig met versturen…” text, the button keeps its width (`min-width` set from the idle width),
`aria-busy="true"`, no spinner icon.

### 6.2 Inputs (`input`, `select`, `textarea`)

Height 48px (44px inside the review card and drawers, matching `ReviewStack.dc.html`), padding
`8px 12px` (artboards: `0 13px`), `surface-input`, 1px `border-strong`, radius 10px, text 17px.
Label above: 13px/600 `text-secondary`, gap 4px (5px on artboards). Helper `.caption` below, gap
4px. Required is the default — mark **optional** fields with “(optioneel)” in the label, never
mark required ones with an asterisk.

States: focus → 2px `accent` border + `0 0 0 3px rgba(accent, 0.16)` ring (`ReviewStack.dc.html`
Grootboekrekening field); the label turns `accent` too. Invalid → `aria-invalid="true"` gives a
2px `attention` border, and a `.field-error` (13px `attention`, warning icon 16px) beneath;
the input keeps its value. Disabled → `surface-raised`, `text-muted`. Read-only value (a sealed
posted entry) → **no input chrome at all**: render as text (`LedgerTable.dc.html` detail).
Prefilled by the system → normal input + `.caption` “Voorgesteld uit het document” (D3).

Numeric/monetary inputs: `inputmode="decimal"`, tabular nums, right-aligned, `€` as a prefix
adornment inside the field (not part of the value). Dates: native `<input type="date">` styled
by `color-scheme`; the displayed format follows the locale.

### 6.3 Selects and comboboxes

Native `<select>` for ≤ 8 options (legal form on phone, period scheme, BTW treatment). A
combobox (input + listbox) for accounts, customers and clients: same input chrome, a 20px
chevron right, listbox as a `.panel` with `shadow-md`, rows 40px, the highlighted row
`accent-wash`, matches bolded, code in `text-secondary` before the name (`4300 — Onderhoud
gebouwen`). Footer keycaps `↑ ↓ kiezen · Enter openen` on desktop (`ClientSwitch.dc.html`).

### 6.4 Choice cards (new — onboarding legal form, appearance, invoice layout)

A radio group rendered as cards: `.choice-card` = `surface-paper`, 1px `border-strong`,
radius 14px, padding 16px 18px, min-height 88px, grid `repeat(auto-fill, minmax(15rem, 1fr))`,
gap 12px. Contents: title 17px/600, plain-language description 14px `text-muted`, the formal
term as a 12px eyebrow chip (`B.V.`, `Eenmanszaak`). Selected: 2px `accent` border,
`accent-wash` background, a 20px check-circle icon top-right in `accent`. Focus: global ring.
The native `<input type="radio">` stays in the DOM, visually hidden, so arrow keys work.
See `Onboarding-Bedrijf.dc.html`.

### 6.5 Chips (`.chip`, `.chip--accent|positive|caution|attention`)

Pill, 12px/600, letter-spacing 0.03em, padding `2px 8px`, `surface-raised` + `text-secondary`
by default. Status chips carry an optional 16px icon. Used for: invoice status, expense status,
session “Dit apparaat”, the `Nu geopend` marker, count badges. **A status chip never sits alone
as the only signal on a row** — the row’s edge or icon carries it too (forced-colors).

Status vocabulary (Dutch, from the catalogue):
`Concept` (caution), `Verstuurd` (neutral), `Te laat` (attention), `Betaald` (positive),
`Geboekt` (positive), `Gecorrigeerd` (caution), `Tegenboeking` (attention), `Geverifieerd`
(positive), `Niet geverifieerd` (caution).

### 6.6 Panels (`.panel`, `.panel__header`, `.panel__body`)

`surface-paper`, 1px `border-subtle`, radius 14px (`--ledgr-radius-panel`; artboards show 12px
on dashboard cards — use 14 everywhere from now on). No shadow. Header 16px padding, bottom rule;
body 16px (18px 20px for figure cards). A panel is never nested in a panel.

### 6.7 Tables (new `.table`, based on `LedgerTable.dc.html`)

CSS grid rows, not `<table>` layout, but real `<table>` semantics via `role="table"` on a
`<div>` grid **or** an actual `<table>` with `display: grid` on `tr` — the lead may choose; the
artboard uses grid `div`s. Header: 40px, `surface-raised`, bottom 1px `border-strong`, 13px/700
uppercase 0.045em `text-secondary`. Body rows: `--ledgr-row-comfortable` (48px) / compact
(40px), 16px text, 1px `border-subtle` between rows, padding `0 16px`. Numeric columns
right-aligned, tabular, and the balance column 600. Placeholder for “no value” is `—` in
`text-muted`. Footer/total row: `surface-raised`, 2px `border-strong` top, 700.

Row states: hover → `surface-raised`; selected → `accent-wash` + `box-shadow: inset 3px 0 0
accent`; exception (reversing entry) → `attention-wash` + attention icon in the 34px marker
column; a marker column is present only when at least one row can carry an exception.
Expanded detail beneath a selected row: `surface-raised`, 2px `accent` top border, padding
`14px 16px 14px 50px`, and it contains **no input chrome** for posted data.

Column widths in the artboards are literal px (`34px 100px 124px minmax(0,1fr) 196px 124px
124px 132px`) — copy them; they were measured against the longest realistic content.

Phone: a table becomes a card list (`.list__row`): primary text, secondary line, amount right in
serif 600 (`.list__row-amount`). Nothing scrolls horizontally.

### 6.8 List rows (`.list`, `.list__row`, `.list__row-text`, `.list__row-amount`)

Min 48px, padding `12px 16px`, an optional 36px icon disc on the left (`surface-raised` neutral,
`caution-wash`/`attention-wash` when the state warrants it — `HomeScreen.css`), text column, an
amount in serif 600 right-aligned min-width 116px, a chevron 18px `text-muted` that turns
`accent` and slides 2px on hover. Attention rows carry a 3px left edge (`Main.dc.html`).

### 6.9 Tabs and segmented controls

Page tabs (Grootboek: `Saldibalans · Rekeningschema · Boekingen`): a row under the title, each
tab 15px/600, padding `10px 2px`, gap 26px, `text-secondary`; active is `text-primary` with a
2px `accent` bottom rule; `role="tablist"`, arrow keys move. Segmented controls
(`.language-switcher`, `.theme-toggle`, `Ruim/Compact`): 3px padded group on `surface-raised`,
selected segment `surface-paper` + `accent` text + `shadow-sm`, `aria-pressed`.

### 6.10 Dialogs (`.dialog-backdrop`, `.dialog`, `.dialog__actions`)

Backdrop `surface-scrim`. Dialog max 32rem (switcher 600px), `surface-paper`, radius 16px
(`radius-panel` +2 for the largest floating surface — acceptable literal), `shadow-lg`. Title
serif 24px/600, close icon-button 36px top-right. Body 17px. Actions right-aligned, quiet →
secondary → primary. Focus is trapped; `Esc` closes unless the dialog is the irreversible-action
kind, in which case `Esc` = cancel. Phone: anchored to the bottom, full width, radius on top
corners only.

### 6.11 Drawers (new `.drawer`)

A side panel for a form that belongs to a list (customer create/edit beside the customers list,
`Klanten.dc.html`). Width `--ledgr-layout-drawer` (30rem), `surface-paper`, 1px `border-subtle`
left, `shadow-lg`, header (title `h2` + close), scrollable body padding 24px, sticky footer
(`surface-ground`, top rule, padding `15px 26px`) with quiet + primary. Below 64rem the drawer
is a full-screen route (`/klanten/nieuw`) — same component, no backdrop. Entry: 200ms
`translateX(16px)→0` + fade, `ease-out`.

### 6.12 Toasts (new `.toast`)

Bottom-left on desktop (24px from edges), above the tab bar on phone, full width minus 16px.
`surface-paper`, 1px `border-subtle`, radius 14px, `shadow-lg`, padding `12px 16px`, 15px text,
a 3px **left** edge in the family colour, a 20px icon in the family solid, an optional action
button (quiet, 600, `Ongedaan maken`) and a 36px close. Max width 420px. Auto-dismiss 6s
(8s with an action), paused on hover/focus, `role="status"` (positive/neutral) or `role="alert"`
(attention). One toast at a time; a newer one replaces the older. Never a toast for a
successful save — use the inline `Opgeslagen` state. See `Toestanden.dc.html`.

### 6.13 Inline notices and banners (`.alert`, `.alert--attention|caution|positive`, new `.banner`)

`.alert`: inside a form or panel, 3px left edge, wash background, 15px text, icon 20px, and a
control on the right if there is a next step. `.banner`: page-wide, sits **between the client bar
and the page**, full width, `caution-wash` (verification) / `attention-wash` (failed), 48px
min-height, padding `10px 26px`, icon 20px + text 15px + action button (secondary, 36px) +
dismiss (only if the state can be dismissed — the email-verification banner cannot; it has
`Opnieuw versturen` and `Ik heb het bevestigd`). Persistent banners do not animate.
See `Emailverificatie.dc.html`.

### 6.14 Skeletons (`.skeleton`)

The shape of what will arrive: a 3.5rem row per list row, a 31px-tall bar for a figure, a full
panel outline. Shimmer 1.4s; off under reduced motion. Skeletons appear only after 300ms of
waiting (avoid flashing on fast loads) and are replaced in place — no layout shift.

### 6.15 Empty states (`.empty-state`, FR-UX-004)

Centred, padding `32px 16px`, icon 24px `text-muted` in a 48px `surface-raised` disc, title
17px/600, body ≤ 46ch 15px `text-muted`, then the **one** action that creates the first item
(primary if the page has no other primary, otherwise secondary). Every list has one. The text
teaches what belongs here, in the user’s words; see `Toestanden.dc.html` for the five written.

### 6.16 Badges and client markers (`.client-marker`, `.client-marker--<colour>`)

Marker: 32px disc (40px in the switcher, 24px in the switch strip), colour from
`--ledgr-client-*`, initials 13px/700 white, `lining-nums`. Unknown colour → `surface-raised`
+ `text-secondary`. Always accompanied by the name; on the bar also by the KvK line. Ambiguity
warning (same colour as another client) is a `caution` inline chip with the warning icon,
full-width under the name (`ClientSwitch.dc.html`).

### 6.17 Keycaps

12px/600 `text-muted`, 1px `border-strong`, radius 5px, padding `1px 6px`, `surface-paper`.
Shown in the footer of any keyboard-first surface (switcher, review stack, table detail) and in
the global search (`Ctrl K`). Hidden on touch devices (`@media (hover: none)`).

### 6.18 Progress (wizard steps and review progress)

Wizard: `Stap 1 van 3` eyebrow + a 6px track (`border-subtle`, radius pill) filled `accent` to
the fraction, 132px wide next to the title on desktop, full width on phone. Steps are also
listed as text (`Bedrijf · Boekjaar · Klaar`) with the current one in `text-primary` 600, done
ones with a 16px check in `positive`.

---

## 7. Iconography

- One grid: 20×20 viewBox, `fill="none"`, `stroke="currentColor"`, round caps and joins.
- Painted stroke is **1.5px at every rendered size**, so `stroke-width` scales inversely:
  22px → 1.36, 20px → 1.5, 18px → 1.67, 16px → 1.88, 24px → 1.25. Tokens: `--ledgr-icon-sm`
  16px (inline in text, chips, keycap rows), `--ledgr-icon-md` 20px (nav, buttons, list
  rows, alerts), `--ledgr-icon-lg` 24px (empty states, choice cards, onboarding done state).
- Icons never carry meaning alone: every icon-only control has `aria-label`; every status icon
  sits next to its word.
- Colour: `currentColor`. Icons inherit `text-secondary` by default, `accent` when interactive
  and active, the family solid when carrying state.
- The set is drawn inline in the artboards — copy the paths. Established glyphs: home, camera,
  check-circle, list, document, book (grootboek), percent (BTW), bars (rapportage), gear, search,
  chevron-down/right, arrow-right/left, clock (overdue), lock (sealed), undo-arrow (reversal),
  pencil (draft), info-circle, warning-triangle, x, plus, users (klanten), building
  (organisatie), shield (beveiliging), key (passkey / password), fingerprint (passkey),
  monitor/phone (sessions), mail (email), brush (factuurontwerp), globe (taal), sun/moon/monitor
  (weergave), upload, download, calendar, refresh, external-link, eye / eye-off.
- No icon package, no emoji, no filled icons. A filled disc behind an icon (36px, wash colour)
  is the only permitted embellishment.

---

## 8. Motion

| Token | Value | Use |
|---|---|---|
| `--ledgr-duration-fast` | 120ms | hover/colour changes, chevron slide, chip state |
| `--ledgr-duration-base` | 200ms | client rule sweep, drawer/dialog enter, tab underline, toast enter |
| `--ledgr-duration-slow` | 320ms | theme swap (background/colour), onboarding step change |
| `--ledgr-ease-out` | `cubic-bezier(0.2, 0, 0, 1)` | everything |
| `--ledgr-ease-spring` | `cubic-bezier(0.2, 0.9, 0.3, 1.1)` | the check-circle draw-on in the onboarding done state, and nothing else |

Rules: motion is always a state transition, never decoration; nothing loops except the
skeleton shimmer; nothing moves more than 16px; exits are faster than entries (fast vs base);
`prefers-reduced-motion` zeroes everything (already global in `tokens.css`) and the done-state
check simply appears. No parallax, no page transitions between routes — a route change is an
immediate swap with focus moved to the new `h1`.

---

## 9. Brand: wordmark, lockup, icons

- Glyph: two entries ruled off by a double line (`Wordmark.tsx`). In the rail it is 22px, teal.
- Lockup (`apps/web/public/brand/ledgr-wordmark.svg`): glyph + `LEDGR` in Source Serif 4 600,
  letter-spacing 0.055em, cap height aligned to the glyph’s top rule; clear space = the glyph
  width on every side. Ink version (`currentColor` text, teal glyph) for paper; a light version
  is the same file with `color: #f2ece3` for the marketing rail.
- Favicon `favicon.svg`: the glyph alone on a teal rounded square (`#00686C`, radius 22%),
  strokes in white; readable at 16px because the double rule is two 2-unit bars.
- PWA icons: same tile at 192 and 512; the maskable variant keeps the glyph inside the central
  80% (safe zone radius 40% of the side) with the teal bleeding to the edge.
- `theme-color` stays `surface-ground` per theme (chrome continues the page, not the brand).

---

## 10. Density

`<html data-density="comfortable|compact">`, default comfortable, set from Instellingen › Weergave
(not from the bar — `app.css` header comment). Compact changes only: table/list row height
(48→40), panel body padding (16→12), dashboard figure size (display→section), nav item padding
(9→6px). Nothing else changes — in particular not type size and not touch targets on phone
(compact is ignored below 64rem).

---

## 11. Focus and keyboard

- One global `:focus-visible` ring: 3px `border-focus`, offset 2px (inset −3px on regions).
  Never remove it; never restyle it per component.
- Tab order = visual order, with the one documented exception (ADR-057 form before rail).
- Every screen: `Skip to content` link first; focus moves to the `h1` after a route change and to
  the first field after a drawer opens; returns to the opener when a dialog/drawer closes.
- Lists and tables: `↑`/`↓` move the selection, `Enter` opens, `Esc` collapses a detail. The
  review stack: `J`/`K` browse, `Enter` posts. The switcher: `Ctrl K` opens the search; typing
  filters; `Enter` opens the highlighted client.
- Shortcuts are shown as keycaps in the surface’s footer and never conflict with browser or
  screen-reader keys (no single-letter shortcuts outside a focused list).
- Touch targets ≥ 48px (`--ledgr-touch-min`) on phone; icon-only buttons 38px on desktop only
  where they sit in a dense toolbar.

---

## 12. Screen-by-screen spec

Every string on the artboards is Dutch; the catalogue keys exist or must be added in both
languages. Glossary terms (`BTW`, `KvK`, `grootboek`, `suppletie`) keep their Dutch form in the
English UI (FR-LOC-001c).

### 12.1 Onboarding wizard — `Onboarding-Bedrijf.dc.html`, `Onboarding-Boekjaar.dc.html`, `Onboarding-Klaar.dc.html`, `Onboarding-Telefoon.dc.html`

Route `/welkom/bedrijf` → `/welkom/boekjaar` → `/welkom/klaar`. No rail, no client bar (there
is no client yet): a centred 640px column on `surface-ground` with the wordmark top-left (22px
glyph + serif) and the language/theme controls top-right. Step eyebrow `Stap 1 van 3`, progress
track, serif `type-display` title (“Over uw onderneming”), one-line lede in `text-secondary`.

Step 1 fields: `Naam van de onderneming` (17px input), `KvK-nummer` (8 digits, `inputmode=
numeric`, caption “Staat op uw uittreksel; wij gebruiken het om uw gegevens op te halen”),
legal form as **choice cards** (Eenmanszaak · V.O.F. · B.V. · Stichting · Vereniging — each with
one plain sentence, e.g. “U bent zelf de onderneming; winst is uw inkomen”), then `<details>
Meer opties` with `Handelsnaam (optioneel)` and `BTW-nummer (optioneel)`. Footer: quiet
`Later afmaken` (D6 — the draft is kept) left, primary `Verder naar boekjaar` right.

Step 2: `Begin boekjaar` / `Einde boekjaar` (date inputs, default 1 jan – 31 dec of the current
year), `Periodes` as a 2-card choice (`Per maand` – “Voor wie maandelijks BTW aangeeft”, `Per
kwartaal` – “De meeste kleine ondernemingen”), and the **period preview**: a `.panel` listing
the generated periods from `GET /v1/fiscal-years/preview` as a 3-column grid of chips (`jan
2026 · 01-01 t/m 31-01`), with the count in the panel header (“12 periodes”). A broken year
(start after end) shows a `.field-error` on the end date and an empty preview with the text
“Kies een einddatum na de begindatum”.

Step 3 (done): the check-circle drawn on in `positive` (48px), title “Uw administratie staat
klaar”, three facts as a `dl` (`62 rekeningen aangemaakt uit RGS 3.8`, `Boekjaar 2026, 12
periodes`, `Van Doorn Bouw B.V. · KvK 34281907`), one primary `Naar het overzicht`, and a
quiet `Eerst een bon vastleggen` — the two things a new user actually does next. No confetti.

Phone (`Onboarding-Telefoon.dc.html`): same content, single column, cards stacked, the footer
sticky at the bottom with the primary full-width and the quiet link beneath it.

### 12.2 Firm portfolio, empty — `Kantoor-Portfolio.dc.html`

Route `/klanten` for a `kind: firm` organization. Rail shows the firm’s nav (Portfolio, Klanten,
Instellingen). The client bar shows the firm itself (`Bakker & Zn. Accountants`, no KvK line,
neutral 3px rule) and no fiscal-year selector. Page: `h1` “Uw klanten”, and one large empty
state in a panel: icon users (24px), title “Voeg uw eerste klant toe”, body “Elke klant krijgt
een eigen administratie, kleur en boekjaar. U wisselt met Ctrl K.”, primary `Klant toevoegen`.
Beneath it, quiet: “Of nodig eerst een collega uit”. Nothing else on the page — an empty
portfolio is one decision.

### 12.3 Sales invoices list — `Facturen.dc.html`, `Facturen-Telefoon.dc.html`

Route `/facturen`. Title row: `h1` “Verkoopfacturen”, primary `Nieuwe factuur` right. Filter
row (38px controls): status segmented (`Alle · Concept · Verstuurd · Te laat · Betaald`), a
period select, a search field (`Zoek op nummer of klant`). Three figure cards above the table
(`Openstaand € 18.940,00`, `Te laat € 4.235,00` in attention text, `Deze maand verstuurd
€ 12.300,00`). Table columns: marker 34px · Nummer 120px · Klant 1fr · Factuurdatum 120px ·
Vervaldatum 120px · Status 130px · Bedrag 132px right. Overdue rows carry the attention edge +
clock icon; drafts the caution pencil. Row click → detail. Footer: “23 facturen · totaal
€ 41.775,00”. Empty state: “Nog geen facturen” / “Maak uw eerste factuur; het PDF-ontwerp en de
verzending zijn al geregeld.” / `Nieuwe factuur`.

Phone: list rows (customer + number line, status chip, amount serif), a floating primary at the
bottom above the tab bar is **not** used — the primary lives in the title row, sticky.

### 12.4 Invoice detail — `Factuur-Detail.dc.html`

Route `/facturen/:id`. Breadcrumb `Verkoopfacturen › 2026-0184`. Title `h1` “Factuur 2026-0184”
+ status chip + customer link. Actions right: secondary `PDF downloaden`, secondary `Dupliceren`,
primary `Versturen` (draft) / `Herinnering sturen` (overdue) / none (paid — the primary
disappears when the invoice is done; D1 allows zero). Layout `minmax(0,1fr) 520px`: left the
facts (`dl` two-column: Klant, Factuurdatum, Vervaldatum, Betaalkenmerk, BTW) and the line
table (Omschrijving · Aantal · Stukprijs · BTW · Bedrag) with totals block (Subtotaal, BTW 21%,
Totaal serif 24px); an activity list beneath (“Verstuurd 3 sep 09:12 naar facturen@kroon.nl”,
“Aangemaakt 2 sep”). Right: the **PDF preview** — a `surface-sunken` pane, the A4 page at 0.6
scale on paper with `shadow-md`, zoom controls bottom-right (as `ReviewStack.dc.html`), a caption
“Zo ziet uw klant de factuur”. Sending opens the irreversible dialog (D7) with the recipient
address editable, the message, and primary `Definitief versturen`.

### 12.5 New invoice — `Factuur-Nieuw.dc.html`

Route `/facturen/nieuw`. Title `h1` “Nieuwe factuur”, `.caption` “Concept opgeslagen 14:32”
(autosave, D6). Two columns `minmax(0,1fr) 340px`: left the form — customer combobox (with
“Nieuwe klant” as the last option), Factuurdatum, Vervaldatum (default +30 days, caption “30
dagen, uw standaard”), then the lines editor: a grid table with 44px inputs (`Omschrijving 1fr ·
Aantal 88px · Stukprijs 120px · BTW 128px · Bedrag 120px read-only`), `Regel toevoegen` as a
quiet button with plus icon under the last row, a per-row remove icon-button (36px, appears on
hover/focus, `aria-label="Regel verwijderen"`), `<details> Meer opties` (Betalingsvoorwaarden,
Opmerking op de factuur, Kostenplaats). Right column: a sticky **totals panel** (Subtotaal, BTW
per tarief, Totaal in serif display) and the primary `Versturen`, secondary `Voorbeeld PDF`,
quiet `Opslaan als concept`. Validation errors from the API (`invoice.*` catalogue) render as
`.field-error` on the exact field or row they name — the API already tells us `{line}`.

### 12.6 Customers — `Klanten.dc.html`

Route `/klanten` (business) — the firm uses the same route for its *portfolio*; the customer
master for a business is `/klanten` and for a firm’s client administration it is
`/klanten` inside that administration. Title `h1` “Klanten”, primary `Nieuwe klant`. Search
38px. Table: Naam 1fr · Plaats 160px · KvK 120px · BTW-nummer 170px · Openstaand 132px right ·
Laatste factuur 120px. Row click opens the **drawer** (§6.11) with the customer form: Naam,
Contactpersoon (optioneel), E-mailadres voor facturen, Adres (Straat en nummer, Postcode 120px +
Plaats, Land select default Nederland), `<details> Zakelijke gegevens` (KvK-nummer,
BTW-nummer with caption “Verplicht bij BTW verlegd en leveringen binnen de EU”), Betaaltermijn
(select 14/30/60 dagen). Footer: quiet `Annuleren`, primary `Klant opslaan`. The artboard shows
the drawer open in the “Nieuwe klant” state. Empty state: “Nog geen klanten” / “Een klant is
wie u factureert. Voeg er een toe, of maak hem straks vanuit een nieuwe factuur.” /
`Nieuwe klant`.

### 12.7 Settings — `Instellingen-Profiel.dc.html`, `Instellingen-Beveiliging.dc.html`, `Instellingen-Organisatie.dc.html`

Route `/instellingen/{profiel|weergave|beveiliging|organisatie|factuurontwerp}`. Layout: a
settings sub-nav (200px, list of links with the active one `accent-wash`) beside a 640px
content column; on phone the sub-nav is the settings screen and each section a route.

- **Profiel en taal**: Naam, E-mailadres (read-only with `Geverifieerd` positive chip, or the
  caution chip + `Opnieuw versturen`), Taal (segmented `Nederlands · English`), secondary
  `Opslaan` inline `Opgeslagen ✓` state.
- **Weergave**: Thema as three choice cards with small previews (Systeem · Licht · Donker),
  Dichtheid segmented (Ruim · Compact), Getallen en datums (read-only example row “€ 1.234,56 ·
  14-09-2026” — follows the administration’s `formatting_locale`, not a preference).
- **Beveiliging** (the artboard): four panels in order — Wachtwoord (Huidig, Nieuw with
  strength caption “Minimaal 12 tekens; wij controleren tegen bekende lekken”, `Wachtwoord
  wijzigen`), Authenticator-app (TOTP: status row `Ingesteld op 12 aug 2026` + danger-outline
  `Verwijderen`, or `Instellen`), Passkeys (list rows: name/device, `Aangemaakt 3 sep`, `Laatst
  gebruikt vandaag`, danger-outline `Verwijderen`; the last remaining factor’s remove button
  is disabled with the caption “Dit is uw enige tweede factor; voeg eerst een andere toe” —
  IAM-010f), Actieve sessies (rows: device icon, `Windows · Chrome`, `Amsterdam · nu` /
  `2 dagen geleden`, `Dit apparaat` chip on the current one, danger-outline `Afmelden` on the
  others; `Alle andere sessies afmelden` secondary at the panel foot). Revoking opens the
  irreversible dialog.
- **Organisatie**: Juridische naam, Handelsnaam, KvK (read-only after onboarding, caption “Wijzigt
  u via de KvK; neem contact op als het hier niet klopt”), BTW-nummer, Adres, Boekjaren panel
  (list of years with `Huidig` chip, secondary `Nieuw boekjaar openen`), Getalnotatie
  (`nl-NL`).
- **Factuurontwerp**: a card with a 240px-wide thumbnail of the current template and primary
  `Ontwerp aanpassen` which mounts `TemplateDesigner` full-page at
  `/instellingen/factuurontwerp/bewerken`.

### 12.8 Grootboek — `Grootboek-Saldibalans.dc.html`, `Grootboek-Rekeningschema.dc.html`, `LedgerTable.dc.html`

Route `/grootboek` (tabs: `Saldibalans · Rekeningschema · Boekingen`), `/grootboek/rekening/:code`
(the account detail = `LedgerTable.dc.html`), `/grootboek/boekingen/:id`.

- **Saldibalans**: filter row (fiscal year select, period range, `Exporteren`). Summary strip of
  four figure cards (`Activa`, `Passiva`, `Kosten`, `Opbrengsten` — plain labels with the
  accounting term as caption) — then the table grouped by RGS class: group header rows
  (`surface-raised`, 13px uppercase, with group subtotals), account rows `Rekening 100px · Naam
  1fr · Debet 132px · Credit 132px · Saldo 132px`, account codes `text-secondary`, and a total
  row `In balans` in positive with debit = credit. A row click opens the account detail.
  Unbalanced (should never happen) renders the total row in attention with the text “Niet in
  balans — neem contact op” (the integrity job’s job).
- **Rekeningschema**: search (`Zoek op nummer of naam`), the same grouped table but with columns
  `Rekening 100px · Naam 1fr · RGS-code 180px · BTW-standaard 160px · Status 110px`; a
  `Rekening toevoegen` secondary (progressive: most users never do); seeded accounts carry the
  caption chip `RGS 3.8`. Empty state (before onboarding seeds): “Nog geen rekeningschema” /
  “Rond de onboarding af; wij zetten het RGS-schema voor uw rechtsvorm klaar.” / `Naar
  onboarding`.

### 12.9 Email-verification banner — `Emailverificatie.dc.html`, `Emailverificatie-Telefoon.dc.html`

Sits between the client bar and the page on every authenticated screen while
`email_verified: false`. `caution-wash`, 3px `caution` left edge (desktop) / top edge (phone),
mail icon 20px, text “Bevestig uw e-mailadres — we hebben een link gestuurd naar
s.bakker@vandoornbouw.nl. Boeken in het grootboek kan pas daarna.”, secondary `Opnieuw
versturen` (36px), quiet `Adres wijzigen`. Not dismissible. After resend, the button becomes
the inline text “Verstuurd — controleer ook uw spam” for 60s. On the artboard it is shown over
the dashboard.

### 12.10 Empty, error and notice patterns — `Toestanden.dc.html`

One sheet, nine tiles: (1) list empty state, (2) search-no-results (“Geen facturen voor
‘kroon 2025’” + `Filters wissen`), (3) first-run dashboard empty state (“Niets vraagt op dit
moment uw aandacht”), (4) offline notice (`caution` alert with queued count — “3 bonnen wachten
op verbinding; ze worden vanzelf verstuurd”), (5) API validation error on a field, (6) a failed
action alert with retry (“Versturen is niet gelukt: de mailserver antwoordde niet. Uw factuur is
nog een concept. Probeer het opnieuw.” + `Opnieuw proberen`), (7) full-page error (404 “Deze
pagina bestaat niet” with `Naar het overzicht`; 403 “U heeft geen toegang tot deze administratie”
with `Wissel van klant`), (8) toasts: positive with undo, neutral, attention, (9) skeleton of a
list. Copy on the sheet is final copy; put it in the catalogue as-is.

### 12.11 Existing artboards, unchanged in structure

`Main.dc.html` (dashboard), `ReviewStack.dc.html` (review), `LedgerTable.dc.html` (account
detail), `ClientSwitch.dc.html` (switcher + palette). Their rail glyph is updated to the
double-rule mark so the four agree with `Wordmark.tsx`.

---

## 13. Checklist for a new screen

1. One `h1` (serif), one primary action, a URL.
2. Every list has an empty state with the creating action.
3. Every error has the next step as a control.
4. Every amount is a string from the API, rendered tabular, serif when it is a headline figure.
5. Attention colour only on state that is already in the data.
6. 48px targets on phone; global focus ring untouched; `↑ ↓ Enter` on lists.
7. Both themes checked (`data-theme="dark"`), both languages, 390 and 1440.
8. No hex value outside `primitives.ts`; no new font; no icon package.
