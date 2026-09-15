import { useEffect, useState } from "react";
import QRCode from "qrcode";
import { useI18n } from "@ledgr/i18n";

import type { AuthApi, AuthResult, MfaEnrollmentStatus } from "./api";
import { errorMessage } from "./LoginForm";
import { createPasskey, getPasskey } from "./webauthn";

/**
 * ADR-054's whole reason for existing: shipping signup without shipping
 * this in the same change means the first thing every design partner meets
 * after creating an account is being locked out of it, because
 * `MfaEnforcementMiddleware` (ADR-008) blocks every request unconditionally
 * until IAM-011 is satisfied. `Shell` renders this in place of the
 * authenticated app the moment `mfaVerified` is false — there is no way
 * past it, by construction, the same way there is no route-level opt-out
 * of the middleware itself.
 *
 * Two independent paths, either of which finishes the screen — a person
 * only ever needs one factor:
 *
 *   - TOTP: `enrollment.hasTotp` tells this screen whether to offer
 *     first-time enrolment (secret + QR + confirm) or step-up verification
 *     (this device/browser already has a factor from a prior session that
 *     just hasn't cleared THIS session's gate — a code is enough).
 *   - Passkey: same split via `enrollment.hasPasskey`, using the browser
 *     WebAuthn API through `./webauthn`.
 *
 * Both call into `onVerified` with the SAME shape (`AuthResult` with
 * `mfaVerified: true`) the moment either succeeds — `Shell` does not care
 * which path got there.
 */
export function MfaEnrollment({
  api,
  enrollment,
  onVerified,
}: {
  api: AuthApi;
  enrollment: MfaEnrollmentStatus;
  onVerified: (result: AuthResult) => void;
}) {
  const { t } = useI18n();

  return (
    <div className="auth-form" data-testid="mfa-enrollment">
      <p>{t("auth.mfa.intro")}</p>
      <TotpSection api={api} alreadyEnrolled={enrollment.hasTotp} onVerified={onVerified} />
      <PasskeySection api={api} alreadyEnrolled={enrollment.hasPasskey} onVerified={onVerified} />
    </div>
  );
}

