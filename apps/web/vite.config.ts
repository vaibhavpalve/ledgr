// `vitest/config`, not `vite`: this file carries a `test` block, which is not
// part of Vite's own UserConfig type. Importing from `vite` typechecked under
// `tsc --noEmit` (this file is in tsconfig.node.json, not the app's tsconfig)
// but failed the `tsc -b` that `build` runs — so the app's production build
// was broken before any of the design work landed. Vitest re-exports
// defineConfig with the test block included.
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Where the dev server proxies the API. Defaults to the local stack's :8000; the Playwright
// golden path (e2e/) points it at an API of its own so a run never touches the dev database.
const apiTarget = process.env.LEDGR_API_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Dev-only wiring to apps/api on :8000. Proxied rather than CORS'd
    // deliberately, and it is what production does too now that the API
    // serves the built SPA itself (ADR-063): WEBAUTHN_ORIGIN
    // defaults to http://localhost:5173 (see apps/api/src/api/config.py) -
    // the browser must see every request as same-origin with the page that
    // ran navigator.credentials.create()/get(), which a proxy gives for
    // free and a cross-origin CORS setup would not.
    proxy: {
      "/v1": apiTarget,
      "/health": apiTarget,
    },
    // Playwright writes screenshots and downloads under e2e/ while the dev server runs; a
    // download's temporary .crdownload file vanishing under the watcher crashed Vite mid-run.
    watch: { ignored: ["**/e2e/**"] },
  },
  test: {
    environment: "jsdom",
    globals: true,
    // e2e/ is Playwright's, run against a live stack by `pnpm test:e2e`, not by vitest.
    exclude: ["**/node_modules/**", "**/dist/**", "e2e/**"],
  },
});
