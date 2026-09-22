import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { useI18n } from "@ledgr/i18n";
import type { ExpenseView } from "@ledgr/shared-types";

import { ExpenseForm } from "../capture/ExpenseForm";
import { useServices } from "../session/ServicesProvider";
import { useAdministration } from "../session/SessionProvider";
import { ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { OriginalDocument } from "./OriginalDocument";

/**
 * One purchase invoice: the review, inside the invoice.
 *
 * What used to be a separate "Review" screen is this: the fields, pre-filled from
 * the reading and flagged where it was not sure, beside the original they were
 * read from. Confirming here (the form's Submit) is the review, and returns to
 * the list.
 *
 * The invoice is fetched in full because the list only carries the summary -
 * enough for a row, not for the form (duplicate warnings, the suggested category
 * and the reading's confidences are per-invoice reads).
 */
export function PurchaseDetail({ expenseId }: { expenseId: string }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { administration } = useAdministration();
  const { capture, invoices } = useServices();
  const [expense, setExpense] = useState<ExpenseView | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setExpense(null);
    setProblem(null);
    capture
      .getExpense(administration.id, expenseId)
      .then((view) => {
        if (!cancelled) setExpense(view);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(error instanceof Error ? error.message : "");
      });
    return () => {
      cancelled = true;
    };
  }, [administration.id, capture, expenseId, attempt]);

  const back = (
    <Link to="/purchases" className="purchase-detail__back" data-testid="purchase-back">
      <ArrowLeft size={16} strokeWidth={1.8} aria-hidden="true" />
      {t("capture.purchases.back")}
    </Link>
  );

  if (problem !== null) {
    return (
      <section className="screen" data-testid="purchase-detail">
        {back}
        <ErrorState message={problem} onRetry={() => setAttempt((count) => count + 1)} />
      </section>
    );
  }
  if (expense === null) {
    return (
      <section className="screen" data-testid="purchase-detail">
        {back}
        <LoadingSkeleton rows={3} />
      </section>
    );
  }

  return (
    <section className="screen purchase-detail" data-testid="purchase-detail">
      {back}
      <PageHeader
        title={expense.supplier ?? t("capture.purchases.detail_fallback")}
        {...(expense.invoice_number ? { context: expense.invoice_number } : {})}
      />
      <div
        className={
          expense.document_id
            ? "purchase-detail__body purchase-detail__body--with-original"
            : "purchase-detail__body"
        }
      >
        <ExpenseForm
          administrationId={administration.id}
          expense={expense}
          api={capture}
          onChanged={(next) => {
            setExpense(next);
            // Submitted: the review is done, and it is on the list as ready.
            if (next.status !== "draft") navigate("/purchases");
          }}
        />
        {expense.document_id ? (
          <OriginalDocument
            administrationId={administration.id}
            documentId={expense.document_id}
            api={invoices}
          />
        ) : null}
      </div>
    </section>
  );
}
