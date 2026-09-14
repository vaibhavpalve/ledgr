import { useState } from "react";
import { useI18n } from "@ledgr/i18n";

import { describeError } from "../api/http";
import { useServices } from "../session/ServicesProvider";
import { Icon } from "./icons";

/**
 * IAM-010b's persistent reminder — shown in the shell for as long as
 * `me.user.email_verified` is false. Onboarding still works unverified
 * (§4.4), posting to the ledger does not, so the banner says what is
 * blocked and offers the one thing that unblocks it: a fresh link.
 *
 * Never dismissible: it is the product's own requirement, not a tip
 * (FR-UX-010 is about tours). It disappears the moment `/v1/me` says
 * verified, which happens on the next session bootstrap after the link is
 * followed (`/verify-email` triggers that refresh itself).
 */
export function EmailVerificationBanner({ email }: { email: string }) {
  const { t } = useI18n();
  const { account } = useServices();
  const [state, setState] = useState<"idle" | "sending" | "sent" | "failed">("idle");
  const [problem, setProblem] = useState<string | null>(null);

  const resend = async () => {
    setState("sending");
    setProblem(null);
    try {
      await account.resendVerificationEmail();
      setState("sent");
    } catch (error) {
      setProblem(describeError(error));
      setState("failed");
    }
  };

  return (
    <div className="alert alert--caution verification-banner" data-testid="verification-banner">
      <span className="verification-banner__icon" aria-hidden="true">
        <Icon name="mail" size={18} />
      </span>
      <div className="verification-banner__text">
        <p>{t("common.verification.banner", { email })}</p>
        {state === "sent" ? (
          <p role="status" data-testid="verification-resent">
            {t("common.verification.resent")}
          </p>
        ) : null}
        {problem !== null ? <p role="alert">{problem}</p> : null}
      </div>
      <button
        type="button"
        disabled={state === "sending"}
        data-testid="verification-resend"
        onClick={() => void resend()}
      >
        {t("common.verification.resend")}
      </button>
    </div>
  );
}
