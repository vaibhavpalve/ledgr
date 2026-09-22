import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type { ExpenseSummaryView } from "@ledgr/shared-types";

import "./Purchases.css";
import { CaptureScreen } from "../capture/CaptureScreen";
import { useQueueSnapshot } from "../capture/CaptureQueueStatus";
import { useSitting } from "../capture/useSitting";
import { useServices } from "../session/ServicesProvider";
import { useAdministration } from "../session/SessionProvider";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { PurchaseDetail } from "./PurchaseDetail";
import { PurchasesTable } from "./PurchasesTable";

/**
 * `/purchases` - the one purchases screen - and `/purchases/:expenseId`, one
 * invoice opened for review.
 *
 * This replaces four things that were separate screens: capturing, reviewing
 * drafts, the list of what had been submitted, and the sub-menu that led to
 * them. Uploading is at the top of the list because adding an invoice is what
 * you do to the list, and reviewing is inside each invoice because that is where
 * the fields are.
 */
export function PurchasesRoute() {
  const { expenseId } = useParams();
  return expenseId === undefined ? <PurchasesList /> : <PurchaseDetail expenseId={expenseId} />;
}

type Loaded =
  | { readonly kind: "loading" }
  | { readonly kind: "error"; readonly message: string }
  | { readonly kind: "ready"; readonly expenses: readonly ExpenseSummaryView[] };

function PurchasesList() {
  const { t } = useI18n();
  const { administration, sittingContext } = useAdministration();
  const { capture, queue, decode } = useServices();
  const sitting = useSitting({ context: sittingContext, queue, api: capture });
  const [loaded, setLoaded] = useState<Loaded>({ kind: "loading" });

  const load = useCallback(() => {
    capture
      .listExpenses(administration.id)
      .then((expenses) => setLoaded({ kind: "ready", expenses }))
      .catch((error: unknown) =>
        setLoaded({ kind: "error", message: error instanceof Error ? error.message : "" }),
      );
  }, [administration.id, capture]);

  useEffect(() => {
    setLoaded({ kind: "loading" });
    load();
  }, [load]);

  // A delivered upload is a new row on this list, and the list has no other way
  // to hear of it: the invoice was created by the server while the upload was in
  // flight. Refreshed in place - not back to a skeleton - so the table does not
  // flash away under somebody who is reading it.
  const delivered = useQueueSnapshot(queue)?.delivered ?? 0;
  useEffect(() => {
    if (delivered > 0) load();
  }, [delivered, load]);

  const count = loaded.kind === "ready" ? loaded.expenses.length : null;

  return (
    <section
      className="screen purchases"
      aria-label={t("common.nav.purchases")}
      data-testid="purchases"
    >
      <PageHeader
        title={t("common.nav.purchases")}
        {...(count !== null && count > 0
          ? { context: t("capture.purchases.count", { count }) }
          : {})}
      />

      <CaptureScreen sitting={sitting} queue={queue} decode={decode} variant="embedded" />

      {loaded.kind === "loading" ? <LoadingSkeleton rows={4} /> : null}
      {loaded.kind === "error" ? <ErrorState message={loaded.message} onRetry={load} /> : null}
      {loaded.kind === "ready" && loaded.expenses.length === 0 ? (
        <EmptyState
          title={t("capture.purchases.empty_title")}
          body={t("capture.purchases.empty_body")}
          testId="purchases-empty"
        />
      ) : null}
      {loaded.kind === "ready" && loaded.expenses.length > 0 ? (
        <PurchasesTable expenses={loaded.expenses} />
      ) : null}
    </section>
  );
}
