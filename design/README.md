# Ledgr design handoff

Brand: cocoa brown with a gold highlight, warm paper neutrals, light and dark themes.

- `DESIGN.md` — the spec: principles, colour roles, type, components, screen layouts, copy and accessibility rules
- `CLAUDE_CODE_PROMPT.md` — the prompt to give Claude Code, with the order of work
- `tokens/tokens.json` — source of truth for all design values
- `tokens/tokens.css` — CSS variables for light and dark, type classes
- `tokens/tailwind.theme.css` — Tailwind v4 `@theme` mapping (optional)
- `tokens/tokens.ts` — the same tokens for React Native / Expo
- `reference/` — static HTML and PNG renders of Login, Home (populated, empty, dark) and the marketing website

Notes
- The HTML in `reference/` is a design reference, not production code. It loads the fonts from Google Fonts; the PNGs were rendered without them, so text widths differ slightly.
- Figures, client names and invoice numbers are sample data.
- The design lives on the "Ledgr UI redesign" canvas and the "Ledgr" design system in Claude. If a value changes there, regenerate `tokens/` from the design system.
