# Ledgr design spec

Ledgr is Dutch bookkeeping for small businesses and their accountants. The interface is calm, precise and confident: warm paper neutrals, one cocoa-brown brand, one gold highlight, and status colours that only ever mean status.

Source of truth: `tokens/tokens.json` (values, usage notes). Visual truth: `reference/*.png` and `reference/*.html`. If this file and the tokens disagree, the tokens win.

## 1. Principles

1. **Status colours are reserved.** Red (`danger`), amber (`warning`), teal (`info`) mean something. The brand is brown so it never reads as a warning. Never use red for a debit; debits are normal bookkeeping.
2. **Numbers are the hero.** Every amount, IBAN and account number is set in IBM Plex Mono with tabular figures, right-aligned in tables.
3. **Borders over shadows.** Separate surfaces with 1px lines. Shadows are for floating things only (menus, dialogs, the marketing mockups).
4. **Status never depends on colour alone.** Every badge and message carries a word and, where space allows, a glyph.
5. **Permanent and exact.** Copy and UI reinforce that entries are never deleted, corrections are new entries, and amounts are exact to the cent.
6. **One highlight per view.** Gold is a fill used sparingly: the active nav pill, one marked key word in a headline, delta chips, chart marks.

## 2. Colour roles (light / dark values are in `tokens.css`)

| Role | Token | Use |
|---|---|---|
| Page | `surface` | App and site background |
| Card / input | `surface-raised` | Cards, tables, inputs, popovers |
| Sidebar / table head | `surface-sunken` | Sidebar, table header rows, code |
| Hairline | `border-subtle` | Dividers, row rules, card borders |
| Control edge | `border` | Input, button and checkbox edges (3:1) |
| Text | `ink`, `ink-muted` | Primary and secondary text |
| Brand | `brand`, `brand-hover`, `on-brand` | Primary button fill, links, focus, brand avatar |
| Brand tint | `brand-tint` | Selected rows, "Booked" badge |
| Highlight | `accent`, `on-accent` | Gold fill with dark-brown text |
| Panel | `panel`, `on-panel`, `on-panel-muted` | Login left half, marketing CTA band |
| Status | `danger`/`-tint`, `warning`/`-tint`, `info`/`-tint` | Overdue, Draft, Importing |
| Focus | `focus` | 2px ring, 2px offset |

Rules:
- Text on a fill uses the matching `on-*` token, never hard-coded white or black.
- Link and label text in brand colour uses `brand` (it meets 4.5:1 on `surface`, `surface-raised` and `brand-tint` in both themes).
- Do not add colours. If something needs a new colour, stop and ask.

## 3. Typography

| Style class | Family | Size / line | Weight | Use |
|---|---|---|---|---|
| `display-lg` | Newsreader | 48 / 52 (marketing hero goes to 84 / 88, -2.4px tracking) | 500 | Hero, report titles |
| `display-md` | Newsreader | 32 / 38 | 500 | Page titles, wordmark |
| `heading-lg` | Hanken Grotesk | 22 / 30 | 600 | Section headings |
| `heading-md` | Hanken Grotesk | 17 / 24 | 600 | Card titles |
| `body` | Hanken Grotesk | 15 / 22 | 400 | Default |
| `body-sm` | Hanken Grotesk | 13 / 20 | 400 | Table cells, helper text |
| `label` | Hanken Grotesk | 13 / 16 | 500 | Field labels, buttons, badges |
| `caption` | Hanken Grotesk | 12 / 16 | 400 | Timestamps |
| `figure-lg` | IBM Plex Mono | 28 / 34 | 500 | KPI totals |
| `figure` | IBM Plex Mono | 14 / 20 | 400 | Amounts in tables |

Wordmark: the word "Ledgr" in Newsreader 500, preceded by a 28-34px outline mark (rounded square with a vertical rule at 1/3 and two horizontal rules; see `reference/Login.html`). This is a placeholder until a real logo exists.

## 4. Spacing, radius, elevation

Spacing scale: 4, 8, 12, 16, 24, 32, 48, 64 (`space-1` to `space-8`). Card padding 16-20, gap between cards 20-24, page gutter 32 (16 on mobile). Radii: `radius-sm` 4 chips, `radius-md` 8 buttons and inputs, `radius-lg` 12 cards, `radius-pill` badges. Marketing mockups may use 14-36px radii for large panels.

## 5. Components

