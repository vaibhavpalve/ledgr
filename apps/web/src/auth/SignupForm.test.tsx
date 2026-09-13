import { fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";

import { ApiError, type AuthApi, type AuthResult } from "./api";
import { SignupForm } from "./SignupForm";

function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

function api(overrides: Partial<AuthApi> = {}) {
  return { signup: vi.fn(), ...overrides } as unknown as AuthApi;
}

const unverified: AuthResult = {
  accessToken: "tok",
  mfaVerified: false,
  enrollment: { hasPasskey: false, hasTotp: false },
};

describe("SignupForm — FR-MDL-001's one question", () => {
  it("defaults to self-managed and hides the KvK field", () => {
    render(<SignupForm api={api()} onSwitchToLogin={vi.fn()} onSignedUp={vi.fn()} />);

    expect(
      (screen.getByTestId("signup-account-model-self-managed") as HTMLInputElement).checked,
    ).toBe(true);
    expect(screen.queryByTestId("signup-kvk-number")).toBeNull();
  });

  it("shows the KvK field only once 'firm' is chosen, and sends kvkNumber null otherwise", async () => {
    const signup = vi.fn(async () => unverified);
    render(<SignupForm api={api({ signup })} onSwitchToLogin={vi.fn()} onSignedUp={vi.fn()} />);

    fireEvent.change(screen.getByTestId("signup-organization-name"), {
      target: { value: "Bakker Consultancy" },
    });
    fireEvent.change(screen.getByTestId("signup-email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByTestId("signup-password"), { target: { value: "x".repeat(12) } });
    fireEvent.click(screen.getByTestId("signup-submit"));

    await waitFor(() => expect(signup).toHaveBeenCalled());
    expect(signup).toHaveBeenCalledWith(
      expect.objectContaining({ accountModel: "self_managed", kvkNumber: null }),
    );
  });

  it("sends the entered KvK number for the firm account model", async () => {
    const signup = vi.fn(async () => unverified);
    render(<SignupForm api={api({ signup })} onSwitchToLogin={vi.fn()} onSignedUp={vi.fn()} />);

    fireEvent.click(screen.getByTestId("signup-account-model-firm"));
    expect(screen.getByTestId("signup-kvk-number")).toBeDefined();

    fireEvent.change(screen.getByTestId("signup-organization-name"), {
      target: { value: "Bakker Accountants" },
    });
    fireEvent.change(screen.getByTestId("signup-kvk-number"), { target: { value: "87654321" } });
    fireEvent.change(screen.getByTestId("signup-email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByTestId("signup-password"), { target: { value: "x".repeat(12) } });
    fireEvent.click(screen.getByTestId("signup-submit"));

    await waitFor(() =>
      expect(signup).toHaveBeenCalledWith(
        expect.objectContaining({ accountModel: "firm", kvkNumber: "87654321" }),
      ),
    );
  });

  it("reports the result to the caller — a fresh signup is never pre-verified (ADR-054)", async () => {
    const onSignedUp = vi.fn();
    render(
      <SignupForm
        api={api({ signup: vi.fn(async () => unverified) })}
        onSwitchToLogin={vi.fn()}
        onSignedUp={onSignedUp}
      />,
    );

    fireEvent.change(screen.getByTestId("signup-organization-name"), {
      target: { value: "Bakker" },
    });
    fireEvent.change(screen.getByTestId("signup-email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByTestId("signup-password"), { target: { value: "x".repeat(12) } });
    fireEvent.click(screen.getByTestId("signup-submit"));

    await waitFor(() => expect(onSignedUp).toHaveBeenCalledWith(unverified));
  });

  it("shows the server's sentence on refusal (weak password, duplicate email, ...)", async () => {
    const signup = vi.fn(async () => {
      throw new ApiError(422, "weak_password", "Dit wachtwoord is te zwak.");
    });
    render(<SignupForm api={api({ signup })} onSwitchToLogin={vi.fn()} onSignedUp={vi.fn()} />);

    fireEvent.change(screen.getByTestId("signup-organization-name"), {
      target: { value: "Bakker" },
    });
    fireEvent.change(screen.getByTestId("signup-email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByTestId("signup-password"), { target: { value: "123" } });
    fireEvent.click(screen.getByTestId("signup-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("signup-error").textContent).toBe("Dit wachtwoord is te zwak."),
    );
  });

  it("clicking the switch-to-login link calls back", () => {
    const onSwitchToLogin = vi.fn();
    render(<SignupForm api={api()} onSwitchToLogin={onSwitchToLogin} onSignedUp={vi.fn()} />);

    fireEvent.click(screen.getByTestId("switch-to-login"));
    expect(onSwitchToLogin).toHaveBeenCalledOnce();
  });
});
