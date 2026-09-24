import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { useI18n } from "@ledgr/i18n";
import type { ExpenseView } from "@ledgr/shared-types";

import { ExpenseForm } from "../capture/ExpenseForm";
import { useServices } from "../session/ServicesProvider";
import { useAdministration } from "../session/SessionProvider";
import { ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { canBook } from "./booking";
import { OriginalDocument } from "./OriginalDocument";

/**
 * One purchase invoice: the review, inside the invoice, and the booking.
 *
 * What used to be a separate "Review" screen is this: the fields, pre-filled from
 * the reading and flagged where it was not sure, beside the original they were
 * read from. Confirming here (the form's Submit) is the review.
 *
 * --- Submitting books it ---
 *
 * A purchase only counts - in the costs, the cash and the BTW return's input VAT -
 * once it is in the ledger. For someone who may post (Owner, Accountant,
 * Bookkeeper), Submit therefore marks the invoice ready AND books it, one click;
 * for anyone else it stops at ready and says who books it. A purchase that is
 * already ready (submitted earlier, or by someone else) shows the booking step on
 * its own. The server is what decides either way (`post journal_entry`).
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
  const [booking, setBooking] = useState(false);
  const [bookProblem, setBookProblem] = useState<string | null>(null);
  const mayBook = canBook(administration.role);

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

  const book = async (target: ExpenseView) => {
    setBooking(true);
    setBookProblem(null);
    try {
      await capture.postExpense(administration.id, target.id);
      navigate("/purchases");
    } catch (error) {
      // The server's own sentence: a missing mapping, a closed period and an
      // unverified e-mail address are each a different next step.
      setBookProblem(error instanceof Error ? error.message : "");
      setExpense({ ...target });
    } finally {
      setBooking(false);
    }
  };

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
      {expense.status === "ready" ? (
        <div className="purchase-detail__book" data-testid="purchase-book">
          {mayBook ? (
            <>
              <p>{t("capture.book.ready_note")}</p>
              <button
                type="button"
                className="button--primary"
                disabled={booking}
                data-testid="purchase-book-button"
                onClick={() => void book(expense)}
              >
                {booking ? t("capture.book.booking") : t("capture.book.button")}
              </button>
            </>
          ) : (
            <p>{t("capture.book.not_permitted")}</p>
          )}
          {bookProblem !== null ? (
            <p className="alert alert--attention" role="alert" data-testid="purchase-book-problem">
              {bookProblem}
            </p>
          ) : null}
        </div>
      ) : null}
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
            if (next.status !== "ready") return;
            // Submitted. Whoever may post books it in the same click; anyone else
            // stays here, where the booking step says who does.
            if (mayBook) void book(next);
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
