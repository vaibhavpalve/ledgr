import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@ledgr/i18n";

import { axeViolations } from "../testing/axe";
import { ThemeButton } from "../theme/ThemeButton";
import type { AuthApi } from "./api";
import { LoginForm } from "./LoginForm";
import { PreAuthScreen } from "./PreAuthScreen";

/**
 * The login redesign (design/reference/Login.png, ADR-080): the additions to
 * the existing form, none of which change what it sends.
 */

const noApi = {} as unknown as AuthApi;

function renderLogin() {
  return render(
    <I18nProvider initialLanguage="en">
      <PreAuthScreen screen="login">
        <LoginForm
          api={noApi}
          onSwitchToSignup={vi.fn()}
          onSignedIn={vi.fn()}
          onGoogleStart={vi.fn()}
        />
      </PreAuthScreen>
    </I18nProvider>,
  );
}

describe("password Show / Hide", () => {
  it("reveals and hides the password without touching its value", () => {
    renderLogin();
    const input = screen.getByTestId("login-password") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "hunter2" } });

    expect(input.type).toBe("password");
    fireEvent.click(screen.getByTestId("login-show-password"));
    expect(input.type).toBe("text");
    expect(screen.getByTestId("login-show-password").textContent).toBe("Hide");
    expect(input.value).toBe("hunter2");
    fireEvent.click(screen.getByTestId("login-show-password"));
    expect(input.type).toBe("password");
  });
});

describe("the theme button", () => {
  beforeEach(() => {
    document.documentElement.removeAttribute("data-theme");
    localStorage.clear();
  });

  it("stamps an explicit data-theme, flips it back, and names the next state", () => {
    render(
      <I18nProvider initialLanguage="en">
        <ThemeButton />
      </I18nProvider>,
    );
    const button = screen.getByTestId("theme-button");
    expect(button.getAttribute("aria-label")).toBe("Switch to dark theme");

    fireEvent.click(button);
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(button.getAttribute("aria-label")).toBe("Switch to light theme");

    fireEvent.click(button);
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });
});

describe("the login screen", () => {
  it("shows the Dutch copy when the language is switched to NL", () => {
    renderLogin();
    fireEvent.click(screen.getByTestId("language-option-nl"));
    expect(screen.getByTestId("pre-auth-heading").textContent).toBe("Welkom terug");
    expect(screen.getByText("of ga verder met")).toBeTruthy();
  });

  it("has no automated WCAG violations (CMP-012)", async () => {
    const { container } = renderLogin();
    expect(await axeViolations(container)).toEqual([]);
  });
});
