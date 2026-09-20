import { useNavigate, useParams } from "react-router-dom";

import { ApproveList } from "../approve/ApproveList";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { ViewList } from "../view/ViewList";

/**
 * `/review` and `/review/:expenseId` — MOB-004's Approve list (ADR-046 §2),
 * unchanged in logic and fitted into the shell. The route parameter is the
 * one addition: opening a specific draft directly, which the dashboard's
 * "complete" action and a deep link both need. The list keeps the URL in
 * step as a person opens and closes items, so the back button works the
 * way it does everywhere else.
 */
export function ReviewRoute() {
  const navigate = useNavigate();
  const { expenseId } = useParams();
  const { administration } = useAdministration();
  const { capture } = useServices();

  return (
    <ApproveList
      administrationId={administration.id}
      api={capture}
      openId={expenseId ?? null}
      onOpenChange={(id) => navigate(id === null ? "/review" : `/review/${encodeURIComponent(id)}`)}
    />
  );
}

/** `/overview` — MOB-004's View lists, unchanged. */
export function OverviewRoute() {
  const { administration } = useAdministration();
  const { capture, invoices } = useServices();

  return (
    <ViewList administrationId={administration.id} captureApi={capture} invoiceApi={invoices} />
  );
}
