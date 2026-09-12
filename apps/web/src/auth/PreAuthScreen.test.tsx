/**
 * IAM-010g and FR-LOC-001a, as the pre-authentication screens behave.
 *
 *   IAM-010g   "Language is selectable BEFORE authentication, on the login and
 *              signup screens, defaulting to the browser or device locale and
 *              falling back to Dutch for `.nl` traffic. The choice persists on
 *              the device and is applied to the account after first login."
 *   FR-LOC-001a "…changeable at any time from the user menu in one click,
 *              taking effect immediately without reload or re-authentication."
 *
 * `packages/i18n/src/language.test.ts` tests the resolution RULE against
 * explicit inputs. This tests the screen: that the control is there before
 * anyone has signed in, that it works with no token, and that the choice
 * outlives the page.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider, LANGUAGE_STORAGE_KEY } from "@ledgr/i18n";

import { App } from "../App";
import { PreAuthScreen } from "./PreAuthScreen";
import { SignInPending } from "./SignInPending";
import { initialLanguage } from "../i18n";

const REAL_LANGUAGES = navigator.languages;

function setBrowserLanguages(languages: readonly string[]) {
  Object.defineProperty(navigator, "languages", { value: languages, configurable: true });
}

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.documentElement.lang = "en";
});

afterEach(() => {
  setBrowserLanguages(REAL_LANGUAGES);
});

describe("IAM-010g: the control is on both pre-authentication screens", () => {
  it.each(["login", "signup"] as const)("renders on the %s screen", (kind) => {
    render(
      <I18nProvider initialLanguage="nl">
        <PreAuthScreen screen={kind}>
          <SignInPending />
        </PreAuthScreen>
      </I18nProvider>,
    );

    expect(screen.getByTestId("pre-auth-screen").dataset.screen).toBe(kind);
    expect(screen.getByTestId("language-switcher")).toBeTruthy();
    expect(screen.getByTestId("language-option-en")).toBeTruthy();
    expect(screen.getByTestId("language-option-nl")).toBeTruthy();
  });

  it("heads each screen with its own words, in the chosen language", () => {
    const { unmount } = render(
      <I18nProvider initialLanguage="nl">
        <PreAuthScreen screen="login">
          <SignInPending />
        </PreAuthScreen>
      </I18nProvider>,
    );
    // "Inloggen", not "Aanmelden" — Dutch "aanmelden" means both sign in and
    // register, so on a screen whose other option is creating an account it
    // would make the two choices read as one.
    expect(screen.getByTestId("pre-auth-heading").textContent).toBe("Inloggen");
    unmount();

    render(
      <I18nProvider initialLanguage="nl">
        <PreAuthScreen screen="signup">
          <SignInPending />
        </PreAuthScreen>
      </I18nProvider>,
    );
    expect(screen.getByTestId("pre-auth-heading").textContent).toBe("Account aanmaken");
  });

  it("says what will happen to the choice", () => {
    // A person choosing a language before they have an account has no way to
    // know whether it survives, and on a shared machine has a reason to care.
    render(
      <I18nProvider initialLanguage="en">
        <PreAuthScreen screen="login">
          <SignInPending />
        </PreAuthScreen>
      </I18nProvider>,
    );

    expect(screen.getByTestId("pre-auth-language-hint").textContent).toContain(
      "remembered on this device",
    );
  });
});

describe("IAM-010g: selectable before authentication", () => {
  it("switches the screen's language with no account and no token", () => {
    // The requirement's core claim. `App` defaults to signed out, and nothing
    // here provides credentials.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 401 })),
    );
    render(<App language="nl" />);

    expect(screen.getByTestId("pre-auth-heading").textContent).toBe("Inloggen");

    fireEvent.click(screen.getByTestId("language-option-en"));

    expect(screen.getByTestId("pre-auth-heading").textContent).toBe("Sign in");
    expect(screen.getByTestId("sign-in-pending").textContent).toContain("not available");
  });

  it("remembers the choice across a reload", () => {
    // IAM-010g's "persists on the device". Unmounting and mounting again is a
    // reload as far as this app's state is concerned: nothing survives it but
    // what was written to the device.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 401 })),
    );
    const first = render(<App language="nl" />);
    fireEvent.click(screen.getByTestId("language-option-en"));
    first.unmount();

    expect(localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe("en");

    render(<App language={initialLanguage()} />);
    expect(screen.getByTestId("pre-auth-heading").textContent).toBe("Sign in");
  });

  it("keeps a 401 from the language write invisible on this screen", () => {
    // Nobody is signed in, so `PUT /v1/me/language` is refused — which is
    // correct and must cost nothing. The choice is already on screen and on
    // the device; the account write is what first login is for.
    const refuse = vi.fn(async () => new Response(null, { status: 401 }));
    vi.stubGlobal("fetch", refuse);

    render(<App language="nl" />);
    fireEvent.click(screen.getByTestId("language-option-en"));

    expect(screen.getByTestId("pre-auth-heading").textContent).toBe("Sign in");
    expect(localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe("en");
  });
});

describe("IAM-010g: defaulting before anyone has chosen", () => {
  it("takes the browser's locale", () => {
    setBrowserLanguages(["en-GB", "en"]);
    expect(initialLanguage()).toBe("en");

    setBrowserLanguages(["nl-NL", "nl"]);
    expect(initialLanguage()).toBe("nl");
  });

  it("falls back to Dutch for .nl traffic, below the browser", () => {
    // §20's rule and its ordering. jsdom serves localhost, so the hostname
    // arm is exercised through resolveLanguage in
    // packages/i18n/src/language.test.ts; what this pins is that a browser
    // asking for English is not overruled.
    setBrowserLanguages(["en-GB"]);
    expect(initialLanguage()).toBe("en");
  });

  it("prefers a remembered choice over the browser", () => {
    // The device choice IS the person overriding their browser, on this very
    // screen. Letting the browser win would make the control do nothing on
    // the next visit.
    setBrowserLanguages(["nl-NL"]);
    localStorage.setItem(LANGUAGE_STORAGE_KEY, "en");
    expect(initialLanguage()).toBe("en");
  });

  it("ignores a stored value that is not a language we ship", () => {
    setBrowserLanguages(["en-GB"]);
    localStorage.setItem(LANGUAGE_STORAGE_KEY, "de");
    expect(initialLanguage()).toBe("en");
  });
});

describe("FR-LOC-004: the page declares the language it is actually in", () => {
  it("sets <html lang> on mount and moves it on every switch", () => {
    // WCAG 2.2 SC 3.1.1. Without this a screen reader announces "Inloggen"
    // through an English synthesiser, and the switch has changed the product
    // for sighted users only — which is not "taking effect".
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 401 })),
    );
    render(<App language="nl" />);

    expect(document.documentElement.lang).toBe("nl");

    fireEvent.click(screen.getByTestId("language-option-en"));
    expect(document.documentElement.lang).toBe("en");

    fireEvent.click(screen.getByTestId("language-option-nl"));
    expect(document.documentElement.lang).toBe("nl");
  });
});
