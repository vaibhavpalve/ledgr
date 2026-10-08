/**
 * The Bank screen's client (`/bank`) — `api.bank.routes`.
 *
 *   POST /v1/administrations/{id}/bank-accounts
 *   GET  /v1/administrations/{id}/bank-accounts
 *   POST /v1/administrations/{id}/bank-accounts/{account_id}/import
 *   GET  /v1/administrations/{id}/bank-accounts/{account_id}/transactions
 *   GET  /v1/administrations/{id}/bank-transactions/{transaction_id}/match-candidates
 *   POST /v1/administrations/{id}/bank-transactions/{transaction_id}/reconcile-with-invoice
 *   POST /v1/administrations/{id}/bank-transactions/{transaction_id}/reconcile
 *
 * And the live bank feed (ADR-108, api.bank.feed_routes):
 *
 *   GET  .../bank-accounts/{account_id}/feed
 *   GET  .../bank-feed/institutions?country=NL
 *   POST .../bank-accounts/{account_id}/feed/connect
 *   POST .../bank-feed/connections/{connection_id}/complete
 *   POST .../bank-accounts/{account_id}/feed/sync
 *   POST .../bank-accounts/{account_id}/feed/disconnect
 *
 * Every amount is a Decimal-shaped STRING end to end (NFR-031).
 */

import type {
  BankAccountView,
  BankFeedConnectionView,
  BankFeedInstitutionView,
  BankFeedStatusView,
  BankImportResultView,
  BankMatchCandidateView,
  BankTransactionView,
} from "@ledgr/shared-types";

import { callJson, pathOf, queryOf, unwrapList, type ApiOptions } from "../api/http";

export { ApiError, OfflineError } from "../api/http";

export class BankApi {
  constructor(private readonly options: ApiOptions) {}

  createAccount(
    administrationId: string,
    account: { name: string; iban: string | null; currency: string; ledgerAccountId: string },
  ): Promise<BankAccountView> {
    return callJson<BankAccountView>(
      this.options,
      "POST",
      pathOf("v1", "administrations", administrationId, "bank-accounts"),
      {
        name: account.name,
        iban: account.iban,
        currency: account.currency,
        ledger_account_id: account.ledgerAccountId,
      },
    );
  }

  async listAccounts(administrationId: string): Promise<BankAccountView[]> {
    const raw = await callJson<
      readonly BankAccountView[] | { bank_accounts: readonly BankAccountView[] }
    >(this.options, "GET", pathOf("v1", "administrations", administrationId, "bank-accounts"));
    return unwrapList<BankAccountView>(raw, "bank_accounts");
  }

  importStatement(
    administrationId: string,
    bankAccountId: string,
    csv: string,
    filename: string | null,
  ): Promise<BankImportResultView> {
    return callJson<BankImportResultView>(
      this.options,
      "POST",
      pathOf("v1", "administrations", administrationId, "bank-accounts", bankAccountId, "import"),
      { csv, filename },
    );
  }

  async listTransactions(
    administrationId: string,
    bankAccountId: string,
    status?: "unmatched" | "reconciled",
  ): Promise<BankTransactionView[]> {
    const raw = await callJson<
      readonly BankTransactionView[] | { transactions: readonly BankTransactionView[] }
    >(
      this.options,
      "GET",
      pathOf(
        "v1",
        "administrations",
        administrationId,
        "bank-accounts",
        bankAccountId,
        "transactions",
      ) + queryOf({ status }),
    );
    return unwrapList<BankTransactionView>(raw, "transactions");
  }

  async matchCandidates(
    administrationId: string,
    transactionId: string,
  ): Promise<BankMatchCandidateView[]> {
    const raw = await callJson<
      readonly BankMatchCandidateView[] | { candidates: readonly BankMatchCandidateView[] }
    >(
      this.options,
      "GET",
      pathOf(
        "v1",
        "administrations",
        administrationId,
        "bank-transactions",
        transactionId,
        "match-candidates",
      ),
    );
    return unwrapList<BankMatchCandidateView>(raw, "candidates");
  }

  reconcileWithInvoice(
    administrationId: string,
    transactionId: string,
    invoiceId: string,
  ): Promise<BankTransactionView> {
    return callJson<BankTransactionView>(
      this.options,
      "POST",
      pathOf(
        "v1",
        "administrations",
        administrationId,
        "bank-transactions",
        transactionId,
        "reconcile-with-invoice",
      ),
      { invoice_id: invoiceId },
    );
  }

