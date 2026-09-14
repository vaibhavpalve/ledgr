import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// Tokens FIRST: every stylesheet below reads `var(--ledgr-*)`, and a custom
// property referenced before it is declared resolves to nothing rather than to
// a sensible default (ADR-055).
import "@ledgr/design-tokens/tokens.css";
import "./accessibility.css";
import "./app.css";
import { App } from "./App";
import { registerServiceWorker } from "./registerServiceWorker";

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root element not found");
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// §5.2/MOB-002's installable PWA. Guarded because jsdom (the test
// environment) has no `serviceWorker` on `navigator` at all - see
// registerServiceWorker.test.ts.
void registerServiceWorker();
