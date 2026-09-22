/**
 * The capture screens' calls to the API — FR-EXP-001a, FR-EXP-001b,
 * FR-EXP-001e.
 *
 * --- What is NOT here ---
 *
 * Uploading a page. That goes through the offline queue and nowhere else
 * (ADR-035 §2): there is no online fast path, because a second delivery path
 * would have its own retry story and the one exercised least often would be the
 * one that runs when the tunnel starts. `useSitting` calls `queue.enqueue`; the
 * uploader drains.
 *
 * Everything here is the part that genuinely needs a server to answer — opening
 * a sitting, reading back the review list, saving the form, finalising — and
 * every one of them is unavailable offline. The screens are built to say so
 * rather than to fail (see `OfflineError`).
 *
 * --- Every mutating call carries an idempotency key ---
 *
 * NFR-032, and the middleware refuses a mutating request without one. A fresh
 * key per call, because each is a new intention rather than a retry: the queue
 * is where retries live, and it keeps its own key per capture.
 */

import type {
  CaptureSessionView,
  ExpenseStatus,
  ExpenseSummaryView,
  ExpenseView,
  PaymentMethod,
  VatTreatment,
} from "@ledgr/shared-types";
import type { Language } from "@ledgr/i18n";

import { languageHeaders } from "../i18n";

/** The call could not be made because there is no connection. */
export class OfflineError extends Error {}

/**
 * The API refused, with the machine-readable `reason` from
 * `api.i18n.http.problem` and the already-translated sentence beside it.
 *
 * `message` is carried and SHOWN: the server rendered it in the language
 * `Accept-Language` asked for (FR-UX-007), so it is the one place a client may
 * display text it did not get from the catalogue itself. `reason` is what code
 * branches on.
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly reason: string | null,
    message: string,
  ) {
    super(message);
  }
}

export interface ApiOptions {
  language: () => Language;
  fetchImpl?: typeof fetch;
}

export class CaptureApi {
  constructor(private readonly options: ApiOptions) {}

  /** FR-EXP-001a: open a sitting. */
  openSession(administrationId: string): Promise<CaptureSessionView> {
    return this.call<CaptureSessionView>(
      "POST",
      `/v1/administrations/${encodeURIComponent(administrationId)}/capture-sessions`,
    );
  }

  /** FR-EXP-001a's "review list before posting", as the server sees it. */
  reviewSession(administrationId: string, sessionId: string): Promise<CaptureSessionView> {
    return this.call<CaptureSessionView>(
      "GET",
      `/v1/administrations/${encodeURIComponent(administrationId)}` +
        `/capture-sessions/${encodeURIComponent(sessionId)}`,
    );
  }

  /**
   * Accept the review list. Closes the sitting and posts nothing (ADR-031 §5):
   * each expense becomes ready individually when its own form is complete.
   */
  finaliseSession(administrationId: string, sessionId: string): Promise<unknown> {
    return this.call(
      "POST",
      `/v1/administrations/${encodeURIComponent(administrationId)}` +
        `/capture-sessions/${encodeURIComponent(sessionId)}/finalise`,
    );
  }

  getExpense(administrationId: string, expenseId: string): Promise<ExpenseView> {
    return this.call<ExpenseView>("GET", this.expensePath(administrationId, expenseId));
  }

  /**
   * MOB-004's Approve tab (`status: "draft"`) and View tab (`status: "ready"`
   * or `"posted"`) - `api.expenses.routes.list_expenses`. Omit `status` for
   * every status.
   *
   * Reuses this same `CaptureApi`/permission rather than a new client: filling
   * in and reviewing the form IS submitting the expense (ADR-012), and
   * `ExpenseForm` above already talks to this class for exactly that.
   */
  listExpenses(administrationId: string, status?: ExpenseStatus): Promise<ExpenseSummaryView[]> {
    const query = status ? `?status=${encodeURIComponent(status)}` : "";
    return this.call<ExpenseSummaryView[]>(
      "GET",
      `/v1/administrations/${encodeURIComponent(administrationId)}/expenses${query}`,
    );
  }

  /**
   * Save any subset of the form.
   *
   * PATCH, and the distinction is load-bearing: a PUT would make an omitted
   * field mean "clear it", so saving one changed field would wipe the other
   * five. Only what is passed is sent.
   *
   * `gross_amount` is a STRING on the wire. An unquoted JSON number is a double
   * before the server can see it, and NFR-031 covers the whole path.
   */
  updateExpense(
    administrationId: string,
    expenseId: string,
    fields: ExpenseFormPatch,
  ): Promise<ExpenseView> {
    return this.call<ExpenseView>("PATCH", this.expensePath(administrationId, expenseId), fields);
  }

  /** Release the claim. Refuses, naming fields, until the minimum is given. */
  markReady(administrationId: string, expenseId: string): Promise<ExpenseView> {
    return this.call<ExpenseView>("POST", `${this.expensePath(administrationId, expenseId)}/ready`);
  }

  private expensePath(administrationId: string, expenseId: string): string {
    return (
      `/v1/administrations/${encodeURIComponent(administrationId)}` +
      `/expenses/${encodeURIComponent(expenseId)}`
    );
  }

  private async call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const mutating = method !== "GET";
    const fetchImpl = this.options.fetchImpl ?? fetch;

    let response: Response;
    try {
      response = await fetchImpl(path, {
        method,
        headers: {
          ...languageHeaders(this.options.language()),
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
          // NFR-032. Required on every mutating endpoint by middleware, so a
          // missing one is a 400 rather than an unprotected write.
          ...(mutating ? { "Idempotency-Key": crypto.randomUUID() } : {}),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (cause) {
      // Told apart from a refusal, because the screens answer them
      // differently: a refusal is shown, and being offline is a state the
      // capture screen carries on working in.
      throw new OfflineError(`${method} ${path} could not reach the API`, { cause });
    }

    if (!response.ok) throw await problemFrom(response);
    return (await response.json()) as T;
  }
}

export interface ExpenseFormPatch {
  readonly expense_date?: string | null;
  readonly supplier?: string | null;
  /** Decimal STRING, never a number — see `updateExpense`. */
  readonly gross_amount?: string | null;
  readonly vat_treatment?: VatTreatment | null;
  readonly category?: string | null;
  readonly payment_method?: PaymentMethod | null;
  readonly invoice_number?: string | null;
}

async function problemFrom(response: Response): Promise<ApiError> {
  let reason: string | null = null;
  let message = "";
  try {
    const body = (await response.json()) as { detail?: unknown };
    const detail = body.detail;
    if (typeof detail === "string") {
      message = detail;
    } else if (detail !== null && typeof detail === "object") {
      const fields = detail as { reason?: unknown; message?: unknown };
      if (typeof fields.reason === "string") reason = fields.reason;
      if (typeof fields.message === "string") message = fields.message;
    }
  } catch {
    // A gateway's HTML or an empty body. The status is still the answer.
  }
  return new ApiError(response.status, reason, message || `HTTP ${response.status}`);
}
