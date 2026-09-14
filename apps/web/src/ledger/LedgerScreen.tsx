import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type {
  ChartAccountView,
  JournalEntrySummaryView,
  JournalEntryView,
  TrialBalanceView,
} from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { Icon } from "../shell/icons";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { useModalFocus } from "../useModalFocus";

/**
 * `/ledger` — the grootboek (D2: "grootboek" is the word on the screen,
 * "general ledger" the term on demand in the glossary), three views chosen
 * by `?view=`:
 *
 *   trial-balance   `TrialBalanceRow`s for the session's fiscal year.
 *   journal         posted entries, cursor-paged, each opening a sealed
 *                   detail drawer with its lines — `.design/LedgerTable`'s
 *                   "Geboekt — definitief": no input chrome, no delete, and
 *                   the one action a posted entry has is the reversing
 *                   entry FR-GL-003 allows, which is not built yet and is
 *                   said so rather than drawn as a dead button.
 *   chart           the accounts, grouped by type, with their RGS code.
 *
 * Everything here is posted, so no per-row "posted" glyph — the marker
 * column carries EXCEPTIONS only: an entry that reverses another.
 *
 * All amounts are strings from the wire to `money()` (NFR-031).
 */
type View = "trial-balance" | "journal" | "chart";

const VIEWS: readonly View[] = ["trial-balance", "journal", "chart"];

export function LedgerScreen() {
  const { t } = useI18n();
  const [params] = useSearchParams();
  const raw = params.get("view");
  const view: View = VIEWS.find((candidate) => candidate === raw) ?? "trial-balance";
  const { administration, fiscalYear } = useAdministration();

  return (
    <section className="screen" aria-label={t("ledger.title")} data-testid="ledger">
      <PageHeader
        title={t("ledger.title")}
        context={t("ledger.fiscal_year_context", { start: fiscalYear.start_date.slice(0, 4), end: fiscalYear.end_date.slice(0, 4) })}
      />
      <nav className="subnav" aria-label={t("ledger.views_label")}>
        {VIEWS.map((candidate) => (
          <Link
            key={candidate}
            to={`/ledger?view=${candidate}`}
            aria-current={candidate === view ? "page" : undefined}
            data-testid={`ledger-view-${candidate}`}
          >
            {t(`ledger.view.${candidate}`)}
          </Link>
        ))}
      </nav>
      {view === "trial-balance" ? <TrialBalance administrationId={administration.id} fiscalYearId={fiscalYear.id} /> : null}
      {view === "journal" ? <Journal administrationId={administration.id} fiscalYearId={fiscalYear.id} /> : null}
      {view === "chart" ? <Chart administrationId={administration.id} /> : null}
    </section>
  );
}

