# Brief: restyle the Boeklite website with this design system

This replaces any earlier Boeklite design kit. Remove the old fonts (Instrument Serif, Geist, Geist Mono), the seal, the torn receipt, the double underlines and the mono labels.

Read in this order:
1. `BRAND.md`: colour roles, type, layout rules
2. `SCREENS.md`: what each screen contains
3. `tokens.css` / `tokens.json`: exact values; use the CSS variables, never hard-coded hex
4. `components.css` and `components/*/preview.html` + `README.md`: reference markup
5. `screens/*.png`: the target. These are rendered with the real font (Inter) and match the intended result.

Rules:
- One font: Inter from Google Fonts (`family=Inter:opsz,wght@14..32,400..700`). Headings 600 with negative tracking as in the tokens. All numbers `tabular-nums`.
- One primary button per view. On sign-in only "Continue with passkey" is filled; "Sign in" and "Continue with Google" are secondary.
- Keep all behaviour, routes, auth, i18n (EN/NL) and the theme toggle. Change presentation only.
- Light and dark theme via `data-theme` and `prefers-color-scheme`.
- Start with the sign-in page and match `screens/SignInScreen-light.png` closely before moving on.
