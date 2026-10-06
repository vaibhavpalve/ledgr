# Brief: redesign the Boeklite website with this design system

Read these files first, in this order:
1. `BRAND.md` (rules: colour roles, type, signature details, logo use)
2. `SCREENS.md` (what each screen contains)
3. `tokens.json` / `tokens.css` (exact values; use the CSS variables, never hard-code hex)
4. `components.css` and `components/*/README.md` + `preview.html` (reference markup for each component)
5. `screens/*.png` (target look; fonts in the PNGs are fallbacks, the real ones are Instrument Serif, Geist, Geist Mono from Google Fonts)

Task:
- Map the tokens into the project's styling setup (Tailwind theme, CSS variables, or the existing theme file). Keep token names.
- Rebuild the sign-in page to match `screens/SignInScreen-light.png` and `components/SignInScreen/preview.html`, then the app shell and dashboard.
- Replace the logo/favicon with the files in `logo/`.
- Reuse existing components where they exist; restyle them rather than duplicating them.
- Keep all existing behaviour, routes, i18n (EN/NL) and auth logic. Change presentation only.
- Support light and dark theme via `data-theme` and `prefers-color-scheme`.
- Check contrast and keyboard focus (2px focus ring, 3px offset) on every page.
- Work screen by screen; show me the sign-in page before moving on.
