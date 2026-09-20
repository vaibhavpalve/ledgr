import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
// Tokens FIRST: every stylesheet below reads `var(--ledgr-*)`, and a custom
// property referenced before it is declared resolves to nothing rather than to
// a sensible default (ADR-055).
import "@ledgr/design-tokens/tokens.css";
// The Ledgr UI handoff tokens and fonts (design/, ADR-080). Different names from
// the `--ledgr-*` set above, so the two coexist while screens move across.
import "@ledgr/design-tokens/ui-tokens.css";
import "@ledgr/design-tokens/ui-fonts.css";
import "./accessibility.css";
import "./app.css";
import { App } from "./App";
import { registerServiceWorker } from "./registerServiceWorker";

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root element not found");
}

// The router lives here, not in `App`, so a test can mount `App` inside a
// `MemoryRouter` at any URL (ADR-058).
createRoot(rootElement).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);

// §5.2/MOB-002's installable PWA. Guarded because jsdom (the test
// environment) has no `serviceWorker` on `navigator` at all - see
// registerServiceWorker.test.ts.
void registerServiceWorker();
