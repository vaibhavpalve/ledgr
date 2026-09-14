import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type { CustomerView, VatNumberStatus } from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { Icon } from "../shell/icons";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";

/**
 * `/customers` — FR-AR-006's master. The search is the SERVER's (exact →
 * prefix → substring), asked on every keystroke; archived customers are
 * behind a checkbox because they are still real records (every invoice
 * ever raised points at them) but should not crowd a picker.
 */
export function CustomerListScreen() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { administration } = useAdministration();
  const { customers } = useServices();
  const [query, setQuery] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [rows, setRows] = useState<readonly CustomerView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setProblem(null);
    customers
      .listCustomers(administration.id, { q: query.trim() === "" ? undefined : query.trim(), includeArchived })
      .then((result) => {
        if (!cancelled) setRows(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [customers, administration.id, query, includeArchived, attempt]);

  const searching = query.trim() !== "";

  return (
    <section className="screen" aria-label={t("customers.list.title")} data-testid="customer-list">
      <PageHeader
        title={t("customers.list.title")}
        context={rows === null ? undefined : t("customers.list.count", { count: rows.length })}
        action={
          <Link to="/customers/new" className="button-link button-link--primary" data-testid="customer-list-new">
            <Icon name="plus" size={18} />
            {t("customers.list.new")}
          </Link>
        }
      />

      <div className="form__grid">
        <div className="form__field">
          <label htmlFor="customer-search">{t("customers.list.search")}</label>
          <input
            id="customer-search"
            type="search"
            value={query}
            placeholder={t("customers.list.search_placeholder")}
            data-testid="customer-search"
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <label className="form__field customers__archived-toggle">
          <span className="option-row">
            <input
              type="checkbox"
              checked={includeArchived}
              data-testid="customer-include-archived"
              onChange={(event) => setIncludeArchived(event.target.checked)}
            />
            {t("customers.list.include_archived")}
          </span>
        </label>
      </div>

      {problem !== null ? (
        <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)} />
      ) : rows === null ? (
        <LoadingSkeleton rows={4} />
      ) : rows.length === 0 ? (
        searching ? (
          <EmptyState
            title={t("customers.list.no_matches_title", { query })}
            body={t("customers.list.no_matches_body")}
            action={
              <button type="button" onClick={() => setQuery("")}>
                {t("customers.list.clear_search")}
              </button>
            }
            testId="customer-no-matches"
          />
        ) : (
          <EmptyState
            icon={<Icon name="customers" size={32} />}
            title={t("customers.list.empty_title")}
            body={t("customers.list.empty_body")}
            action={
              <Link to="/customers/new" className="button-link button-link--primary">
                {t("customers.list.new")}
              </Link>
            }
          />
        )
      ) : (
        <div className="panel table-wrap">
          <table className="table" data-testid="customer-table">
            <thead>
              <tr>
                <th scope="col">{t("customers.field.name")}</th>
                <th scope="col">{t("customers.field.city")}</th>
                <th scope="col">{t("customers.field.vat_number")}</th>
                <th scope="col">{t("customers.field.delivery_channel")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((customer) => (
                <tr key={customer.id} data-testid="customer-row">
                  <td>
                    <button
                      type="button"
                      className="table__row-button"
                      data-testid={`customer-open-${customer.id}`}
                      onClick={() => navigate(`/customers/${encodeURIComponent(customer.id)}`)}
                    >
                      {customer.trade_name ?? customer.name}
                    </button>
                    {customer.archived_at !== null ? (
                      <span className="chip">{t("customers.status.archived")}</span>
                    ) : null}
                  </td>
                  <td className="table__muted">{customer.city ?? "—"}</td>
                  <td>
                    {customer.vat_number === null ? (
                      <span className="table__muted">—</span>
                    ) : (
                      <span className="meta-line">
                        <span className="ledgr-num">{customer.vat_number}</span>
                        <VatStatusChip status={customer.vat_number_status} />
                      </span>
                    )}
                  </td>
                  <td>{t(`customers.delivery_channel.${customer.delivery_channel}`)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/** FR-ONB-003: the VIES verdict, always shown WITH the number. */
export function VatStatusChip({ status }: { status: VatNumberStatus }) {
  const { t } = useI18n();
  const tone =
    status === "valid" ? "chip chip--positive" : status === "invalid" ? "chip chip--attention" : "chip";
  return (
    <span className={tone} data-testid="vat-status">
      {t(`customers.vat_status.${status}`)}
    </span>
  );
}