function TrialBalance({ administrationId, fiscalYearId }: { administrationId: string; fiscalYearId: string }) {
  const { t, money } = useI18n();
  const { ledger } = useServices();
  const [balance, setBalance] = useState<TrialBalanceView | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setBalance(null);
    setProblem(null);
    ledger
      .getTrialBalance(administrationId, fiscalYearId)
      .then((result) => {
        if (!cancelled) setBalance(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [ledger, administrationId, fiscalYearId, attempt]);

  if (problem !== null) return <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)} />;
  if (balance === null) return <LoadingSkeleton rows={6} />;
  if (balance.rows.length === 0) {
    return (
      <EmptyState
        icon={<Icon name="ledger" size={32} />}
        title={t("ledger.trial_balance.empty_title")}
        body={t("ledger.trial_balance.empty_body")}
        action={
          <Link to="/capture" className="button-link button-link--primary">
            {t("common.nav.capture")}
          </Link>
        }
        testId="trial-balance-empty"
      />
    );
  }

  return (
    <div className="panel table-wrap">
      <table className="table" data-testid="trial-balance">
        <thead>
          <tr>
            <th scope="col">{t("ledger.column.account")}</th>
            <th scope="col">{t("ledger.column.name")}</th>
            <th scope="col" className="table__num">{t("ledger.column.debit")}</th>
            <th scope="col" className="table__num">{t("ledger.column.credit")}</th>
            <th scope="col" className="table__num">{t("ledger.column.balance")}</th>
          </tr>
        </thead>
        <tbody>
          {balance.rows.map((row) => (
            <tr key={row.account_id} data-testid="trial-balance-row">
              <td className="ledgr-num">{row.account_code}</td>
              <td>
                {row.account_name}
                <span className="caption"> · {t(`ledger.account_type.${row.account_type}`)}</span>
              </td>
              <td className="table__num">{money(row.total_debit)}</td>
              <td className="table__num">{money(row.total_credit)}</td>
              <td className="table__num">{money(row.balance)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td colSpan={2}>
              {balance.total_debit === balance.total_credit ? (
                <span className="chip chip--positive">{t("ledger.trial_balance.balanced")}</span>
              ) : (
                <span className="chip chip--attention">{t("ledger.trial_balance.unbalanced")}</span>
              )}
            </td>
            <td className="table__num" data-testid="trial-balance-total-debit">{money(balance.total_debit)}</td>
            <td className="table__num" data-testid="trial-balance-total-credit">{money(balance.total_credit)}</td>
            <td />
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

function Journal({ administrationId, fiscalYearId }: { administrationId: string; fiscalYearId: string }) {
  const { t, money, date } = useI18n();
  const { ledger } = useServices();
  const [entries, setEntries] = useState<readonly JournalEntrySummaryView[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setEntries(null);
    setCursor(null);
    setProblem(null);
    ledger
      .listJournalEntries(administrationId, { fiscalYearId, limit: 50 })
      .then((page) => {
        if (cancelled) return;
        setEntries(page.entries);
        setCursor(page.next_cursor);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [ledger, administrationId, fiscalYearId, attempt]);

  const loadMore = useCallback(async () => {
    if (cursor === null) return;
    setLoadingMore(true);
    try {
      const page = await ledger.listJournalEntries(administrationId, { fiscalYearId, cursor, limit: 50 });
      setEntries((current) => [...(current ?? []), ...page.entries]);
      setCursor(page.next_cursor);
    } catch (error) {
      setProblem(describeError(error));
    } finally {
      setLoadingMore(false);
    }
  }, [ledger, administrationId, fiscalYearId, cursor]);

  if (problem !== null && entries === null) return <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)} />;
  if (entries === null) return <LoadingSkeleton rows={6} />;
  if (entries.length === 0) {
    return (
      <EmptyState
        icon={<Icon name="ledger" size={32} />}
        title={t("ledger.journal.empty_title")}
        body={t("ledger.journal.empty_body")}
        testId="journal-empty"
      />
    );
  }

  return (
    <>
      <div className="panel table-wrap">
        <table className="table" data-testid="journal">
          <thead>
            <tr>
              <th scope="col">
                <span className="ledgr-visually-hidden">{t("ledger.column.marker")}</span>
              </th>
              <th scope="col">{t("ledger.column.date")}</th>
              <th scope="col">{t("ledger.column.entry")}</th>
              <th scope="col">{t("ledger.column.description")}</th>
              <th scope="col" className="table__num">{t("ledger.column.amount")}</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr
                key={entry.id}
                data-testid="journal-row"
                className={entry.reverses_entry_id !== null ? "journal__reversal" : undefined}
              >
                <td>
                  {entry.reverses_entry_id !== null ? (
                    <span className="journal__marker" title={t("ledger.journal.reversal")}>
                      <Icon name="reverse" size={16} />
                      <span className="ledgr-visually-hidden">{t("ledger.journal.reversal")}</span>
                    </span>
                  ) : null}
                </td>
                <td className="ledgr-num">{date(entry.entry_date)}</td>
                <td className="ledgr-num table__muted">
                  {t("ledger.journal.entry_number", { journal: entry.journal_code, number: entry.entry_number })}
                </td>
                <td>
                  <button
                    type="button"
                    className="table__row-button"
                    data-testid={`journal-open-${entry.id}`}
                    onClick={() => setOpenId(entry.id)}
                  >
                    {entry.description}
                  </button>
                </td>
                <td className="table__num">{money(entry.total)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {problem !== null ? <ErrorState message={problem} onRetry={() => void loadMore()} /> : null}
      {cursor !== null ? (
        <div className="form__actions">
          <button type="button" disabled={loadingMore} data-testid="journal-more" onClick={() => void loadMore()}>
            {loadingMore ? t("mobile.common.loading") : t("ledger.journal.load_more")}
          </button>
        </div>
      ) : null}
      {openId !== null ? (
        <EntryDrawer administrationId={administrationId} entryId={openId} onClose={() => setOpenId(null)} />
      ) : null}
    </>
  );
}

function EntryDrawer({ administrationId, entryId, onClose }: { administrationId: string; entryId: string; onClose: () => void }) {
  const { t, money, date } = useI18n();
  const { ledger } = useServices();
  const [entry, setEntry] = useState<JournalEntryView | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalFocus(true, dialogRef);

  useEffect(() => {
    let cancelled = false;
    ledger
      .getJournalEntry(administrationId, entryId)
      .then((result) => {
        if (!cancelled) setEntry(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [ledger, administrationId, entryId]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="drawer-backdrop">
      <div ref={dialogRef} className="drawer" role="dialog" aria-modal="true" aria-label={t("ledger.entry.title")} data-testid="entry-drawer">
        <div className="drawer__head">
          <h2>{t("ledger.entry.title")}</h2>
          <button type="button" className="button--quiet" aria-label={t("common.action.close")} data-testid="entry-drawer-close" onClick={onClose}>
            <Icon name="close" size={18} />
          </button>
        </div>
        {problem !== null ? <ErrorState message={problem} /> : null}
        {entry === null && problem === null ? <LoadingSkeleton rows={3} /> : null}
        {entry !== null ? (
          <>
            <p className="meta-line">
              <span className="chip">
                <Icon name="lock" size={14} />
                {t("ledger.entry.posted_final")}
              </span>
              <span className="ledgr-num">{date(entry.entry_date)}</span>
              <span className="ledgr-num">{t("ledger.journal.entry_number", { journal: entry.journal_code, number: entry.entry_number })}</span>
            </p>
            <p>{entry.description}</p>
            {entry.reverses_entry_id !== null ? (
              <p className="alert alert--attention">{t("ledger.journal.reversal")}</p>
            ) : null}
            <div className="table-wrap">
              <table className="table" data-testid="entry-lines">
                <thead>
                  <tr>
                    <th scope="col">{t("ledger.column.account")}</th>
                    <th scope="col">{t("ledger.column.description")}</th>
                    <th scope="col" className="table__num">{t("ledger.column.debit")}</th>
                    <th scope="col" className="table__num">{t("ledger.column.credit")}</th>
                  </tr>
                </thead>
                <tbody>
                  {entry.lines.map((line) => (
                    <tr key={line.id}>
                      <td className="ledgr-num">{line.account_code}</td>
                      <td>{line.description ?? line.account_name}</td>
                      <td className="table__num">{line.debit === "0.00" ? "—" : money(line.debit)}</td>
                      <td className="table__num">{line.credit === "0.00" ? "—" : money(line.credit)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="caption">{t("ledger.entry.immutable_note")}</p>
          </>
        ) : null}
      </div>
    </div>
  );
}

function Chart({ administrationId }: { administrationId: string }) {
  const { t } = useI18n();
  const { ledger } = useServices();
  const [accounts, setAccounts] = useState<readonly ChartAccountView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setAccounts(null);
    setProblem(null);
    ledger
      .listChartOfAccounts(administrationId)
      .then((result) => {
        if (!cancelled) setAccounts(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [ledger, administrationId, attempt]);

  if (problem !== null) return <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)} />;
  if (accounts === null) return <LoadingSkeleton rows={8} />;
  if (accounts.length === 0) {
    return <EmptyState title={t("ledger.chart.empty_title")} body={t("ledger.chart.empty_body")} testId="chart-empty" />;
  }

  return (
    <div className="panel table-wrap">
      <table className="table" data-testid="chart">
        <thead>
          <tr>
            <th scope="col">{t("ledger.column.account")}</th>
            <th scope="col">{t("ledger.column.name")}</th>
            <th scope="col">{t("ledger.column.type")}</th>
            <th scope="col">{t("ledger.column.rgs")}</th>
          </tr>
        </thead>
        <tbody>
          {accounts.map((account) => (
            <tr key={account.id} data-testid="chart-row">
              <td className="ledgr-num">{account.code}</td>
              <td>
                {account.name}
                {account.status === "blocked" ? <span className="chip"> {t("ledger.chart.blocked")}</span> : null}
              </td>
              <td>{t(`ledger.account_type.${account.account_type}`)}</td>
              <td className="ledgr-num table__muted">{account.rgs_code ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
