import { fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import { StrictMode, type ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";

import { ApiError, type AuthApi, type AuthResult, type GoogleCallbackResult } from "./api";
import { GoogleCallback } from "./GoogleCallback";

function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

function api(
  loginGoogleCallback: () => Promise<GoogleCallbackResult>,
  overrides: Partial<AuthApi> = {},
): AuthApi {
  return {
    loginGoogleCallback: vi.fn(loginGoogleCallback),
    ...overrides,
  } as unknown as AuthApi;
}

/** A fresh code per render: GoogleCallback shares one request per code across mounts. */
let codeCounter = 0;
function freshCode(): string {
  codeCounter += 1;
  return `auth-code-${codeCounter}`;
}

const signedIn: AuthResult = {
  accessToken: "tok",
  mfaVerified: true,
  enrollment: null,
  trustedDeviceToken: null,
};

describe("GoogleCallback — the landing leg of IAM-010's Google redirect", () => {
  it("shows a pending state, then reports a signed-in result", async () => {
    const onSignedIn = vi.fn();
    render(
      <GoogleCallback
        api={api(async () => ({ kind: "signed_in", result: signedIn }))}
        code={freshCode()}
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
        code="my-code-unique"
        state="my-state"
        onSignedIn={vi.fn()}
        onBackToLogin={vi.fn()}
      />,
    );

    expect(loginGoogleCallback).toHaveBeenCalledWith("my-code-unique", "my-state");
  });

  it("shows IAM-010c's link-required message and offers a way back, without reporting a sign-in", async () => {
    const onSignedIn = vi.fn();
    render(
      <GoogleCallback
        api={api(async () => ({
          kind: "link_required",
          message: "Dit Google-account is nog niet gekoppeld aan een BOEKLITE-account.",
          ticket: null,
          email: null,
        }))}
        code={freshCode()}
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
        code={freshCode()}
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
        api={api(async () => ({ kind: "link_required", message: "x", ticket: null, email: null }))}
        code={freshCode()}
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

describe("GoogleCallback — FR-MDL-001 for a brand-new Google identity", () => {
  it("shows the one-question form, naming the Google-verified email", async () => {
    render(
      <GoogleCallback
        api={api(async () => ({
          kind: "signup_required",
          ticket: "ticket-1",
          email: "brand.new@example.com",
        }))}
        code={freshCode()}
        state="state-value"
        onSignedIn={vi.fn()}
        onBackToLogin={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("google-signup-form")).toBeDefined());
    expect(screen.getByTestId("google-signup-email").textContent).toContain(
      "brand.new@example.com",
    );
    // Never asks for email or password again - both already settled by Google.
    expect(screen.queryByTestId("login-email")).toBeNull();
    expect(screen.queryByTestId("signup-password")).toBeNull();
  });

  it("submits the ticket with the chosen account model and reports the result", async () => {
    const signupGoogle = vi.fn(async () => signedIn);
    const onSignedIn = vi.fn();
    render(
      <GoogleCallback
        api={api(
          async () => ({ kind: "signup_required", ticket: "ticket-2", email: "a@example.com" }),
          { signupGoogle },
        )}
        code={freshCode()}
        state="state-value"
        onSignedIn={onSignedIn}
        onBackToLogin={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("google-signup-form")).toBeDefined());
    fireEvent.change(screen.getByTestId("google-signup-organization-name"), {
      target: { value: "Bakker Consultancy" },
    });
    fireEvent.click(screen.getByTestId("google-signup-submit"));

    await waitFor(() => expect(onSignedIn).toHaveBeenCalledWith(signedIn));
    expect(signupGoogle).toHaveBeenCalledWith({
      ticket: "ticket-2",
      accountModel: "self_managed",
      organizationName: "Bakker Consultancy",
      kvkNumber: null,
    });
  });

  it("collects and sends the KvK number for the firm account model", async () => {
    const signupGoogle = vi.fn(async () => signedIn);
    render(
      <GoogleCallback
        api={api(
          async () => ({ kind: "signup_required", ticket: "ticket-3", email: "a@example.com" }),
          { signupGoogle },
        )}
        code={freshCode()}
        state="state-value"
        onSignedIn={vi.fn()}
        onBackToLogin={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("google-signup-form")).toBeDefined());
    fireEvent.click(screen.getByTestId("google-signup-account-model-firm"));
    expect(screen.getByTestId("google-signup-kvk-number")).toBeDefined();

    fireEvent.change(screen.getByTestId("google-signup-organization-name"), {
      target: { value: "Bakker Accountants" },
    });
    fireEvent.change(screen.getByTestId("google-signup-kvk-number"), {
      target: { value: "87654321" },
    });
    fireEvent.click(screen.getByTestId("google-signup-submit"));

    await waitFor(() =>
      expect(signupGoogle).toHaveBeenCalledWith({
        ticket: "ticket-3",
        accountModel: "firm",
        organizationName: "Bakker Accountants",
        kvkNumber: "87654321",
      }),
    );
  });

  it("shows the server's refusal (e.g. an already-used ticket) without reporting success", async () => {
    const signupGoogle = vi.fn(async () => {
      throw new ApiError(410, "ceremony_not_found", "Deze aanmeldpoging is verlopen.");
    });
    const onSignedIn = vi.fn();
    render(
      <GoogleCallback
        api={api(
          async () => ({ kind: "signup_required", ticket: "ticket-4", email: "a@example.com" }),
          { signupGoogle },
        )}
        code={freshCode()}
        state="state-value"
        onSignedIn={onSignedIn}
        onBackToLogin={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("google-signup-form")).toBeDefined());
    fireEvent.change(screen.getByTestId("google-signup-organization-name"), {
      target: { value: "Bakker" },
    });
    fireEvent.click(screen.getByTestId("google-signup-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("google-signup-error").textContent).toBe(
        "Deze aanmeldpoging is verlopen.",
      ),
    );
    expect(onSignedIn).not.toHaveBeenCalled();
  });
});

describe("GoogleCallback — IAM-010c, an email that already has a password account", () => {
  const linkRequired = async (): Promise<GoogleCallbackResult> => ({
    kind: "link_required",
    message: "Er bestaat al een account.",
    ticket: "link-ticket-1",
    email: "owner@example.com",
  });

  it("asks for the existing account's password, naming the address", async () => {
    render(
      <GoogleCallback
        api={api(linkRequired)}
        code={freshCode()}
        state="state-value"
        onSignedIn={vi.fn()}
        onBackToLogin={vi.fn()}
      />,
      "en",
    );

    await waitFor(() => expect(screen.getByTestId("google-link-form")).toBeDefined());
    expect(screen.getByTestId("google-link-intro").textContent).toContain("owner@example.com");
    expect(screen.getByTestId("google-link-password")).toBeDefined();
  });

  it("sends only the ticket and the password, then reports the sign-in", async () => {
    const linkGoogle = vi.fn(async () => signedIn);
    const onSignedIn = vi.fn();
    render(
      <GoogleCallback
        api={api(linkRequired, { linkGoogle })}
        code={freshCode()}
        state="state-value"
        onSignedIn={onSignedIn}
        onBackToLogin={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("google-link-form")).toBeDefined());
    fireEvent.change(screen.getByTestId("google-link-password"), {
      target: { value: "the-real-password" },
    });
    fireEvent.click(screen.getByTestId("google-link-submit"));

    await waitFor(() => expect(onSignedIn).toHaveBeenCalledWith(signedIn));
    expect(linkGoogle).toHaveBeenCalledWith("link-ticket-1", "the-real-password");
  });

  it("shows a wrong password's refusal and stays on the form to try again", async () => {
    const linkGoogle = vi.fn(async () => {
      throw new ApiError(401, "invalid_credentials", "E-mailadres of wachtwoord klopt niet.");
    });
    const onSignedIn = vi.fn();
    render(
      <GoogleCallback
        api={api(linkRequired, { linkGoogle })}
        code={freshCode()}
        state="state-value"
        onSignedIn={onSignedIn}
        onBackToLogin={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("google-link-form")).toBeDefined());
    fireEvent.change(screen.getByTestId("google-link-password"), { target: { value: "typo" } });
    fireEvent.click(screen.getByTestId("google-link-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("google-link-error").textContent).toContain("klopt niet"),
    );
    expect(screen.getByTestId("google-link-form")).toBeDefined();
    expect(onSignedIn).not.toHaveBeenCalled();
  });
});

describe("GoogleCallback — one request per authorization code", () => {
  it("does not replay the single-use code when StrictMode runs the effect twice", async () => {
    const loginGoogleCallback = vi.fn(async () => ({
      kind: "signed_in" as const,
      result: signedIn,
    }));
    const onSignedIn = vi.fn();
    render(
      <StrictMode>
        <GoogleCallback
          api={{ loginGoogleCallback } as unknown as AuthApi}
          code={freshCode()}
          state="state-value"
          onSignedIn={onSignedIn}
          onBackToLogin={vi.fn()}
        />
      </StrictMode>,
    );

    await waitFor(() => expect(onSignedIn).toHaveBeenCalledWith(signedIn));
    expect(loginGoogleCallback).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("google-callback-problem")).toBeNull();
  });
});
