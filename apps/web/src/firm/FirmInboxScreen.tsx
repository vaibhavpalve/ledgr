import { useCallback, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type { FirmInboxItemView } from "@ledgr/shared-types";

type InboxTab = "firm" | "client";
const TABS: readonly InboxTab[] = ["firm", "client"];

import "./FirmHome.css";
import { describeError } from "../api/http";
import { useSession } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { Icon } from "../shell/icons";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { localDateOf, useResource } from "./useResource";

/**
 * `/inbox` - the client inbox (FR-FRM-005): the latest replies from clients to the firm's
 * questions, across every client the caller holds a grant on (`GET /v1/firm/inbox`). Opening a
 * reply SWITCHES to that client first (as ClientsScreen.open does): the thread routes live under
 * `/v1/administrations/{id}/questions/{thread_id}`, and posting or resolving with a different
 * client open is refused with 409 `active_client_mismatch`.
 *
 * Two tabs (ADR-111): "Replies to you" (`awaiting=firm`, the default and what the sidebar badge
 * counts) and "Waiting on client" (`awaiting=client`, the firm's own questions still out).
 *
 * There is no thread screen in this wave; this is the list the sidebar's unread badge points at.
 */
export function FirmInboxRoute() {
  const { me } = useSession();
  if (me.organization.kind !== "firm") return <Navigate to="/" replace />;
  return <FirmInboxScreen />;
}

function FirmInboxScreen() {
  const { t, date } = useI18n();
  const navigate = useNavigate();
  const { firm } = useServices();
  const { administration, switchAdministration } = useSession();
  const [tab, setTab] = useState<InboxTab>("firm");
  const load = useCallback(() => firm.getInbox({ limit: 50, awaiting: tab }), [firm, tab]);
  const inbox = useResource(load);
  const [openingId, setOpeningId] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const open = async (item: FirmInboxItemView) => {
    setOpeningId(item.thread_id);
    setProblem(null);
    try {
      if (item.administration_id !== administration?.id) {
        await switchAdministration(item.administration_id);
      }
      navigate("/");
    } catch (error) {
      setProblem(describeError(error));
      setOpeningId(null);
    }
  };

  const data = inbox.data;
  return (
    <section
      className="screen firm-home"
      aria-label={t("common.nav.inbox")}
      data-testid="firm-inbox"
    >
      <PageHeader
        title={t("common.nav.inbox")}
        context={
          data !== null && tab === "firm"
            ? t("client.todo.inbox.context", { count: data.unread_count })
            : undefined
        }
      />
      <div className="tabs" role="tablist" aria-label={t("client.todo.inbox.tabs")}>
        {TABS.map((value) => (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={tab === value}
            className="tabs__tab"
            data-testid={`firm-inbox-tab-${value}`}
            onClick={() => setTab(value)}
          >
            {t(`client.todo.inbox.tab.${value}`)}
          </button>
        ))}
      </div>
      {problem !== null ? <ErrorState message={problem} /> : null}
      {data === null && inbox.loading ? (
        <LoadingSkeleton rows={4} />
      ) : data === null ? (
        <ErrorState message={inbox.error ?? ""} onRetry={inbox.reload} />
      ) : data.items.length === 0 ? (
        <EmptyState
          icon={<Icon name="mail" size={32} />}
          title={
            tab === "firm"
              ? t("client.todo.inbox.empty_title")
              : t("client.todo.inbox.waiting_empty_title")
          }
          body={
            tab === "firm"
              ? t("client.todo.inbox.empty_body")
              : t("client.todo.inbox.waiting_empty_body")
          }
          testId="firm-inbox-empty"
        />
      ) : (
        <ul className="firm-panel firm-inbox__list">
          {data.items.map((item) => {
            const day = localDateOf(item.last_message_at);
            return (
              <li key={item.thread_id} className="firm-reply">
                <span className="firm-reply__head">
                  <span className="firm-reply__client">{item.display_name}</span>
                  {item.unread ? (
                    <span className="chip chip--accent">{t("client.todo.replies.unread")}</span>
                  ) : null}
                  {day !== null ? <span className="firm-muted">{date(day)}</span> : null}
                </span>
                <span className="firm-reply__subject">{item.subject}</span>
                <span className="firm-muted firm-reply__excerpt">{item.excerpt}</span>
                <button
                  type="button"
                  className="ui-textbutton"
                  disabled={openingId !== null}
                  onClick={() => void open(item)}
                  data-testid={`firm-inbox-open-${item.thread_id}`}
                >
                  {openingId === item.thread_id
                    ? t("client.portfolio.opening")
                    : t("client.todo.inbox.open_client", { name: item.display_name })}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
