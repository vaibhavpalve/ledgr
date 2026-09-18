// `vitest/config`, not `vite`: this file carries a `test` block, which is not
// part of Vite's own UserConfig type. Importing from `vite` typechecked under
// `tsc --noEmit` (this file is in tsconfig.node.json, not the app's tsconfig)
// but failed the `tsc -b` that `build` runs — so the app's production build
// was broken before any of the design work landed. Vitest re-exports
// defineConfig with the test block included.
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

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
      "/v1": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
});
