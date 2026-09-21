# ADR-080: the Ledgr UI handoff tokens, adopted beside the `--ledgr-*` set

- **Status**: Accepted
- **Date**: 2026-09-21

## Context

An approved design handoff (`design/`, spec in `design/DESIGN.md`) redraws Login, Home, the app shell
and a marketing site in a cocoa-brown and gold palette, set in Newsreader, Hanken Grotesk and
IBM Plex Mono. ADR-055 and ADR-065 already give the web app a token package (`--ledgr-*`, graphite
and teal, Source Sans 3 and Source Serif 4) that every existing screen reads, with contrast and
CSS-to-TypeScript parity tests behind it.

## Decision

1. **`design/tokens/tokens.css` is copied byte for byte** to `packages/design-tokens/ui-tokens.css`
   and exported as `@ledgr/design-tokens/ui-tokens.css`. A test fails if the copy drifts from
   `design/tokens/tokens.css`. Token values are never edited here; the design system is upstream.
2. **The two sets coexist.** The handoff names (`--surface`, `--ink`, `--brand`, ...) do not collide
   with `--ledgr-*`. New and redesigned screens read only the handoff tokens; screens not yet
   redesigned keep reading `--ledgr-*`. Both respond to the same `data-theme` on `<html>`, so the
   existing theme switch and its pre-paint script drive both.
3. **Fonts stay self-hosted.** The handoff says to load from Google Fonts. ADR-061 removed that
   third-party request on PRIV grounds, so the three families ship as OFL woff2 files in
   `apps/web/public/fonts` (latin and latin-ext) with `@font-face` rules in `ui-fonts.css`.
4. **Icons are `lucide-react`**, the mapping in DESIGN.md section 5.

5. **Client identity (FR-FRM-000a).** The client's own ten-colour marker (`--ledgr-client-*`) is
   kept: the handoff's four avatar fills cannot tell ten clients apart. The header keeps its 3px
   client-colour rule and its name first in reading order; on a wide screen the visible marker,
   name and KvK sit in the sidebar company card, on a phone in the header itself. Needs sign-off
   against the PRD's "in the header on every screen".
6. **Home shows only what the API supplies.** No deltas, chart, filing deadline, invoice amounts,
   bell or bank step: none has a data source (item 7 lists what has since been added). The empty state (all three figures
   zero, nothing needing attention) swaps the bank step for "Add a customer".
7. **Recovery and dashboard follow-ups.** `POST /v1/auth/recover` (IAM-018) and a web forgot-password screen exist now. The dashboard returns `receipts_to_review` (Review nav count), `customer_name` and `amount`: a draft receipt's gross, or an overdue invoice's outstanding balance from the same open-items query the ageing report reads (after payments and credits, not the invoice total). The ledger gained one read-only function, `ledger.balances_as_of` (migration 0062, SECURITY INVOKER, same population as `trial_balance`), behind `LedgerService.balances_as_of`, so the dashboard returns `cash_history` (month-end cash, only months inside the fiscal year; the last point equals the headline) and `cash_change`. It also returns `vat_return`: the open filing period, its estimate and its due date from `api.vat.deadlines`, the end of the month after the period (owner-confirmed rule). The filing period is the fiscal year's `period_scheme` because the product has no filing-frequency setting yet; a business that books monthly but files quarterly would be shown the wrong period until that setting exists. A draft invoice carries no amount: nothing is owed on it yet.
8. **Website** lives at `/welcome` (public), English only, copy verbatim from the reference. `/`
   remains the signed-in home. Its footer links point nowhere yet and are plain text. Three reference lines were changed because the product cannot back them: Create account (not a free trial), the bank-import tick dropped, and the 42-days mockup line.

## Consequences

- Until the remaining screens move over, the app has two palettes: redesigned screens are cocoa
  and gold, the rest graphite and teal. Finishing that migration needs a decision on the one thing
  the handoff palette has no token for: a positive/success state (`--ledgr-positive`).
- The handoff supersedes ADR-065's palette for redesigned screens only.
- Client colours: the handoff's `ClientAvatar` fills use brand, accent, info and ink, whereas
  FR-FRM-000a's per-client marker colours (`clientTokens`) are unchanged and still drive the header rule
  where it is kept.
