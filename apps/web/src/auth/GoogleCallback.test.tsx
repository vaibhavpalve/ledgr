import { fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";

import { ApiError, type AuthApi, type AuthResult, type GoogleCallbackResult } from "./api";
import { GoogleCallback } from "./GoogleCallback";

function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

function api(loginGoogleCallback: () => Promise<GoogleCallbackResult>): AuthApi {
  return { loginGoogleCallback: vi.fn(loginGoogleCallback) } as unknown as AuthApi;
}

const signedIn: AuthResult = { accessToken: "tok", mfaVerified: true, enrollment: null };

describe("GoogleCallback — the landing leg of IAM-010's Google redirect", () => {
  it("shows a pending state, then reports a signed-in result", async () => {
    const onSignedIn = vi.fn();
    render(
      <GoogleCallback
        api={api(async () => ({ kind: "signed_in", result: signedIn }))}
        code="auth-code"
        state="state-value"
        onSignedIn={onSignedIn}
        onBackToLogin={vi.fn()}
      />,
    );

    expect(screen.getByTestId("google-callback-pending")).toBeDefined();
    await waitFor(() => expect(onSignedIn).toHaveBeenCalledWith(signedIn));
  });

  it("calls the API with exactly the code/state it was given", () => {
    const loginGoogleCallback = vi.fn(async () => ({
      kind: "signed_in" as const,
      result: signedIn,
    }));
    render(
      <GoogleCallback
        api={{ loginGoogleCallback } as unknown as AuthApi}
        code="my-code"
        state="my-state"
        onSignedIn={vi.fn()}
        onBackToLogin={vi.fn()}
      />,
    );

    expect(loginGoogleCallback).toHaveBeenCalledWith("my-code", "my-state");
  });

  it("shows IAM-010c's link-required message and offers a way back, without reporting a sign-in", async () => {
    const onSignedIn = vi.fn();
    render(
      <GoogleCallback
        api={api(async () => ({
          kind: "link_required",
          message: "Dit Google-account is nog niet gekoppeld aan een LEDGR-account.",
        }))}
        code="auth-code"
        state="state-value"
        onSignedIn={onSignedIn}
        onBackToLogin={vi.fn()}
      />,
    );

    await waitFor(() =>
      expect(screen.getByTestId("google-callback-problem").textContent).toContain(
        "nog niet gekoppeld",
      ),
    );
    expect(onSignedIn).not.toHaveBeenCalled();
  });

  it("shows a failure message when the ceremony itself is refused", async () => {
    render(
      <GoogleCallback
        api={api(async () => {
          throw new ApiError(410, "ceremony_not_found", "Deze aanmeldpoging is verlopen.");
        })}
        code="auth-code"
        state="state-value"
        onSignedIn={vi.fn()}
        onBackToLogin={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("google-callback-problem")).toBeDefined());
  });

  it("the back-to-login button calls back", async () => {
    const onBackToLogin = vi.fn();
    render(
      <GoogleCallback
        api={api(async () => ({ kind: "link_required", message: "x" }))}
        code="auth-code"
        state="state-value"
        onSignedIn={vi.fn()}
        onBackToLogin={onBackToLogin}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("google-callback-back")).toBeDefined());
    fireEvent.click(screen.getByTestId("google-callback-back"));
    expect(onBackToLogin).toHaveBeenCalledOnce();
  });
});
