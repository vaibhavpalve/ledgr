import { useCallback, useEffect, useRef, useState } from "react";
import { useI18n } from "@ledgr/i18n";
import type {
  ChartAccountView,
  DepreciationRunView,
  FixedAssetView,
  PeriodView,
} from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { sumDecimals } from "../ledger/api";
import { useAdministration } from "../session/SessionProvider";
import { useServices } from "../session/ServicesProvider";
import { Icon } from "../shell/icons";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { useModalFocus } from "../useModalFocus";

/**
 * `/assets` — "Activa (MVA)": PRD §13's fixed-asset register, ungraded by
 * Appendix A. A list of assets, a create form, and a per-asset drawer where
 * depreciation is posted (one period at a time, `api.assets.service.
 * next_depreciation_amount`'s straight-line charge) and an asset is disposed
 * of (a real double-entry posting, not a status flip - see the drawer).
 */
export function AssetsScreen() {
  const { t } = useI18n();
  const { administration, fiscalYear } = useAdministration();
  const { assets, ledger } = useServices();

  const [items, setItems] = useState<readonly FixedAssetView[] | null>(null);
  const [accounts, setAccounts] = useState<readonly ChartAccountView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [creating, setCreating] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;
    setItems(null);
    setProblem(null);
    Promise.all([assets.listAssets(administration.id), ledger.listChartOfAccounts(administration.id)])
      .then(([a, c]) => {
        if (cancelled) return;
        setItems(a);
        setAccounts(c.filter((account) => account.status === "active"));
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [assets, ledger, administration.id, attempt]);

  return (
    <section className="screen" aria-label={t("assets.title")} data-testid="assets-screen">
      <PageHeader
        title={t("assets.title")}
        action={
          items !== null && accounts !== null ? (
            <button type="button" onClick={() => setCreating(true)} data-testid="assets-new">
              <Icon name="plus" size={16} /> {t("assets.new")}
            </button>
          ) : undefined
        }
      />
      {problem !== null ? <ErrorState message={problem} onRetry={reload} /> : null}
      {items === null && problem === null ? <LoadingSkeleton rows={6} /> : null}
      {items !== null && items.length === 0 ? (
        <EmptyState
          icon={<Icon name="ledger" size={32} />}
          title={t("assets.empty_title")}
          body={t("assets.empty_body")}
          testId="assets-empty"
        />
      ) : null}
      {items !== null && items.length > 0 ? (
        <div className="panel table-wrap">
          <table className="table" data-testid="assets-list">
            <thead>
              <tr>
                <th scope="col">{t("assets.column.name")}</th>
                <th scope="col">{t("assets.column.category")}</th>
                <th scope="col">{t("assets.column.acquired")}</th>
                <th scope="col" className="table__num">
                  {t("assets.column.cost")}
                </th>
                <th scope="col">{t("assets.column.status")}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((asset) => (
                <AssetRow key={asset.id} asset={asset} onOpen={() => setOpenId(asset.id)} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {creating && accounts !== null ? (
        <CreateAssetForm
          administrationId={administration.id}
          accounts={accounts}
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            reload();
          }}
        />
      ) : null}

      {openId !== null && accounts !== null ? (
        <AssetDrawer
          administrationId={administration.id}
          fiscalYearId={fiscalYear.id}
          assetId={openId}
          accounts={accounts}
          onClose={() => setOpenId(null)}
          onChanged={reload}
        />
      ) : null}
    </section>
  );
}

function AssetRow({ asset, onOpen }: { asset: FixedAssetView; onOpen: () => void }) {
  const { t, money, date } = useI18n();
  return (
    <tr data-testid="asset-row">
      <td>
        <button
          type="button"
          className="table__row-button"
          onClick={onOpen}
          data-testid={`asset-open-${asset.id}`}
        >
          {asset.name}
        </button>
      </td>
      <td>{asset.category ?? "—"}</td>
      <td className="ledgr-num">{date(asset.acquisition_date)}</td>
      <td className="table__num">{money(asset.acquisition_cost)}</td>
      <td>
        {asset.status === "disposed" ? (
          <span className="chip">{t("assets.status.disposed")}</span>
        ) : (
          <span className="chip chip--positive">{t("assets.status.active")}</span>
        )}
      </td>
    </tr>
  );
}

function CreateAssetForm({
  administrationId,
  accounts,
  onClose,
  onCreated,
}: {
  administrationId: string;
  accounts: readonly ChartAccountView[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const { t } = useI18n();
  const { assets } = useServices();
  const assetAccounts = accounts.filter((a) => a.account_type === "asset");
  const expenseAccounts = accounts.filter((a) => a.account_type === "expense");

  const [name, setName] = useState("");
  const [category, setCategory] = useState("");
  const [acquisitionDate, setAcquisitionDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [cost, setCost] = useState("");
  const [residual, setResidual] = useState("0");
  const [usefulLife, setUsefulLife] = useState("60");
  const [assetAccountId, setAssetAccountId] = useState(assetAccounts[0]?.id ?? "");
  const [accumulatedAccountId, setAccumulatedAccountId] = useState(assetAccounts[0]?.id ?? "");
  const [expenseAccountId, setExpenseAccountId] = useState(expenseAccounts[0]?.id ?? "");
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalFocus(true, dialogRef);

  const submit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setSending(true);
      setProblem(null);
      try {
        await assets.createAsset(administrationId, {
          name,
          category: category.trim() === "" ? null : category,
          acquisitionDate,
          acquisitionCost: cost,
          residualValue: residual === "" ? "0" : residual,
          usefulLifeMonths: Number(usefulLife),
          assetAccountId,
          depreciationExpenseAccountId: expenseAccountId,
          accumulatedDepreciationAccountId: accumulatedAccountId,
        });
        onCreated();
      } catch (error) {
        setProblem(describeError(error));
      } finally {
        setSending(false);
      }
    },
    [
      assets,
      administrationId,
      name,
      category,
      acquisitionDate,
      cost,
      residual,
      usefulLife,
      assetAccountId,
      expenseAccountId,
      accumulatedAccountId,
      onCreated,
    ],
  );

  return (
    <div className="drawer-backdrop">
      <div
        ref={dialogRef}
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={t("assets.new")}
        data-testid="asset-create-form"
      >
        <div className="drawer__head">
          <h2>{t("assets.new")}</h2>
          <button
            type="button"
            className="button--quiet"
            aria-label={t("common.action.close")}
            onClick={onClose}
          >
            <Icon name="close" size={18} />
          </button>
        </div>
        <form className="form" onSubmit={(event) => void submit(event)}>
          {problem !== null ? <ErrorState message={problem} /> : null}
          <label className="form__field">
            <span>{t("assets.field.name")}</span>
            <input
              type="text"
              required
              value={name}
              onChange={(event) => setName(event.target.value)}
              data-testid="asset-field-name"
            />
          </label>
          <label className="form__field">
            <span>{t("assets.field.category")}</span>
            <input type="text" value={category} onChange={(event) => setCategory(event.target.value)} />
          </label>
          <div className="form__row">
            <label className="form__field">
              <span>{t("assets.field.acquisition_date")}</span>
              <input
                type="date"
                value={acquisitionDate}
                onChange={(event) => setAcquisitionDate(event.target.value)}
              />
            </label>
            <label className="form__field">
              <span>{t("assets.field.cost")}</span>
              <input
                type="text"
                inputMode="decimal"
                required
                value={cost}
                onChange={(event) => setCost(event.target.value)}
                data-testid="asset-field-cost"
              />
            </label>
            <label className="form__field">
              <span>{t("assets.field.residual_value")}</span>
              <input
                type="text"
                inputMode="decimal"
                value={residual}
                onChange={(event) => setResidual(event.target.value)}
              />
            </label>
            <label className="form__field">
              <span>{t("assets.field.useful_life_months")}</span>
              <input
                type="number"
                min={1}
                required
                value={usefulLife}
                onChange={(event) => setUsefulLife(event.target.value)}
              />
            </label>
          </div>
          <div className="form__row">
            <label className="form__field">
              <span>{t("assets.field.asset_account")}</span>
              <select
                value={assetAccountId}
                onChange={(event) => setAssetAccountId(event.target.value)}
                data-testid="asset-field-asset-account"
              >
                {assetAccounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.code} — {account.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="form__field">
              <span>{t("assets.field.accumulated_account")}</span>
              <select
                value={accumulatedAccountId}
                onChange={(event) => setAccumulatedAccountId(event.target.value)}
              >
                {assetAccounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.code} — {account.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="form__field">
              <span>{t("assets.field.expense_account")}</span>
              <select
                value={expenseAccountId}
                onChange={(event) => setExpenseAccountId(event.target.value)}
              >
                {expenseAccounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.code} — {account.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="form__actions">
            <button type="submit" disabled={sending} data-testid="asset-create-submit">
              {sending ? t("assets.form.saving") : t("assets.form.save")}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function AssetDrawer({
  administrationId,
  fiscalYearId,
  assetId,
  accounts,
  onClose,
  onChanged,
}: {
  administrationId: string;
  fiscalYearId: string;
  assetId: string;
  accounts: readonly ChartAccountView[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const { t, money, date } = useI18n();
  const { assets } = useServices();
  const [asset, setAsset] = useState<FixedAssetView | null>(null);
  const [runs, setRuns] = useState<readonly DepreciationRunView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [depreciating, setDepreciating] = useState(false);
  const [disposing, setDisposing] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalFocus(true, dialogRef);

  useEffect(() => {
    let cancelled = false;
    setProblem(null);
    Promise.all([
      assets.getAsset(administrationId, assetId),
      assets.listDepreciationRuns(administrationId, assetId),
    ])
      .then(([a, r]) => {
        if (cancelled) return;
        setAsset(a);
        setRuns(r);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [assets, administrationId, assetId, attempt]);

  const accumulated = runs === null ? null : sumDecimals(runs.map((run) => run.amount));

  return (
    <div className="drawer-backdrop">
      <div
        ref={dialogRef}
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={t("assets.detail_title")}
        data-testid="asset-drawer"
      >
        <div className="drawer__head">
          <h2>{asset?.name ?? t("assets.detail_title")}</h2>
          <button
            type="button"
            className="button--quiet"
            aria-label={t("common.action.close")}
            onClick={onClose}
          >
            <Icon name="close" size={18} />
          </button>
        </div>
        {problem !== null ? <ErrorState message={problem} /> : null}
        {asset === null ? <LoadingSkeleton rows={3} /> : null}
        {asset !== null ? (
          <>
            <p className="meta-line">
              <span className="ledgr-num">{date(asset.acquisition_date)}</span>
              <span>{money(asset.acquisition_cost)}</span>
              {asset.status === "disposed" ? (
                <span className="chip">{t("assets.status.disposed")}</span>
              ) : (
                <span className="chip chip--positive">{t("assets.status.active")}</span>
              )}
            </p>
            {accumulated !== null ? (
              <p data-testid="asset-accumulated">
                {t("assets.accumulated_depreciation")}: {money(accumulated)}
              </p>
            ) : null}
            <div className="table-wrap">
              <table className="table" data-testid="asset-depreciation-runs">
                <thead>
                  <tr>
                    <th scope="col">{t("ledger.column.date")}</th>
                    <th scope="col" className="table__num">
                      {t("journal.form.total_debit")}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {(runs ?? []).map((run) => (
                    <tr key={run.id}>
                      <td className="ledgr-num">{date(run.posted_at.slice(0, 10))}</td>
                      <td className="table__num">{money(run.amount)}</td>
                    </tr>
                  ))}
                  {runs !== null && runs.length === 0 ? (
                    <tr>
                      <td colSpan={2} className="caption">
                        {t("assets.no_runs_yet")}
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>

            {asset.status === "active" ? (
              <div className="form__actions">
                <button type="button" onClick={() => setDepreciating(true)} data-testid="asset-depreciate-open">
                  {t("assets.depreciate")}
                </button>
                <button type="button" onClick={() => setDisposing(true)} data-testid="asset-dispose-open">
                  {t("assets.dispose")}
                </button>
              </div>
            ) : null}

            {depreciating ? (
              <DepreciateForm
                administrationId={administrationId}
                fiscalYearId={fiscalYearId}
                asset={asset}
                onClose={() => setDepreciating(false)}
                onDone={() => {
                  setDepreciating(false);
                  setAttempt((n) => n + 1);
                  onChanged();
                }}
              />
            ) : null}
            {disposing ? (
              <DisposeForm
                administrationId={administrationId}
                fiscalYearId={fiscalYearId}
                asset={asset}
                accounts={accounts}
                onClose={() => setDisposing(false)}
                onDone={() => {
                  setDisposing(false);
                  setAttempt((n) => n + 1);
                  onChanged();
                }}
              />
            ) : null}
          </>
        ) : null}
      </div>
    </div>
  );
}

function useOpenPeriods(administrationId: string, fiscalYearId: string): readonly PeriodView[] | null {
  const { journal } = useServices();
  const [periods, setPeriods] = useState<readonly PeriodView[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    journal
      .listPeriods(administrationId, fiscalYearId)
      .then((result) => {
        if (!cancelled) setPeriods(result.filter((p) => p.status === "open"));
      })
      .catch(() => {
        if (!cancelled) setPeriods([]);
      });
    return () => {
      cancelled = true;
    };
  }, [journal, administrationId, fiscalYearId]);

  return periods;
}

function DepreciateForm({
  administrationId,
  fiscalYearId,
  asset,
  onClose,
  onDone,
}: {
  administrationId: string;
  fiscalYearId: string;
  asset: FixedAssetView;
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useI18n();
  const { assets } = useServices();
  const periods = useOpenPeriods(administrationId, fiscalYearId);
  const [periodId, setPeriodId] = useState("");
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    if (periods !== null && periodId === "") setPeriodId(periods[0]?.id ?? "");
  }, [periods, periodId]);

  const submit = useCallback(async () => {
    setSending(true);
    setProblem(null);
    try {
      await assets.depreciate(administrationId, asset.id, periodId);
      onDone();
    } catch (error) {
      setProblem(describeError(error));
    } finally {
      setSending(false);
    }
  }, [assets, administrationId, asset.id, periodId, onDone]);

  return (
    <div className="panel form" data-testid="asset-depreciate-form">
      <h3>{t("assets.depreciate")}</h3>
      {problem !== null ? <ErrorState message={problem} /> : null}
      {periods === null ? (
        <LoadingSkeleton rows={2} />
      ) : (
        <>
          <label className="form__field">
            <span>{t("journal.form.period_label")}</span>
            <select
              value={periodId}
              onChange={(event) => setPeriodId(event.target.value)}
              data-testid="asset-depreciate-period"
            >
              {periods.map((period) => (
                <option key={period.id} value={period.id}>
                  {period.start_date.slice(0, 7)}
                </option>
              ))}
            </select>
          </label>
          <div className="form__actions">
            <button
              type="button"
              disabled={sending || periodId.trim() === ""}
              onClick={() => void submit()}
              data-testid="asset-depreciate-confirm"
            >
              {t("assets.depreciate")}
            </button>
            <button type="button" className="button--quiet" onClick={onClose}>
              {t("journal.periods.unlock_cancel")}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function DisposeForm({
  administrationId,
  fiscalYearId,
  asset,
  accounts,
  onClose,
  onDone,
}: {
  administrationId: string;
  fiscalYearId: string;
  asset: FixedAssetView;
  accounts: readonly ChartAccountView[];
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useI18n();
  const { assets } = useServices();
  const periods = useOpenPeriods(administrationId, fiscalYearId);
  const [periodId, setPeriodId] = useState("");
  const [disposalDate, setDisposalDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [proceeds, setProceeds] = useState("0");
  const [proceedsAccountId, setProceedsAccountId] = useState("");
  const [gainLossAccountId, setGainLossAccountId] = useState("");
  const [sending, setSending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    if (periods !== null && periodId === "") setPeriodId(periods[0]?.id ?? "");
  }, [periods, periodId]);

  const submit = useCallback(async () => {
    setSending(true);
    setProblem(null);
    try {
      await assets.dispose(administrationId, asset.id, {
        periodId,
        disposalDate,
        proceeds: proceeds === "" ? "0" : proceeds,
        proceedsAccountId: proceedsAccountId.trim() === "" ? null : proceedsAccountId,
        gainLossAccountId: gainLossAccountId.trim() === "" ? null : gainLossAccountId,
      });
      onDone();
    } catch (error) {
      setProblem(describeError(error));
    } finally {
      setSending(false);
    }
  }, [assets, administrationId, asset.id, periodId, disposalDate, proceeds, proceedsAccountId, gainLossAccountId, onDone]);

  return (
    <div className="panel form" data-testid="asset-dispose-form">
      <h3>{t("assets.dispose")}</h3>
      {problem !== null ? <ErrorState message={problem} /> : null}
      <label className="form__field">
        <span>{t("journal.form.period_label")}</span>
        {periods === null ? (
          <LoadingSkeleton rows={1} />
        ) : (
          <select
            value={periodId}
            onChange={(event) => setPeriodId(event.target.value)}
            data-testid="asset-dispose-period"
          >
            {periods.map((period) => (
              <option key={period.id} value={period.id}>
                {period.start_date.slice(0, 7)}
              </option>
            ))}
          </select>
        )}
      </label>
      <label className="form__field">
        <span>{t("assets.field.disposal_date")}</span>
        <input type="date" value={disposalDate} onChange={(event) => setDisposalDate(event.target.value)} />
      </label>
      <label className="form__field">
        <span>{t("assets.field.proceeds")}</span>
        <input
          type="text"
          inputMode="decimal"
          value={proceeds}
          onChange={(event) => setProceeds(event.target.value)}
          data-testid="asset-dispose-proceeds"
        />
      </label>
      <label className="form__field">
        <span>{t("assets.field.proceeds_account")}</span>
        <select
          value={proceedsAccountId}
          onChange={(event) => setProceedsAccountId(event.target.value)}
          data-testid="asset-dispose-proceeds-account"
        >
          <option value="" />
          {accounts
            .filter((a) => a.account_type === "asset")
            .map((account) => (
              <option key={account.id} value={account.id}>
                {account.code} — {account.name}
              </option>
            ))}
        </select>
      </label>
      <label className="form__field">
        <span>{t("assets.field.gain_loss_account")}</span>
        <select value={gainLossAccountId} onChange={(event) => setGainLossAccountId(event.target.value)}>
          <option value="" />
          {accounts.map((account) => (
            <option key={account.id} value={account.id}>
              {account.code} — {account.name}
            </option>
          ))}
        </select>
      </label>
      <div className="form__actions">
        <button
          type="button"
          disabled={sending || periodId.trim() === ""}
          onClick={() => void submit()}
          data-testid="asset-dispose-confirm"
        >
          {t("assets.dispose")}
        </button>
        <button type="button" className="button--quiet" onClick={onClose}>
          {t("journal.periods.unlock_cancel")}
        </button>
      </div>
    </div>
  );
}

