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

/**
 * `/bank` — bank accounts, CSV statement import, and reconciliation. No live
 * feed (PSD2/AISP) exists yet: import is a canonical CSV a person exports
 * from their own bank (`api.bank.csv_parser`'s documented P0 substitute).
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
    Promise.all([bank.listAccounts(administration.id), ledger.listChartOfAccounts(administration.id)])
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
        <AccountTransactions administrationId={administration.id} account={selected} accounts={chartAccounts} />
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

  const reload = useCallback(() => setAttempt((n) => n + 1), []);

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

      {problem !== null ? <ErrorState message={problem} onRetry={reload} /> : null}
      {transactions === null && problem === null ? <LoadingSkeleton rows={5} /> : null}
      {transactions !== null && transactions.length === 0 ? (
        <EmptyState title={t("bank.no_transactions_title")} body={t("bank.no_transactions_body")} testId="bank-transactions-empty" />
      ) : null}
      {transactions !== null && transactions.length > 0 ? (
        <div className="table-wrap">
          <table className="table" data-testid="bank-transactions">
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
                  <tr data-testid="bank-transaction-row">
                    <td className="ledgr-num">{date(transaction.booking_date)}</td>
                    <td>
                      {transaction.counterparty_name ?? "—"}
                      {transaction.description ? <span className="caption"> · {transaction.description}</span> : null}
                    </td>
                    <td className="table__num">{money(transaction.amount)}</td>
                    <td>
                      {transaction.status === "unmatched" ? (
                        <button
                          type="button"
                          onClick={() => setOpenId((current) => (current === transaction.id ? null : transaction.id))}
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
                    <tr>
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
  const [filename, setFilename] = useState("statement.csv");
  const [csv, setCsv] = useState("date,amount,counterparty_name,counterparty_iban,description\n");
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  const submit = useCallback(async () => {
    setSending(true);
    setProblem(null);
    try {
      const imported = await bank.importStatement(administrationId, bankAccountId, csv, filename);
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
  }, [bank, administrationId, bankAccountId, csv, filename, t]);

  return (
    <div className="panel form" data-testid="bank-import-form">
      <h3>{t("bank.import")}</h3>
      <p className="caption">{t("bank.import_hint")}</p>
      {problem !== null ? <ErrorState message={problem} /> : null}
      {result !== null ? <p className="alert alert--positive">{result}</p> : null}
      <label className="form__field">
        <span>{t("bank.field.filename")}</span>
        <input type="text" value={filename} onChange={(event) => setFilename(event.target.value)} />
      </label>
      <label className="form__field form__field--grow">
        <span>{t("bank.field.csv")}</span>
        <textarea
          rows={6}
          value={csv}
          onChange={(event) => setCsv(event.target.value)}
          data-testid="bank-import-csv"
        />
      </label>
      <div className="form__actions">
        <button type="button" disabled={sending} onClick={() => void submit()} data-testid="bank-import-submit">
          {sending ? t("journal.form.submitting") : t("bank.import")}
        </button>
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
  const [offsetAccountId, setOffsetAccountId] = useState(accounts[0]?.id ?? "");
  const [description, setDescription] = useState(transaction.description ?? "");
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    if (!isInflow) return;
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
  }, [bank, administrationId, transaction.id, isInflow]);

  const matchInvoice = useCallback(
    async (invoiceId: string) => {
      setSending(true);
      setProblem(null);
      try {
        await bank.reconcileWithInvoice(administrationId, transaction.id, invoiceId);
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
      await bank.reconcileGeneric(administrationId, transaction.id, offsetAccountId, description || null);
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
      {isInflow && candidates !== null && candidates.length > 0 ? (
        <div>
          <p className="caption">{t("bank.suggested_invoices")}</p>
          <ul>
            {candidates.map((candidate) => (
              <li key={candidate.invoice_id}>
                {candidate.customer_name} — {money(candidate.outstanding)}{" "}
                <button
                  type="button"
                  disabled={sending}
                  onClick={() => void matchInvoice(candidate.invoice_id)}
                  data-testid={`bank-match-invoice-${candidate.invoice_id}`}
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
            {accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.code} — {account.name}
              </option>
            ))}
          </select>
        </label>
        <label className="form__field">
          <span>{t("journal.form.description_label")}</span>
          <input type="text" value={description} onChange={(event) => setDescription(event.target.value)} />
        </label>
      </div>
      <div className="form__actions">
        <button
          type="button"
          disabled={sending}
          onClick={() => void reconcileGeneric()}
          data-testid={`bank-reconcile-confirm-${transaction.id}`}
        >
          {t("bank.reconcile")}
        </button>
      </div>
    </div>
  );
}
