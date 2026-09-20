import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@ledgr/i18n";

import { axeViolations } from "../testing/axe";
import { ApiError, type AuthApi } from "./api";
import { ForgotPasswordForm } from "./ForgotPasswordForm";
import { LoginForm } from "./LoginForm";

function renderForm(recover: AuthApi["recover"], onBackToLogin = vi.fn()) {
  const api = { recover } as unknown as AuthApi;
  return render(
    <I18nProvider initialLanguage="en">
      <ForgotPasswordForm api={api} onBackToLogin={onBackToLogin} />
    </I18nProvider>,
  );
}

function fill(email: string, code: string, password: string) {
  fireEvent.change(screen.getByTestId("recover-email"), { target: { value: email } });
  fireEvent.change(screen.getByTestId("recover-code"), { target: { value: code } });
  fireEvent.change(screen.getByTestId("recover-password"), { target: { value: password } });
  fireEvent.click(screen.getByTestId("recover-submit"));
}

describe("password recovery screen (IAM-018)", () => {
  it("sends the e-mail, the code without spaces and the new password, then points to sign in", async () => {
    const recover = vi.fn(async () => undefined);
    renderForm(recover);
    fill("a@example.com", "123 456", "a long new passphrase 1");
    await waitFor(() => expect(screen.getByTestId("recover-done")).not.toBeNull());
    expect(recover).toHaveBeenCalledWith("a@example.com", "123456", "a long new passphrase 1");
  });

  it("shows the server's own sentence on refusal and stays on the form", async () => {
    const recover = vi.fn(async () => {
      throw new ApiError(401, "invalid_recovery_proof", "The email or code is not right.");
    });
    renderForm(recover);
    fill("a@example.com", "000000", "a long new passphrase 1");
    await waitFor(() => expect(screen.getByTestId("recover-error")).not.toBeNull());
    expect(screen.getByTestId("recover-error").textContent).toContain("not right");
    expect(screen.queryByTestId("recover-done")).toBeNull();
  });

  it("has no automated WCAG violations (CMP-012)", async () => {
    const { container } = renderForm(vi.fn());
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("the login screen's forgot-password link", () => {
  const api = {} as unknown as AuthApi;

  it("appears only when the route exists, and calls back", () => {
    const onForgotPassword = vi.fn();
    const { unmount } = render(
      <I18nProvider initialLanguage="en">
        <LoginForm
          api={api}
          onSwitchToSignup={vi.fn()}
          onSignedIn={vi.fn()}
          onGoogleStart={vi.fn()}
          onForgotPassword={onForgotPassword}
        />
      </I18nProvider>,
    );
    fireEvent.click(screen.getByTestId("login-forgot"));
    expect(onForgotPassword).toHaveBeenCalledTimes(1);
    unmount();

    render(
      <I18nProvider initialLanguage="en">
        <LoginForm
          api={api}
          onSwitchToSignup={vi.fn()}
          onSignedIn={vi.fn()}
          onGoogleStart={vi.fn()}
        />
      </I18nProvider>,
    );
    expect(screen.queryByTestId("login-forgot")).toBeNull();
  });
});
