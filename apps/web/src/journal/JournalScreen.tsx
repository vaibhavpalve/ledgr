import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type {
  ChartAccountView,
  JournalDefView,
  PeriodView,
  PostedJournalEntryView,
} from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { Icon } from "../shell/icons";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { sumDecimals } from "../ledger/api";
import { toDecimalInput } from "../ui/decimal";

/**
 * `/journal` — "Memoriaal": FR-GL-001/FR-GL-004's manual entry, FR-GL-007's
 * period lock/unlock. The Grootboek screen (`/ledger`) shows what has been
 * posted; this is where a bookkeeper posts something by hand.
 *
 * Two tabs, the same `?view=` pattern `/ledger` uses.
 */
type View = "new" | "periods";
const VIEWS: readonly View[] = ["new", "periods"];

export function JournalScreen() {
  const { t } = useI18n();
  const [params] = useSearchParams();
  const raw = params.get("view");
  const view: View = VIEWS.find((candidate) => candidate === raw) ?? "new";
  const { administration, fiscalYear } = useAdministration();

  return (
    <section className="screen" aria-label={t("journal.title")} data-testid="journal-screen">
      <PageHeader
        title={t("journal.title")}
        context={t("ledger.fiscal_year_context", {
          start: fiscalYear.start_date.slice(0, 4),
          end: fiscalYear.end_date.slice(0, 4),
        })}
      />
      <nav className="subnav" aria-label={t("journal.title")}>
        {VIEWS.map((candidate) => (
          <Link
            key={candidate}
            to={`/journal?view=${candidate}`}
            aria-current={candidate === view ? "page" : undefined}
            data-testid={`journal-view-${candidate}`}
          >
            {t(`journal.tab.${candidate}`)}
          </Link>
        ))}
      </nav>
      {view === "new" ? (
        <NewEntryForm administrationId={administration.id} fiscalYearId={fiscalYear.id} />
      ) : null}
      {view === "periods" ? (
        <Periods administrationId={administration.id} fiscalYearId={fiscalYear.id} />
      ) : null}
    </section>
  );
}

interface DraftLine {
  readonly key: string;
  readonly accountId: string;
  readonly debit: string;
  readonly credit: string;
  readonly description: string;
}

function emptyLine(): DraftLine {
  return { key: crypto.randomUUID(), accountId: "", debit: "", credit: "", description: "" };
}

function updateLine(
  lines: readonly DraftLine[],
  index: number,
  patch: Partial<DraftLine>,
): DraftLine[] {
  return lines.map((line, i) => (i === index ? { ...line, ...patch } : line));
}

function totalOf(lines: readonly DraftLine[], side: "debit" | "credit"): string {
  // Typed as a person writes it ("1.250,00"); summed as the decimal it is.
  return sumDecimals(
    lines.map((line) => (line[side].trim() === "" ? "0" : toDecimalInput(line[side]))),
  );
}

