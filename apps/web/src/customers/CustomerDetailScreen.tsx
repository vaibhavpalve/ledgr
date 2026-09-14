import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type { CustomerView } from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { VatStatusChip } from "./CustomerListScreen";

/**
 * `/customers/:id` — the record, the VIES verdict with the number
 * (FR-ONB-003), the on-demand re-check, and archive/restore. There is no
 * delete: a customer is pointed at by statutory documents kept seven years
 * (CMP-001), and archiving is what takes them out of pickers.
 *
 * The primary action is "new invoice for this customer" — the reason a
 * customer record exists — and it is a link to the invoice form.
 */
export function CustomerDetailScreen() {
  const { t, money, date } = useI18n();
  const { customerId = "" } = useParams();
  const { administration } = useAdministration();
  const { customers } = useServices();
  const [customer, setCustomer] = useState<CustomerView | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState<"archive" | "vies" | null>(null);
  const [actionProblem, setActionProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setCustomer(null);
    setProblem(null);
    customers
      .getCustomer(administration.id, customerId)
      .then((result) => {
        if (!cancelled) setCustomer(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [customers, administration.id, customerId, attempt]);

  // `action` returns `unknown` rather than `Promise<CustomerView>` on purpose:
  // spelling out `Promise<...>` in a .tsx file trips
  // scripts/check_translations.py's JSX-text heuristic, which reads the `>` of
  // `=>` followed by a capitalised word and a `<` as untranslated screen text.
  // `await` accepts any type, so the cast below is the whole cost.
  const run = async (kind: "archive" | "vies", action: () => unknown) => {
    setBusy(kind);
    setActionProblem(null);
    try {
      setCustomer((await action()) as CustomerView);
    } catch (error) {
      setActionProblem(describeError(error));
    } finally {
      setBusy(null);
    }
  };

  if (problem !== null) {
    return (
      <section className="screen" aria-label={t("customers.detail.title")}>
        <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)}>
          <Link to="/customers" className="button-link">
            {t("customers.detail.back")}
          </Link>
        </ErrorState>
      </section>
    );
  }
  if (customer === null) {
    return (
      <section className="screen" aria-label={t("customers.detail.title")}>
        <LoadingSkeleton rows={5} />
      </section>
    );
  }

  const archived = customer.archived_at !== null;
  const erased = customer.erased_at !== null;

  return (
    <section className="screen" aria-label={t("customers.detail.title")} data-testid="customer-detail">
      <PageHeader
        title={customer.trade_name ?? customer.name}
        context={customer.trade_name !== null ? customer.name : undefined}
        action={
          !archived && !erased ? (
            <Link to="/invoices/new" className="button-link button-link--primary" data-testid="customer-invoice">
              {t("customers.detail.new_invoice")}
            </Link>
          ) : undefined
        }
      >
        {archived ? <span className="chip">{t("customers.status.archived")}</span> : null}
        {erased ? <span className="chip chip--attention">{t("customers.status.erased")}</span> : null}
      </PageHeader>

      <p className="meta-line">
        <Link to="/customers">{t("customers.detail.back")}</Link>
        {!erased ? (
          <Link to={`/customers/${encodeURIComponent(customer.id)}/edit`} data-testid="customer-edit">
            {t("common.action.edit")}
          </Link>
        ) : null}
      </p>

      {actionProblem !== null ? (
        <p role="alert" className="alert alert--attention" data-testid="customer-action-problem">
          {actionProblem}
        </p>
      ) : null}

      <dl className="facts panel panel__body">
        <div>
          <dt className="label">{t("customers.field.address_line1")}</dt>
          <dd>{customer.address_line1 ?? "—"}</dd>
          {customer.address_line2 !== null ? <dd>{customer.address_line2}</dd> : null}
          <dd>{[customer.postal_code, customer.city].filter((part) => part !== null).join(" ") || "—"}</dd>
          <dd className="caption">{customer.country}</dd>
          {!customer.address_is_complete ? (
            <dd className="field-error" data-testid="customer-address-incomplete">
              {t("customers.detail.address_incomplete")}
            </dd>
          ) : null}
        </div>
        <div>
          <dt className="label">{t("customers.field.vat_number")}</dt>
          <dd className="meta-line">
            <span className="ledgr-num">{customer.vat_number ?? "—"}</span>
            {customer.vat_number !== null ? <VatStatusChip status={customer.vat_number_status} /> : null}
          </dd>
          {customer.vat_number_checked_at !== null ? (
            <dd className="caption ledgr-num">
              {t("customers.detail.vat_checked_at", { date: date(customer.vat_number_checked_at.slice(0, 10)) })}
            </dd>
          ) : null}
          {customer.vat_number_status === "unavailable" ? (
            <dd className="caption">{t("customers.detail.vat_unavailable_hint")}</dd>
          ) : null}
          {customer.vat_number !== null && !erased ? (
            <dd>
              <button
                type="button"
                disabled={busy !== null}
                data-testid="customer-check-vat"
                onClick={() => void run("vies", () => customers.validateVatNumber(administration.id, customer.id))}
              >
                {busy === "vies" ? t("customers.detail.vat_checking") : t("customers.detail.vat_check")}
              </button>
            </dd>
          ) : null}
        </div>
        <div>
          <dt className="label">{t("customers.field.kvk_number")}</dt>
          <dd className="ledgr-num">{customer.kvk_number ?? "—"}</dd>
        </div>
        <div>
          <dt className="label">{t("customers.field.invoice_email")}</dt>
          <dd>{customer.invoice_email ?? "—"}</dd>
          <dd className="caption">{t(`customers.delivery_channel.${customer.delivery_channel}`)}</dd>
        </div>
        <div>
          <dt className="label">{t("customers.field.payment_terms_days")}</dt>
          <dd className="ledgr-num">{t("customers.detail.days", { count: customer.payment_terms_days })}</dd>
        </div>
        <div>
          <dt className="label">{t("customers.field.credit_limit")}</dt>
          <dd className="ledgr-num">{customer.credit_limit === null ? t("customers.detail.no_credit_limit") : money(customer.credit_limit)}</dd>
        </div>
        <div>
          <dt className="label">{t("customers.field.language")}</dt>
          <dd>{t(`common.language.name.${customer.language}`)}</dd>
        </div>
        {customer.notes !== null ? (
          <div>
            <dt className="label">{t("customers.field.notes")}</dt>
            <dd>{customer.notes}</dd>
          </div>
        ) : null}
      </dl>

      {!erased ? (
        <div className="form__actions">
          {archived ? (
            <button
              type="button"
              disabled={busy !== null}
              data-testid="customer-restore"
              onClick={() => void run("archive", () => customers.restoreCustomer(administration.id, customer.id))}
            >
              {t("customers.detail.restore")}
            </button>
          ) : (
            <button
              type="button"
              className="button--danger"
              disabled={busy !== null}
              data-testid="customer-archive"
              onClick={() => void run("archive", () => customers.archiveCustomer(administration.id, customer.id))}
            >
              {t("customers.detail.archive")}
            </button>
          )}
          <p className="caption">{archived ? t("customers.detail.restore_hint") : t("customers.detail.archive_hint")}</p>
        </div>
      ) : null}
    </section>
  );
}
