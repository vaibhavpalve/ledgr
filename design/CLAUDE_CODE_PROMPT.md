# Prompt for Claude Code

Paste this into Claude Code at the root of the Ledgr repo, after copying this folder into the repo as `design/`.

---

You are implementing a new UI for Ledgr from an approved design handoff in `design/`.

Read first, in this order:
1. `design/DESIGN.md` (the spec and the rules)
2. `design/tokens/tokens.json` and `design/tokens/tokens.css` (the only source of colour, type, spacing, radius, shadow values)
3. `design/reference/*.png` (visual truth) and `design/reference/*.html` (exact markup, spacing and CSS to translate; not to ship as is)

Then work in this order, committing after each step:

1. **Tokens.** Copy `tokens.css` into the app's global styles (or `tokens.ts` for React Native). If the web app uses Tailwind v4, also add `tailwind.theme.css`. Load the fonts Newsreader (400, 500), Hanken Grotesk (400-700) and IBM Plex Mono (400, 500). Add a theme switch using `data-theme` (light, dark, system).
2. **Primitives.** Build Button, Input, Badge, Card, KPI card, NavItem, DataTable, Amount, ClientAvatar, EmptyState, and the cash-position chart, all using tokens only. Follow section 5 of `DESIGN.md`.
3. **Login** to match `reference/Login.png` and `Login-dark.png`. Keep the existing auth logic (email and password, Google, passkey). Add EN/NL switching and the theme toggle.
4. **Home** (app shell, sidebar, header, dashboard) to match `Home.png`, `Home-empty.png`, `Home-dark.png`. Replace the sample data with real data from the existing API. Keep the empty state for companies with no bookings.
5. **Marketing website** to match `Website.png`, using the exact copy in the reference.
6. **Verify.** Run the app and compare each screen with its reference screenshot at 1440x900 in light and dark, and at 390 wide for the app screens (collapse the sidebar into a bottom bar or drawer; keep the same tokens). Fix differences in spacing, type and colour. Report anything you could not match.

Rules:
- Do not change token values or add colours. If something is missing, stop and ask.
- Do not use red, orange or amber except for danger, warning and status meaning. Debits are never red.
- Keep existing routes, data fetching and auth behaviour. Only the presentation changes.
- Amounts always use the mono figure style, right-aligned in tables, formatted with `Intl.NumberFormat('nl-NL', ...)`.
- Do not invent statistics, testimonials, prices or customer logos.
- The screenshots were rendered with fallback fonts; the real fonts are Newsreader, Hanken Grotesk and IBM Plex Mono, so expect slightly different text widths.
- Use Lucide icons (mapping in `DESIGN.md` section 5).

When done, list what changed, what deviates from the reference and why, and what still needs a decision.
