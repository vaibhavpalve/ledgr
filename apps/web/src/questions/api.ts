/**
 * FR-FRM-005's question threads, client side (ADR-111). The thread routes name the
 * administration, so every call is authorized against it by the one library:
 *
 *   GET  /v1/administrations/{id}/questions?status=open|resolved|all
 *   POST /v1/administrations/{id}/questions                      open a thread (firm "New question")
 *   GET  /v1/administrations/{id}/questions/{thread_id}          thread + messages; MARKS IT READ
 *   POST /v1/administrations/{id}/questions/{thread_id}/messages reply (flips `awaiting`)
 *   POST /v1/administrations/{id}/questions/{thread_id}/resolve  either side may resolve
 *
 * Built on `api/http`'s `callJson`: every mutating call carries a fresh Idempotency-Key (NFR-032).
 * Shapes stay in the wire's snake_case, as `@ledgr/shared-types` declares them.
 */

import type {
  QuestionReplyView,
  QuestionResourceType,
  QuestionThreadDetailView,
  QuestionThreadView,
} from "@ledgr/shared-types";

import { callJson, pathOf, queryOf, unwrapList, type ApiOptions } from "../api/http";

export type QuestionListStatus = "open" | "resolved" | "all";

export interface NewQuestion {
  readonly subject: string;
  readonly body: string;
  readonly resourceType?: QuestionResourceType;
  readonly resourceId?: string;
}

export interface QuestionsApiShape {
  listThreads(administrationId: string, status: QuestionListStatus): Promise<QuestionThreadView[]>;
  /** A read with one side effect the API owns: the caller's read receipt. */
  openThread(administrationId: string, threadId: string): Promise<QuestionThreadDetailView>;
  createThread(administrationId: string, question: NewQuestion): Promise<QuestionThreadDetailView>;
  reply(administrationId: string, threadId: string, body: string): Promise<QuestionReplyView>;
  resolve(administrationId: string, threadId: string): Promise<QuestionThreadView>;
}

const base = (administrationId: string, ...rest: string[]) =>
  pathOf("v1", "administrations", administrationId, "questions", ...rest);

export class QuestionsApi implements QuestionsApiShape {
  constructor(private readonly options: ApiOptions) {}

  async listThreads(
    administrationId: string,
    status: QuestionListStatus,
  ): Promise<QuestionThreadView[]> {
    const raw = await callJson<{ threads: readonly QuestionThreadView[] }>(
      this.options,
      "GET",
      `${base(administrationId)}${queryOf({ status })}`,
    );
    return unwrapList<QuestionThreadView>(raw, "threads");
  }

  openThread(administrationId: string, threadId: string): Promise<QuestionThreadDetailView> {
    return callJson<QuestionThreadDetailView>(
      this.options,
      "GET",
      base(administrationId, threadId),
    );
  }

  createThread(administrationId: string, question: NewQuestion): Promise<QuestionThreadDetailView> {
    return callJson<QuestionThreadDetailView>(this.options, "POST", base(administrationId), {
      subject: question.subject,
      body: question.body,
      ...(question.resourceType !== undefined && question.resourceId !== undefined
        ? { resource_type: question.resourceType, resource_id: question.resourceId }
        : {}),
    });
  }

  reply(administrationId: string, threadId: string, body: string): Promise<QuestionReplyView> {
    return callJson<QuestionReplyView>(
      this.options,
      "POST",
      base(administrationId, threadId, "messages"),
      { body },
    );
  }

  resolve(administrationId: string, threadId: string): Promise<QuestionThreadView> {
    return callJson<QuestionThreadView>(
      this.options,
      "POST",
      base(administrationId, threadId, "resolve"),
      {},
    );
  }
}

export const RESOURCE_TYPES: readonly QuestionResourceType[] = [
  "bank_transaction",
  "document",
  "expense",
  "sales_invoice",
];

export function isResourceType(value: string | null): value is QuestionResourceType {
  return value !== null && (RESOURCE_TYPES as readonly string[]).includes(value);
}
