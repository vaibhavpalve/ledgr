import { useCallback, useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type {
  AdministrationView,
  InvoiceTemplateView,
  PasskeyView,
  SessionView,
  TrustedDeviceView,
} from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { useAuth } from "../auth/AuthProvider";
import { PasskeySection, TotpSection } from "../auth/MfaEnrollment";
import { storeSession } from "../auth/session";
import { LanguageSwitcher } from "../LanguageSwitcher";
import { FormattingLocaleField } from "../onboarding/OnboardingWizard";
import { useSession } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { Icon } from "../shell/icons";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { TemplateDesigner } from "../templates/TemplateDesigner";
import { ThemeToggle } from "../theme/ThemeToggle";

/**
 * `/settings/*` — the settings area the brief lists: profile, appearance,
 * security (§4.4), organization, invoice design. One layout with a
 * sub-navigation, one screen per URL.
 *
 * Nothing here is required for the core loop (FR-UX-003): every default
 * is already right, and these screens exist for the person who wants to
 * change one.
 */
const SECTIONS = [
  { to: "/settings/profile", key: "settings.profile.title" },
  { to: "/settings/appearance", key: "settings.appearance.title" },
  { to: "/settings/security", key: "settings.security.title" },
  { to: "/settings/organization", key: "settings.organization.title" },
  { to: "/settings/invoice-design", key: "settings.invoice_design.title" },
] as const;

export function SettingsLayout() {
  const { t } = useI18n();
  const { administration } = useSession();
  return (
    <section className="screen" aria-label={t("common.nav.settings")} data-testid="settings">
      <PageHeader title={t("common.nav.settings")} />
      <nav className="subnav" aria-label={t("settings.sections_label")}>
        {SECTIONS.filter(
          (section) => administration !== null || section.to !== "/settings/invoice-design",
        ).map((section) => (
          <NavLink
            key={section.to}
            to={section.to}
            data-testid={`settings-nav-${section.to.split("/").pop()}`}
          >
            {t(section.key)}
          </NavLink>
        ))}
      </nav>
      <Outlet />
    </section>
  );
}

/* ------------------------------------------------------------------------ */

export function ProfileSettings() {
  const { t } = useI18n();
  const { me } = useSession();
  const { account } = useServices();
  const [resend, setResend] = useState<"idle" | "sending" | "sent" | "failed">("idle");
  const [problem, setProblem] = useState<string | null>(null);

  const resendVerification = async () => {
    setResend("sending");
    setProblem(null);
    try {
      await account.resendVerificationEmail();
      setResend("sent");
    } catch (error) {
      setProblem(describeError(error));
      setResend("failed");
    }
  };

  return (
    <section
      className="screen__section"
      aria-label={t("settings.profile.title")}
      data-testid="settings-profile"
    >
      <h2>{t("settings.profile.title")}</h2>
      <dl className="facts panel panel__body">
        <div>
          <dt className="label">{t("auth.sign_in.email")}</dt>
          <dd data-testid="profile-email">{me.user.email}</dd>
          <dd className="meta-line">
            {me.user.email_verified ? (
              <span className="chip chip--positive" data-testid="profile-verified">
                {t("settings.profile.verified")}
              </span>
            ) : (
              <>
                <span className="chip chip--caution" data-testid="profile-unverified">
                  {t("settings.profile.unverified")}
                </span>
                <button
                  type="button"
                  disabled={resend === "sending"}
                  data-testid="profile-resend"
                  onClick={() => void resendVerification()}
                >
                  {t("common.verification.resend")}
                </button>
              </>
            )}
          </dd>
          {resend === "sent" ? <dd role="status">{t("common.verification.resent")}</dd> : null}
          {problem !== null ? (
            <dd role="alert" className="field-error">
              {problem}
            </dd>
          ) : null}
        </div>
        <div>
          <dt className="label">{t("common.language.label")}</dt>
          <dd>
            <LanguageSwitcher />
          </dd>
          <dd className="caption">{t("settings.profile.language_hint")}</dd>
        </div>
        <div>
          <dt className="label">{t("settings.organization.title")}</dt>
          <dd>{me.organization.name}</dd>
          <dd className="caption">{t(`settings.organization.kind.${me.organization.kind}`)}</dd>
        </div>
      </dl>
    </section>
  );
}

/* ------------------------------------------------------------------------ */

export function AppearanceSettings() {
  const { t } = useI18n();
  return (
    <section
      className="screen__section"
      aria-label={t("settings.appearance.title")}
      data-testid="settings-appearance"
    >
      <h2>{t("settings.appearance.title")}</h2>
      <div className="panel panel__body form">
        <p className="label">{t("common.theme.label")}</p>
        <ThemeToggle />
        <p className="caption">{t("settings.appearance.theme_hint")}</p>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------------ */

export function SecuritySettings() {
  const { t } = useI18n();
  const { authApi } = useAuth();
  const { me, refresh } = useSession();

  // A factor enrolled here answers with a fresh token, exactly as the MFA
  // gate's does; storing it keeps this session's `mfa_verified` and the
  // token in step, then `/v1/me` is re-read so the factor list updates.
  const onVerified = useCallback(
    (result: { accessToken: string; mfaVerified: boolean }) => {
      storeSession({ accessToken: result.accessToken, mfaVerified: result.mfaVerified });
      void refresh();
    },
    [refresh],
  );

  return (
    <div className="screen" data-testid="settings-security">
      <PasswordSection />

      <section className="screen__section" aria-label={t("settings.security.totp_title")}>
        <h2>{t("settings.security.totp_title")}</h2>
        <div className="panel panel__body form">
          {me.mfa.has_totp ? (
            <>
              <p className="meta-line">
                <span className="chip chip--positive" data-testid="totp-enrolled">
                  {t("settings.security.totp_enrolled")}
                </span>
              </p>
              <p className="caption">{t("settings.security.totp_remove_unavailable")}</p>
            </>
          ) : (
            <TotpSection api={authApi} alreadyEnrolled={false} onVerified={onVerified} />
          )}
        </div>
      </section>

      <PasskeysSection onVerified={onVerified} />
      <SessionsSection />
      <TrustedDevicesSection />
    </div>
  );
}

function PasswordSection() {
  const { t } = useI18n();
  const { account } = useServices();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [state, setState] = useState<"idle" | "saving" | "saved">("idle");
  const [problem, setProblem] = useState<string | null>(null);
  const mismatch = confirm !== "" && next !== confirm;

  const submit = async () => {
    if (mismatch) return;
    setState("saving");
    setProblem(null);
    try {
      await account.changePassword(current, next);
      setState("saved");
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (error) {
      setProblem(describeError(error));
      setState("idle");
    }
  };

  return (
    <section className="screen__section" aria-label={t("settings.security.password_title")}>
      <h2>{t("settings.security.password_title")}</h2>
      <form
        className="panel panel__body form"
        aria-label={t("settings.security.password_title")}
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className="form__grid">
          <div className="form__field form__span">
            <label htmlFor="pw-current">{t("settings.security.password_current")}</label>
            <input
              id="pw-current"
              type="password"
              autoComplete="current-password"
              required
              value={current}
              data-testid="password-current"
              onChange={(e) => setCurrent(e.target.value)}
            />
          </div>
          <div className="form__field">
            <label htmlFor="pw-next">{t("settings.security.password_new")}</label>
            <input
              id="pw-next"
              type="password"
              autoComplete="new-password"
              required
              minLength={12}
              value={next}
              data-testid="password-new"
              onChange={(e) => setNext(e.target.value)}
            />
            <p className="form__hint">{t("settings.security.password_hint")}</p>
          </div>
          <div className="form__field">
            <label htmlFor="pw-confirm">{t("settings.security.password_confirm")}</label>
            <input
              id="pw-confirm"
              type="password"
              autoComplete="new-password"
              required
              value={confirm}
              aria-invalid={mismatch ? true : undefined}
              aria-describedby="pw-confirm-hint"
              data-testid="password-confirm"
              onChange={(e) => setConfirm(e.target.value)}
            />
            {mismatch ? (
              <p id="pw-confirm-hint" className="field-error">
                {t("settings.security.password_mismatch")}
              </p>
            ) : null}
          </div>
        </div>
        {problem !== null ? (
          <p role="alert" className="alert alert--attention" data-testid="password-problem">
            {problem}
          </p>
        ) : null}
        {state === "saved" ? (
          <p role="status" className="alert alert--positive" data-testid="password-saved">
            {t("settings.security.password_changed")}
          </p>
        ) : null}
        <div className="form__actions">
          <button
            type="submit"
            disabled={state === "saving" || mismatch}
            data-testid="password-submit"
          >
            {t("settings.security.password_submit")}
          </button>
        </div>
      </form>
    </section>
  );
}

function PasskeysSection({
  onVerified,
}: {
  onVerified: (result: { accessToken: string; mfaVerified: boolean }) => void;
}) {
  const { t, date } = useI18n();
  const { authApi } = useAuth();
  const { account } = useServices();
  const [passkeys, setPasskeys] = useState<readonly PasskeyView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [removeProblem, setRemoveProblem] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setPasskeys(null);
    setProblem(null);
    account
      .listPasskeys()
      .then((result) => {
        if (!cancelled) setPasskeys(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [account, attempt]);

  const remove = async (passkey: PasskeyView) => {
    setRemoveProblem(null);
    try {
      await account.removePasskey(passkey.id);
      setAttempt((n) => n + 1);
    } catch (error) {
      // IAM-010f's continuity guard, in the server's own words: the last
      // factor stays.
      setRemoveProblem(describeError(error));
    }
  };

  return (
    <section
      className="screen__section"
      aria-label={t("settings.security.passkeys_title")}
      data-testid="passkeys"
    >
      <h2>{t("settings.security.passkeys_title")}</h2>
      <div className="panel panel__body form">
        {problem !== null ? (
          <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)} />
        ) : null}
        {passkeys === null && problem === null ? <LoadingSkeleton rows={2} /> : null}
        {passkeys !== null && passkeys.length === 0 ? (
          <p className="caption" data-testid="passkeys-empty">
            {t("settings.security.passkeys_empty")}
          </p>
        ) : null}
        {passkeys !== null && passkeys.length > 0 ? (
          <ul className="list" data-testid="passkeys-list">
            {passkeys.map((passkey) => (
              <li key={passkey.id} className="list__row" data-testid="passkey-row">
                <span className="list__row-text">
                  <span>{passkey.name}</span>
                  <span className="caption ledgr-num">
                    {t("settings.security.passkey_added", {
                      date: date(passkey.created_at.slice(0, 10)),
                    })}
                    {passkey.last_used_at !== null
                      ? ` · ${t("settings.security.passkey_used", { date: date(passkey.last_used_at.slice(0, 10)) })}`
                      : ""}
                  </span>
                </span>
                <button
                  type="button"
                  className="button--danger"
                  data-testid={`passkey-remove-${passkey.id}`}
                  onClick={() => void remove(passkey)}
                >
                  {t("settings.security.passkey_remove")}
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        {removeProblem !== null ? (
          <p role="alert" className="alert alert--attention" data-testid="passkey-remove-problem">
            {removeProblem}
          </p>
        ) : null}
        {adding ? (
          <PasskeySection
            api={authApi}
            alreadyEnrolled={false}
            onVerified={(result) => {
              onVerified(result);
              setAdding(false);
              setAttempt((n) => n + 1);
            }}
          />
        ) : (
          <div className="form__actions">
            <button type="button" data-testid="passkey-add" onClick={() => setAdding(true)}>
              <Icon name="plus" size={18} />
              {t("auth.mfa.passkey.enroll_button")}
            </button>
          </div>
        )}
      </div>
    </section>
  );
}

function SessionsSection() {
  const { t, date } = useI18n();
  const { account } = useServices();
  const [sessions, setSessions] = useState<readonly SessionView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [revokeProblem, setRevokeProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setSessions(null);
    setProblem(null);
    account
      .listSessions()
      .then((result) => {
        if (!cancelled) setSessions(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [account, attempt]);

  const revoke = async (session: SessionView) => {
    setRevokeProblem(null);
    try {
      await account.revokeSession(session.id);
      setAttempt((n) => n + 1);
    } catch (error) {
      setRevokeProblem(describeError(error));
    }
  };

  return (
    <section
      className="screen__section"
      aria-label={t("settings.security.sessions_title")}
      data-testid="sessions"
    >
      <h2>{t("settings.security.sessions_title")}</h2>
      <p className="caption">{t("settings.security.sessions_hint")}</p>
      <div className="panel">
        {problem !== null ? (
          <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)} />
        ) : null}
        {sessions === null && problem === null ? <LoadingSkeleton rows={2} /> : null}
        {sessions !== null ? (
          <ul className="list" data-testid="sessions-list">
            {sessions.map((session) => (
              <li key={session.id} className="list__row" data-testid="session-row">
                <span className="list__row-text">
                  <span className="meta-line">
                    <span className="ledgr-num">
                      {t("settings.security.session_started", {
                        date: date(session.created_at.slice(0, 10)),
                      })}
                    </span>
                    {session.is_current ? (
                      <span className="chip chip--accent" data-testid="session-current">
                        {t("settings.security.session_current")}
                      </span>
                    ) : null}
                  </span>
                  <span className="caption ledgr-num">
                    {t("settings.security.session_active", {
                      date: date(session.last_active_at.slice(0, 10)),
                    })}
                  </span>
                </span>
                {!session.is_current ? (
                  <button
                    type="button"
                    className="button--danger"
                    data-testid={`session-revoke-${session.id}`}
                    onClick={() => void revoke(session)}
                  >
                    {t("settings.security.session_revoke")}
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
        {revokeProblem !== null ? (
          <p role="alert" className="alert alert--attention">
            {revokeProblem}
          </p>
        ) : null}
      </div>
    </section>
  );
}

function TrustedDevicesSection() {
  const { t, date } = useI18n();
  const { account } = useServices();
  const [devices, setDevices] = useState<readonly TrustedDeviceView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [revokeProblem, setRevokeProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setDevices(null);
    setProblem(null);
    account
      .listTrustedDevices()
      .then((result) => {
        if (!cancelled) setDevices(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [account, attempt]);

  const revoke = async (deviceTrust: TrustedDeviceView) => {
    setRevokeProblem(null);
    try {
      await account.revokeTrustedDevice(deviceTrust.id);
      setAttempt((n) => n + 1);
    } catch (error) {
      setRevokeProblem(describeError(error));
    }
  };

  return (
    <section
      className="screen__section"
      aria-label={t("settings.security.trusted_devices_title")}
      data-testid="trusted-devices"
    >
      <h2>{t("settings.security.trusted_devices_title")}</h2>
      <p className="caption">{t("settings.security.trusted_devices_hint")}</p>
      <div className="panel">
        {problem !== null ? (
          <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)} />
        ) : null}
        {devices === null && problem === null ? <LoadingSkeleton rows={2} /> : null}
        {devices !== null && devices.length === 0 ? (
          <p className="caption" data-testid="trusted-devices-empty">
            {t("settings.security.trusted_devices_empty")}
          </p>
        ) : null}
        {devices !== null && devices.length > 0 ? (
          <ul className="list" data-testid="trusted-devices-list">
            {devices.map((deviceTrust) => (
              <li key={deviceTrust.id} className="list__row" data-testid="trusted-device-row">
                <span className="list__row-text">
                  <span>{deviceTrust.name ?? t("settings.security.trusted_devices_title")}</span>
                  <span className="caption ledgr-num">
                    {t("settings.security.trusted_device_added", {
                      date: date(deviceTrust.created_at.slice(0, 10)),
                    })}
                    {` · ${t("settings.security.trusted_device_used", { date: date(deviceTrust.last_used_at.slice(0, 10)) })}`}
                  </span>
                </span>
                <button
                  type="button"
                  className="button--danger"
                  data-testid={`trusted-device-revoke-${deviceTrust.id}`}
                  onClick={() => void revoke(deviceTrust)}
                >
                  {t("settings.security.trusted_device_revoke")}
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        {revokeProblem !== null ? (
          <p role="alert" className="alert alert--attention">
            {revokeProblem}
          </p>
        ) : null}
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------------ */

export function OrganizationSettings() {
  const { t } = useI18n();
  const { me, administrations, refresh } = useSession();

  return (
    <div className="screen" data-testid="settings-organization">
      <section className="screen__section" aria-label={t("settings.organization.title")}>
        <h2>{t("settings.organization.title")}</h2>
        <dl className="facts panel panel__body">
          <div>
            <dt className="label">{t("auth.sign_up.organization_name")}</dt>
            <dd data-testid="organization-name">{me.organization.name}</dd>
            <dd className="caption">{t(`settings.organization.kind.${me.organization.kind}`)}</dd>
          </div>
          <div>
            <dt className="label">{t("auth.sign_up.kvk_number")}</dt>
            <dd className="ledgr-num">{me.organization.kvk_number ?? "—"}</dd>
          </div>
        </dl>
        <p className="caption">{t("settings.organization.name_readonly")}</p>
      </section>

      <section className="screen__section" aria-label={t("settings.organization.administrations")}>
        <div className="section-head">
          <h2>{t("settings.organization.administrations")}</h2>
          <span className="chip">{administrations.length}</span>
        </div>
        {administrations.length === 0 ? (
          <EmptyState
            title={t("settings.organization.no_administrations")}
            body={t("settings.organization.no_administrations_body")}
          />
        ) : (
          <ul className="list" data-testid="administrations-list">
            {administrations.map((entry) => (
              <li key={entry.id} className="panel">
                <AdministrationEditor administration={entry} onSaved={() => void refresh()} />
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function AdministrationEditor({
  administration,
  onSaved,
}: {
  administration: AdministrationView;
  onSaved: () => void;
}) {
  const { t, date } = useI18n();
  const { onboarding } = useServices();
  const [legalName, setLegalName] = useState(administration.legal_name);
  const [tradeName, setTradeName] = useState(administration.trade_name ?? "");
  const [vatNumber, setVatNumber] = useState(administration.vat_number ?? "");
  const [iban, setIban] = useState(administration.iban ?? "");
  const [locale, setLocale] = useState(administration.formatting_locale);
  const [state, setState] = useState<"idle" | "saving" | "saved">("idle");
  const [problem, setProblem] = useState<string | null>(null);

  const dirty =
    legalName !== administration.legal_name ||
    tradeName !== (administration.trade_name ?? "") ||
    vatNumber !== (administration.vat_number ?? "") ||
    iban !== (administration.iban ?? "") ||
    locale !== administration.formatting_locale;

  const submit = async () => {
    setState("saving");
    setProblem(null);
    try {
      await onboarding.updateAdministration(administration.id, {
        ...(legalName !== administration.legal_name ? { legal_name: legalName.trim() } : {}),
        ...(tradeName !== (administration.trade_name ?? "")
          ? { trade_name: tradeName.trim() === "" ? null : tradeName.trim() }
          : {}),
        ...(vatNumber !== (administration.vat_number ?? "")
          ? { vat_number: vatNumber.trim() === "" ? null : vatNumber.trim().toUpperCase() }
          : {}),
        ...(iban !== (administration.iban ?? "")
          ? { iban: iban.trim() === "" ? null : iban.trim() }
          : {}),
        ...(locale !== administration.formatting_locale ? { formatting_locale: locale } : {}),
      });
      setState("saved");
      onSaved();
    } catch (error) {
      setProblem(describeError(error));
      setState("idle");
    }
  };

  const prefix = `adm-${administration.id}`;

  return (
    <form
      className="panel__body form"
      aria-label={administration.legal_name}
      data-testid={`administration-${administration.id}`}
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div className="card__head">
        <span
          className={`client-marker client-marker--${administration.colour}`}
          aria-hidden="true"
        >
          {administration.initials}
        </span>
        <div>
          <p className="card__title">{administration.trade_name ?? administration.legal_name}</p>
          <p className="card__meta ledgr-num">
            {administration.kvk_number === null
              ? t("client.portfolio.no_kvk")
              : t("client.switcher.kvk", { number: administration.kvk_number })}
            {" · "}
            {t(`onboarding.legal_form.${administration.legal_form.toLowerCase()}.title`)}
          </p>
        </div>
      </div>
      <div className="form__grid">
        <div className="form__field">
          <label htmlFor={`${prefix}-legal`}>{t("onboarding.company.legal_name")}</label>
          <input
            id={`${prefix}-legal`}
            type="text"
            required
            value={legalName}
            data-testid="administration-legal-name"
            onChange={(e) => setLegalName(e.target.value)}
          />
        </div>
        <div className="form__field">
          <label htmlFor={`${prefix}-trade`}>{t("onboarding.company.trade_name")}</label>
          <input
            id={`${prefix}-trade`}
            type="text"
            value={tradeName}
            data-testid="administration-trade-name"
            onChange={(e) => setTradeName(e.target.value)}
          />
        </div>
        <div className="form__field">
          <label htmlFor={`${prefix}-vat`}>{t("onboarding.company.vat_number")}</label>
          <input
            id={`${prefix}-vat`}
            type="text"
            value={vatNumber}
            data-testid="administration-vat-number"
            onChange={(e) => setVatNumber(e.target.value)}
          />
        </div>
        <div className="form__field">
          <label htmlFor={`${prefix}-iban`}>{t("settings.organization.iban")}</label>
          <input
            id={`${prefix}-iban`}
            type="text"
            value={iban}
            placeholder={t("settings.organization.iban_placeholder")}
            data-testid="administration-iban"
            onChange={(e) => setIban(e.target.value)}
          />
          <p className="caption">{t("settings.organization.iban_hint")}</p>
        </div>
        <div className="form__field">
          {/* Same list, same reason, one source: see FormattingLocaleField's
              own docstring for the locale this screen used to offer and the
              API refusal it earned. */}
          <FormattingLocaleField
            id={`${prefix}-locale`}
            value={locale}
            testId="administration-locale"
            onChange={setLocale}
          />
        </div>
      </div>
      <p className="caption ledgr-num">
        {administration.fiscal_years
          .map((year) =>
            t("onboarding.review.year_range", {
              start: date(year.start_date),
              end: date(year.end_date),
            }),
          )
          .join(" · ") || t("settings.organization.no_fiscal_years")}
      </p>
      {problem !== null ? (
        <p role="alert" className="alert alert--attention">
          {problem}
        </p>
      ) : null}
      {state === "saved" && !dirty ? (
        <p role="status" className="alert alert--positive">
          {t("common.action.saved")}
        </p>
      ) : null}
      <div className="form__actions">
        <button
          type="submit"
          disabled={!dirty || state === "saving"}
          data-testid="administration-save"
        >
          {state === "saving" ? t("common.action.saving") : t("common.action.save")}
        </button>
      </div>
    </form>
  );
}

/* ------------------------------------------------------------------------ */

/**
 * `/settings/invoice-design` — mounts `TemplateDesigner` (FR-TPL-001..010,
 * 1,277 lines that were never mounted). The bootstrap its docstring scoped
 * out lives here: list the administration's templates, open the default,
 * and — for an administration with none — create one with just a name,
 * which the server fills with an already-compliant scaffold.
 */
export function InvoiceDesignSettings() {
  const { t } = useI18n();
  const { administration } = useSession();
  const { templates } = useServices();
  const [template, setTemplate] = useState<InvoiceTemplateView | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (administration === null) return;
    let cancelled = false;
    setTemplate(null);
    setProblem(null);
    templates
      .listTemplates(administration.id)
      .then(async ({ templates: list }) => {
        const chosen = list.find((entry) => entry.is_default) ?? list[0];
        if (chosen !== undefined) return chosen;
        return templates.createTemplate(administration.id, {
          name: t("settings.invoice_design.default_name"),
          is_default: true,
        });
      })
      .then((result) => {
        if (!cancelled) setTemplate(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
    // `t` only names the scaffold on first creation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [templates, administration?.id, attempt]);

  if (administration === null) return null;

  return (
    <section
      className="screen__section"
      aria-label={t("settings.invoice_design.title")}
      data-testid="settings-invoice-design"
    >
      <h2>{t("settings.invoice_design.title")}</h2>
      <p className="caption">{t("settings.invoice_design.intro")}</p>
      {problem !== null ? (
        <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)} />
      ) : null}
      {template === null && problem === null ? <LoadingSkeleton rows={6} /> : null}
      {template !== null ? (
        <div className="panel panel__body">
          <TemplateDesigner
            key={template.id}
            administrationId={administration.id}
            template={template}
            api={templates}
            onChanged={setTemplate}
          />
        </div>
      ) : null}
    </section>
  );
}
