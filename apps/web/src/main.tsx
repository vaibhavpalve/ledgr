import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./accessibility.css";
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
