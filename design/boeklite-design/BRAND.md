Dutch bookkeeping for small businesses and their accountants. The art direction is **editorial ledger** in a fresh, finished palette: warm pistachio green and lemon yellow on cream paper, with deep forest green for type and actions. Green says done; yellow says look here. Big serif figures, mono overlines, hairline rules, and the accountant's double underline for a total.

## Voice

- Short declarative sentences. "Balanced to the cent." not "Your books are perfectly balanced!"
- Address the user as "you". Sentence case for buttons, titles and menus.
- Keep Dutch accounting terms in Dutch in both languages: grootboek, BTW, aangifte, KvK. Explain them once in helper text.
- Money: `€ 1.234,56` in both languages. Dates: `18 Sep 2026` (EN), `18 sep 2026` (NL).
- No emoji, no exclamation marks. Errors say what happened and what to do: "Debit and credit differ by € 0,40. Adjust a line or add a rounding line."

## Colour

- Three families. **Green:** `pistachio` (the light brand field), `leaf` (graphic accents), `forest` (actions, feature cards). **Yellow:** `lemon` (highlights) and `lemon-tint`. **Paper:** `cream`, `surface`, `sand`, `mint`.
- Proportion per screen: about 65% `cream`/`surface`, 20% greens, 10% `forest`, 5% `lemon`. Green carries calm and completion; yellow only points at what needs attention.
- `pistachio` takes a whole field once: the sign-in story panel. In the app it fills one card (Unbooked) at most.
- `lemon` marks what needs money attention: the BTW card, count badges, the current-month bar, double rules on forest, the seal core. Never text on light grounds.
- `action` is the primary button: a forest pill in light, a pistachio pill in dark. One per view. Secondary buttons are outline pills in `line-strong`.
- `forest` cards (cash, receipt lightbox) carry `cream` text and `lemon` figures.
- Status: `positive`, `negative`, `lemon-tint`/`lemon-ink`, always with a word and icon. Because the brand is green, a "done" state is never shown by green alone.
- Control borders `line-strong`; `line` for dividers. Focus: 2px solid `focus`, 3px offset.

## Type

- `serif` (Instrument Serif) for headlines and hero figures: `display-xl`, `display-l`, `heading`, `figure-xl`. Italic is reserved for the one word that carries the promise ("*automatically.*", "*back.*", "*today.*").
- `sans` (Geist) for all interface text: `title`, `body`, `body-s`, `label`, `button`.
- `mono` (Geist Mono) for `overline` (uppercase, tracked) above every figure and section, and for every `amount` in tables, tabular and right-aligned.
- Google Fonts: `family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500`.

## Signature details

- **The double rule.** A total, and the key word of a hero headline, sits on two 2px rules 3px apart (`ink`, or `lemon` on forest). It is the accountant's "this is final" mark. Use it once per screen.
- **The seal.** A round `forest` stamp with circular mono text ("BALANCED TO THE CENT ·") and a lemon check. It confirms a balanced entry. Never more than one on screen.
- **Paper objects.** Receipts are white with a torn zigzag bottom edge and `receipt` type, tilted -4° to -6° only on the pistachio story panel.
- **Overline + figure.** Every number of consequence has a mono overline above it.

## Layout and shape

- Bento grid: 12 columns, `space-3` gaps, cards `radius-md`. Pills for buttons, chips and nav (`radius-pill`), `radius-sm` for inputs.
- Inputs and buttons 48px tall. Table rows 48px, `line` between rows, header band in `sand`.
- The sign-in story panel is inset `space-3` from the window edge with `radius-xl`.

## Logo

- The **balanced B**: a stem and two equal bowls split by a hairline: debit equals credit. Files in Logos.
- `boeklite-mark.svg` for app icon and favicon: forest tile, cream B, lemon lower bowl.
- `boeklite-glyph.svg` on `cream`/`surface`: forest with a `leaf` lower bowl. `boeklite-glyph-reversed.svg` on `forest` and dark theme: cream with a lemon lower bowl. `boeklite-mark-mono.svg` on `pistachio` and `lemon` grounds and for one-colour print.
- Lockup: glyph at cap height, gap `space-2`, then "Boeklite" in Instrument Serif. The wordmark is live type.
- Never put the two-colour glyph on `pistachio` or `lemon`; never stretch the bowls.

## Iconography

- Lucide, 1.5px stroke, 18–20px, colour inherits text. Icons sit beside a label except in toolbars (tooltip + `aria-label`).

## Components

Button, Input, StatusChip, LanguageSwitch, JournalEntry, Receipt, BalanceSeal and Logo are defined here. Screens are described in the Screens section, with SignIn, Dashboard, ReceiptReview and MobileCapture previews.
