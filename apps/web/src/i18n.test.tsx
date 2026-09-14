/**
 * FR-LOC-001, FR-LOC-001a, FR-LOC-001b, FR-LOC-002 and IAM-010g, as the web
 * app actually behaves.
 *
 * `packages/i18n` tests the rules in isolation. This tests the wiring: that a
 * click changes the words on screen without a reload, that the change is
 * persisted afterwards rather than awaited, and — the one that matters most —
 * that changing language does not change a single figure.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  I18nProvider,
  LANGUAGE_STORAGE_KEY,
  roleMessageKey,
  useI18n,
  type Language,
} from "@ledgr/i18n";
import type { SwitcherEntry } from "@ledgr/shared-types";

import { App } from "./App";
import { LanguageSwitcher } from "./LanguageSwitcher";
import { languageHeaders, persistLanguage, reconcileWithAccount } from "./i18n";
import { ClientHeader } from "./client/ClientHeader";
import { ClientSwitcher } from "./client/ClientSwitcher";
import { inRouter } from "./testing/renderApp";

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("FR-LOC-001a: one click, immediately, without a reload", () => {
  it("repaints in the new language on click", () => {
    render(
      <I18nProvider initialLanguage="nl">
        <LanguageSwitcher />
        <ClientHeader badge={null} />
      </I18nProvider>,
    );

    expect(screen.getByTestId("client-header").textContent).toContain("Geen klant geselecteerd");

    fireEvent.click(screen.getByTestId("language-option-en"));

    // Same mounted tree, no reload, no re-authentication — the requirement's
    // three words, as one assertion.
    expect(screen.getByTestId("client-header").textContent).toContain("No client selected");
  });

  it("is one click, not two", () => {
    // A <select> would be open-then-pick. With exactly two languages the
    // whole list is on screen, so reaching the other one is a single action.
    render(
      <I18nProvider initialLanguage="nl">
        <LanguageSwitcher />
      </I18nProvider>,
    );

    const options = screen.getAllByRole("button");
    expect(options).toHaveLength(2);
    expect(screen.getByTestId("language-option-nl").getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("language-option-en").getAttribute("aria-pressed")).toBe("false");
  });

  it("names each language in its own language, whichever is active", () => {
    // Someone who cannot read the current UI language has to be able to find
    // their own. The endonym is the string they are certain to recognise.
    const { unmount } = render(
      <I18nProvider initialLanguage="nl">
        <LanguageSwitcher />
      </I18nProvider>,
    );
    expect(screen.getByTestId("language-option-en").textContent).toBe("English");
    expect(screen.getByTestId("language-option-nl").textContent).toBe("Nederlands");
    unmount();

    render(
      <I18nProvider initialLanguage="en">
        <LanguageSwitcher />
      </I18nProvider>,
    );
    expect(screen.getByTestId("language-option-en").textContent).toBe("English");
    expect(screen.getByTestId("language-option-nl").textContent).toBe("Nederlands");
  });

  it("keeps the active language reachable by keyboard", () => {
    // `disabled` on the current option would drop it out of the tab order, so
    // a keyboard user tabbing through would find only the language they are
    // not using (FR-LOC-004, WCAG 2.2 AA).
    render(
      <I18nProvider initialLanguage="nl">
        <LanguageSwitcher />
      </I18nProvider>,
    );

    for (const option of screen.getAllByRole("button")) {
      expect(option.hasAttribute("disabled")).toBe(false);
    }
  });

  it("does not wait for the server before repainting", () => {
    // The switch is local state; persistence is a consequence. A UI that
    // awaited a round trip would take effect on the network's schedule, which
    // is not "immediately".
    let resolveFetch: () => void = () => {};
    const never = new Promise<Response>((resolve) => {
      resolveFetch = () => resolve(new Response(null, { status: 200 }));
    });
    const fetchSpy = vi.fn(() => never);
    vi.stubGlobal("fetch", fetchSpy);

    render(inRouter(<App language="nl" />));
    fireEvent.click(screen.getByTestId("language-option-en"));

    expect(screen.getByTestId("language-option-en").getAttribute("aria-pressed")).toBe("true");
    expect(fetchSpy).toHaveBeenCalled();
    resolveFetch();
  });
});

describe("IAM-010g: the choice persists on the device", () => {
  it("writes the choice to device storage and to the account", async () => {
    const fetchSpy = vi.fn(async () => new Response(null, { status: 200 }));

    await persistLanguage("en", fetchSpy as unknown as typeof fetch);

    expect(localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe("en");

    const [url, init] = fetchSpy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/v1/me/language");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({ language: "en" });

    const headers = init.headers as Record<string, string>;
    // NFR-032: every mutating endpoint requires a key, this one included.
    expect(headers["Idempotency-Key"]).toBeTruthy();
    // FR-UX-007: and the request says which language to answer errors in.
    expect(headers["Accept-Language"]).toBe("en");
  });

  it("keeps the device choice when the account write fails", async () => {
    // A signed-out visitor on the login screen reaches here and gets a 401.
    // The device still remembers, which is the whole point of IAM-010g's
    // "persists on the device": first login is what applies it to the account.
    const failing = vi.fn(async () => {
      throw new Error("network down");
    });

    await expect(
      persistLanguage("en", failing as unknown as typeof fetch),
    ).resolves.toBeUndefined();
    expect(localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe("en");
  });
});

describe("IAM-010g: applying the device choice to the account at first login", () => {
  it("seeds the account when the person has never chosen", () => {
    expect(reconcileWithAccount(null, "en")).toEqual({ language: "en", seedAccount: true });
  });

  it("does not overwrite a choice they have already made", () => {
    // The account setting wins, and `seedAccount` is false. This is the case
    // that needs `GET /v1/me/language` to return null rather than a default:
    // with a default, these two would be indistinguishable and the device
    // would quietly overwrite a deliberate choice on every sign-in.
    expect(reconcileWithAccount("nl", "en")).toEqual({ language: "nl", seedAccount: false });
  });
});

describe("FR-LOC-002: figures follow the administration, not the reader", () => {
  function Figures() {
    const { money, date, number, language } = useI18n();
    return (
      <dl data-testid="figures" data-language={language}>
        <dd data-testid="amount">{money("1234.56")}</dd>
        <dd data-testid="date">{date("2026-09-02", "long")}</dd>
        <dd data-testid="rate">{number("21", { scale: 0 })}</dd>
      </dl>
    );
  }

  function figures(language: Language) {
    const { unmount } = render(
      <I18nProvider initialLanguage={language} formattingLocale="nl-NL">
        <Figures />
      </I18nProvider>,
    );
    const read = {
      amount: screen.getByTestId("amount").textContent,
      date: screen.getByTestId("date").textContent,
      rate: screen.getByTestId("rate").textContent,
    };
    unmount();
    return read;
  }

  it("renders identical characters to a Dutch reader and an English one", () => {
    // The requirement, as one assertion. The failure it prevents: an
    // English-speaking owner reading "1,234.56" off a screen a Dutch
    // bookkeeper reads as "1.234,56", and the two of them agreeing on the
    // phone that the number matches.
    const dutch = figures("nl");
    const english = figures("en");

    expect(english).toEqual(dutch);
    expect(dutch.amount).toBe("€\u00a01.234,56");
    // Including the month name, which is locale data rather than UI text —
    // an English reader of a Dutch administration sees "2 september 2026".
    expect(dutch.date).toBe("2 september 2026");
    expect(dutch.rate).toBe("21");
  });

  it("keeps formatting fixed while the words change under it", () => {
    render(
      <I18nProvider initialLanguage="nl" formattingLocale="nl-NL">
        <LanguageSwitcher />
        <Figures />
        <ClientHeader badge={null} />
      </I18nProvider>,
    );

    const before = screen.getByTestId("amount").textContent;
    fireEvent.click(screen.getByTestId("language-option-en"));

    expect(screen.getByTestId("client-header").textContent).toContain("No client selected");
    expect(screen.getByTestId("amount").textContent).toBe(before);
  });
});

describe("FR-LOC-001: no fallback between languages", () => {
  it("refuses to render outside a provider rather than picking a language", () => {
    // A default here would render an unwrapped subtree in Dutch regardless of
    // what the user chose, silently — the same class of failure as falling
    // back to English for a missing string.
    const quiet = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<ClientHeader badge={null} />)).toThrow(/I18nProvider/);
    quiet.mockRestore();
  });
});

describe("FR-LOC-001: role names", () => {
  const bookkeeper = (roleIsSystem: boolean): SwitcherEntry => ({
    administrationId: "a",
    displayName: "Bakker IT",
    legalName: "Bakker Consultancy B.V.",
    tradeName: "Bakker IT",
    kvkNumber: "12345678",
    colour: "indigo",
    initials: "BI",
    colourIsAmbiguous: false,
    role: "Bookkeeper",
    roleIsSystem,
    expiresAt: null,
  });

  function switcherWith(entry: SwitcherEntry, language: Language) {
    const { unmount } = render(
      <I18nProvider initialLanguage={language}>
        <ClientSwitcher
          open
          entries={[entry]}
          onSearch={() => {}}
          onSelect={() => {}}
          onClose={() => {}}
        />
      </I18nProvider>,
    );
    const text = screen.getByTestId("client-switcher-option").textContent ?? "";
    unmount();
    return text;
  }

  it("translates one of §8.4's own roles", () => {
    expect(switcherWith(bookkeeper(true), "nl")).toContain("Boekhouder");
    expect(switcherWith(bookkeeper(true), "en")).toContain("Bookkeeper");
  });

  it("leaves a custom role's name exactly as its organization wrote it", () => {
    // The case that makes `roleIsSystem` necessary rather than a nicety: an
    // organization is free to compose a custom role (ADR-013) and call it
    // "Bookkeeper". Rendering that as "Boekhouder" would show a Dutch reader a
    // role their organization does not have — and the name is tenant data,
    // like the client's own legal name beside it.
    expect(switcherWith(bookkeeper(false), "nl")).toContain("Bookkeeper");
    expect(switcherWith(bookkeeper(false), "nl")).not.toContain("Boekhouder");
  });

  it("shows the identifier rather than crashing on a role it has no label for", () => {
    // A role row can appear without a deploy. This is the package's one
    // fallback, and it is deliberately NOT a fallback between languages: it
    // falls back to the identifier, which is conspicuous, where falling back
    // from Dutch to English would be invisible (FR-LOC-001).
    const invented = { ...bookkeeper(true), role: "Chief Ledger Officer" };
    expect(switcherWith(invented, "nl")).toContain("Chief Ledger Officer");
  });

  it("maps a role name to its key the same way the API test does", () => {
    expect(roleMessageKey("Organization Admin")).toBe("roles.organization_admin");
    expect(roleMessageKey("Expense Submitter")).toBe("roles.expense_submitter");
    expect(roleMessageKey("Owner")).toBe("roles.owner");
  });
});

describe("FR-UX-007: the API is told which language to answer in", () => {
  it("sends the app's language rather than leaving it to the browser", () => {
    // `Accept-Language` would otherwise carry the BROWSER's preference, which
    // is exactly what the in-app control exists to override. A Dutch browser
    // whose user chose English would get Dutch error messages under an
    // English interface.
    expect(languageHeaders("en")).toEqual({ "Accept-Language": "en" });
    expect(languageHeaders("nl")).toEqual({ "Accept-Language": "nl" });
  });
});


