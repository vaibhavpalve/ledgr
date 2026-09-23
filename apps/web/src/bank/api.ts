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
 * No live feed (PSD2/AISP) exists - statement import is a canonical CSV
 * (see api.bank.csv_parser) a person exports from their own bank. Every
 * amount is a Decimal-shaped STRING end to end (NFR-031).
 */

import type {
  BankAccountView,
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
}
