import { useNavigate } from "react-router-dom";
import type { DashboardActionItemView } from "@ledgr/shared-types";

import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { HomeScreen } from "./HomeScreen";

/**
 * `/`: FR-UX-005's prioritised home, on the session's own tenant context.
 *
 * An item opens the record it is about, which the mobile shell could not
 * do (ADR-048's known gap: its tabs had no "open this one" entry point).
 * With URLs, they do: an invoice is `/invoices/:id`, a draft expense is
 * `/purchases/:id`, the purchase opening straight onto that receipt's form.
 */
export function destinationFor(item: DashboardActionItemView): string {
  return item.kind === "draft_expense"
    ? `/purchases/${encodeURIComponent(item.id)}`
    : `/invoices/${encodeURIComponent(item.id)}`;
}

export function DashboardRoute() {
  const navigate = useNavigate();
  const { administration, fiscalYear } = useAdministration();
  const { dashboard, invoices } = useServices();

  return (
    <HomeScreen
      administrationId={administration.id}
      fiscalYearId={fiscalYear.id}
      api={dashboard}
      invoicesApi={invoices}
      companyName={administration.trade_name ?? administration.legal_name}
      onOpenItem={(item) => navigate(destinationFor(item))}
      onNavigate={(path) => navigate(path)}
    />
  );
}
