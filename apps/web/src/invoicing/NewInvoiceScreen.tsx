import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type { CustomerView } from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { ErrorState, LoadingSkeleton } from "../shell/ScreenState";
import { SendInvoiceForm } from "./SendInvoiceForm";

/**
 * `/invoices/new` — the existing `SendInvoiceForm` (MOB-005's four-call
 * sequence, unchanged) with the customer master offered as a picker. The
 * create-inline path is a navigation to `/customers/new?return=/invoices/new`:
 * the customer form is one screen, not two, and it comes back here once
 * the customer exists.
 *
 * A customer list that cannot be loaded does not block invoicing: the typed
 * path still works, and the form is rendered with the error beside it
 * rather than instead of it.
 */
export function NewInvoiceScreen() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { administration, fiscalYear } = useAdministration();
  const { invoices, customers } = useServices();
  const [list, setList] = useState<readonly CustomerView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    customers
      .listCustomers(administration.id, { limit: 200 })
      .then((result) => {
        if (!cancelled) setList(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setProblem(describeError(error));
          setList([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [customers, administration.id]);

  if (list === null) {
    return (
      <section className="screen" aria-label={t("mobile.invoice.title")}>
        <LoadingSkeleton rows={4} />
      </section>
    );
  }

  return (
    <div className="screen" data-testid="new-invoice">
      {problem !== null ? (
        <ErrorState
          message={t("invoice.new.customers_unavailable")}
          testId="new-invoice-customers-error"
        />
      ) : null}
      <SendInvoiceForm
        administrationId={administration.id}
        fiscalYearId={fiscalYear.id}
        api={invoices}
        customers={problem === null ? list : undefined}
        onCreateCustomer={() => navigate("/customers/new?return=/invoices/new")}
        onSent={() => navigate("/invoices")}
        onOpenInvoice={(invoiceId) => navigate(`/invoices/${encodeURIComponent(invoiceId)}`)}
      />
    </div>
  );
}
