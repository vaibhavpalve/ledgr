# Screens

Every screen after sign-in uses one shell: a 232px sidebar on `cream`, separated by a `line` hairline (lockup, nav pills, the administration card at the bottom), and a content area with a mono overline, a `heading` title (one italic word allowed) and one `action` pill top-right. Under 900px the sidebar becomes a bottom bar with four tabs and a central lemon Capture button.

## Sign in / Create account

- Inset `pistachio` story panel left: lockup, `display-xl` headline with the italic key word on the double rule, one-line pitch, the paper composition (torn receipt, forest journal entry, seal, hand-drawn arrow), three numbered mono proof points.
- Form on `cream`: overline, "Welcome *back.*", passkey as the `action` pill, email and password under an "or" divider, outline pills for Sign in and Google.
- Under 900px the story panel becomes a 200px pistachio band with lockup and headline only.

## Onboarding (3 screens)

1. Company: KvK number fills name, address, legal form; confirm on a `surface` card.
2. BTW: frequency, KOR, BTW number, each with one helper line.
3. Bank: PSD2 connect or MT940/CAMT upload. Skippable.
- Progress as a mono overline "STEP 2 / 3". Pistachio panel left carries one paper object per step.

## Dashboard (Overzicht)

- Bento: `forest` cash card with lemon figure and sparkline (5 cols, tall); `lemon` BTW-due card with deadline and "Prepare return" (4 cols); `pistachio` unbooked card with a single big serif figure (3 cols); To do list (7 cols); profit chart with this year in `forest`, last year in `mint`, current month in `lemon` (5 cols).
- No welcome banners, no tips.

## Inbox

- Drop zone (`surface`, dashed `line-strong`, "Drop receipts or forward to inbox@boeklite.nl"), then a table: thumbnail, supplier, date, amount, BTW, status chip.

## Receipt review

- `forest` lightbox left with the receipt and zoom controls; proposed entry right with editable account, BTW code, debit, credit.
- Bottom bar: the seal (small) or the difference in `negative`; "Skip" ghost and "Book entry" `action`. Book entry is disabled until balanced. J/K moves, Enter books.

## Mobile capture

- Full-screen camera, receipt edges detected with lemon corner marks, a 72px lemon shutter. After capture a `surface` sheet slides up with supplier, total, BTW and the proposed account; "Book" as the `action` pill.

## Grootboek

- Account tree by class (0 Vaste activa … 8 Omzet) left, entries right with a running balance column. Booked entries are read-only; "Correct" creates a reversing entry and a new one, shown as a three-step chain.

## BTW return

- Stepper Review → Check → Submit as mono overlines. Rubrieken (1a, 1b, 5b …) as a table, each row opens its source entries. Final step: the amount to pay or reclaim in `figure-xl` on the double rule, then "Submit to Belastingdienst". After filing: `positive` chip "Filed 28 Oct 2026" and a PDF.

## Accountant view

- Client table sorted by nearest deadline with per-quarter BTW chips. Opening a client shows a `lemon-tint` banner "You are working in Papierhuis Amsterdam".

## States

- Empty: a `mint` shape, one sentence, one action. Loading: `sand` skeleton rows. Errors: inline in `negative` with an icon.