function NewEntryForm({
  administrationId,
  fiscalYearId,
}: {
  administrationId: string;
  fiscalYearId: string;
}) {
  const { t, money } = useI18n();
  const { journal, ledger } = useServices();

  const [journals, setJournals] = useState<readonly JournalDefView[] | null>(null);
  const [periods, setPeriods] = useState<readonly PeriodView[] | null>(null);
  const [accounts, setAccounts] = useState<readonly ChartAccountView[] | null>(null);
  const [loadProblem, setLoadProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setJournals(null);
    setPeriods(null);
    setAccounts(null);
    setLoadProblem(null);
    Promise.all([
      journal.listJournals(administrationId),
      journal.listPeriods(administrationId, fiscalYearId),
      ledger.listChartOfAccounts(administrationId),
    ])
      .then(([j, p, a]) => {
        if (cancelled) return;
        setJournals(j.filter((entry) => entry.status === "active"));
        setPeriods(p);
        setAccounts(a.filter((account) => account.status === "active"));
      })
      .catch((error: unknown) => {
        if (!cancelled) setLoadProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [journal, ledger, administrationId, fiscalYearId, attempt]);

  if (loadProblem !== null)
    return <ErrorState message={loadProblem} onRetry={() => setAttempt((n) => n + 1)} />;
  if (journals === null || periods === null || accounts === null)
    return <LoadingSkeleton rows={6} />;

  const memorialJournals = journals.filter((j) => j.journal_type === "memorial");
  if (memorialJournals.length === 0) {
    return (
      <EmptyState
        icon={<Icon name="ledger" size={32} />}
        title={t("journal.form.no_journal_title")}
        body={t("journal.form.no_journal_body")}
        testId="journal-no-journal"
      />
    );
  }

  const openPeriods = periods.filter((p) => p.status === "open");

  return (
    <EntryForm
      administrationId={administrationId}
      journals={memorialJournals}
      periods={openPeriods}
      accounts={accounts}
      money={money}
    />
  );
}

function EntryForm({
  administrationId,
  journals,
  periods,
  accounts,
  money,
}: {
  administrationId: string;
  journals: readonly JournalDefView[];
  periods: readonly PeriodView[];
  accounts: readonly ChartAccountView[];
  money: (value: string) => string;
}) {
  const { t, language } = useI18n();
  const { journal } = useServices();

  const [journalId, setJournalId] = useState(journals[0]?.id ?? "");
  const [periodId, setPeriodId] = useState(periods[0]?.id ?? "");
  const [entryDate, setEntryDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [description, setDescription] = useState("");
  const [documentReference, setDocumentReference] = useState("");
  const [lines, setLines] = useState<DraftLine[]>([emptyLine(), emptyLine()]);
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [posted, setPosted] = useState<PostedJournalEntryView | null>(null);

  const totalDebit = useMemo(() => totalOf(lines, "debit"), [lines]);
  const totalCredit = useMemo(() => totalOf(lines, "credit"), [lines]);
  const balanced =
    totalDebit === totalCredit && totalDebit !== "0.00" && lines.every((l) => l.accountId !== "");

  const reset = useCallback(() => {
    setDescription("");
    setDocumentReference("");
    setLines([emptyLine(), emptyLine()]);
    setPosted(null);
    setProblem(null);
  }, []);

  const submit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setSending(true);
      setProblem(null);
      try {
        const result = await journal.postEntry(administrationId, {
          journalId,
          periodId,
          entryDate,
          description,
          documentReference: documentReference.trim() === "" ? null : documentReference,
          lines: lines.map((line) => ({
            accountId: line.accountId,
            debit: line.debit.trim() === "" ? "0.00" : toDecimalInput(line.debit, language),
            credit: line.credit.trim() === "" ? "0.00" : toDecimalInput(line.credit, language),
            description: line.description.trim() === "" ? null : line.description,
          })),
        });
        setPosted(result);
      } catch (error) {
        setProblem(describeError(error));
      } finally {
        setSending(false);
      }
    },
    [
      journal,
      administrationId,
      journalId,
      periodId,
      entryDate,
      description,
      documentReference,
      language,
      lines,
    ],
  );

  if (periods.length === 0) {
    return (
      <EmptyState
        icon={<Icon name="lock" size={32} />}
        title={t("journal.periods.empty_title")}
        body={t("journal.periods.empty_body")}
        testId="journal-no-open-period"
      />
    );
  }

  if (posted !== null) {
    const postedJournal = journals.find((j) => j.id === posted.journal_id);
    return (
      <div className="panel" data-testid="journal-posted">
        <EmptyState
          icon={<Icon name="check" size={32} />}
          title={t("journal.form.posted_title")}
          body={t("journal.form.posted_body", {
            journal: postedJournal?.code ?? "",
            number: posted.entry_number,
          })}
          action={
            <div className="form__actions">
              <button type="button" onClick={reset} data-testid="journal-post-another">
                {t("journal.form.post_another")}
              </button>
              <Link to="/ledger?view=journal" className="button-link">
                {t("journal.form.view_in_ledger")}
              </Link>
            </div>
          }
        />
      </div>
    );
  }

  return (
    <form
      className="panel form"
      onSubmit={(event) => void submit(event)}
      data-testid="journal-form"
    >
      {problem !== null ? <ErrorState message={problem} /> : null}
      <div className="form__row">
        <label className="form__field">
          <span>{t("journal.form.journal_label")}</span>
          <select
            value={journalId}
            onChange={(event) => setJournalId(event.target.value)}
            data-testid="journal-field-journal"
          >
            {journals.map((j) => (
              <option key={j.id} value={j.id}>
                {j.code} — {j.name}
              </option>
            ))}
          </select>
        </label>
        <label className="form__field">
          <span>{t("journal.form.period_label")}</span>
          <select
            value={periodId}
            onChange={(event) => setPeriodId(event.target.value)}
            data-testid="journal-field-period"
          >
            {periods.map((p) => (
              <option key={p.id} value={p.id}>
                {p.start_date.slice(0, 7)}
              </option>
            ))}
          </select>
        </label>
        <label className="form__field">
          <span>{t("journal.form.date_label")}</span>
          <input
            type="date"
            value={entryDate}
            onChange={(event) => setEntryDate(event.target.value)}
            data-testid="journal-field-date"
          />
        </label>
      </div>
      <div className="form__row">
        <label className="form__field form__field--grow">
          <span>{t("journal.form.description_label")}</span>
          <input
            type="text"
            required
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            data-testid="journal-field-description"
          />
        </label>
        <label className="form__field">
          <span>{t("journal.form.reference_label")}</span>
          <input
            type="text"
            value={documentReference}
            onChange={(event) => setDocumentReference(event.target.value)}
            data-testid="journal-field-reference"
          />
        </label>
      </div>

      <div className="table-wrap">
        <table className="table" data-testid="journal-lines">
          <thead>
            <tr>
              <th scope="col">{t("journal.form.line.account_label")}</th>
              <th scope="col">{t("journal.form.line.description_label")}</th>
              <th scope="col" className="table__num">
                {t("journal.form.line.debit_label")}
              </th>
              <th scope="col" className="table__num">
                {t("journal.form.line.credit_label")}
              </th>
              <th scope="col">
                <span className="ledgr-visually-hidden">{t("journal.form.line.remove")}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {lines.map((line, index) => (
              <tr key={line.key}>
                <td>
                  <select
                    value={line.accountId}
                    onChange={(event) =>
                      setLines((current) =>
                        updateLine(current, index, { accountId: event.target.value }),
                      )
                    }
                    data-testid={`journal-line-account-${index}`}
                  >
                    <option value="" />
                    {accounts.map((account) => (
                      <option key={account.id} value={account.id}>
                        {account.code} — {account.name}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <input
                    type="text"
                    value={line.description}
                    onChange={(event) =>
                      setLines((current) =>
                        updateLine(current, index, { description: event.target.value }),
                      )
                    }
                    data-testid={`journal-line-description-${index}`}
                  />
                </td>
                <td>
                  <input
                    type="text"
                    inputMode="decimal"
                    value={line.debit}
                    onChange={(event) =>
                      setLines((current) =>
                        updateLine(current, index, { debit: event.target.value, credit: "" }),
                      )
                    }
                    data-testid={`journal-line-debit-${index}`}
                  />
                </td>
                <td>
                  <input
                    type="text"
                    inputMode="decimal"
                    value={line.credit}
                    onChange={(event) =>
                      setLines((current) =>
                        updateLine(current, index, { credit: event.target.value, debit: "" }),
                      )
                    }
                    data-testid={`journal-line-credit-${index}`}
                  />
                </td>
                <td>
                  {lines.length > 2 ? (
                    <button
                      type="button"
                      className="button--quiet"
                      aria-label={t("journal.form.line.remove")}
                      onClick={() => setLines((current) => current.filter((_, i) => i !== index))}
                      data-testid={`journal-line-remove-${index}`}
                    >
                      <Icon name="close" size={16} />
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td colSpan={2}>
                <button
                  type="button"
                  className="button--quiet"
                  onClick={() => setLines((current) => [...current, emptyLine()])}
                  data-testid="journal-add-line"
                >
                  <Icon name="plus" size={16} /> {t("journal.form.add_line")}
                </button>
              </td>
              <td className="table__num" data-testid="journal-total-debit">
                {money(totalDebit)}
              </td>
              <td className="table__num" data-testid="journal-total-credit">
                {money(totalCredit)}
              </td>
              <td />
            </tr>
          </tfoot>
        </table>
      </div>

      <p aria-live="polite">
        {balanced ? (
          <span className="chip chip--positive">{t("journal.form.balanced")}</span>
        ) : (
          <span className="chip chip--attention">{t("journal.form.unbalanced")}</span>
        )}
      </p>

      <div className="form__actions">
        <button type="submit" disabled={sending || !balanced} data-testid="journal-submit">
          {sending ? t("journal.form.submitting") : t("journal.form.submit")}
        </button>
      </div>
    </form>
  );
}

function Periods({
  administrationId,
  fiscalYearId,
}: {
  administrationId: string;
  fiscalYearId: string;
}) {
  const { t, date } = useI18n();
  const { journal } = useServices();
  const [periods, setPeriods] = useState<readonly PeriodView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [unlocking, setUnlocking] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPeriods(null);
    setProblem(null);
    journal
      .listPeriods(administrationId, fiscalYearId)
      .then((result) => {
        if (!cancelled) setPeriods(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [journal, administrationId, fiscalYearId, attempt]);

  const lock = useCallback(
    async (periodId: string) => {
      setBusy(periodId);
      try {
        await journal.lockPeriod(administrationId, periodId);
        setAttempt((n) => n + 1);
      } catch (error) {
        setProblem(describeError(error));
      } finally {
        setBusy(null);
      }
    },
    [journal, administrationId],
  );

  const unlock = useCallback(
    async (periodId: string) => {
      if (reason.trim() === "") return;
      setBusy(periodId);
      try {
        await journal.unlockPeriod(administrationId, periodId, reason.trim());
        setUnlocking(null);
        setReason("");
        setAttempt((n) => n + 1);
      } catch (error) {
        setProblem(describeError(error));
      } finally {
        setBusy(null);
      }
    },
    [journal, administrationId, reason],
  );

  if (problem !== null && periods === null)
    return <ErrorState message={problem} onRetry={() => setAttempt((n) => n + 1)} />;
  if (periods === null) return <LoadingSkeleton rows={6} />;
  if (periods.length === 0) {
    return (
      <EmptyState
        title={t("journal.periods.empty_title")}
        body={t("journal.periods.empty_body")}
        testId="journal-periods-empty"
      />
    );
  }

  return (
    <div className="panel table-wrap">
      {problem !== null ? <ErrorState message={problem} /> : null}
      <table className="table" data-testid="journal-periods">
        <thead>
          <tr>
            <th scope="col">{t("journal.periods.column.period")}</th>
            <th scope="col">{t("journal.periods.column.range")}</th>
            <th scope="col">{t("journal.periods.column.status")}</th>
            <th scope="col" />
          </tr>
        </thead>
        <tbody>
          {periods.map((period) => (
            <tr key={period.id} data-testid="journal-period-row">
              <td className="ledgr-num">{period.period_number}</td>
              <td className="ledgr-num">
                {date(period.start_date)} – {date(period.end_date)}
              </td>
              <td>{t(`journal.periods.status.${period.status}`)}</td>
              <td>
                {period.status === "open" ? (
                  <button
                    type="button"
                    disabled={busy === period.id}
                    onClick={() => void lock(period.id)}
                    data-testid={`journal-period-lock-${period.id}`}
                  >
                    <Icon name="lock" size={14} /> {t("journal.periods.lock")}
                  </button>
                ) : null}
                {period.status === "locked" ? (
                  unlocking === period.id ? (
                    <span className="form__row">
                      <input
                        type="text"
                        value={reason}
                        placeholder={t("journal.periods.unlock_reason_label")}
                        onChange={(event) => setReason(event.target.value)}
                        data-testid={`journal-period-unlock-reason-${period.id}`}
                      />
                      <button
                        type="button"
                        disabled={busy === period.id || reason.trim() === ""}
                        onClick={() => void unlock(period.id)}
                        data-testid={`journal-period-unlock-confirm-${period.id}`}
                      >
                        {t("journal.periods.unlock_confirm")}
                      </button>
                      <button
                        type="button"
                        className="button--quiet"
                        onClick={() => {
                          setUnlocking(null);
                          setReason("");
                        }}
                      >
                        {t("journal.periods.unlock_cancel")}
                      </button>
                    </span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setUnlocking(period.id)}
                      data-testid={`journal-period-unlock-${period.id}`}
                    >
                      {t("journal.periods.unlock")}
                    </button>
                  )
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
