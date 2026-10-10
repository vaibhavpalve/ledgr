import type { QuestionResourceType, QuestionSide } from "@ledgr/shared-types";

import { ApiError, describeError } from "../api/http";

type Translate = (key: string, params?: Readonly<Record<string, string | number>>) => string;

/**
 * Which side the person reading is on. The API decides a writer's side from the session (the
 * organization that OWNS the administration is the client, an engaged firm is the firm); the
 * closest the web session has to that is the kind of organization signed in. A firm keeping its
 * own books would read "client" on the server and "firm" here - an edge the wording survives,
 * because "You" is decided by user id, not by side.
 */
export function viewerSide(organizationKind: string): QuestionSide {
  return organizationKind === "firm" ? "firm" : "client";
}

/** "Waiting for your reply" / "Waiting for your accountant" / "Waiting for the client". */
export function awaitingText(t: Translate, awaiting: QuestionSide, viewer: QuestionSide): string {
  if (awaiting === viewer) return t("client.questions.awaiting.you");
  return viewer === "client"
    ? t("client.questions.awaiting.accountant")
    : t("client.questions.awaiting.client");
}

export function resourceText(t: Translate, type: QuestionResourceType): string {
  return t(`client.questions.about.${type}`);
}

/** Where an attached record can be opened, if this app has a screen for one. */
export function resourcePath(type: QuestionResourceType, id: string): string | null {
  switch (type) {
    case "expense":
      return `/purchases/${encodeURIComponent(id)}`;
    case "sales_invoice":
      return `/invoices/${encodeURIComponent(id)}`;
    case "bank_transaction":
      return "/bank";
    case "document":
      return null;
  }
}

/**
 * The refusals a person meets here, said in what-happened-and-what-next terms (D5). Anything
 * else falls back to the server's own, already translated sentence (FR-UX-007).
 */
export function questionProblem(t: Translate, error: unknown): string {
  if (error instanceof ApiError) {
    if (error.reason === "question_thread_resolved") return t("client.questions.error.resolved");
    if (error.reason === "active_client_mismatch") return t("client.questions.error.mismatch");
    if (error.reason === "question_thread_not_found" || error.status === 404) {
      return t("client.questions.error.not_found");
    }
  }
  return describeError(error);
}
