Dutch bookkeeping for small businesses and their accountants. The look is calm and precise: one clean typeface, generous white space, deep forest green with light green for "done" and a single touch of lemon yellow for "look here". Nothing decorative; the product itself is the illustration.

## Voice

- Short declarative sentences. "Balanced to the cent." not "Your books are perfectly balanced!"
- Address the user as "you". Sentence case for buttons, titles and menus.
- Keep Dutch accounting terms in Dutch in both languages: grootboek, BTW, aangifte, KvK. Explain them once in helper text.
- Money: `€ 1.234,56` in both languages. Dates: `18 Sep 2026` (EN), `18 sep 2026` (NL).
- No emoji, no exclamation marks. Errors say what happened and what to do.

## Colour

- `canvas` page, `surface` cards, `sunken` for header bands and quiet fills, `line` dividers.
- `forest` is the brand: the sign-in panel, the cash card, the app icon, the primary button (`action`).
- Light green (`leaf`, `leaf-tint`, `leaf-ink`) means done: Booked, Balanced, Filed, the active nav item, the To book card.
- `lemon` means attention and is used sparingly: one highlighted word on the sign-in panel, the BTW card, count badges, the current-month bar. `lemon-tint` / `lemon-ink` for review chips.
- One `action` button per view. Everything else is a secondary (white with `line-strong` border) or ghost button.
- Status always carries a word and an icon, never colour alone. Focus: 2px solid `focus`, 2px offset.

## Type

- One family: **Inter**. Headings use the display cut (`display` family; on Google Fonts the `opsz` axis switches automatically) at weight 600 with negative tracking: `display-xl` 56px, `display-l` 36px, `heading` 28px, `figure-xl` 40px.
- Interface text in `sans`: `title`, `body`, `body-s`, `label`, `button`. Body text never below 13px.
- Every number uses `font-variant-numeric: tabular-nums`; amounts right-aligned with Dutch decimal comma.
- Monospace only for scanned receipt text (`receipt`).
- Load: `https://fonts.googleapis.com/css2?family=Inter:opsz,wght@14..32,400..700&display=swap`.

## Layout and shape

- 8px grid via `space-*`. App pages: 240px sidebar, content padding `space-7`/`space-6`, card grid 12 columns with 16px gaps.
- Buttons and inputs 48px tall (36px small), `radius-sm`. Cards `radius-md` with a 1px `line` border, no heavy shadows. The sign-in panel `radius-lg`.
- Tables: header band in `sunken`, 44px rows, `line` between rows, totals above a 1px `ink` rule.
- No rotated objects, stamps, hand-drawn marks or textures.

## Logo

- The B: a stem and two equal bowls split by a hairline, debit equals credit. Files in Logos.
- `boeklite-mark.svg` for app icon and favicon. `boeklite-glyph.svg` on light grounds, `boeklite-glyph-reversed.svg` on `forest` and in dark theme, `boeklite-mark-mono.svg` for one-colour print.
- Lockup: glyph at 1.05× the cap size, gap 10px, "Boeklite" in Inter Display 600 with -3% tracking.

## Iconography

- Lucide, 1.75px stroke, 18px in UI, colour inherits text. Icons sit beside a label except in toolbars (tooltip + `aria-label`).

## Components

Button, Input, StatusChip, LanguageSwitch, JournalEntry, Receipt and Logo are defined here. Screens are described in the Screens section, with SignIn, Dashboard, ReceiptReview and MobileCapture previews.
