/**
 * IAM-010g's second half — "The choice persists on the device and is applied
 * to the account after first login" — and FR-LOC-001a's "without
 * re-authentication".
 *
 * This is the part that turns a pre-login click into a preference that
 * follows a person to another machine. Somebody who chose English on the
 * login screen of a borrowed laptop should find LEDGR in English on their own
 * one, and first login is the only moment that can be arranged: after it, the
 * account is authoritative (FR-LOC-001b) and the device is a cache.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LANGUAGE_STORAGE_KEY } from "@ledgr/i18n";

import { App } from "../App";
import { applyAccountLanguage } from "../i18n";
import { inRouter } from "../testing/renderApp";
import { meFixture } from "../testing/session";

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/**
 * A fetch that answers the language endpoint and records every PUT.
 *
 * It also answers `GET /v1/me`, which an authenticated render now makes on
 * its own (the session bootstrap, ADR-058) — unrelated to what this file
 * tests, but a render that cannot load a session shows the error state
 * instead of the app. `languageCalls()` is what the counting assertions
 * below use, so that adding another unrelated startup read never silently
 * turns one of them into a different claim.
 */
function apiWith(accountLanguage: string | null) {
  const puts: Array<{ language: string; idempotencyKey: string }> = [];
  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      const headers = (init.headers ?? {}) as Record<string, string>;
      puts.push({
        language: JSON.parse(String(init.body)).language,
        idempotencyKey: headers["Idempotency-Key"] ?? "",
      });
      return new Response(null, { status: 200 });
    }
    if (String(url) === "/v1/me") {
      return new Response(JSON.stringify(meFixture()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (String(url) === "/v1/me/language") {
      return new Response(JSON.stringify({ language: accountLanguage, supported: ["en", "nl"] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    // Everything else — the dashboard, the client badge — refused rather than
    // answered with the language payload. Handing every endpoint one shape is
    // what a blanket stub does, and a screen given a 200 whose body is the
    // wrong type crashes on it instead of showing its error state.
    return new Response(null, { status: 503 });
  });
  const languageCalls = () =>
    fetchImpl.mock.calls.filter(([url]) => String(url) === "/v1/me/language").length;
  return { fetchImpl, puts, languageCalls };
}

describe("applyAccountLanguage", () => {
  it("seeds the account from the device when the person has never chosen", async () => {
    const { fetchImpl, puts } = apiWith(null);

    const resolved = await applyAccountLanguage("en", fetchImpl as unknown as typeof fetch);

    expect(resolved).toBe("en");
    expect(puts).toEqual([{ language: "en", idempotencyKey: expect.any(String) }]);
    expect(puts[0]?.idempotencyKey).toBeTruthy();
  });

  it("lets the account win, and writes nothing to it, when a choice already exists", async () => {
    // The case that needs `GET /v1/me/language` to answer null rather than a
    // default. With a default these two would be indistinguishable and this
    // path would overwrite a deliberate choice on every single sign-in.
    const { fetchImpl, puts } = apiWith("nl");

    const resolved = await applyAccountLanguage("en", fetchImpl as unknown as typeof fetch);

    expect(resolved).toBe("nl");
    expect(puts).toEqual([]);
    // The device is still updated, so the next reload opens in the language
    // the account holds rather than flashing the old one and correcting.
    expect(localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe("nl");
  });

  it("keeps showing the device's language when the account cannot be read", async () => {
    // An unreadable preference is a reason to keep showing what the person
    // already chose, never a reason to fail a login that otherwise worked.
    const failing = vi.fn(async () => {
      throw new Error("network down");
    });

    await expect(applyAccountLanguage("en", failing as unknown as typeof fetch)).resolves.toBe(
      "en",
    );
  });

  it("does the same on a non-OK response", async () => {
    const refused = vi.fn(async () => new Response(null, { status: 403 }));

    await expect(applyAccountLanguage("nl", refused as unknown as typeof fetch)).resolves.toBe(
      "nl",
    );
  });
});

describe("IAM-010g: applied at the moment authentication lands", () => {
  it("adopts the account's language and repaints", async () => {
    // The person signed in on a machine that remembered Dutch; their account
    // says English. The account is authoritative (FR-LOC-001b) — the same
    // person on a borrowed laptop should not switch language.
    const { fetchImpl, puts, languageCalls } = apiWith("en");
    vi.stubGlobal("fetch", fetchImpl);

    render(inRouter(<App language="nl" authenticated />));

    // `<html lang>` rather than the switcher's own pressed state: inside the
    // app the control lives in the user menu, which is closed. This is the
    // repaint itself (WCAG 2.2 SC 3.1.1, FR-LOC-004) and it does not depend
    // on where the chrome happens to put the control.
    await waitFor(() => expect(document.documentElement.lang).toBe("en"));
    // And the device now caches it, so the next reload starts there.
    await waitFor(() => {
      expect(localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe("en");
    });

    // But the ACCOUNT is not told what it just told us. Adopting a value is
    // not choosing one, and writing it back would be a round trip per
    // sign-in on every machine that remembered something else.
    expect(puts).toEqual([]);
    expect(languageCalls()).toBe(1);
  });

  it("seeds the account with the pre-login choice and leaves the screen alone", async () => {
    const { fetchImpl, puts } = apiWith(null);
    vi.stubGlobal("fetch", fetchImpl);

    render(inRouter(<App language="en" authenticated />));

    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]?.language).toBe("en");
    expect(document.documentElement.lang).toBe("en");
  });

  it("does not run while signed out", () => {
    // The pre-authentication screen must not call an endpoint that will
    // refuse it. A 401 on every visit to the login page is noise in the log
    // and a needless round trip.
    const { fetchImpl } = apiWith(null);
    vi.stubGlobal("fetch", fetchImpl);

    render(inRouter(<App language="nl" />));

    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("does not re-run while one session continues", async () => {
    // The bug this guards: re-reading the account on every render would
    // overwrite a click with whatever the account said when the page loaded,
    // and the switcher would appear to bounce.
    const { fetchImpl, languageCalls } = apiWith("nl");
    vi.stubGlobal("fetch", fetchImpl);

    const { rerender } = render(inRouter(<App language="nl" authenticated />));
    await waitFor(() => expect(languageCalls()).toBe(1));

    rerender(inRouter(<App language="nl" authenticated />));
    rerender(inRouter(<App language="nl" authenticated />));

    expect(languageCalls()).toBe(1);
  });

  it("runs again for the next person to sign in on this machine", async () => {
    // Not the same as "runs once". A "have we done this already" flag would
    // pass the test above and fail this one, leaving a colleague signing in
    // after somebody else with that person's language — which is precisely
    // what FR-LOC-001b's per-user setting exists to prevent.
    const { fetchImpl, languageCalls } = apiWith("en");
    vi.stubGlobal("fetch", fetchImpl);

    const first = render(inRouter(<App language="nl" authenticated />));
    await waitFor(() => expect(languageCalls()).toBe(1));

    // Unmount and mount again, rather than re-rendering with a different
    // `authenticated`: since ADR-058 that prop seeds `AuthProvider`'s initial
    // status and is not read again, and signing out in the real app unmounts
    // everything under `RequireAuth` anyway. This is that, exactly.
    first.unmount();
    render(inRouter(<App language="nl" authenticated />)); // a different person signs in

    await waitFor(() => expect(languageCalls()).toBe(2));
  });
});

describe("FR-LOC-001a: without reload or re-authentication", () => {
  it("changes language with no request that could end the session", async () => {
    // "Without re-authentication" is a claim about what the switch does NOT
    // do. The only call it makes is the preference write; nothing touches a
    // token, a session or a sign-in endpoint.
    const { fetchImpl } = apiWith("nl");
    vi.stubGlobal("fetch", fetchImpl);

    render(inRouter(<App language="nl" authenticated />));
    await waitFor(() => expect(fetchImpl).toHaveBeenCalled());
    // The control is in the user menu once someone is signed in.
    fireEvent.click(await screen.findByTestId("user-menu-trigger"));
    fetchImpl.mockClear();

    fireEvent.click(screen.getByTestId("language-option-en"));

    // Repainted synchronously, before any request resolves.
    expect(screen.getByTestId("language-option-en").getAttribute("aria-pressed")).toBe("true");

    // The preference write happens…
    await waitFor(() =>
      expect(
        fetchImpl.mock.calls.some(
          ([url, init]) =>
            String(url) === "/v1/me/language" &&
            (init as RequestInit | undefined)?.method === "PUT",
        ),
      ).toBe(true),
    );

    // …and nothing touches authentication. Screens may well re-read their own
    // data in the new language — that is the switch working, not a session
    // being renegotiated — so the claim is about WHICH endpoints are called,
    // not how many.
    const authCalls = fetchImpl.mock.calls.filter(([url]) => String(url).startsWith("/v1/auth/"));
    expect(authCalls).toEqual([]);
  });
});
