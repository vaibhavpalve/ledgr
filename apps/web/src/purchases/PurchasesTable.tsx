import { Link } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type { ExpenseStatus, ExpenseSummaryView } from "@ledgr/shared-types";

/**
 * The purchases list: one row per invoice, answering what it is, where it is
 * booked, where it stands and what it costs.
 *
 * A real `<table>`, because the four columns are the point and a screen reader
 * should hear them as columns. The whole row opens the invoice through the link
 * in its first cell (stretched over the row by the stylesheet), so it is one
 * tab stop per invoice and a click anywhere on the row works.
 *
 * --- An invoice nobody has filled in yet still has a row ---
 *
 * With reading off, or when it failed, a captured invoice has no supplier. It is
 * shown as "Invoice to fill in" with the reason beneath, rather than as a blank
 * row that looks like a bug - it is the one a person most needs to open.
 */
export function PurchasesTable({ expenses }: { expenses: readonly ExpenseSummaryView[] }) {
  const { t, money } = useI18n();

  return (
    <div className="purchases__card">
      <table className="purchases__table" aria-label={t("capture.purchases.list_label")}>
        <thead>
          <tr>
            <th scope="col">{t("capture.purchases.col.supplier")}</th>
            <th scope="col" className="purchases__col-ledger">
              {t("capture.purchases.col.ledger")}
            </th>
            <th scope="col">{t("capture.purchases.col.status")}</th>
            <th scope="col" className="purchases__num">
              {t("capture.purchases.col.amount")}
            </th>
          </tr>
        </thead>
        <tbody>
          {expenses.map((expense) => (
            <tr key={expense.id} data-testid="purchase-row" data-status={expense.status}>
              <td>
                <Link
                  to={`/purchases/${encodeURIComponent(expense.id)}`}
                  className="purchases__supplier"
                  data-testid={`purchase-open-${expense.id}`}
                >
                  {expense.supplier ?? t("capture.purchases.unread_title")}
                </Link>
                <span className="purchases__sub">{subline(expense, t)}</span>
                {/* On a phone the Ledger column is folded in here instead, so
                    Status and Amount stay on screen. */}
                <span className="purchases__sub purchases__ledger-inline" aria-hidden="true">
                  {ledger(expense, t)}
                </span>
              </td>
              <td className="purchases__ledger purchases__col-ledger">{ledger(expense, t)}</td>
              <td>
                <span className={`purchases__status purchases__status--${expense.status}`}>
                  {t(statusKey(expense.status))}
                </span>
              </td>
              <td className="purchases__num purchases__amount">
                {expense.gross_amount !== null ? money(expense.gross_amount) : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

type Translate = (key: string) => string;

/** The invoice number, or - for one with no supplier - why it is empty. */
function subline(expense: ExpenseSummaryView, t: Translate): string {
  if (expense.invoice_number) return expense.invoice_number;
  if (expense.supplier !== null) return "";
  return expense.extraction_status === "failed"
    ? t("capture.purchases.unread_failed")
    : t("capture.purchases.unread_waiting");
}

/**
 * `4600 · Energie`: the category's reference account and its name. A category
 * typed into the form that is not on the shared list has no account, and is
 * shown as typed.
 */
function ledger(expense: ExpenseSummaryView, t: Translate): string {
  if (expense.category_key) {
    const name = t(`capture.category.${expense.category_key}`);
    return expense.rgs_code ? `${expense.rgs_code} · ${name}` : name;
  }
  return expense.category ?? "—";
}

function statusKey(status: ExpenseStatus): string {
  return `capture.purchases.status.${status}`;
}
