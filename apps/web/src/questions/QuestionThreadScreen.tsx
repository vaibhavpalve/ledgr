import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { useI18n } from "@ledgr/i18n";
import type {
  QuestionMessageView,
  QuestionThreadDetailView,
  QuestionThreadView,
} from "@ledgr/shared-types";

import "./Questions.css";
import { ApiError } from "../api/http";
import { localDateOf } from "../firm/useResource";
import { useServices } from "../session/ServicesProvider";
import { useAdministration, useSession } from "../session/SessionProvider";
import { ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { Button } from "../ui";
import { awaitingText, questionProblem, resourcePath, resourceText, viewerSide } from "./labels";

type Loaded =
  | { readonly kind: "loading" }
  | { readonly kind: "error"; readonly message: string }
  | { readonly kind: "ready"; readonly thread: QuestionThreadDetailView };

/**
 * One thread. Opening it is `GET .../questions/{id}`, which is also the read receipt (the API marks
 * it read on the GET): there is no separate "mark read" call to make, so nothing is posted on
 * mount and StrictMode's second effect costs one extra read and nothing else.
 */
export function QuestionThreadScreen({ threadId }: { threadId: string }) {
  const { t, date } = useI18n();
  const { me } = useSession();
  const { administration } = useAdministration();
  const { questions } = useServices();
  const viewer = viewerSide(me.organization.kind);
  const clientName = administration.trade_name ?? administration.legal_name;

  const [loaded, setLoaded] = useState<Loaded>({ kind: "loading" });
  const [version, setVersion] = useState(0);
  useEffect(() => {
    let cancelled = false;
    questions.openThread(administration.id, threadId).then(
      (thread) => {
        if (!cancelled) setLoaded({ kind: "ready", thread });
      },
      (error: unknown) => {
        if (!cancelled) setLoaded({ kind: "error", message: questionProblem(t, error) });
      },
    );
    return () => {
      cancelled = true;
    };
    // `t` is read for the error sentence only; a language switch re-renders, it need not refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [questions, administration.id, threadId, version]);

  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState<"reply" | "resolve" | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const replaceThread = (next: QuestionThreadView, appended?: QuestionMessageView) =>
    setLoaded((current) =>
      current.kind !== "ready"
        ? current
        : {
            kind: "ready",
            thread: {
              ...current.thread,
              ...next,
              messages:
                appended !== undefined
                  ? [...current.thread.messages, appended]
                  : current.thread.messages,
            },
          },
    );

  const refuse = (error: unknown) => {
    setProblem(questionProblem(t, error));
    // Resolved by the other side while this one was typing: show the thread as it now is. The
    // text stays in the box so nothing anybody wrote is lost.
    if (error instanceof ApiError && error.reason === "question_thread_resolved") {
      setVersion((value) => value + 1);
    }
  };

  const send = async (event: FormEvent) => {
    event.preventDefault();
    if (reply.trim() === "") {
      setProblem(t("client.questions.reply.empty"));
      return;
    }
    setBusy("reply");
    setProblem(null);
    try {
      const posted = await questions.reply(administration.id, threadId, reply.trim());
      const { thread, ...message } = posted;
      replaceThread(thread, message);
      setReply("");
    } catch (error) {
      refuse(error);
    } finally {
      setBusy(null);
    }
  };

  const resolve = async () => {
    setBusy("resolve");
    setProblem(null);
    try {
      replaceThread(await questions.resolve(administration.id, threadId));
    } catch (error) {
      refuse(error);
    } finally {
      setBusy(null);
    }
  };

  const back = (
    <Link to="/questions" className="ui-textbutton questions__back" data-testid="question-back">
      <ArrowLeft size={16} aria-hidden="true" />
      <span>{t("client.questions.back")}</span>
    </Link>
  );

  if (loaded.kind === "loading") {
    return (
      <section className="screen questions" data-testid="question-thread">
        {back}
        <LoadingSkeleton rows={3} />
      </section>
    );
  }
  if (loaded.kind === "error") {
    return (
      <section className="screen questions" data-testid="question-thread">
        {back}
        <ErrorState
          message={loaded.message}
          onRetry={() => setVersion((value) => value + 1)}
          testId="question-error"
        />
      </section>
    );
  }

  const thread = loaded.thread;
  const open = thread.status === "open";
  const resolvedDay = thread.resolved_at !== null ? localDateOf(thread.resolved_at) : null;
  const link =
    thread.resource_type !== null && thread.resource_id !== null
      ? resourcePath(thread.resource_type, thread.resource_id)
      : null;

  const authorLabel = (message: QuestionMessageView): string => {
    if (message.author_user_id === me.user.id) return t("client.questions.author.you");
    if (message.author_side === viewer) {
      return message.author_email !== null
        ? t("client.questions.author.colleague_named", { email: message.author_email })
        : t("client.questions.author.colleague");
    }
    return viewer === "client"
      ? t("client.questions.author.accountant")
      : t("client.questions.author.client", { name: clientName });
  };

  return (
    <section className="screen questions" aria-label={thread.subject} data-testid="question-thread">
      {back}
      <PageHeader title={thread.subject}>
        <span className="questions__meta">
          <span
            className={`chip ${open ? "chip--caution" : "chip--positive"}`}
            data-testid="question-status"
          >
            {t(`client.questions.status.${thread.status}`)}
          </span>
          {open ? (
            <span className="questions__awaiting" data-testid="question-awaiting">
              {awaitingText(t, thread.awaiting, viewer)}
            </span>
          ) : null}
        </span>
      </PageHeader>

      {thread.resource_type !== null ? (
        <p className="questions__muted" data-testid="question-resource">
          {resourceText(t, thread.resource_type)}
          {link !== null ? (
            <>
              {" "}
              <Link to={link}>{t("client.questions.about.open")}</Link>
            </>
          ) : null}
        </p>
      ) : null}

      <ol className="questions__messages" aria-label={t("client.questions.messages")}>
        {thread.messages.map((message) => {
          const mine = message.author_user_id === me.user.id;
          const day = localDateOf(message.created_at);
          return (
            <li
              key={message.id}
              className={`questions__message${mine ? " questions__message--mine" : ""}`}
              data-testid={`question-message-${message.id}`}
            >
              <span className="questions__author">
                <strong>{authorLabel(message)}</strong>
                {day !== null ? <span className="questions__muted">{date(day)}</span> : null}
              </span>
              <p className="questions__body">{message.body}</p>
            </li>
          );
        })}
      </ol>

      {problem !== null ? (
        <p className="alert alert--attention" role="alert" data-testid="question-problem">
          {problem}
        </p>
      ) : null}

      {open ? (
        <form
          className="panel questions__reply"
          onSubmit={(event) => void send(event)}
          data-testid="question-reply-form"
        >
          <label className="questions__field">
            <span className="ui-label">{t("client.questions.reply.label")}</span>
            <textarea
              value={reply}
              rows={3}
              maxLength={5000}
              onChange={(event) => setReply(event.target.value)}
              data-testid="question-reply"
            />
          </label>
          <div className="questions__actions">
            <Button
              onClick={() => void resolve()}
              disabled={busy !== null}
              data-testid="question-resolve"
            >
              {busy === "resolve" ? t("client.questions.resolving") : t("client.questions.resolve")}
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={busy !== null}
              data-testid="question-send"
            >
              {busy === "reply"
                ? t("client.questions.reply.sending")
                : t("client.questions.reply.send")}
            </Button>
          </div>
        </form>
      ) : (
        <p className="alert alert--positive" role="status" data-testid="question-resolved-note">
          {resolvedDay !== null
            ? t("client.questions.resolved_note", { date: date(resolvedDay) })
            : t("client.questions.status.resolved")}
        </p>
      )}
    </section>
  );
}
