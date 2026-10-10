import { useCallback, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { MessagesSquare } from "lucide-react";
import { useI18n } from "@ledgr/i18n";
import type { QuestionResourceType, QuestionThreadView } from "@ledgr/shared-types";

import "./Questions.css";
import { localDateOf, useResource } from "../firm/useResource";
import { useServices } from "../session/ServicesProvider";
import { useAdministration, useSession } from "../session/SessionProvider";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { Button } from "../ui";
import { isResourceType, type QuestionListStatus } from "./api";
import { awaitingText, questionProblem, resourceText, viewerSide } from "./labels";
import { QuestionThreadScreen } from "./QuestionThreadScreen";

/**
 * `/questions` and `/questions/:threadId` (FR-FRM-005, ADR-111): the conversation between a client
 * and the firm keeping its books, inside the client that is open. The same screen for both sides;
 * only "New question" is the firm's (the contract's web section), and either side may reply and
 * resolve, because the API lets either side do both.
 */
export function QuestionsRoute() {
  const { threadId } = useParams();
  return threadId === undefined ? (
    <QuestionsList />
  ) : (
    <QuestionThreadScreen key={threadId} threadId={threadId} />
  );
}

const TABS: readonly Exclude<QuestionListStatus, "all">[] = ["open", "resolved"];

function QuestionsList() {
  const { t, date } = useI18n();
  const { me } = useSession();
  const { administration } = useAdministration();
  const { questions } = useServices();
  const [searchParams, setSearchParams] = useSearchParams();
  const viewer = viewerSide(me.organization.kind);
  const isFirm = viewer === "firm";

  const [tab, setTab] = useState<Exclude<QuestionListStatus, "all">>("open");
  const load = useCallback(
    () => questions.listThreads(administration.id, tab),
    [questions, administration.id, tab],
  );
  const list = useResource(load);

  // `?resource_type=&resource_id=` opens the composer already attached to that record (a firm
  // user arriving from a bank line or a purchase). Showing a form is not a mutation; sending is.
  const rawType = searchParams.get("resource_type");
  const rawId = searchParams.get("resource_id");
  const attached: { type: QuestionResourceType; id: string } | null =
    isResourceType(rawType) && rawId !== null && rawId !== "" ? { type: rawType, id: rawId } : null;
  const [composing, setComposing] = useState(false);
  const showComposer = isFirm && (composing || attached !== null);

  const closeComposer = () => {
    setComposing(false);
    if (attached !== null) setSearchParams({}, { replace: true });
  };

  const data = list.data;
  return (
    <section
      className="screen questions"
      aria-label={t("client.questions.title")}
      data-testid="questions"
    >
      <PageHeader
        title={t("client.questions.title")}
        {...(data !== null && tab === "open" && data.length > 0
          ? { context: t("client.questions.context", { count: data.length }) }
          : {})}
        {...(isFirm && !showComposer
          ? {
              action: (
                <Button
                  variant="primary"
                  data-testid="questions-new"
                  onClick={() => setComposing(true)}
                >
                  {t("client.questions.new")}
                </Button>
              ),
            }
          : {})}
      />

      {showComposer ? <NewQuestion attached={attached} onCancel={closeComposer} /> : null}

      <div className="tabs" role="tablist" aria-label={t("client.questions.tabs")}>
        {TABS.map((value) => (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={tab === value}
            className="tabs__tab"
            data-testid={`questions-tab-${value}`}
            onClick={() => setTab(value)}
          >
            {t(`client.questions.tab.${value}`)}
          </button>
        ))}
      </div>

      {data === null && list.loading ? (
        <LoadingSkeleton rows={4} />
      ) : data === null ? (
        <ErrorState message={list.error ?? ""} onRetry={list.reload} />
      ) : data.length === 0 ? (
        <EmptyState
          icon={<MessagesSquare size={32} aria-hidden="true" />}
          title={t(`client.questions.empty.${tab}_title`)}
          body={
            tab === "resolved"
              ? t("client.questions.empty.resolved_body")
              : isFirm
                ? t("client.questions.empty.open_body_firm")
                : t("client.questions.empty.open_body_client")
          }
          testId="questions-empty"
        />
      ) : (
        <ul className="questions__list" data-testid="questions-list">
          {data.map((thread) => (
            <ThreadRow
              key={thread.id}
              thread={thread}
              awaiting={thread.status === "open" ? awaitingText(t, thread.awaiting, viewer) : null}
              day={localDateOf(thread.last_message_at)}
              formatDate={date}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function ThreadRow({
  thread,
  awaiting,
  day,
  formatDate,
}: {
  thread: QuestionThreadView;
  awaiting: string | null;
  day: string | null;
  formatDate: (isoDate: string) => string;
}) {
  const { t } = useI18n();
  return (
    <li className={`questions__row${thread.unread ? " questions__row--unread" : ""}`}>
      <Link
        to={`/questions/${encodeURIComponent(thread.id)}`}
        className="questions__link"
        data-testid={`questions-thread-${thread.id}`}
      >
        <span className="questions__subject">{thread.subject}</span>
        <span className="questions__meta">
          {thread.unread ? (
            <span className="chip chip--accent" data-testid={`questions-unread-${thread.id}`}>
              {t("client.questions.unread")}
            </span>
          ) : null}
          <span className={`chip ${thread.status === "open" ? "chip--caution" : "chip--positive"}`}>
            {t(`client.questions.status.${thread.status}`)}
          </span>
          {awaiting !== null ? <span className="questions__awaiting">{awaiting}</span> : null}
          {thread.resource_type !== null ? (
            <span className="questions__muted">{resourceText(t, thread.resource_type)}</span>
          ) : null}
          {day !== null ? (
            <span className="questions__muted">
              {t("client.questions.last_message", { date: formatDate(day) })}
            </span>
          ) : null}
        </span>
      </Link>
    </li>
  );
}

/** The firm's composer. Sending is the only mutation, and only on submit. */
function NewQuestion({
  attached,
  onCancel,
}: {
  attached: { type: QuestionResourceType; id: string } | null;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { administration } = useAdministration();
  const { questions } = useServices();
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (subject.trim() === "" || body.trim() === "") {
      setProblem(t("client.questions.form.required"));
      return;
    }
    setSending(true);
    setProblem(null);
    try {
      const created = await questions.createThread(administration.id, {
        subject: subject.trim(),
        body: body.trim(),
        ...(attached !== null ? { resourceType: attached.type, resourceId: attached.id } : {}),
      });
      navigate(`/questions/${encodeURIComponent(created.id)}`);
    } catch (error) {
      setProblem(questionProblem(t, error));
      setSending(false);
    }
  };

  return (
    <form
      className="panel questions__composer"
      onSubmit={(event) => void submit(event)}
      aria-label={t("client.questions.new")}
      data-testid="questions-composer"
    >
      <h2 className="questions__composer-title">{t("client.questions.new")}</h2>
      {attached !== null ? (
        <p className="questions__muted" data-testid="questions-composer-attached">
          {resourceText(t, attached.type)}
        </p>
      ) : null}
      <label className="questions__field">
        <span className="ui-label">{t("client.questions.form.subject")}</span>
        <input
          value={subject}
          maxLength={200}
          onChange={(event) => setSubject(event.target.value)}
          data-testid="questions-composer-subject"
        />
      </label>
      <label className="questions__field">
        <span className="ui-label">{t("client.questions.form.body")}</span>
        <textarea
          value={body}
          rows={4}
          maxLength={5000}
          onChange={(event) => setBody(event.target.value)}
          data-testid="questions-composer-body"
        />
      </label>
      {problem !== null ? (
        <p className="alert alert--attention" role="alert" data-testid="questions-composer-problem">
          {problem}
        </p>
      ) : null}
      <div className="questions__actions">
        <Button type="button" onClick={onCancel} disabled={sending}>
          {t("client.questions.form.cancel")}
        </Button>
        <Button
          type="submit"
          variant="primary"
          disabled={sending}
          data-testid="questions-composer-send"
        >
          {sending ? t("client.questions.reply.sending") : t("client.questions.form.send")}
        </Button>
      </div>
    </form>
  );
}
