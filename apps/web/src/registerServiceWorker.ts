/**
 * Registers `public/sw.js` — §5.2/MOB-002's installable PWA.
 *
 * Split out of main.tsx so it can be imported and tested directly: jsdom (the
 * test environment every other suite in this app runs under) has no
 * `serviceWorker` on `navigator` at all, which is exactly the environment
 * this function has to behave correctly in - `if ('serviceWorker' in
 * navigator)` is the guard, not a `typeof` check, because the property is
 * simply absent rather than present-and-undefined.
 *
 * Swallows a registration failure rather than throwing: a browser that
 * refuses (private browsing, an unsupported context, `sw.js` 404 in a
 * misconfigured deployment) should not stop the app underneath it from
 * rendering — installability is additive, not a precondition for the product
 * working.
 */
export async function registerServiceWorker(): Promise<void> {
  if (!("serviceWorker" in navigator)) return;

  try {
    await navigator.serviceWorker.register("/sw.js");
  } catch {
    // See the docstring above - a failed registration is not a reason to
    // fail the app that would otherwise run fine without one.
  }
}
