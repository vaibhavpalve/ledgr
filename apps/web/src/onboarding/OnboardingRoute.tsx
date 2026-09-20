import { useNavigate } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";

import "./onboarding.css";
import { useAuth } from "../auth/AuthProvider";
import { LanguageSwitcher } from "../LanguageSwitcher";
import { useSession } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { ThemeToggle } from "../theme/ThemeToggle";
import { Wordmark } from "../Wordmark";
import { OnboardingWizard } from "./OnboardingWizard";

/**
 * `/onboarding`: the wizard in its own frame — no rail, because there is
 * nothing to navigate to yet; the wordmark, the two presentation controls
 * and sign-out, because a person stuck here must still be able to leave.
 *
 * After `POST /v1/administrations` the session is re-read (the server set
 * the new administration active) and the dashboard is next. A firm adding
 * a client goes to the portfolio instead, where the new client is now
 * listed and open.
 */
export function OnboardingRoute() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { requestSignOut } = useAuth();
  const { onboarding } = useServices();
  const { me, refresh } = useSession();
  const isFirm = me.organization.kind === "firm";

  return (
    <div className="onboarding-frame" data-testid="onboarding-frame">
      <header className="onboarding-frame__bar">
        <Wordmark />
        <div className="app__bar-spacer" />
        <ThemeToggle />
        <LanguageSwitcher />
        <button
          type="button"
          className="button--quiet"
          data-testid="sign-out"
          onClick={requestSignOut}
        >
          {t("auth.sign_out")}
        </button>
      </header>
      <main id="main-content" className="onboarding-frame__main">
        <OnboardingWizard
          api={onboarding}
          storageKey={`ledgr.onboarding.${me.organization.id}`}
          firmClient={isFirm}
          onCreated={() => {
            void refresh().then(() => navigate(isFirm ? "/clients" : "/", { replace: true }));
          }}
          onCancel={
            isFirm && me.administrations.length > 0 ? () => navigate("/clients") : undefined
          }
        />
      </main>
    </div>
  );
}
