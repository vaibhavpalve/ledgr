import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type {
  BankAccountView,
  BankMatchCandidateView,
  BankTransactionView,
  ChartAccountView,
} from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { useAdministration } from "../session/SessionProvider";
import { fiscalYearLabel } from "../shell/AppShell";
import { useServices } from "../session/ServicesProvider";
import { Icon } from "../shell/icons";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { toDecimalInput } from "../ui/decimal";
import { useModalFocus } from "../useModalFocus";
import { BankFeedPanel, FeedNoticeLine, type FeedNotice } from "./BankFeed";
import { formatIban, isValidIban, normaliseIban } from "./iban";
import "./Bank.css";

/**
 * `/bank` — bank accounts, statement import, and reconciliation. Lines come in two ways: the
 * statement file a bank exports (CAMT.053, MT940 or its CSV, ADR-091), or, where a provider is
 * configured, the live bank feed (PSD2, ADR-108, `BankFeed.tsx`). Both land the same way. Each
 * unmatched incoming line shows its best open invoice and how sure that is; the certain ones can
 * be matched in one go.
 */
export function BankScreen() {
  const { t } = useI18n();
  const { administration, fiscalYear } = useAdministration();
  const { bank, ledger } = useServices();

  const [accounts, setAccounts] = useState<readonly BankAccountView[] | null>(null);
  const [chartAccounts, setChartAccounts] = useState<readonly ChartAccountView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [creating, setCreating] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // Each ledger account's balance in the selected fiscal year, from the trial balance. Secondary:
  // when it cannot be read the summary simply shows no balance.
  const [balances, setBalances] = useState<ReadonlyMap<string, string> | null>(null);
  const fiscalYearId = fiscalYear?.id ?? null;
  // Bumped when the bank feed brings in new lines, so the transactions list reads them.
  const [fetched, setFetched] = useState(0);
  // What the return from the bank (`/bank/feed-return`) has to say, once.
  const location = useLocation();
  const returnNotice = (location.state as { feedNotice?: FeedNotice } | null)?.feedNotice ?? null;

  useEffect(() => {
    if (fiscalYearId === null) return;
    let cancelled = false;
    setBalances(null);
    void Promise.resolve()
      .then(() => ledger.getTrialBalance(administration.id, fiscalYearId))
      .then((trial) => {
        if (!cancelled)
          setBalances(new Map(trial.rows.map((row) => [row.account_id, row.balance])));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [ledger, administration.id, fiscalYearId, attempt]);

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
  // One primary action per view: adding the first account when there is none, otherwise
  // importing a statement (in the transactions panel), so this one steps back to secondary.
  const firstAccount = accounts !== null && accounts.length === 0;

  return (
    <section className="screen" aria-label={t("bank.title")} data-testid="bank-screen">
      <PageHeader
        title={t("bank.title")}
        action={
          chartAccounts !== null ? (
            <button
              type="button"
              className={firstAccount ? "button--primary" : "button-link"}
              onClick={() => setCreating(true)}
              data-testid="bank-new-account"
            >
              <Icon name="plus" size={16} /> {t("bank.new_account")}
            </button>
          ) : undefined
        }
      />
      {returnNotice !== null ? <FeedNoticeLine notice={returnNotice} /> : null}
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
      {/* Several accounts: a pill per account to switch between them. One account needs none. */}
      {accounts !== null && accounts.length > 1 ? (
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
        <AccountSummary
          account={selected}
          ledgerAccount={chartAccounts.find((a) => a.id === selected.ledger_account_id) ?? null}
          balance={balances?.get(selected.ledger_account_id) ?? null}
          yearLabel={
            fiscalYear ? fiscalYearLabel(fiscalYear.start_date, fiscalYear.end_date) : null
          }
        >
          <BankFeedPanel
            administrationId={administration.id}
            account={selected}
            onFetched={() => {
              setFetched((n) => n + 1);
              reload();
            }}
          />
        </AccountSummary>
      ) : null}

      {selected !== null && chartAccounts !== null ? (
        <AccountTransactions
          key={`${selected.id}-${fetched}`}
          administrationId={administration.id}
          account={selected}
          accounts={chartAccounts}
        />
      ) : null}

      {creating && chartAccounts !== null ? (
        <CreateBankAccountDialog
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

/**
 * The selected account at a glance: its name, IBAN as banks print it, the grootboek account its
 * lines are booked to, and that account's balance in the selected fiscal year. The balance is the
 * ledger's own figure, formatted, never computed here (NFR-031). There is no "last imported" line:
 * the API does not record one yet.
 */
function AccountSummary({
  account,
  ledgerAccount,
  balance,
  yearLabel,
  children,
}: {
  account: BankAccountView;
  ledgerAccount: ChartAccountView | null;
  balance: string | null;
  yearLabel: string | null;
  /** The live bank feed's row (ADR-108), across the card's foot. */
  children?: ReactNode;
}) {
  const { t, money } = useI18n();
  return (
    <section className="panel bank-summary" aria-label={account.name} data-testid="bank-summary">
      <div className="bank-summary__who">
        <h2 className="bank-summary__name">{account.name}</h2>
        <dl className="bank-summary__facts">
          <div>
            <dt>{t("bank.account.iban")}</dt>
            <dd className="ledgr-num" data-testid="bank-summary-iban">
              {account.iban ? formatIban(account.iban) : t("bank.account.no_iban")}
            </dd>
          </div>
          {ledgerAccount !== null ? (
            <div>
              <dt>{t("bank.account.ledger")}</dt>
              <dd data-testid="bank-summary-ledger">
                {ledgerAccount.code} — {ledgerAccount.name}
              </dd>
            </div>
          ) : null}
        </dl>
      </div>
      {balance !== null && yearLabel !== null ? (
        <div className="bank-summary__balance">
          <span className="label">{t("bank.account.balance", { year: yearLabel })}</span>
          <span className="bank-summary__amount figure" data-testid="bank-summary-balance">
            {money(balance)}
          </span>
        </div>
      ) : null}
      {children}
    </section>
  );
}

/**
 * The bank and cash accounts of the chart (RGS `BLim*`: BLimBan bank, BLimKas cash), bank first.
 * A chart without RGS codes falls back to every asset account, with nothing preselected, so a
 * bank is never quietly booked against machinery.
 */
export function bankLedgerChoices(accounts: readonly ChartAccountView[]): {
  options: readonly ChartAccountView[];
  preselected: string;
} {
  const liquid = accounts
    .filter((a) => a.account_type === "asset" && (a.rgs_code ?? "").startsWith("BLim"))
    .sort(
      (a, b) =>
        Number(b.rgs_code === "BLimBan") - Number(a.rgs_code === "BLimBan") ||
        a.code.localeCompare(b.code),
    );
  if (liquid.length > 0) return { options: liquid, preselected: liquid[0]?.id ?? "" };
  return { options: accounts.filter((a) => a.account_type === "asset"), preselected: "" };
}

/**
 * Adding a bank account, as a dialog over the page rather than a form under the transactions:
 * it opens where the button is, and the rest of the screen waits. Each field says what it is for;
 * the IBAN is grouped as it is typed and checked before anything is sent.
 */
function CreateBankAccountDialog({
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
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalFocus(true, dialogRef);
  // Escape closes it, as every dialog does; listened for on the document so it works wherever
  // focus is inside the dialog.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  const { options, preselected } = bankLedgerChoices(accounts);

  const [name, setName] = useState("");
  const [iban, setIban] = useState("");
  const [ibanTouched, setIbanTouched] = useState(false);
  const [ledgerAccountId, setLedgerAccountId] = useState(preselected);
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const ibanEntered = normaliseIban(iban) !== "";
  const ibanInvalid = ibanEntered && !isValidIban(iban);
  const canSubmit = !sending && name.trim() !== "" && ledgerAccountId !== "" && !ibanInvalid;

  const submit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setIbanTouched(true);
      if (!canSubmit) return;
      setSending(true);
      setProblem(null);
      try {
        const account = await bank.createAccount(administrationId, {
          name: name.trim(),
          iban: ibanEntered ? normaliseIban(iban) : null,
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
    [bank, administrationId, name, iban, ibanEntered, ledgerAccountId, canSubmit, onCreated],
  );

  return (
    <div className="dialog-backdrop bank-dialog-backdrop">
      <div
        ref={dialogRef}
        className="dialog bank-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="bank-dialog-title"
        data-testid="bank-account-form"
      >
        <div>
          <h2 id="bank-dialog-title" className="bank-dialog__title">
            {t("bank.new_account")}
          </h2>
          <p className="bank-dialog__intro">{t("bank.form.intro")}</p>
        </div>
        {problem !== null ? <ErrorState message={problem} /> : null}
        <form className="bank-dialog__form" onSubmit={(event) => void submit(event)} noValidate>
          <label className="form__field">
            <span>{t("bank.field.name")}</span>
            <input
              type="text"
              required
              autoComplete="off"
              value={name}
              onChange={(event) => setName(event.target.value)}
              aria-describedby="bank-field-name-hint"
              data-testid="bank-field-name"
            />
            <span id="bank-field-name-hint" className="form__hint">
              {t("bank.field.name_hint")}
            </span>
          </label>
          <label className="form__field">
            <span>{t("bank.field.iban")}</span>
            <input
              type="text"
              inputMode="text"
              autoComplete="off"
              spellCheck={false}
              value={iban}
              onChange={(event) => setIban(formatIban(event.target.value))}
              onBlur={() => setIbanTouched(true)}
              aria-invalid={ibanTouched && ibanInvalid ? true : undefined}
              aria-describedby="bank-field-iban-note"
              data-testid="bank-field-iban"
            />
            {ibanTouched && ibanInvalid ? (
              <span
                id="bank-field-iban-note"
                className="ui-error"
                role="alert"
                data-testid="bank-field-iban-error"
              >
                <Icon name="warning" size={14} />
                {t("bank.field.iban_invalid")}
              </span>
            ) : (
              <span id="bank-field-iban-note" className="form__hint">
                {t("bank.field.iban_hint")}
              </span>
            )}
          </label>
          <label className="form__field">
            <span>{t("bank.field.ledger_account")}</span>
            <select
              required
              value={ledgerAccountId}
              onChange={(event) => setLedgerAccountId(event.target.value)}
              aria-describedby="bank-field-ledger-hint"
              data-testid="bank-field-ledger-account"
            >
              {preselected === "" ? (
                <option value="" disabled>
                  {t("bank.field.ledger_choose")}
                </option>
              ) : null}
              {options.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.code} — {account.name}
                </option>
              ))}
            </select>
            <span id="bank-field-ledger-hint" className="form__hint">
              {t("bank.field.ledger_hint")}
            </span>
          </label>
          <div className="dialog__actions">
            <button type="button" className="button--quiet" onClick={onClose}>
              {t("bank.action.cancel")}
            </button>
            <button
              type="submit"
              className="button--primary"
              disabled={!canSubmit}
              data-testid="bank-account-submit"
            >
              {sending ? t("bank.form.submitting") : t("bank.form.submit")}
            </button>
          </div>
        </form>
      </div>
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
        {/* With nothing to show, the import is the empty state's own action instead. */}
        {!importing && (transactions === null || transactions.length > 0) ? (
          <button
            type="button"
            className="button--primary"
            disabled={importing}
            onClick={() => setImporting(true)}
            data-testid="bank-import-open"
          >
            {t("bank.import")}
          </button>
        ) : null}
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
      {transactions !== null && transactions.length === 0 && !importing ? (
        <EmptyState
          title={t("bank.no_transactions_title")}
          body={t("bank.no_transactions_body")}
          testId="bank-transactions-empty"
          action={
            <button
              type="button"
              className="button--primary"
              onClick={() => setImporting(true)}
              data-testid="bank-import-open"
            >
              {t("bank.import")}
            </button>
          }
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
    <div className="panel form bank-import" data-testid="bank-import-form">
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
            {t("bank.action.cancel")}
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
  const [splitting, setSplitting] = useState(false);

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
            {candidates.map((candidate) => {
              const partial = candidate.reasons.includes("partial");
              return (
                <li key={candidate.document_id}>
                  <ConfidenceChip candidate={candidate} /> <CandidateText candidate={candidate} /> —{" "}
                  {partial ? (
                    // FR-BNK-005 (ADR-097): both figures, never a client-side subtraction of
                    // money (NFR-031) - the invoice stays open for the rest afterward.
                    <span data-testid={`bank-partial-${candidate.document_id}`}>
                      {t("bank.partial_amount", {
                        paid: money(transaction.amount),
                        open: money(candidate.amount),
                      })}
                    </span>
                  ) : (
                    money(candidate.amount)
                  )}{" "}
                  <button
                    type="button"
                    disabled={sending}
                    onClick={() => void matchCandidate(candidate)}
                    data-testid={`bank-match-${candidate.kind}-${candidate.document_id}`}
                  >
                    {t("bank.match")}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
      {/* FR-BNK-005 (ADR-098): one line settling several invoices at once - the expense side has
          no batched shape yet, so this is offered only for money in. */}
      {isInflow && candidates !== null && candidates.length >= 2 ? (
        <div>
          <button
            type="button"
            className="bank-split__toggle"
            onClick={() => setSplitting((current) => !current)}
            data-testid={`bank-split-toggle-${transaction.id}`}
          >
            {t(splitting ? "bank.split.hide" : "bank.split.show")}
          </button>
          {splitting ? (
            <SplitAllocationPanel
              administrationId={administrationId}
              transaction={transaction}
              candidates={candidates}
              onDone={onDone}
            />
          ) : null}
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

/**
 * FR-BNK-005 (ADR-098): one bank line settling several invoices at once - a person checks which
 * invoices it paid and types each one's share, and the server (never this component) is the
 * authority on whether they add up. The running total shown here is a live hint only, computed
 * with `Number()` for that purpose alone - the amounts actually SENT are the typed strings
 * (NFR-031), never round-tripped through this sum.
 */
function SplitAllocationPanel({
  administrationId,
  transaction,
  candidates,
  onDone,
}: {
  administrationId: string;
  transaction: BankTransactionView;
  candidates: readonly BankMatchCandidateView[];
  onDone: () => void;
}) {
  const { t, money, language } = useI18n();
  const { bank } = useServices();
  const [selected, setSelected] = useState<Record<string, string>>({});
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const toggle = (candidate: BankMatchCandidateView) => {
    setSelected((current) => {
      const { [candidate.document_id]: existing, ...rest } = current;
      return existing === undefined
        ? { ...current, [candidate.document_id]: candidate.amount }
        : rest;
    });
  };

  const total = Object.values(selected).reduce((sum, value) => sum + (Number(value) || 0), 0);
  const target = Math.abs(Number(transaction.amount));
  // Rounded to cents before comparing: two numbers that are each the sum of 2-decimal amounts
  // never disagree past that precision, so this cannot itself be the source of a false mismatch.
  const balances = Math.round(total * 100) === Math.round(target * 100);
  const canSubmit = Object.keys(selected).length >= 2 && balances;

  const submit = useCallback(async () => {
    setSending(true);
    setProblem(null);
    try {
      await bank.reconcileWithInvoices(
        administrationId,
        transaction.id,
        Object.entries(selected).map(([invoiceId, amount]) => ({
          invoice_id: invoiceId,
          amount: toDecimalInput(amount, language),
        })),
      );
      onDone();
    } catch (error) {
      setProblem(describeError(error));
    } finally {
      setSending(false);
    }
  }, [bank, administrationId, transaction.id, selected, language, onDone]);

  return (
    <div className="bank-split" data-testid={`bank-split-${transaction.id}`}>
      {problem !== null ? <ErrorState message={problem} /> : null}
      <ul className="bank-split__list">
        {candidates.map((candidate) => {
          const checked = candidate.document_id in selected;
          return (
            <li key={candidate.document_id}>
              <label>
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggle(candidate)}
                  data-testid={`bank-split-check-${candidate.document_id}`}
                />
                <CandidateText candidate={candidate} /> —{" "}
                {t("bank.split.open_amount", { amount: money(candidate.amount) })}
              </label>
              {checked ? (
                <input
                  type="text"
                  inputMode="decimal"
                  value={selected[candidate.document_id]}
                  onChange={(event) =>
                    setSelected((current) => ({
                      ...current,
                      [candidate.document_id]: event.target.value,
                    }))
                  }
                  data-testid={`bank-split-amount-${candidate.document_id}`}
                />
              ) : null}
            </li>
          );
        })}
      </ul>
      <p className="caption" data-testid={`bank-split-total-${transaction.id}`}>
        {t("bank.split.total", { total: total.toFixed(2), target: money(transaction.amount) })}
      </p>
      <button
        type="button"
        disabled={sending || !canSubmit}
        onClick={() => void submit()}
        data-testid={`bank-split-submit-${transaction.id}`}
      >
        {t("bank.split.confirm")}
      </button>
    </div>
  );
}
