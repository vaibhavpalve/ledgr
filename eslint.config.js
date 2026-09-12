import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";

export default tseslint.config(
  { ignores: ["**/dist/**", "**/node_modules/**", "**/coverage/**"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    // CMP-012/FR-LOC-004 (WCAG 2.2 AA). Static analysis, not a substitute for
    // the axe-core render tests (src/testing/axe.ts) — this catches what is
    // visible in the JSX itself (a missing alt, a click handler with no key
    // handler, an interactive element built from a non-interactive one)
    // before anything ever renders; axe catches what only exists once real
    // DOM and ARIA relationships are computed.
    files: ["apps/web/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks, "jsx-a11y": jsxA11y },
    rules: {
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.flatConfigs.recommended.rules,
    },
  },
  {
    // MOB-002/§5.2's installable-PWA service worker. Its own global scope
    // (`self`, `caches`, `fetch`, `URL`) is the Service Worker API, which
    // eslint's plain-JS `no-undef` has no notion of — the TS-aware config
    // above turns that rule off for .ts/.tsx precisely because TypeScript's
    // own lib types already cover it, but sw.js is deliberately plain JS
    // (hand-rolled, no build step) so that same escape hatch does not apply.
    files: ["apps/web/public/sw.js"],
    languageOptions: {
      globals: { self: "readonly", caches: "readonly", fetch: "readonly", URL: "readonly" },
    },
  },
);
