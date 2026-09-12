import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";

export default tseslint.config(
  { ignores: ["**/dist/**", "**/node_modules/**", "**/coverage/**"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["apps/web/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
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
