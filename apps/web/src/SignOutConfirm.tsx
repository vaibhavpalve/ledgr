import { useRef } from "react";
import { useI18n } from "@ledgr/i18n";

import { useModalFocus } from "./useModalFocus";

/**
 * MOB-009's warning: "purged on logout... [taking unposted captures] costs
 * one sentence." `App.tsx`'s `Shell` renders this in place of signing out
 * immediately whenever `capture/queue.capturesAtRisk()` finds queued,
 * undelivered captures — an ordinary sign-out with nothing at risk skips
 * this dialog and purges (a no-op) without asking anything.
 *
 * Mirrors `capture/CaptureScreen.tsx`'s `QualityPrompt` exactly: an
 * `alertdialog` (an interruption that requires a decision before
 * continuing), `useModalFocus(true, ...)` because the parent unmounts this
 * entirely rather than hiding it, and the safe option (cancel) listed
 * before the destructive one.
 */
export function SignOutConfirm({
  count,
  onCancel,
  onConfirm,
}: {
  count: number;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { t } = useI18n();
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalFocus(true, dialogRef);

  return (
    <div
      ref={dialogRef}
      role="alertdialog"
      aria-modal="true"
      aria-label={t("auth.sign_out.confirm_heading")}
      data-testid="sign-out-confirm"
    >
      <p>{t("auth.sign_out.confirm_heading")}</p>
      <p>{t("auth.sign_out.confirm_body", { count })}</p>
      <button type="button" data-testid="sign-out-confirm-cancel" onClick={onCancel}>
        {t("auth.sign_out.confirm_cancel")}
      </button>
      <button type="button" data-testid="sign-out-confirm-anyway" onClick={onConfirm}>
        {t("auth.sign_out.confirm_anyway")}
      </button>
    </div>
  );
}