**Button.** Height 40 (app), 48 (login), 52 (marketing). Padding 0 16-24. Radius 8-10. Label `label` weight 500-600. Variants:
- primary: fill `brand`, text `on-brand`, hover `brand-hover`.
- secondary: fill `surface-raised`, 1px `border`, text `ink`, hover `surface-sunken`.
- ghost/link: text `brand`, underline on hover.
- small: height 34, padding 0 12.
Only one primary per view. Disabled: 50% opacity, `not-allowed`.

**Input.** Height 48 (login) or 40 (app). 1px `border`, radius 8, fill `surface-raised`, padding 0 14. Label above at `label` in `ink`; helper text below in `body-sm` `ink-muted`. Error: border `danger` plus an error sentence telling the user what to change. Placeholder in `ink-muted`. Focus: 2px `focus` ring, 2px offset. Password field has a "Show / Hide" text button top-right of the label row.

**Badge.** Pill, `label` 12/16, padding 2/8. Variants: `booked` (brand-tint / brand), `draft` (warning-tint / warning), `overdue` (danger-tint / danger), `review` and `syncing` (info-tint / info), neutral (surface-sunken / ink). Always a word; add a check glyph for Booked.

**Card.** `surface-raised`, 1px `border-subtle`, radius 12, padding 16-20. No shadow in the app, no coloured left border, no nesting.

**KPI card.** 132px high. Label (`label`, `ink-muted`), value (`figure-lg`, 30/38 in the reference), then a caption line: a gold **delta chip** (pill, `accent` fill, `on-accent` text, 600) followed by `ink-muted` text. When a KPI has no data the chip is omitted.

**Nav item (sidebar).** Height 40, radius 8, padding 0 12, gap 12, icon 20 (stroke 1.7). Default `ink-muted`; hover `border-subtle` fill; active `accent` fill with `on-accent` text. Group labels are 12/600, letter-spacing 0.6px, uppercase, `ink-muted`. Review shows an info badge with the count.

**Data table / list rows.** Header row `surface-sunken`, `label` `ink-muted`. Rows 51-72px, 1px `border-subtle` top border, padding 0 20. Debit and Credit are separate right-aligned columns in `figure`. Selected row `brand-tint`. Totals row weight 500-600.

**Amount.** `figure` mono, tabular, right-aligned, locale-formatted (`Intl.NumberFormat('nl-NL', {style:'currency', currency:'EUR'})` gives `€ 1.250,00`). Real minus sign U+2212 for negatives. `danger` colour only for overdue or overdrawn, with a word.

**Client avatar.** 34-44px square, radius 8-12, initials 700, each client has its own fill (brand, accent, info, ink). Fill plus initials plus name are always visible together.

**Chart (cash position).** One series: 2px `brand` line, area under it `brand` at 16% opacity, 1px `border-subtle` gridlines at 4 ticks, mono 11px y labels, sans 12px x labels, no legend for a single series. Hover shows a crosshair line, a 10px dot and a tooltip (fill `ink`, text `surface`, mono 12px). Last point highlighted by default.

**Icons.** Lucide, 1.5-1.7px stroke, 20px. Mapping used in the references: House, Camera, CircleCheck, List, FileText, User, BookOpen, Settings, Search, Bell, Plus, LogOut, KeyRound, Moon, Check, ArrowRight, ChevronDown.

## 6. Screens (1440 wide desktop references)

### 6.1 Login (`reference/Login.png`, `Login-dark.png`)
Two columns: left 800px brand panel, right 640px form on `surface`.
- **Left (`panel`):** padding 56/72. Background: 12 horizontal hairlines 60px apart at 9% `on-panel`, one vertical rule at x=72, three concentric quarter-rings from the top-right corner (radii 190/300/410, 16% opacity) and a solid `accent` disc (radius 80) in that corner. Top: wordmark. Middle: headline in Newsreader 64/66, -1.2px tracking, "automatically." wrapped in a gold marker (`accent` fill, `on-accent` text, padding 0 14, radius 10). Below: 18/27 `on-panel-muted`, max 500px. Then a 656x318 composition: a rotated (-4deg) receipt card (236 wide) overlapping a "Journal entry" card (388 wide) with a gold circular arrow between them, plus two floating pill chips ("BTW 21% detected", "Balanced to the cent"). Bottom: three points in a grid, each with a top rule (`on-panel` at 28%), title 15/600, text 13/20.
- **Right:** 400px column, vertically centred. Title Newsreader 42/46 "Welcome back", sub 16/24. Fields: email, password (with Show), "Forgot password?" right-aligned. Primary "Sign in" 48px full width. Divider "or continue with". Two equal secondary buttons: Google, Passkey. Footer line "Don't have an account? Create one". Top-right: EN/NL segmented control and a theme toggle button. Bottom: copyright line.
- Behaviours: EN/NL switch, theme toggle, show/hide password, Sign in goes to Home. Passkey uses WebAuthn.
- Dutch strings: Welkom terug / Log in op je boekhouding. / E-mailadres / Wachtwoord / Toon, Verberg / Wachtwoord vergeten? / Inloggen / of ga verder met / Nog geen account? Account aanmaken. Headline: "Van bon tot aangifte, automatisch." Points: Blijvende boekingen, Tot op de cent, Inloggen met passkey.

