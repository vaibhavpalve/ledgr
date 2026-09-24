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
import { canBook } from "./booking";
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

  // Purchases that were submitted and never booked - before booking was part of
  // submitting, every one of them. One action books them all, for whoever may post.
  const readyIds =
    loaded.kind === "ready"
      ? loaded.expenses.filter((expense) => expense.status === "ready").map((e) => e.id)
      : [];
  const mayBook = canBook(administration.role);
  const [bookingAll, setBookingAll] = useState(false);
  const [bookResult, setBookResult] = useState<{ booked: number; failed: number } | null>(null);
  const bookAll = async () => {
    setBookingAll(true);
    let booked = 0;
    let failed = 0;
    // One at a time: each is its own ledger entry with its own idempotency key,
    // and a refusal on one (a closed period) must not stop the others.
    for (const id of readyIds) {
      try {
        await capture.postExpense(administration.id, id);
        booked += 1;
      } catch {
        failed += 1;
      }
    }
    setBookResult({ booked, failed });
    setBookingAll(false);
    load();
  };

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
        {...(mayBook && readyIds.length > 0
          ? {
              action: (
                <button
                  type="button"
                  className="button--primary"
                  disabled={bookingAll}
                  data-testid="purchases-book-all"
                  onClick={() => void bookAll()}
                >
                  {bookingAll
                    ? t("capture.book.booking")
                    : t("capture.book.all", { count: readyIds.length })}
                </button>
              ),
            }
          : {})}
      />
      {bookResult !== null ? (
        <p
          className={`alert ${bookResult.failed > 0 ? "alert--attention" : "alert--positive"}`}
          role="status"
          data-testid="purchases-book-result"
        >
          {bookResult.booked > 0 ? t("capture.book.all_done", { count: bookResult.booked }) : null}{" "}
          {bookResult.failed > 0
            ? t("capture.book.all_failed", { count: bookResult.failed })
            : null}
        </p>
      ) : null}

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