  /** FR-BNK-005 (ADR-098): one line settling several invoices at once. */
  reconcileWithInvoices(
    administrationId: string,
    transactionId: string,
    allocations: readonly { invoice_id: string; amount: string }[],
  ): Promise<BankTransactionView> {
    return callJson<BankTransactionView>(
      this.options,
      "POST",
      pathOf(
        "v1",
        "administrations",
        administrationId,
        "bank-transactions",
        transactionId,
        "reconcile-with-invoices",
      ),
      { allocations },
    );
  }

  /** ADR-092: an outgoing line settles the receipt it paid. */
  reconcileWithExpense(
    administrationId: string,
    transactionId: string,
    expenseId: string,
  ): Promise<BankTransactionView> {
    return callJson<BankTransactionView>(
      this.options,
      "POST",
      pathOf(
        "v1",
        "administrations",
        administrationId,
        "bank-transactions",
        transactionId,
        "reconcile-with-expense",
      ),
      { expense_id: expenseId },
    );
  }

  /** A suggested or chosen match, whichever kind of document it is. */
  reconcileWithCandidate(
    administrationId: string,
    transactionId: string,
    candidate: Pick<BankMatchCandidateView, "kind" | "document_id">,
  ): Promise<BankTransactionView> {
    return candidate.kind === "expense"
      ? this.reconcileWithExpense(administrationId, transactionId, candidate.document_id)
      : this.reconcileWithInvoice(administrationId, transactionId, candidate.document_id);
  }

  reconcileGeneric(
    administrationId: string,
    transactionId: string,
    offsetAccountId: string,
    description: string | null,
  ): Promise<BankTransactionView> {
    return callJson<BankTransactionView>(
      this.options,
      "POST",
      pathOf(
        "v1",
        "administrations",
        administrationId,
        "bank-transactions",
        transactionId,
        "reconcile",
      ),
      { offset_account_id: offsetAccountId, description },
    );
  }

  // -- The live bank feed (ADR-108) ---------------------------------------------------------

  private feedPath(administrationId: string, bankAccountId: string, ...rest: string[]): string {
    return pathOf(
      "v1",
      "administrations",
      administrationId,
      "bank-accounts",
      bankAccountId,
      "feed",
      ...rest,
    );
  }

  getFeed(administrationId: string, bankAccountId: string): Promise<BankFeedStatusView> {
    return callJson<BankFeedStatusView>(
      this.options,
      "GET",
      this.feedPath(administrationId, bankAccountId),
    );
  }

  async listFeedInstitutions(
    administrationId: string,
    country = "NL",
  ): Promise<BankFeedInstitutionView[]> {
    const raw = await callJson<{ institutions: readonly BankFeedInstitutionView[] }>(
      this.options,
      "GET",
      pathOf("v1", "administrations", administrationId, "bank-feed", "institutions") +
        queryOf({ country }),
    );
    return unwrapList<BankFeedInstitutionView>(raw, "institutions");
  }

  /** Starts a consent; the caller sends the person to `link`, their bank's own page. */
  connectFeed(
    administrationId: string,
    bankAccountId: string,
    institution: { id: string; name: string },
    language: "en" | "nl",
  ): Promise<{ connection: BankFeedConnectionView; link: string }> {
    return callJson(
      this.options,
      "POST",
      this.feedPath(administrationId, bankAccountId, "connect"),
      {
        institution_id: institution.id,
        institution_name: institution.name,
        language,
      },
    );
  }

  /** The person came back from their bank (`/bank/feed-return?ref=<connection id>`). */
  async completeFeed(
    administrationId: string,
    connectionId: string,
  ): Promise<BankFeedConnectionView> {
    const result = await callJson<{ connection: BankFeedConnectionView }>(
      this.options,
      "POST",
      pathOf(
        "v1",
        "administrations",
        administrationId,
        "bank-feed",
        "connections",
        connectionId,
        "complete",
      ),
    );
    return result.connection;
  }

  syncFeed(
    administrationId: string,
    bankAccountId: string,
  ): Promise<{ connection: BankFeedConnectionView; imported: BankImportResultView | null }> {
    return callJson(this.options, "POST", this.feedPath(administrationId, bankAccountId, "sync"));
  }

  async disconnectFeed(
    administrationId: string,
    bankAccountId: string,
  ): Promise<BankFeedConnectionView> {
    const result = await callJson<{ connection: BankFeedConnectionView }>(
      this.options,
      "POST",
      this.feedPath(administrationId, bankAccountId, "disconnect"),
    );
    return result.connection;
  }
}
