import { fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";

import { ApiError, type AuthApi, type AuthResult } from "./api";
import { LoginForm } from "./LoginForm";

function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

function api(overrides: Partial<AuthApi> = {}) {
  return {
    login: vi.fn(),
    loginGoogleStart: vi.fn(),
    loginPasskeyBegin: vi.fn(),
    loginPasskeyFinish: vi.fn(),
    ...overrides,
  } as unknown as AuthApi;
}

const verified: AuthResult = {
  accessToken: "tok",
  mfaVerified: true,
  enrollment: null,
  trustedDeviceToken: null,
};

describe("LoginForm — IAM-010's password path", () => {
  it("submits email/password and reports the result", async () => {
    const login = vi.fn(async () => verified);
    const onSignedIn = vi.fn();
    render(
      <LoginForm
        api={api({ login })}
        onSwitchToSignup={vi.fn()}
        onSignedIn={onSignedIn}
        onGoogleStart={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByTestId("login-email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByTestId("login-password"), { target: { value: "hunter2" } });
    fireEvent.click(screen.getByTestId("login-submit"));

    await waitFor(() => expect(onSignedIn).toHaveBeenCalledWith(verified));
    expect(login).toHaveBeenCalledWith("a@example.com", "hunter2");
  });

  it("shows the server's own translated sentence on refusal (FR-UX-007) and does not report success", async () => {
    const login = vi.fn(async () => {
      throw new ApiError(401, "invalid_credentials", "E-mailadres of wachtwoord onjuist.");
    });
    const onSignedIn = vi.fn();
    render(
      <LoginForm
        api={api({ login })}
        onSwitchToSignup={vi.fn()}
        onSignedIn={onSignedIn}
        onGoogleStart={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByTestId("login-email"), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByTestId("login-password"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByTestId("login-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("login-error").textContent).toBe(
        "E-mailadres of wachtwoord onjuist.",
      ),
    );
    expect(onSignedIn).not.toHaveBeenCalled();
  });

  it("clicking the switch-to-signup link calls back rather than navigating", () => {
    const onSwitchToSignup = vi.fn();
    render(
      <LoginForm
        api={api()}
        onSwitchToSignup={onSwitchToSignup}
        onSignedIn={vi.fn()}
        onGoogleStart={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByTestId("switch-to-signup"));
    expect(onSwitchToSignup).toHaveBeenCalledOnce();
  });
});

describe("LoginForm — Google sign-in button", () => {
  it("fetches the authorization URL and hands it to onGoogleStart, never navigating itself", async () => {
    const loginGoogleStart = vi.fn(async () => ({
      authorizationUrl: "https://accounts.google.com/o/oauth2/...",
    }));
    const onGoogleStart = vi.fn();
    render(
      <LoginForm
        api={api({ loginGoogleStart })}
        onSwitchToSignup={vi.fn()}
        onSignedIn={vi.fn()}
        onGoogleStart={onGoogleStart}
      />,
    );

    fireEvent.click(screen.getByTestId("login-google"));

    await waitFor(() =>
      expect(onGoogleStart).toHaveBeenCalledWith("https://accounts.google.com/o/oauth2/..."),
    );
  });

  it("shows a problem instead when Google sign-in is not configured", async () => {
    const loginGoogleStart = vi.fn(async () => {
      throw new ApiError(
        503,
        "not_configured",
        "Inloggen met Google is momenteel niet beschikbaar.",
      );
    });
    render(
      <LoginForm
        api={api({ loginGoogleStart })}
        onSwitchToSignup={vi.fn()}
        onSignedIn={vi.fn()}
        onGoogleStart={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByTestId("login-google"));

    await waitFor(() =>
      expect(screen.getByTestId("login-error").textContent).toBe(
        "Inloggen met Google is momenteel niet beschikbaar.",
      ),
    );
  });
});

describe("LoginForm — passkey sign-in (IAM-012: satisfies MFA in one step)", () => {
  it("begins the ceremony, calls the browser WebAuthn API, and reports an already-verified result", async () => {
    const loginPasskeyBegin = vi.fn(async () => ({
      ceremonyId: "cer-1",
      optionsJson: JSON.stringify({ challenge: "AAAA" }),
    }));
    const loginPasskeyFinish = vi.fn(async () => verified);
    const onSignedIn = vi.fn();

    const credential = {
      id: "cred-1",
      rawId: new Uint8Array([1, 2, 3]).buffer,
      response: {
        clientDataJSON: new Uint8Array([4]).buffer,
        authenticatorData: new Uint8Array([5]).buffer,
        signature: new Uint8Array([6]).buffer,
      },
    };
    vi.stubGlobal("navigator", {
      ...navigator,
      credentials: { get: vi.fn(async () => credential) },
    });
    // Adding to the real `window` rather than stubbing it wholesale - jsdom's
    // `window` is what React itself renders against, and replacing it would
    // break far more than this test's own webauthn feature-detection check.
    (window as unknown as { PublicKeyCredential: unknown }).PublicKeyCredential = class {};

    render(
      <LoginForm
        api={api({ loginPasskeyBegin, loginPasskeyFinish })}
        onSwitchToSignup={vi.fn()}
        onSignedIn={onSignedIn}
        onGoogleStart={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByTestId("login-passkey"));

    await waitFor(() => expect(onSignedIn).toHaveBeenCalledWith(verified));
    expect(loginPasskeyFinish).toHaveBeenCalledWith(
      "cer-1",
      expect.objectContaining({ id: "cred-1" }),
    );

    vi.unstubAllGlobals();
    delete (window as unknown as { PublicKeyCredential?: unknown }).PublicKeyCredential;
  });
});
