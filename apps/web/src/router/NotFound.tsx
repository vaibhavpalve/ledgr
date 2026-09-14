import { Link } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";

import { EmptyState } from "../shell/ScreenState";

/** D5: a wrong URL says so and offers the way out, never a blank page. */
export function NotFound() {
  const { t } = useI18n();
  return (
    <section className="screen" aria-label={t("common.not_found.title")} data-testid="not-found">
      <h1>{t("common.not_found.title")}</h1>
      <EmptyState
        title={t("common.not_found.title")}
        body={t("common.not_found.body")}
        action={
          <Link to="/" className="button-link">
            {t("common.not_found.home")}
          </Link>
        }
      />
    </section>
  );
}
