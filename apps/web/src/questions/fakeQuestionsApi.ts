/**
 * An in-memory `QuestionsApiShape` for tests, behaving like `api.questions.service`: opening a
 * thread marks it read, a reply flips `awaiting` to the other side, a reply on a resolved thread is
 * a 409 `question_thread_resolved`, and either side may resolve. Every call is recorded. Not
 * imported by the app, so it never reaches the production bundle.
 */

import type {
  QuestionMessageView,
  QuestionSide,
  QuestionThreadDetailView,
  QuestionThreadView,
} from "@ledgr/shared-types";

import { ApiError } from "../api/http";
import type { QuestionsApiShape } from "./api";

export function fakeThread(
  overrides: Partial<QuestionThreadDetailView> = {},
): QuestionThreadDetailView {
  const id = overrides.id ?? "t-1";
  return {
    id,
    administration_id: "adm-A",
    subject: "Betaling KPN",
    status: "open",
    awaiting: "client",
    resource_type: null,
    resource_id: null,
    created_by_user_id: "firm-user",
    created_at: "2026-10-06T09:30:00+00:00",
    last_message_at: "2026-10-06T09:30:00+00:00",
    resolved_at: null,
    resolved_by_user_id: null,
    unread: true,
    messages: [
      {
        id: `${id}-m1`,
        thread_id: id,
        author_user_id: "firm-user",
        author_email: "sanne@kantoor.nl",
        author_side: "firm",
        body: "Waar is deze betaling van 12 september voor?",
        created_at: "2026-10-06T09:30:00+00:00",
      },
    ],
    ...overrides,
  };
}

export interface FakeQuestionsApi extends QuestionsApiShape {
  readonly calls: { method: string; args: unknown[] }[];
  readonly threads: Map<string, QuestionThreadDetailView>;
}

function strip(detail: QuestionThreadDetailView): QuestionThreadView {
  const thread: { messages?: unknown } & QuestionThreadView = { ...detail };
  delete thread.messages;
  return thread;
}

export function fakeQuestionsApi({
  threads = [fakeThread()],
  side = "client",
  userId = "user-1",
  overrides = {},
}: {
  threads?: readonly QuestionThreadDetailView[];
  /** Which side the caller is on, as the server would derive it from the session. */
  side?: QuestionSide;
  userId?: string;
  overrides?: Partial<QuestionsApiShape>;
} = {}): FakeQuestionsApi {
  const store = new Map(threads.map((thread) => [thread.id, thread]));
  const calls: { method: string; args: unknown[] }[] = [];
  let sequence = 0;
  const notFound = () => new ApiError(404, "question_thread_not_found", "Question not found");
  const other: QuestionSide = side === "firm" ? "client" : "firm";

  const base: QuestionsApiShape = {
    listThreads: async (_administrationId, status) =>
      [...store.values()]
        .filter((thread) => status === "all" || thread.status === status)
        .map(strip),
    openThread: async (_administrationId, threadId) => {
      const thread = store.get(threadId);
      if (thread === undefined) throw notFound();
      const read = { ...thread, unread: false };
      store.set(threadId, read);
      return read;
    },
    createThread: async (administrationId, question) => {
      sequence += 1;
      const id = `t-new-${sequence}`;
      const thread = fakeThread({
        id,
        administration_id: administrationId,
        subject: question.subject,
        awaiting: other,
        unread: false,
        created_by_user_id: userId,
        resource_type: question.resourceType ?? null,
        resource_id: question.resourceId ?? null,
        messages: [
          {
            id: `${id}-m1`,
            thread_id: id,
            author_user_id: userId,
            author_email: null,
            author_side: side,
            body: question.body,
            created_at: "2026-10-09T10:00:00+00:00",
          },
        ],
      });
      store.set(id, thread);
      return thread;
    },
    reply: async (_administrationId, threadId, body) => {
      const thread = store.get(threadId);
      if (thread === undefined) throw notFound();
      if (thread.status === "resolved") {
        throw new ApiError(409, "question_thread_resolved", "Question resolved");
      }
      sequence += 1;
      const message: QuestionMessageView = {
        id: `${threadId}-r${sequence}`,
        thread_id: threadId,
        author_user_id: userId,
        author_email: null,
        author_side: side,
        body,
        created_at: "2026-10-09T10:00:00+00:00",
      };
      const next = {
        ...thread,
        awaiting: other,
        unread: false,
        last_message_at: message.created_at,
        messages: [...thread.messages, message],
      };
      store.set(threadId, next);
      return { ...message, thread: strip(next) };
    },
    resolve: async (_administrationId, threadId) => {
      const thread = store.get(threadId);
      if (thread === undefined) throw notFound();
      const next: QuestionThreadDetailView =
        thread.status === "resolved"
          ? thread
          : {
              ...thread,
              status: "resolved",
              resolved_at: "2026-10-09T10:00:00+00:00",
              resolved_by_user_id: userId,
            };
      store.set(threadId, next);
      return strip(next);
    },
    ...overrides,
  };

  const record =
    <A extends unknown[], R>(method: string, impl: (...args: A) => Promise<R>) =>
    (...args: A): Promise<R> => {
      calls.push({ method, args });
      return impl(...args);
    };

  return {
    calls,
    threads: store,
    listThreads: record("listThreads", base.listThreads),
    openThread: record("openThread", base.openThread),
    createThread: record("createThread", base.createThread),
    reply: record("reply", base.reply),
    resolve: record("resolve", base.resolve),
  };
}
