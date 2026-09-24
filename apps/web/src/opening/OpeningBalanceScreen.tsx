import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, Upload } from "lucide-react";
import { useI18n } from "@ledgr/i18n";
import type { OpeningBalanceView, OpeningEntryView } from "@ledgr/shared-types";

import "./Opening.css";
import { describeError } from "../api/http";
import { sumDecimals } from "../ledger/api";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { useMoney } from "../ui";
import { toDecimalInput } from "../ui/decimal";
import { readTrialBalance } from "./csv";

/**
 * `/ledger/opening-balance` — the balance sheet a business brings in (ADR-088).
 *
 * Every balance-sheet account the chart has, with a debit and a credit box, and the running
 * difference. A trial balance exported from the previous package fills the boxes by account code.
 * Whatever does not balance can go to an equity account the person chooses; nothing is posted
 * until the figures are theirs. Once posted it is shown, not edited: the ledger is append-only,
 * and redoing it is a reversal in the journal.
 */
const EXACT = /^\d+(\.\d{1,2})?$/;

type Amounts = Record<string, { debit: string; credit: string }>;

function exact(typed: string, language: string): string | null {
  const value = toDecimalInput(typed, language);
  if (value === "") return "0";
  return EXACT.test(value) ? value : null;
}

export function OpeningBalanceScreen() {
  const { t, date, language } = useI18n();
  const money = useMoney();
  const { administration, fiscalYear } = useAdministration();
  const { opening } = useServices();
  const [view, setView] = useState<OpeningBalanceView | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [amounts, setAmounts] = useState<Amounts>({});
  const [balanceAccount, setBalanceAccount] = useState<string>("");
  const [importNote, setImportNote] = useState<string | null>(null);
  const [posting, setPosting] = useState(false);
  const [postProblem, setPostProblem] = useState<string | null>(null);
  const [posted, setPosted] = useState<OpeningEntryView | null>(null);

  useEffect(() => {
    let cancelled = false;
    setView(null);
    setProblem(null);
    opening
      .getOpeningBalance(administration.id, fiscalYear.id)
      .then((result) => {
        if (cancelled) return;
        setView(result);
        // Undistributed result, else capital: where a Dutch bookkeeper parks a difference.
        const preferred =
          result.balance_accounts.find((a) => a.code === "0530") ?? result.balance_accounts[0];
        setBalanceAccount(preferred?.id ?? "");
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [opening, administration.id, fiscalYear.id, attempt]);

  const totals = useMemo(() => {
    const debits: string[] = [];
    const credits: string[] = [];
    let invalid = false;
    for (const { debit, credit } of Object.values(amounts)) {
      const d = exact(debit, language);
      const c = exact(credit, language);
      if (d === null || c === null) invalid = true;
      else {
        debits.push(d);
        credits.push(c);
      }
    }
    const debit = sumDecimals(debits);
    const credit = sumDecimals(credits);
    return { debit, credit, difference: sumDecimals([debit, `-${credit}`]), invalid };
  }, [amounts, language]);

  const entered = Object.values(amounts).some(
    ({ debit, credit }) => debit.trim() !== "" || credit.trim() !== "",
  );
  const unbalanced = totals.difference !== "0.00";

  const set = (accountId: string, side: "debit" | "credit", value: string) =>
    setAmounts((current) => ({
      ...current,
      [accountId]: {
        debit: side === "debit" ? value : (current[accountId]?.debit ?? ""),
        credit: side === "credit" ? value : (current[accountId]?.credit ?? ""),
      },
    }));

  const importFile = async (file: File) => {
    if (view === null) return;
    const parsed = readTrialBalance(await file.text(), language);
    if (parsed === null) {
      setImportNote(t("ledger.opening.import_unrecognised"));
      return;
    }
    const byCode = new Map(view.accounts.map((account) => [account.code, account.id]));
    const next: Amounts = {};
    const missing: string[] = [];
    for (const line of parsed.lines) {
      const id = byCode.get(line.code);
      if (id === undefined) {
        if (line.debit !== "0" || line.credit !== "0") missing.push(line.code);
        continue;
      }
      next[id] = {
        debit: line.debit === "0" ? "" : line.debit,
        credit: line.credit === "0" ? "" : line.credit,
      };
    }
    setAmounts(next);
    setImportNote(
      [
        t("ledger.opening.import_read", { count: Object.keys(next).length }),
        missing.length > 0 ? t("ledger.opening.import_missing", { codes: missing.join(", ") }) : "",
        parsed.unreadable.length > 0
          ? t("ledger.opening.import_unreadable", { codes: parsed.unreadable.join(", ") })
          : "",
      ]
        .filter(Boolean)
        .join(" "),
    );
  };

  const post = async () => {
    setPosting(true);
    setPostProblem(null);
    try {
      const entry = await opening.postOpeningBalance(administration.id, {
        fiscal_year_id: fiscalYear.id,
        lines: Object.entries(amounts).map(([account_id, value]) => ({
          account_id,
          debit: exact(value.debit, language) ?? value.debit,
          credit: exact(value.credit, language) ?? value.credit,
        })),
        balance_account_id: unbalanced ? balanceAccount || null : null,
      });
      setPosted(entry);
    } catch (error) {
      setPostProblem(describeError(error));
    } finally {
      setPosting(false);
    }
  };

  const back = (
    <Link to="/ledger" className="button-link button-link--quiet opening__back">
      <ArrowLeft size={16} strokeWidth={1.8} aria-hidden="true" /> {t("ledger.title")}
    </Link>
  );

  if (problem !== null) {
    return (
      <section className="screen opening" data-testid="opening-balance">
        {back}
        <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)} />
      </section>
    );
  }
  if (view === null) {
    return (
      <section className="screen opening" data-testid="opening-balance">
        <LoadingSkeleton rows={6} />
      </section>
    );
  }

  const shown = posted ?? view.posted;
  return (
    <section
      className="screen opening"
      aria-label={t("ledger.opening.title")}
      data-testid="opening-balance"
    >
      {back}
      <PageHeader
        title={t("ledger.opening.title")}
        context={t("ledger.opening.as_of", { date: date(view.entry_date) })}
      />

      {shown !== null ? (
        <>
          <p className="alert alert--positive" role="status" data-testid="opening-posted">
            {t("ledger.opening.posted", { number: shown.entry_number })}{" "}
            {t("ledger.opening.posted_hint")}{" "}
            <Link to="/journal">{t("ledger.opening.to_journal")}</Link>
          </p>
          <div className="panel table-wrap">
            <table className="table" data-testid="opening-posted-lines">
              <thead>
                <tr>
                  <th scope="col">{t("ledger.column.account")}</th>
                  <th scope="col">{t("ledger.column.name")}</th>
                  <th scope="col" className="table__num">
                    {t("ledger.opening.debit")}
                  </th>
                  <th scope="col" className="table__num">
                    {t("ledger.opening.credit")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {shown.lines.map((line) => (
                  <tr key={line.account_id}>
                    <td className="ledgr-num">{line.account_code}</td>
                    <td>{line.account_name}</td>
                    <td className="table__num">{line.debit !== "0.00" ? money(line.debit) : ""}</td>
                    <td className="table__num">
                      {line.credit !== "0.00" ? money(line.credit) : ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <>
          <p className="opening__intro">{t("ledger.opening.intro")}</p>
          <div className="opening__import">
            <label className="button-link">
              <Upload size={16} strokeWidth={1.8} aria-hidden="true" /> {t("ledger.opening.import")}
              <input
                type="file"
                accept=".csv,.txt,text/csv"
                className="ledgr-visually-hidden"
                data-testid="opening-import"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) void importFile(file);
                  event.target.value = "";
                }}
              />
            </label>
            <span className="caption">{t("ledger.opening.import_hint")}</span>
          </div>
          {importNote !== null ? (
            <p className="alert alert--caution" role="status" data-testid="opening-import-note">
              {importNote}
            </p>
          ) : null}

          <div className="panel table-wrap">
            <table className="table opening__table" data-testid="opening-form">
              <thead>
                <tr>
                  <th scope="col">{t("ledger.column.account")}</th>
                  <th scope="col">{t("ledger.column.name")}</th>
                  <th scope="col" className="table__num">
                    {t("ledger.opening.debit")}
                  </th>
                  <th scope="col" className="table__num">
                    {t("ledger.opening.credit")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {view.accounts.map((account) => (
                  <tr key={account.id}>
                    <td className="ledgr-num">{account.code}</td>
                    <td>{account.name}</td>
                    {(["debit", "credit"] as const).map((side) => {
                      const value = amounts[account.id]?.[side] ?? "";
                      const invalid = value.trim() !== "" && exact(value, language) === null;
                      return (
                        <td key={side} className="table__num">
                          <input
                            type="text"
                            inputMode="decimal"
                            className="opening__amount"
                            aria-label={`${account.code} ${t(`ledger.opening.${side}`)}`}
                            aria-invalid={invalid}
                            value={value}
                            data-testid={`opening-${side}-${account.code}`}
                            onChange={(event) => set(account.id, side, event.target.value)}
                          />
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <td colSpan={2}>
                    <strong>{t("ledger.opening.total")}</strong>
                  </td>
                  <td className="table__num">
                    <strong>{money(totals.debit)}</strong>
                  </td>
                  <td className="table__num">
                    <strong>{money(totals.credit)}</strong>
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>

          <div className="panel opening__finish">
            {unbalanced ? (
              <div className="form__field">
                <label htmlFor="opening-balance-account">
                  {t("ledger.opening.difference", {
                    amount: money(totals.difference.replace(/^-/, "")),
                  })}
                </label>
                <select
                  id="opening-balance-account"
                  value={balanceAccount}
                  data-testid="opening-balance-account"
                  onChange={(event) => setBalanceAccount(event.target.value)}
                >
                  {view.balance_accounts.map((account) => (
                    <option key={account.id} value={account.id}>
                      {account.code} · {account.name}
                    </option>
                  ))}
                </select>
              </div>
            ) : entered ? (
              <p className="opening__balanced">{t("ledger.opening.balanced")}</p>
            ) : null}
            {totals.invalid ? (
              <p className="alert alert--attention">{t("ledger.opening.invalid")}</p>
            ) : null}
            {postProblem !== null ? (
              <p className="alert alert--attention" role="alert" data-testid="opening-problem">
                {postProblem}
              </p>
            ) : null}
            <button
              type="button"
              className="button--primary"
              disabled={!entered || totals.invalid || posting}
              data-testid="opening-submit"
              onClick={() => void post()}
            >
              {t("ledger.opening.submit")}
            </button>
          </div>
        </>
      )}
    </section>
  );
}
