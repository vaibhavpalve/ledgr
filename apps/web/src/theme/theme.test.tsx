import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@ledgr/i18n";
import { applyTheme, readThemePreference, THEME_STORAGE_KEY } from "@ledgr/design-tokens";

import { ThemeToggle } from "./ThemeToggle";

/**
 * ADR-055's theme switch.
 *
 * The suite is in three parts, and the middle one is the reason it exists:
 *
 *   1. the control behaves (three options, the choice is announced, it sticks)
 *   2. the INLINE SCRIPT in index.html still agrees with @ledgr/design-tokens
 *   3. storage that throws does not take the app down
 */

/*
 * Resolved from the working directory, not from `import.meta.url`: this suite
 * runs in the jsdom environment, where `import.meta.url` is an http:// URL and
 * `fileURLToPath` rejects it. Vitest's cwd is the package root, so index.html
 * sits directly under it.
 */
const INDEX_HTML = readFileSync(resolve(process.cwd(), "index.html"), "utf8");

function renderToggle() {
  return render(
    <I18nProvider initialLanguage="nl">
      <ThemeToggle />
    </I18nProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the theme control", () => {
  it("offers exactly three options", () => {
    renderToggle();
    // Three, not two: "system" is the default and is a different choice from
    // picking whichever theme the device happens to be in right now.
    expect(screen.getByTestId("theme-option-system")).toBeDefined();
    expect(screen.getByTestId("theme-option-light")).toBeDefined();
    expect(screen.getByTestId("theme-option-dark")).toBeDefined();
  });

  it("starts on system when nothing has been chosen", () => {
    renderToggle();
    expect(screen.getByTestId("theme-option-system").getAttribute("aria-pressed")).toBe("true");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });

  it("stamps the document and persists the choice", () => {
    renderToggle();

    act(() => {
      screen.getByTestId("theme-option-dark").click();
    });

    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });

  it("announces the current choice rather than only colouring it", () => {
    renderToggle();

    act(() => {
      screen.getByTestId("theme-option-light").click();
    });

    expect(screen.getByTestId("theme-option-light").getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("theme-option-dark").getAttribute("aria-pressed")).toBe("false");
  });

  it("leaves no option disabled, so every one stays reachable by keyboard", () => {
    renderToggle();
    act(() => {
      screen.getByTestId("theme-option-dark").click();
    });
    // A disabled control drops out of the tab order, which would leave a
    // keyboard user able to reach only the themes they had not chosen.
    for (const option of ["system", "light", "dark"]) {
      expect((screen.getByTestId(`theme-option-${option}`) as HTMLButtonElement).disabled).toBe(
        false,
      );
    }
  });

  it("returns to system by REMOVING the stamp, not by stamping a resolved value", () => {
    renderToggle();

    act(() => {
      screen.getByTestId("theme-option-dark").click();
    });
    act(() => {
      screen.getByTestId("theme-option-system").click();
    });

    // Both halves matter. The attribute has to go so the media query in
    // tokens.css takes over and keeps following the OS; the stored value has
    // to go so a returning user is indistinguishable from one who never chose.
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
  });
});

describe("the pre-paint script in index.html", () => {
  /*
   * The script in <head> is a hand-written copy of readThemePreference +
   * applyTheme, and it has to be: it runs before any module loads, which is
   * the whole point (anything later paints one light frame first). These
   * assertions are what stops the copy drifting from the module.
   */

  it("reads the same storage key the module writes", () => {
    expect(INDEX_HTML).toContain(THEME_STORAGE_KEY);
  });

  it("runs before the module bundle", () => {
    const script = INDEX_HTML.indexOf(THEME_STORAGE_KEY);
    const bundle = INDEX_HTML.indexOf("/src/main.tsx");
    expect(script).toBeGreaterThan(-1);
    expect(bundle).toBeGreaterThan(-1);
    expect(script).toBeLessThan(bundle);
  });

  it("stamps only the two explicit values, never a resolved 'system'", () => {
    // If it stamped the resolved theme for a "system" user, the page would be
    // frozen at whatever the OS was on load and would stop following it.
    expect(INDEX_HTML).toMatch(/stored === "light" \|\| stored === "dark"/);
  });

  it("is wrapped in a try/catch, because localStorage throws when blocked", () => {
    const start = INDEX_HTML.indexOf(THEME_STORAGE_KEY);
    const scriptEnd = INDEX_HTML.indexOf("</script>", start);
    expect(INDEX_HTML.slice(start, scriptEnd)).toContain("catch");
  });

  it("declares a theme-color for each theme, matching the tokens", () => {
    // These cannot be custom properties — the address bar cannot read CSS —
    // so they are literals, and literals are exactly what drifts.
    expect(INDEX_HTML).toContain('content="#fafafa"');
    expect(INDEX_HTML).toContain('content="#09090b"');
  });
});

describe("when storage is unavailable", () => {
  it("falls back to system rather than throwing", () => {
    // A browser set to block site data throws on ACCESS, not on read — the
    // usual `?? null` guard never runs.
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });

    expect(readThemePreference()).toBe("system");
  });

  it("still applies a chosen theme to the page it is on", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });

    renderToggle();
    act(() => {
      screen.getByTestId("theme-option-dark").click();
    });

    // Persisting is a side effect of the switch, never a precondition for it.
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });
});

describe("applyTheme", () => {
  it("can target an element other than the document", () => {
    const element = document.createElement("div");
    applyTheme("dark", element);
    expect(element.getAttribute("data-theme")).toBe("dark");
    applyTheme("system", element);
    expect(element.hasAttribute("data-theme")).toBe(false);
  });
});
