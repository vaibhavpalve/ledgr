import { useI18n } from "@ledgr/i18n";

/**
 * A placeholder, and marked as one so it is deleted rather than built around.
 *
 * IAM-010's sign-in methods — password, Google, passkey — exist as a service
 * layer in `apps/api/src/api/auth/` and are not exposed over HTTP: there is no
 * `POST /v1/auth/...` in `api.main`. So there is nothing for a form on this
 * screen to submit to.
 *
 * Saying that is better than drawing an e-mail field, a password field and a
 * button that do nothing. A dead form is indistinguishable from a broken one,
 * and somebody would eventually try to sign in with it.
 *
 * **When IAM-010 ships, delete this file, its catalogue record
 * (`auth.methods_pending`), and pass the real form to `PreAuthScreen` as
 * children.** Nothing about the language behaviour on this screen changes when
 * that happens — which is the point of the form being a slot.
 */
export function SignInPending() {
  const { t } = useI18n();

  return (
    <p className="pre-auth__pending" data-testid="sign-in-pending">
      {t("auth.methods_pending")}
    </p>
  );
}
