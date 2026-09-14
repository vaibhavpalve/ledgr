import { fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";

import { ApiError, type AuthApi, type AuthResult, type MfaEnrollmentStatus } from "./api";
import { MfaEnrollment } from "./MfaEnrollment";

function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

function api(overrides: Partial<AuthApi> = {}) {
  return {
    mfaTotpEnrollBegin: vi.fn(),
    mfaTotpEnrollConfirm: vi.fn(),
    mfaTotpVerify: vi.fn(),
    mfaPasskeyEnrollBegin: vi.fn(),
    mfaPasskeyEnrollFinish: vi.fn(),
    mfaPasskeyVerifyBegin: vi.fn(),
    mfaPasskeyVerifyFinish: vi.fn(),
    ...overrides,
  } as unknown as AuthApi;
}

const nothingEnrolled: MfaEnrollmentStatus = { hasPasskey: false, hasTotp: false };
const verified: AuthResult = { accessToken: "tok2", mfaVerified: true, enrollment: null };

function stubWebAuthn(credential: unknown) {
  vi.stubGlobal("navigator", {
    ...navigator,
    credentials: {
      create: vi.fn(async () => credential),
      get: vi.fn(async () => credential),
    },
  });
  (window as unknown as { PublicKeyCredential: unknown }).PublicKeyCredential = class {};
}

afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as unknown as { PublicKeyCredential?: unknown }).PublicKeyCredential;
});

