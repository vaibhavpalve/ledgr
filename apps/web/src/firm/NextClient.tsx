import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type { FirmNextClientView } from "@ledgr/shared-types";

import "./FirmHome.css";
import { describeError } from "../api/http";
import { useServices } from "../session/ServicesProvider";
import { useSession } from "../session/SessionProvider";
import { Button } from "../ui";
import { parseLastQuery, readLastQueryRaw } from "./lastQuery";

/** Screens that are not "inside a client", where there is nothing to move on from. */
const OUTSIDE_A_CLIENT = ["/todo", "/inbox", "/clients", "/settings", "/onboarding"];

/**
 * "Next client with work →" (contract-wave2 decision 7, FR-FRM-002): inside a client, a firm
 * user who came from the worklist moves straight on to the next client in that same query, in
 * the client header on every screen, with how many are left. The server picks the next one
 * (`GET /v1/firm/worklist/next`) against current data, skipping snoozed clients; the switch is
 * the one ClientsScreen.open makes.
 *
 * Hidden for a business user, outside a client, and when this tab has no worklist query (the
 * client was not reached from To do).
 */
export function NextClientControl() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const location = useLocation();
  const { me, administration, switchAdministration } = useSession();
  const { firm } = useServices();
  const isFirm = me.organization.kind === "firm";
  const administrationId = administration?.id ?? null;
  const outside = OUTSIDE_A_CLIENT.some(
    (path) => location.pathname === path || location.pathname.startsWith(`${path}/`),
  );
  // Read on every render (To do may have stored a new query), parsed only when the text changes.
  const raw = isFirm && administrationId !== null ? readLastQueryRaw() : null;
  const stored = useMemo(() => parseLastQuery(raw), [raw]);
  const visible = isFirm && administrationId !== null && !outside && stored !== null;

  const [next, setNext] = useState<FirmNextClientView | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  // A read only: "{remaining} left" for the client open now.
  useEffect(() => {
    if (!visible || administrationId === null || stored === null) return;
    let cancelled = false;
    setNext(null);
    setProblem(null);
    void Promise.resolve()
      .then(() => firm.nextClient(administrationId, stored))
      .then((answer) => {
        if (!cancelled) setNext(answer);
      })
      .catch(() => {
        // Secondary chrome: a failed count leaves the control usable without one.
      });
    return () => {
      cancelled = true;
    };
  }, [firm, visible, administrationId, stored]);

  if (!visible || administrationId === null || stored === null) return null;

  const go = async () => {
    setBusy(true);
    setProblem(null);
    try {
      // Asked again on the click, so the move is against current data, not the count's.
      const answer = await firm.nextClient(administrationId, stored);
      setNext(answer);
      if (answer.administration_id !== null) {
        if (answer.administration_id !== administrationId) {
          await switchAdministration(answer.administration_id);
        }
        navigate("/");
      }
    } catch (error) {
      setProblem(describeError(error));
    } finally {
      setBusy(false);
    }
  };

  if (next !== null && next.administration_id === null) {
    return (
      <span className="firm-next firm-next--done" role="status" data-testid="next-client-done">
        <span>{t("client.todo.next.done")}</span>
        <Link to="/todo" className="ui-textbutton" data-testid="next-client-back">
          {t("client.todo.next.back")}
        </Link>
      </span>
    );
  }

  return (
    <span className="firm-next" data-testid="next-client">
      <Button
        size="sm"
        className="firm-next__button"
        disabled={busy}
        title={next?.display_name ?? undefined}
        data-testid="next-client-button"
        onClick={() => void go()}
      >
        {/* CSS shows one of the two; whichever is shown is the button's name. */}
        <span className="firm-next__long">{t("client.todo.next.button")}</span>
        <span className="firm-next__short">{t("client.todo.next.short")}</span>
      </Button>
      {next !== null ? (
        <span className="firm-muted firm-next__left ui-num" data-testid="next-client-remaining">
          {t("client.todo.next.remaining", { count: next.remaining })}
        </span>
      ) : null}
      {problem !== null ? (
        <span className="ledgr-visually-hidden" role="alert">
          {problem}
        </span>
      ) : null}
    </span>
  );
}