### 6.2 Home (`reference/Home.png`, `Home-empty.png`, `Home-dark.png`)
Sidebar 248px (`surface-sunken`, 1px right border, padding 20/16) + main. Header 72px with search (440 wide, "Ctrl K" hint), FY select, bell, avatar. Content padding 28/32, 20px gaps.
- Sidebar top: wordmark, then the **company switcher** card (avatar DB, "Datapal BV", "KvK 92590659", chevron). Groups: Daily work (Home, Capture, Review, Overview), Sales (Sales invoices, Customers), The books (Grootboek). Bottom: Settings, Sign out.
- Title row: "Good afternoon, Max" (Newsreader 36/40) + date; right: secondary "Capture receipt" and primary "New invoice".
- KPI row: three cards (Cash position, Receivables, BTW estimate).
- Left column 688px: "Needs your attention" card (count badge, rows 72px: status badge 72 wide, title 15/22 500, meta 13/20, amount, action button 128 wide) then "Recent activity" (rows 51px: text, amount, relative date).
- Right column 420px: Cash position chart card (276 high), then "Next BTW return" card (Q3 2026 in Newsreader 30, due date, estimate, two check lines, "Prepare return" secondary button).
- **Empty state** (`Home-empty.png`): KPIs show `€ 0,00` with a helpful caption and no delta chip; the left column becomes a "Set up <company>" four-step checklist (numbered circles, done step shows a check, one primary action); the chart card shows a dashed empty message.
- Data shown is sample data. Wire real data; keep the empty state for new companies.

### 6.3 Website (`reference/Website.png`)
1440 wide, sections: header (88), hero (700), fact strip (140), Capture feature (720), The books feature (720, `surface-sunken` band), Accountants feature (640), CTA band (400), footer.
- Hero: headline 84/88 with the gold marker on "automatically.", 20/30 lead, primary + secondary button. Right: a `panel` block (radius 36 on the left corners, bleeds off the right edge) with a browser-frame product mockup and two floating cards.
- Features alternate text and mockup. Each has an eyebrow (13/600, 1.4px tracking, `brand`), a 52/56 Newsreader heading, a 19/29 lead and 2-3 tick bullets.
- CTA band: `panel` fill, rules and rings pattern, 56/60 headline, one `surface` button.
- All copy on the site comes from the product's real capabilities (offline capture, permanent entries, exact decimals, BTW, per-client colours). Pricing, testimonials and customer logos are intentionally absent: add only real ones.

## 7. Content rules

- Sentence case everywhere. Short, direct. No exclamation marks, no emoji, no filler ("Oops", "Awesome").
- Address the user as "you". Actions are verbs: Book entry, Import bank file, File BTW return, Send reminder.
- Keep Dutch accounting terms (BTW, grootboek, dagboek, RGS, KvK). Gloss once in English on first appearance.
- Errors say what to change: "Enter an amount with two decimals", not "Invalid input".
- Dates in tables: `01 Oct 2026`. Money: `€ 1.250,00` (nl-NL).

## 8. Accessibility

- Text 4.5:1 (3:1 at 24px+), control edges and focus ring 3:1, in both themes. All token pairs above already meet this.
- Real `<button>`, `<a>`, `<label>` + `<input>`. Icon-only buttons need `aria-label`. Charts get a text summary via `aria-label` and a table alternative.
- Touch targets 44px minimum on mobile (Expo: 48). Respect `prefers-reduced-motion` and `prefers-color-scheme`.
- Motion: fades or 120ms ease-out only.

## 9. Do not

- Do not use red, orange or amber for anything except status.
- Do not add gradients, coloured left-border cards, emoji, or drop shadows on cards.
- Do not hard-code hex values in components; use tokens.
- Do not invent statistics, testimonials, customer logos or prices.
- Do not show a debit in `danger`.