describe("MfaEnrollment — ADR-054's whole reason for existing", () => {
  it("TOTP: begins enrolment, shows the secret, confirms the code, reports the result", async () => {
    const mfaTotpEnrollBegin = vi.fn(async () => ({
      secret: "JBSWY3DPEHPK3PXP",
      provisioningUri: "otpauth://totp/LEDGR:a@example.com?secret=JBSWY3DPEHPK3PXP",
    }));
    const mfaTotpEnrollConfirm = vi.fn(async () => verified);
    const onVerified = vi.fn();

    render(
      <MfaEnrollment
        api={api({ mfaTotpEnrollBegin, mfaTotpEnrollConfirm })}
        enrollment={nothingEnrolled}
        onVerified={onVerified}
      />,
    );

    fireEvent.click(screen.getByTestId("mfa-totp-begin"));
    await waitFor(() =>
      expect(screen.getByTestId("mfa-totp-secret").textContent).toContain("JBSWY3DPEHPK3PXP"),
    );

    fireEvent.change(screen.getByTestId("mfa-totp-code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByTestId("mfa-totp-confirm"));

    await waitFor(() => expect(onVerified).toHaveBeenCalledWith(verified));
    expect(mfaTotpEnrollConfirm).toHaveBeenCalledWith("JBSWY3DPEHPK3PXP", "123456");
  });

  it("TOTP: renders a real scannable QR code, not just the raw secret", async () => {
    const mfaTotpEnrollBegin = vi.fn(async () => ({
      secret: "JBSWY3DPEHPK3PXP",
      provisioningUri: "otpauth://totp/LEDGR:a@example.com?secret=JBSWY3DPEHPK3PXP",
    }));
    const onVerified = vi.fn();

    render(
      <MfaEnrollment
        api={api({ mfaTotpEnrollBegin })}
        enrollment={nothingEnrolled}
        onVerified={onVerified}
      />,
    );

    fireEvent.click(screen.getByTestId("mfa-totp-begin"));

    // Generation is a real async call into the `qrcode` package (not
    // mocked) - waiting for the <svg> to actually land is what tells apart
    // "renders a QR code" from "renders a container that would have one".
    await waitFor(() => {
      const container = screen.getByTestId("mfa-totp-qr");
      expect(container.querySelector("svg")).not.toBeNull();
    });

    // The QR is decorative (the manual key beside it carries the same
    // information for anyone who can't use it visually), so it must not
    // duplicate an accessible announcement of the encoded secret.
    expect(screen.getByTestId("mfa-totp-qr").getAttribute("aria-hidden")).toBe("true");

    // The "don't have an app" guidance this whole change exists to add.
    expect(screen.getByTestId("mfa-totp-no-app").textContent).toContain("Google Authenticator");
  });

  it("TOTP: an already-enrolled factor goes straight to a code prompt (step-up, not enrolment)", async () => {
    const mfaTotpVerify = vi.fn(async () => verified);
    const onVerified = vi.fn();

    render(
      <MfaEnrollment
        api={api({ mfaTotpVerify })}
        enrollment={{ hasPasskey: false, hasTotp: true }}
        onVerified={onVerified}
      />,
    );

    expect(screen.queryByTestId("mfa-totp-begin")).toBeNull();
    fireEvent.change(screen.getByTestId("mfa-totp-code"), { target: { value: "654321" } });
    fireEvent.click(screen.getByTestId("mfa-totp-verify"));

    await waitFor(() => expect(onVerified).toHaveBeenCalledWith(verified));
    expect(mfaTotpVerify).toHaveBeenCalledWith("654321");
  });

  it("TOTP: shows the server's refusal (e.g. an already-used code) without reporting success", async () => {
    const mfaTotpVerify = vi.fn(async () => {
      throw new ApiError(422, "invalid_code", "Ongeldige code.");
    });
    const onVerified = vi.fn();

    render(
      <MfaEnrollment
        api={api({ mfaTotpVerify })}
        enrollment={{ hasPasskey: false, hasTotp: true }}
        onVerified={onVerified}
      />,
    );

    fireEvent.change(screen.getByTestId("mfa-totp-code"), { target: { value: "000000" } });
    fireEvent.click(screen.getByTestId("mfa-totp-verify"));

    await waitFor(() =>
      expect(screen.getByTestId("mfa-totp-error").textContent).toBe("Ongeldige code."),
    );
    expect(onVerified).not.toHaveBeenCalled();
  });

  it("passkey: enrols a new factor through the browser WebAuthn API and reports the result", async () => {
    stubWebAuthn({
      id: "cred-1",
      rawId: new Uint8Array([1]).buffer,
      response: {
        clientDataJSON: new Uint8Array([2]).buffer,
        attestationObject: new Uint8Array([3]).buffer,
      },
    });
    const mfaPasskeyEnrollBegin = vi.fn(async () => ({
      ceremonyId: "cer-1",
      optionsJson: JSON.stringify({
        rp: { name: "LEDGR" },
        user: { id: "AAAA", name: "a@example.com", displayName: "a@example.com" },
        challenge: "AAAA",
        pubKeyCredParams: [{ type: "public-key", alg: -7 }],
      }),
    }));
    const mfaPasskeyEnrollFinish = vi.fn(async () => verified);
    const onVerified = vi.fn();

    render(
      <MfaEnrollment
        api={api({ mfaPasskeyEnrollBegin, mfaPasskeyEnrollFinish })}
        enrollment={nothingEnrolled}
        onVerified={onVerified}
      />,
    );

    fireEvent.change(screen.getByTestId("mfa-passkey-name"), { target: { value: "My laptop" } });
    fireEvent.click(screen.getByTestId("mfa-passkey-enroll"));

    await waitFor(() => expect(onVerified).toHaveBeenCalledWith(verified));
    expect(mfaPasskeyEnrollFinish).toHaveBeenCalledWith(
      "cer-1",
      "My laptop",
      expect.objectContaining({ id: "cred-1" }),
    );
  });

  it("passkey: an already-enrolled factor offers step-up verification instead of enrolment", async () => {
    stubWebAuthn({
      id: "cred-1",
      rawId: new Uint8Array([1]).buffer,
      response: {
        clientDataJSON: new Uint8Array([2]).buffer,
        authenticatorData: new Uint8Array([3]).buffer,
        signature: new Uint8Array([4]).buffer,
      },
    });
    const mfaPasskeyVerifyBegin = vi.fn(async () => ({
      ceremonyId: "cer-2",
      optionsJson: JSON.stringify({ challenge: "AAAA" }),
    }));
    const mfaPasskeyVerifyFinish = vi.fn(async () => verified);
    const onVerified = vi.fn();

    render(
      <MfaEnrollment
        api={api({ mfaPasskeyVerifyBegin, mfaPasskeyVerifyFinish })}
        enrollment={{ hasPasskey: true, hasTotp: false }}
        onVerified={onVerified}
      />,
    );

    expect(screen.queryByTestId("mfa-passkey-enroll")).toBeNull();
    fireEvent.click(screen.getByTestId("mfa-passkey-verify"));

    await waitFor(() => expect(onVerified).toHaveBeenCalledWith(verified));
    expect(mfaPasskeyVerifyFinish).toHaveBeenCalledWith(
      "cer-2",
      expect.objectContaining({ id: "cred-1" }),
    );
  });

  it("passkey: offers no button, just a message, when this browser has no WebAuthn support", () => {
    render(<MfaEnrollment api={api()} enrollment={nothingEnrolled} onVerified={vi.fn()} />);

    expect(screen.getByTestId("mfa-passkey-unavailable")).toBeDefined();
    expect(screen.queryByTestId("mfa-passkey-enroll")).toBeNull();
  });
});
