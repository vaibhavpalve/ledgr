import { Fragment, useCallback, useEffect, useState } from "react";
import { useI18n } from "@ledgr/i18n";
import type {
  BankAccountView,
  BankMatchCandidateView,
  BankTransactionView,
  ChartAccountView,
} from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { Icon } from "../shell/icons";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import "./Bank.css";

/**
 * `/bank` — bank accounts, statement import, and reconciliation. No live feed (PSD2/AISP) exists
 * yet: a person imports the statement file their bank exports (CAMT.053, MT940 or the bank's CSV,
 * ADR-091). Each unmatched incoming line shows its best open invoice and how sure that is; the
 * certain ones can be matched in one go.
 */
export function BankScreen() {
  const { t } = useI18n();
  const { administration } = useAdministration();
  const { bank, ledger } = useServices();

  const [accounts, setAccounts] = useState<readonly BankAccountView[] | null>(null);
  const [chartAccounts, setChartAccounts] = useState<readonly ChartAccountView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [creating, setCreating] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setAccounts(null);
    setProblem(null);
    Promise.all([
      bank.listAccounts(administration.id),
      ledger.listChartOfAccounts(administration.id),
    ])
      .then(([a, c]) => {
        if (cancelled) return;
        setAccounts(a);
        setChartAccounts(c.filter((account) => account.status === "active"));
        const first = a[0];
        if (first !== undefined) setSelectedId((current) => current ?? first.id);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [bank, ledger, administration.id, attempt]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  const selected = accounts?.find((account) => account.id === selectedId) ?? null;

  return (
    <section className="screen" aria-label={t("bank.title")} data-testid="bank-screen">
      <PageHeader
        title={t("bank.title")}
        action={
          chartAccounts !== null ? (
            <button type="button" onClick={() => setCreating(true)} data-testid="bank-new-account">
              <Icon name="plus" size={16} /> {t("bank.new_account")}
            </button>
          ) : undefined
        }
      />
      {problem !== null ? <ErrorState message={problem} onRetry={reload} /> : null}
      {accounts === null && problem === null ? <LoadingSkeleton rows={4} /> : null}
      {accounts !== null && accounts.length === 0 ? (
        <EmptyState
          icon={<Icon name="ledger" size={32} />}
          title={t("bank.empty_title")}
          body={t("bank.empty_body")}
          testId="bank-empty"
        />
      ) : null}
      {accounts !== null && accounts.length > 0 ? (
        <nav className="subnav" aria-label={t("bank.accounts_label")}>
          {accounts.map((account) => (
            <button
              key={account.id}
              type="button"
              aria-current={account.id === selectedId ? "page" : undefined}
              onClick={() => setSelectedId(account.id)}
              data-testid={`bank-account-tab-${account.id}`}
            >
              {account.name}
            </button>
          ))}
        </nav>
      ) : null}

      {selected !== null && chartAccounts !== null ? (
        <AccountTransactions
          administrationId={administration.id}
          account={selected}
          accounts={chartAccounts}
        />
      ) : null}

      {creating && chartAccounts !== null ? (
        <CreateBankAccountForm
          administrationId={administration.id}
          accounts={chartAccounts}
          onClose={() => setCreating(false)}
          onCreated={(account) => {
            setCreating(false);
            setSelectedId(account.id);
            reload();
          }}
        />
      ) : null}
    </section>
  );
}

function CreateBankAccountForm({
  administrationId,
  accounts,
  onClose,
  onCreated,
}: {
  administrationId: string;
  accounts: readonly ChartAccountView[];
  onClose: () => void;
  onCreated: (account: BankAccountView) => void;
}) {
  const { t } = useI18n();
  const { bank } = useServices();
  const assetAccounts = accounts.filter((a) => a.account_type === "asset");

  const [name, setName] = useState("");
  const [iban, setIban] = useState("");
  const [ledgerAccountId, setLedgerAccountId] = useState(assetAccounts[0]?.id ?? "");
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setSending(true);
      setProblem(null);
      try {
        const account = await bank.createAccount(administrationId, {
          name,
          iban: iban.trim() === "" ? null : iban,
          currency: "EUR",
          ledgerAccountId,
        });
        onCreated(account);
      } catch (error) {
        setProblem(describeError(error));
      } finally {
        setSending(false);
      }
    },
    [bank, administrationId, name, iban, ledgerAccountId, onCreated],
  );

  return (
    <div className="panel form" data-testid="bank-account-form">
      <h3>{t("bank.new_account")}</h3>
      {problem !== null ? <ErrorState message={problem} /> : null}
      <form onSubmit={(event) => void submit(event)}>
        <div className="form__row">
          <label className="form__field">
            <span>{t("bank.field.name")}</span>
            <input
              type="text"
              required
              value={name}
              onChange={(event) => setName(event.target.value)}
              data-testid="bank-field-name"
            />
          </label>
          <label className="form__field">
            <span>{t("bank.field.iban")}</span>
            <input type="text" value={iban} onChange={(event) => setIban(event.target.value)} />
          </label>
          <label className="form__field">
            <span>{t("bank.field.ledger_account")}</span>
            <select
              value={ledgerAccountId}
              onChange={(event) => setLedgerAccountId(event.target.value)}
              data-testid="bank-field-ledger-account"
            >
              {assetAccounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.code} — {account.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="form__actions">
          <button type="submit" disabled={sending} data-testid="bank-account-submit">
            {sending ? t("assets.form.saving") : t("assets.form.save")}
          </button>
          <button type="button" className="button--quiet" onClick={onClose}>
            {t("journal.periods.unlock_cancel")}
          </button>
        </div>
      </form>
    </div>
  );
}

function AccountTransactions({
  administrationId,
  account,
  accounts,
}: {
  administrationId: string;
  account: BankAccountView;
  accounts: readonly ChartAccountView[];
}) {
  const { t, money, date } = useI18n();
  const { bank } = useServices();
  const [statusFilter, setStatusFilter] = useState<"unmatched" | "reconciled">("unmatched");
  const [transactions, setTransactions] = useState<readonly BankTransactionView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [importing, setImporting] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);

  const [matching, setMatching] = useState(false);
  const [matchResult, setMatchResult] = useState<{
    tone: "positive" | "attention";
    text: string;
  } | null>(null);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);

  const matchOne = useCallback(
    async (transactionId: string, candidate: BankMatchCandidateView) => {
      setMatching(true);
      setMatchResult(null);
      try {
        await bank.reconcileWithCandidate(administrationId, transactionId, candidate);
        reload();
      } catch (error) {
        setMatchResult({ tone: "attention", text: describeError(error) });
      } finally {
        setMatching(false);
      }
    },
    [bank, administrationId, reload],
  );

  // FR-BNK-004 (ADR-091, ADR-092): only HIGH suggestions, one at a time, each through the same
  // reconcile call a person's click makes - an invoice for money in, a receipt for money out. A
  // refusal (the invoice was paid meanwhile) leaves that line for a person and does not stop the rest.
  const certainLines = (transactions ?? []).filter(
    (transaction) =>
      transaction.status === "unmatched" && transaction.suggestion?.confidence === "high",
  );
  const matchAllCertain = useCallback(async () => {
    setMatching(true);
    setMatchResult(null);
    let matched = 0;
    let failed = 0;
    for (const transaction of certainLines) {
      const suggestion = transaction.suggestion;
      if (!suggestion) continue;
      try {
        await bank.reconcileWithCandidate(administrationId, transaction.id, suggestion);
        matched += 1;
      } catch {
        failed += 1;
      }
    }
    setMatching(false);
    setMatchResult(
      failed === 0
        ? { tone: "positive", text: t("bank.match_certain_done", { count: matched }) }
        : {
            tone: "attention",
            text: t("bank.match_certain_partial", { count: matched, failed }),
          },
    );
    reload();
  }, [bank, administrationId, certainLines, reload, t]);

  useEffect(() => {
    let cancelled = false;
    setTransactions(null);
    setProblem(null);
    bank
      .listTransactions(administrationId, account.id, statusFilter)
      .then((result) => {
        if (!cancelled) setTransactions(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [bank, administrationId, account.id, statusFilter, attempt]);

  return (
    <div className="panel">
      <div className="form__row">
        <nav className="subnav" aria-label={t("bank.status_label")}>
          <button
            type="button"
            aria-current={statusFilter === "unmatched" ? "page" : undefined}
            onClick={() => setStatusFilter("unmatched")}
            data-testid="bank-filter-unmatched"
          >
            {t("bank.status.unmatched")}
          </button>
          <button
            type="button"
            aria-current={statusFilter === "reconciled" ? "page" : undefined}
            onClick={() => setStatusFilter("reconciled")}
            data-testid="bank-filter-reconciled"
          >
            {t("bank.status.reconciled")}
          </button>
        </nav>
        <button type="button" onClick={() => setImporting(true)} data-testid="bank-import-open">
          {t("bank.import")}
        </button>
      </div>

      {importing ? (
        <ImportStatementForm
          administrationId={administrationId}
          bankAccountId={account.id}
          onClose={() => setImporting(false)}
          onImported={() => {
            setImporting(false);
            reload();
          }}
        />
      ) : null}

      {matchResult !== null ? (
        <p
          className={`alert alert--${matchResult.tone}`}
          role="status"
          data-testid="bank-match-result"
        >
          {matchResult.text}
        </p>
      ) : null}
      {certainLines.length > 0 ? (
        <div className="alert alert--positive bank-certain" data-testid="bank-certain">
          <span>{t("bank.certain_banner", { count: certainLines.length })}</span>
          <button
            type="button"
            className="button--primary"
            disabled={matching}
            onClick={() => void matchAllCertain()}
            data-testid="bank-match-certain"
          >
            {matching ? t("journal.form.submitting") : t("bank.match_certain")}
          </button>
        </div>
      ) : null}
      {problem !== null ? <ErrorState message={problem} onRetry={reload} /> : null}
      {transactions === null && problem === null ? <LoadingSkeleton rows={5} /> : null}
      {transactions !== null && transactions.length === 0 ? (
        <EmptyState
          title={t("bank.no_transactions_title")}
          body={t("bank.no_transactions_body")}
          testId="bank-transactions-empty"
        />
      ) : null}
      {transactions !== null && transactions.length > 0 ? (
        <div className="table-wrap">
          <table className="table bank-table" data-testid="bank-transactions">
            <thead>
              <tr>
                <th scope="col">{t("ledger.column.date")}</th>
                <th scope="col">{t("ledger.column.description")}</th>
                <th scope="col" className="table__num">
                  {t("ledger.column.amount")}
                </th>
                <th scope="col" />
              </tr>
            </thead>
            <tbody>
              {transactions.map((transaction) => (
                <Fragment key={transaction.id}>
                  <tr className="bank-row" data-testid="bank-transaction-row">
                    <td className="ledgr-num bank-row__date">{date(transaction.booking_date)}</td>
                    <td className="bank-row__what">
                      {transaction.counterparty_name ?? "—"}
                      {transaction.description ? (
                        <span className="caption"> · {transaction.description}</span>
                      ) : null}
                      {transaction.status === "unmatched" && transaction.suggestion ? (
                        <SuggestionLine
                          suggestion={transaction.suggestion}
                          disabled={matching}
                          onMatch={(candidate) => void matchOne(transaction.id, candidate)}
                          testId={`bank-suggestion-${transaction.id}`}
                        />
                      ) : null}
                    </td>
                    <td className="table__num bank-row__amount">{money(transaction.amount)}</td>
                    <td className="bank-row__action">
                      {transaction.status === "unmatched" ? (
                        <button
                          type="button"
                          onClick={() =>
                            setOpenId((current) =>
                              current === transaction.id ? null : transaction.id,
                            )
                          }
                          data-testid={`bank-reconcile-open-${transaction.id}`}
                        >
                          {t("bank.reconcile")}
                        </button>
                      ) : (
                        <span className="chip chip--positive">{t("bank.status.reconciled")}</span>
                      )}
                    </td>
                  </tr>
                  {openId === transaction.id ? (
                    <tr className="bank-row__panel">
                      <td colSpan={4}>
                        <ReconcilePanel
                          administrationId={administrationId}
                          transaction={transaction}
                          accounts={accounts}
                          onDone={() => {
                            setOpenId(null);
                            reload();
                          }}
                        />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

/**
 * FR-BNK-002 (ADR-091): the statement file as the bank exported it - CAMT.053, MT940, or the
 * bank's CSV. Choosing the file imports it; there is nothing to fill in. Decoded as UTF-8, and as
 * Windows-1252 when it is not valid UTF-8 (ING's CSV), so a name like "Privé" survives.
 */
async function readStatement(file: File): Promise<string> {
  const bytes = await file.arrayBuffer();
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return new TextDecoder("windows-1252").decode(bytes);
  }
}

function ImportStatementForm({
  administrationId,
  bankAccountId,
  onClose,
  onImported,
}: {
  administrationId: string;
  bankAccountId: string;
  onClose: () => void;
  onImported: () => void;
}) {
  const { t } = useI18n();
  const { bank } = useServices();
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  const submit = useCallback(
    async (file: File) => {
      setSending(true);
      setProblem(null);
      setResult(null);
      try {
        const text = await readStatement(file);
        const imported = await bank.importStatement(
          administrationId,
          bankAccountId,
          text,
          file.name,
        );
        setResult(
          t("bank.import_result", {
            count: imported.transaction_count,
            duplicates: imported.duplicate_count,
          }),
        );
      } catch (error) {
        setProblem(describeError(error));
      } finally {
        setSending(false);
      }
    },
    [bank, administrationId, bankAccountId, t],
  );

  return (
    <div className="panel form" data-testid="bank-import-form">
      <h3>{t("bank.import")}</h3>
      <p className="caption">{t("bank.import_hint")}</p>
      {problem !== null ? <ErrorState message={problem} /> : null}
      {result !== null ? (
        <p className="alert alert--positive" role="status" data-testid="bank-import-result">
          {result}
        </p>
      ) : null}
      <label className="button-link">
        {sending ? t("journal.form.submitting") : t("bank.import_choose")}
        <input
          type="file"
          accept=".xml,.sta,.940,.mt940,.swi,.csv,.txt,text/csv,text/xml,application/xml"
          className="ledgr-visually-hidden"
          disabled={sending}
          data-testid="bank-import-file"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void submit(file);
            event.target.value = "";
          }}
        />
      </label>
      <div className="form__actions">
        {result !== null ? (
          <button type="button" onClick={onImported} data-testid="bank-import-done">
            {t("common.action.close")}
          </button>
        ) : (
          <button type="button" className="button--quiet" onClick={onClose}>
            {t("journal.periods.unlock_cancel")}
          </button>
        )}
      </div>
    </div>
  );
}

const CONFIDENCE_CHIP = {
  high: "chip chip--positive",
  medium: "chip chip--caution",
  low: "chip",
} as const;

function ConfidenceChip({ candidate }: { candidate: BankMatchCandidateView }) {
  const { t } = useI18n();
  const why = candidate.reasons.map((reason) => t(`bank.reason.${reason}`)).join(", ");
  return (
    <span className={CONFIDENCE_CHIP[candidate.confidence]} title={why}>
      {t(`bank.confidence.${candidate.confidence}`)}
    </span>
  );
}

/** "Invoice 2026-0007 to Hotel De Gouden Leeuw" or "Receipt from Staples, 16-09-2026". */
function CandidateText({ candidate }: { candidate: BankMatchCandidateView }) {
  const { t, date } = useI18n();
  return (
    <span className="caption">
      {candidate.kind === "expense"
        ? t("bank.suggestion_expense", {
            party: candidate.party_name,
            date: date(candidate.document_date),
          })
        : t("bank.suggestion", {
            reference: candidate.reference ?? "—",
            party: candidate.party_name,
          })}
    </span>
  );
}

/** The best open document for one bank line, with a one-click match (FR-BNK-003, ADR-091). */
function SuggestionLine({
  suggestion,
  disabled,
  onMatch,
  testId,
}: {
  suggestion: BankMatchCandidateView;
  disabled: boolean;
  onMatch: (candidate: BankMatchCandidateView) => void;
  testId: string;
}) {
  const { t } = useI18n();
  return (
    <div className="bank-suggestion" data-testid={testId}>
      <ConfidenceChip candidate={suggestion} />
      <CandidateText candidate={suggestion} />
      <button
        type="button"
        className="bank-suggestion__match"
        disabled={disabled}
        onClick={() => onMatch(suggestion)}
        data-testid={`${testId}-match`}
      >
        {t("bank.match")}
      </button>
    </div>
  );
}

function ReconcilePanel({
  administrationId,
  transaction,
  accounts,
  onDone,
}: {
  administrationId: string;
  transaction: BankTransactionView;
  accounts: readonly ChartAccountView[];
  onDone: () => void;
}) {
  const { t, money } = useI18n();
  const { bank } = useServices();
  const isInflow = !transaction.amount.startsWith("-");
  const [candidates, setCandidates] = useState<readonly BankMatchCandidateView[] | null>(null);
  // No default: the first account in the chart (a fixed-asset account) is never a safe guess.
  const [offsetAccountId, setOffsetAccountId] = useState("");
  const [description, setDescription] = useState(transaction.description ?? "");
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    bank
      .matchCandidates(administrationId, transaction.id)
      .then((result) => {
        if (!cancelled) setCandidates(result);
      })
      .catch(() => {
        if (!cancelled) setCandidates([]);
      });
    return () => {
      cancelled = true;
    };
  }, [bank, administrationId, transaction.id]);

  const matchCandidate = useCallback(
    async (candidate: BankMatchCandidateView) => {
      setSending(true);
      setProblem(null);
      try {
        await bank.reconcileWithCandidate(administrationId, transaction.id, candidate);
        onDone();
      } catch (error) {
        setProblem(describeError(error));
      } finally {
        setSending(false);
      }
    },
    [bank, administrationId, transaction.id, onDone],
  );

  const reconcileGeneric = useCallback(async () => {
    setSending(true);
    setProblem(null);
    try {
      await bank.reconcileGeneric(
        administrationId,
        transaction.id,
        offsetAccountId,
        description || null,
      );
      onDone();
    } catch (error) {
      setProblem(describeError(error));
    } finally {
      setSending(false);
    }
  }, [bank, administrationId, transaction.id, offsetAccountId, description, onDone]);

  return (
    <div className="panel form" data-testid={`bank-reconcile-panel-${transaction.id}`}>
      {problem !== null ? <ErrorState message={problem} /> : null}
      {candidates !== null && candidates.length > 0 ? (
        <div>
          <p className="caption">
            {isInflow ? t("bank.suggested_invoices") : t("bank.suggested_receipts")}
          </p>
          <ul className="bank-candidates">
            {candidates.map((candidate) => (
              <li key={candidate.document_id}>
                <ConfidenceChip candidate={candidate} /> <CandidateText candidate={candidate} /> —{" "}
                {money(candidate.amount)}{" "}
                <button
                  type="button"
                  disabled={sending}
                  onClick={() => void matchCandidate(candidate)}
                  data-testid={`bank-match-${candidate.kind}-${candidate.document_id}`}
                >
                  {t("bank.match")}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <p className="caption">{t("bank.generic_reconcile_hint")}</p>
      <div className="form__row">
        <label className="form__field">
          <span>{t("bank.field.offset_account")}</span>
          <select
            value={offsetAccountId}
            onChange={(event) => setOffsetAccountId(event.target.value)}
            data-testid={`bank-offset-account-${transaction.id}`}
          >
            <option value="" disabled>
              {t("bank.offset_account_choose")}
            </option>
            {accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.code} — {account.name}
              </option>
            ))}
          </select>
        </label>
        <label className="form__field">
          <span>{t("journal.form.description_label")}</span>
          <input
            type="text"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>
      </div>
      <div className="form__actions">
        <button
          type="button"
          disabled={sending || offsetAccountId === ""}
          onClick={() => void reconcileGeneric()}
          data-testid={`bank-reconcile-confirm-${transaction.id}`}
        >
          {t("bank.reconcile")}
        </button>
      </div>
    </div>
  );
}