/** Exported for the security settings screen, which offers the same enrolment after sign-in. */
export function TotpSection({
  api,
  alreadyEnrolled,
  onVerified,
}: {
  api: AuthApi;
  alreadyEnrolled: boolean;
  onVerified: (result: AuthResult) => void;
}) {
  const { t } = useI18n();
  const [secret, setSecret] = useState<string | null>(null);
  const [provisioningUri, setProvisioningUri] = useState<string | null>(null);
  const [qrSvg, setQrSvg] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [rememberDevice, setRememberDevice] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  // Generated client-side from the SAME provisioning URI the manual key and
  // the "open in app" link below already carry — nothing extra is sent
  // anywhere, and nothing about the secret leaves this browser tab any more
  // than it already did. SVG string output rather than a canvas render: it
  // needs no <canvas> 2D context (unavailable in some locked-down/managed
  // browser setups, and in this component's own test environment), so the
  // same code path renders in a real browser and under jsdom alike.
  useEffect(() => {
    if (provisioningUri === null) {
      setQrSvg(null);
      return;
    }
    let cancelled = false;
    QRCode.toString(provisioningUri, { type: "svg", margin: 1, width: 220 })
      .then((svg) => {
        if (!cancelled) setQrSvg(svg);
      })
      .catch(() => {
        // Degraded, not blocking (auth.mfa.totp.qr_unavailable) - the
        // manual key rendered alongside it is a complete, independent path
        // to the same secret.
        if (!cancelled) setQrSvg(null);
      });
    return () => {
      cancelled = true;
    };
  }, [provisioningUri]);

  const beginEnrollment = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const enrollment = await api.mfaTotpEnrollBegin();
      setSecret(enrollment.secret);
      setProvisioningUri(enrollment.provisioningUri);
    } catch (error) {
      setProblem(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const submitCode = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const result = alreadyEnrolled
        ? await api.mfaTotpVerify(code, rememberDevice)
        : await api.mfaTotpEnrollConfirm(secret ?? "", code);
      onVerified(result);
    } catch (error) {
      setProblem(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section data-testid="mfa-totp-section">
      <h2>{t("auth.mfa.totp.heading")}</h2>

      {alreadyEnrolled ? (
        <>
          <p>{t("auth.mfa.totp.verify_heading")}</p>
          <label>
            {t("auth.mfa.totp.code_label")}
            <input
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              data-testid="mfa-totp-code"
              value={code}
              onChange={(event) => setCode(event.target.value)}
            />
          </label>
          <label className="mfa-remember-device">
            <input
              type="checkbox"
              data-testid="mfa-totp-remember-device"
              checked={rememberDevice}
              onChange={(event) => setRememberDevice(event.target.checked)}
            />
            {t("auth.mfa.remember_device")}
          </label>
          <button
            type="button"
            data-testid="mfa-totp-verify"
            disabled={busy || code.length === 0}
            onClick={() => void submitCode()}
          >
            {t("auth.mfa.totp.confirm")}
          </button>
        </>
      ) : secret === null ? (
        <button
          type="button"
          data-testid="mfa-totp-begin"
          disabled={busy}
          onClick={() => void beginEnrollment()}
        >
          {t("auth.mfa.totp.heading")}
        </button>
      ) : (
        <>
          <p>{t("auth.mfa.totp.instructions")}</p>

          {qrSvg !== null ? (
            // Decorative: the QR code is a visual shortcut to the SAME
            // secret already rendered as text below it, which is what a
            // screen reader user (or anyone who can't scan) uses instead —
            // announcing the encoded URI a second time here would be noise,
            // not a second way to reach it.
            <div
              className="mfa-totp-qr"
              data-testid="mfa-totp-qr"
              aria-hidden="true"
              // The markup rendered here is this component's OWN SVG output
              // from the `qrcode` package, built from a provisioning URI
              // this same browser just requested from our API — not
              // third-party or user-supplied content.
              dangerouslySetInnerHTML={{ __html: qrSvg }}
            />
          ) : provisioningUri !== null ? (
            <p className="caption" data-testid="mfa-totp-qr-unavailable">
              {t("auth.mfa.totp.qr_unavailable")}
            </p>
          ) : null}

          {provisioningUri ? (
            <a href={provisioningUri} className="mfa-totp-open-link" data-testid="mfa-totp-uri">
              {t("auth.mfa.totp.open_in_app")}
            </a>
          ) : null}

          <div className="mfa-totp-no-app" data-testid="mfa-totp-no-app">
            <p className="label">{t("auth.mfa.totp.no_app_heading")}</p>
            <p className="caption">{t("auth.mfa.totp.no_app_body")}</p>
          </div>

          <div className="mfa-totp-manual">
            <p className="label">{t("auth.mfa.totp.manual_entry_heading")}</p>
            <p data-testid="mfa-totp-secret">
              <code>{secret}</code>
            </p>
          </div>

          <label>
            {t("auth.mfa.totp.code_label")}
            <input
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              data-testid="mfa-totp-code"
              value={code}
              onChange={(event) => setCode(event.target.value)}
            />
          </label>
          <button
            type="button"
            className="button--primary"
            data-testid="mfa-totp-confirm"
            disabled={busy || code.length === 0}
            onClick={() => void submitCode()}
          >
            {t("auth.mfa.totp.confirm")}
          </button>
        </>
      )}

      {problem ? (
        <p role="alert" data-testid="mfa-totp-error">
          {problem}
        </p>
      ) : null}
    </section>
  );
}

/** Exported for the security settings screen — adding a passkey is this same ceremony. */
export function PasskeySection({
  api,
  alreadyEnrolled,
  onVerified,
}: {
  api: AuthApi;
  alreadyEnrolled: boolean;
  onVerified: (result: AuthResult) => void;
}) {
  const { t } = useI18n();
  const [name, setName] = useState("");
  const [rememberDevice, setRememberDevice] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const supported = typeof window !== "undefined" && "PublicKeyCredential" in window;

  const enroll = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const challenge = await api.mfaPasskeyEnrollBegin();
      const { credential, suggestedName } = await createPasskey(challenge.optionsJson);
      const result = await api.mfaPasskeyEnrollFinish(
        challenge.ceremonyId,
        name.trim() || suggestedName,
        credential,
      );
      onVerified(result);
    } catch (error) {
      setProblem(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const verify = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const challenge = await api.mfaPasskeyVerifyBegin();
      const credential = await getPasskey(challenge.optionsJson);
      const result = await api.mfaPasskeyVerifyFinish(challenge.ceremonyId, credential, rememberDevice);
      onVerified(result);
    } catch (error) {
      setProblem(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section data-testid="mfa-passkey-section">
      <h2>{t("auth.mfa.passkey.heading")}</h2>

      {!supported ? (
        <p data-testid="mfa-passkey-unavailable">{t("auth.mfa.unavailable")}</p>
      ) : alreadyEnrolled ? (
        <>
          <label className="mfa-remember-device">
            <input
              type="checkbox"
              data-testid="mfa-passkey-remember-device"
              checked={rememberDevice}
              onChange={(event) => setRememberDevice(event.target.checked)}
            />
            {t("auth.mfa.remember_device")}
          </label>
          <button
            type="button"
            data-testid="mfa-passkey-verify"
            disabled={busy}
            onClick={() => void verify()}
          >
            {t("auth.mfa.passkey.verify_button")}
          </button>
        </>
      ) : (
        <>
          <label>
            {t("auth.mfa.passkey.name_label")}
            <input
              type="text"
              data-testid="mfa-passkey-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <button
            type="button"
            data-testid="mfa-passkey-enroll"
            disabled={busy}
            onClick={() => void enroll()}
          >
            {t("auth.mfa.passkey.enroll_button")}
          </button>
        </>
      )}

      {problem ? (
        <p role="alert" data-testid="mfa-passkey-error">
          {problem}
        </p>
      ) : null}
    </section>
  );
}
