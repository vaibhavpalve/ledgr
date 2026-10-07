# Screens

After sign-in every screen uses one shell: a 240px white sidebar (lockup, nav, administration card at the bottom) with the active item on `leaf-tint`, and a content area with a small label above a `heading` title and one `action` button top-right. Under 900px the sidebar becomes a bottom tab bar with a central Capture button.

## Sign in / Create account

- Left: an inset `forest` panel (max 44% of the width, `radius-lg`): lockup, `display-xl` headline with "automatically." in `lemon`, one paragraph in `on-forest-muted`, a real product card (receipt → journal entry → Balanced chip), three proof points.
- Right: form centred in a 400px column. "Welcome back", passkey as the `action` button, divider "or sign in with email", email and password (Forgot password? on the label row), Sign in and Continue with Google as secondary buttons, Create an account link. Language switch and theme toggle top-right.
- Under 900px the panel collapses to a 160px band with lockup and headline.

## Onboarding (3 steps)

1. Company: KvK number fills name, address, legal form; confirm on a card.
2. BTW: frequency, KOR, BTW number, one helper line each.
3. Bank: PSD2 connect or MT940/CAMT upload. Skippable.
- "Step 2 of 3" as a `label` above the title; forest panel left as on sign-in.

## Overview (dashboard)

- Row 1: `forest` cash card with sparkline in `leaf` (5 cols), `lemon` BTW-due card with Prepare return (4 cols), `leaf-tint` To book card (3 cols).
- Row 2: To do list (7 cols), profit chart with this year in `forest`, last year in `sunken`, current month in `lemon` (5 cols).

## Inbox

- Drop zone (`surface`, dashed `line-strong`, "Drop receipts or forward to inbox@boeklite.nl"), then a table: thumbnail, supplier, date, amount, BTW, status chip.

## Receipt review

- Receipt on a `sunken` card left; proposed entry right with editable account and BTW code. Footer: Balanced chip (or the difference in `negative`), keyboard hint, Skip and Book entry. Book entry is disabled until balanced.

## Mobile capture

- Full-screen camera with `lemon` corner marks and shutter; after capture a white sheet with supplier, total, account and BTW, and Book entry.

## Grootboek

- Account tree by class left, entries right with running balance. Booked entries are read-only; Correct creates a reversing entry plus a new one.

## BTW return

- Stepper Review → Check → Submit. Rubrieken table, each row opens its entries. Final step shows the amount to pay or reclaim in `figure-xl`, then Submit to Belastingdienst. After filing a `leaf-tint` "Filed 28 Oct 2026" chip and a PDF.

## Accountant view

- Client table sorted by nearest deadline with BTW status chips per quarter. Inside a client, a `lemon-tint` banner names the client.

## States

- Empty: one sentence and one action. Loading: `sunken` skeleton rows. Errors: inline in `negative` with an icon.
