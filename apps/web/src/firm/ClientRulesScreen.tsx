import { useCallback, useEffect, useId, useState, type FormEvent } from "react";
import { useI18n } from "@ledgr/i18n";
import type {
  BookingRuleStatus,
  BookingRuleView,
  ChaseCadence,
  ChaseSettingView,
  RulePostingView,
} from "@ledgr/shared-types";

import "./FirmHome.css";
import { describeError } from "../api/http";
import { useServices } from "../session/ServicesProvider";
import { useSession } from "../session/SessionProvider";
import { Icon } from "../shell/icons";
import { EmptyState, ErrorState, LoadingSkeleton, PageHeader } from "../shell/ScreenState";
import { Badge, Button, useMoney, type BadgeVariant } from "../ui";
import { toDecimalInput } from "../ui/decimal";
import type { FirmApiShape } from "./api";
import { localDateOf, useResource } from "./useResource";

const STATUS_BADGE: Record<BookingRuleStatus, BadgeVariant> = {
  active: "booked",
  suspended: "overdue",
  retired: "neutral",
};

const CADENCES: readonly ChaseCadence[] = ["weekly", "fortnightly"];

/**
 * `/rules` - one client's approval rules and receipt reminders (contract-wave2, ADR-113/114,
 * FR-BNK-004/006, FR-NTF-001). Everything that acts on this client without someone clicking:
 *
 *   - its rules (made by "Always do this" in the review sheet), each retirable and cappable;
 *   - the bookings those rules posted, each undoable where the bank module allows (`undoable`);
 *   - whether this client gets receipt reminders, and how often.
 *
 * All of it is per administration (FR-BNK-006: never across tenants) and decided by the server
 * against current grants; nothing here hides an action as a control (CLAUDE.md rule 3). Undo is
 * a reversal through the bank path, never an edit of a posting (FR-GL-003).
 */
export function ClientRulesRoute() {
  const { administration } = useSession();
  const { firm } = useServices();
  if (administration === null) return null;
  return <ClientRulesScreen api={firm} administrationId={administration.id} />;
}

export function ClientRulesScreen({
  api,
  administrationId,
}: {
  api: FirmApiShape;
  administrationId: string;
}) {
  const { t } = useI18n();
  return (
    <section
      className="screen firm-rules"
      aria-label={t("client.rules.title")}
      data-testid="rules-screen"
    >
      <PageHeader title={t("client.rules.title")} context={t("client.rules.context")} />
      <ChaseSection api={api} administrationId={administrationId} />
      <RulesSection api={api} administrationId={administrationId} />
    </section>
  );
}

// --- receipt reminders ---

function ChaseSection({ api, administrationId }: { api: FirmApiShape; administrationId: string }) {
  const { t } = useI18n();
  const load = useCallback(() => api.getChaseSetting(administrationId), [api, administrationId]);
  const setting = useResource(load);
  const [draft, setDraft] = useState<ChaseSettingView | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const enabledId = useId();
  const cadenceId = useId();
  const hintId = useId();

  // The form starts from what the server holds (state only; nothing is sent until Save).
  useEffect(() => {
    if (setting.data !== null) setDraft(setting.data);
  }, [setting.data]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (draft === null) return;
    setBusy(true);
    setProblem(null);
    setSaved(false);
    try {
      const answer = await api.setChaseSetting(administrationId, draft);
      setDraft(answer);
      setSaved(true);
      setting.reload();
    } catch (error) {
      setProblem(describeError(error));
    } finally {
      setBusy(false);
    }
  };

  const current = setting.data;
  return (
    <section
      className="firm-panel firm-rules__section"
      aria-labelledby={`${hintId}-title`}
      data-testid="rules-chase"
    >
      <div className="firm-panel__head">
        <h2 id={`${hintId}-title`} className="firm-panel__title">
          {t("client.rules.chase.title")}
        </h2>
        {current !== null ? (
          <Badge variant={current.enabled ? "booked" : "neutral"}>
            {current.enabled
              ? t("client.rules.chase.status_on", {
                  cadence: t(`client.rules.chase.cadence.${current.cadence}`),
                })
              : t("client.rules.chase.status_off")}
          </Badge>
        ) : null}
      </div>
      <p id={hintId} className="firm-muted">
        {t("client.rules.chase.intro")}
      </p>
      {current === null && setting.loading ? (
        <LoadingSkeleton rows={1} testId="rules-chase-loading" />
      ) : current === null || draft === null ? (
        <ErrorState
          message={setting.error ?? t("client.todo.panel_error")}
          onRetry={setting.reload}
          testId="rules-chase-error"
        />
      ) : (
        <form className="firm-form firm-rules__chase-form" onSubmit={(event) => void submit(event)}>
          <div className="firm-rules__check">
            <input
              id={enabledId}
              type="checkbox"
              checked={draft.enabled}
              aria-describedby={hintId}
              data-testid="rules-chase-enabled"
              onChange={(event) => {
                setSaved(false);
                setDraft({ ...draft, enabled: event.target.checked });
              }}
            />
            <label htmlFor={enabledId}>{t("client.rules.chase.enabled")}</label>
          </div>
          <div className="ui-field">
            <label className="ui-label" htmlFor={cadenceId}>
              {t("client.rules.chase.cadence_label")}
            </label>
            <select
              id={cadenceId}
              className="ui-input firm-select-input"
              value={draft.cadence}
              disabled={!draft.enabled}
              data-testid="rules-chase-cadence"
              onChange={(event) => {
                setSaved(false);
                setDraft({ ...draft, cadence: event.target.value as ChaseCadence });
              }}
            >
              {CADENCES.map((value) => (
                <option key={value} value={value}>
                  {t(`client.rules.chase.cadence.${value}`)}
                </option>
              ))}
            </select>
          </div>
          {problem !== null ? (
            <p className="field-error" role="alert">
              {problem}
            </p>
          ) : null}
          {saved ? (
            <p role="status" data-testid="rules-chase-saved">
              {t("client.rules.chase.saved")}
            </p>
          ) : null}
          <div>
            <Button
              type="submit"
              variant="primary"
              size="sm"
              disabled={
                busy || (draft.enabled === current.enabled && draft.cadence === current.cadence)
              }
              data-testid="rules-chase-save"
            >
              {busy ? t("client.todo.working") : t("client.rules.chase.save")}
            </Button>
          </div>
        </form>
      )}
    </section>
  );
}

// --- rules and the bookings they posted ---

function RulesSection({ api, administrationId }: { api: FirmApiShape; administrationId: string }) {
  const { t } = useI18n();
  const loadRules = useCallback(() => api.listRules(administrationId), [api, administrationId]);
  const loadPostings = useCallback(
    () => api.listRulePostings(administrationId, 50),
    [api, administrationId],
  );
  const rules = useResource(loadRules);
  const postings = useResource(loadPostings);
  const [problem, setProblem] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const act = async (action: () => unknown, done: string) => {
    setProblem(null);
    setNotice(null);
    try {
      await action();
      setNotice(done);
      rules.reload();
      postings.reload();
      return true;
    } catch (error) {
      setProblem(describeError(error));
      return false;
    }
  };

  const ruleList = rules.data ?? [];
  const nameOfRule = (ruleId: string) => {
    const rule = ruleList.find((entry) => entry.id === ruleId);
    return rule === undefined ? null : (rule.counterparty_label ?? rule.counterparty_key);
  };

  return (
    <>
      {notice !== null ? (
        <p role="status" className="firm-rules__notice" data-testid="rules-notice">
          {notice}
        </p>
      ) : null}
      {problem !== null ? (
        <p className="field-error" role="alert" data-testid="rules-problem">
          {problem}
        </p>
      ) : null}

      <section
        className="firm-panel firm-rules__section"
        aria-label={t("client.rules.list.title")}
        data-testid="rules-list"
      >
        <div className="firm-panel__head">
          <h2 className="firm-panel__title">{t("client.rules.list.title")}</h2>
        </div>
        <p className="firm-muted">{t("client.rules.list.intro")}</p>
        {rules.data === null && rules.loading ? (
          <LoadingSkeleton rows={2} testId="rules-loading" />
        ) : rules.data === null ? (
          <ErrorState
            message={rules.error ?? t("client.todo.panel_error")}
            onRetry={rules.reload}
            testId="rules-error"
          />
        ) : ruleList.length === 0 ? (
          <EmptyState
            icon={<Icon name="clients" size={32} />}
            title={t("client.rules.list.empty_title")}
            body={t("client.rules.list.empty_body")}
            testId="rules-empty"
          />
        ) : (
          <ul className="firm-rules__list">
            {ruleList.map((rule) => (
              <RuleItem
                key={rule.id}
                rule={rule}
                onRetire={() =>
                  act(
                    () => api.retireRule(administrationId, rule.id),
                    t("client.rules.retired_notice"),
                  )
                }
                onMax={(value) =>
                  act(
                    () => api.setRuleMaxAmount(administrationId, rule.id, value),
                    t("client.rules.max_notice"),
                  )
                }
              />
            ))}
          </ul>
        )}
      </section>

      <section
        className="firm-panel firm-rules__section"
        aria-label={t("client.rules.postings.title")}
        data-testid="rules-postings"
      >
        <div className="firm-panel__head">
          <h2 className="firm-panel__title">{t("client.rules.postings.title")}</h2>
        </div>
        <p className="firm-muted">{t("client.rules.postings.intro")}</p>
        {postings.data === null && postings.loading ? (
          <LoadingSkeleton rows={2} testId="rules-postings-loading" />
        ) : postings.data === null ? (
          <ErrorState
            message={postings.error ?? t("client.todo.panel_error")}
            onRetry={postings.reload}
            testId="rules-postings-error"
          />
        ) : postings.data.length === 0 ? (
          <p className="firm-muted" data-testid="rules-postings-empty">
            {t("client.rules.postings.empty")}
          </p>
        ) : (
          <ul className="firm-rules__list">
            {postings.data.map((posting) => (
              <PostingItem
                key={posting.proposal_id}
                posting={posting}
                ruleName={nameOfRule(posting.rule_id)}
                onUndo={() =>
                  act(
                    () => api.undoRulePosting(administrationId, posting.proposal_id),
                    t("client.rules.postings.undone_notice"),
                  )
                }
              />
            ))}
          </ul>
        )}
      </section>
    </>
  );
}

function RuleItem({
  rule,
  onRetire,
  onMax,
}: {
  rule: BookingRuleView;
  onRetire: () => unknown;
  onMax: (value: string | null) => unknown;
}) {
  const { t, date, language } = useI18n();
  const money = useMoney();
  const [mode, setMode] = useState<"idle" | "max" | "retire">("idle");
  const [typed, setTyped] = useState(rule.max_amount ?? "");
  const [busy, setBusy] = useState(false);
  const maxId = useId();
  const hintId = useId();
  const counterparty = rule.counterparty_label ?? rule.counterparty_key;
  const account = [rule.account_code, rule.account_name].filter(Boolean).join(" ");
  const created = localDateOf(rule.created_at);
  const lastPosted = rule.last_posted_at !== null ? localDateOf(rule.last_posted_at) : null;
  const retired = rule.status === "retired";

  const run = async (action: () => unknown) => {
    setBusy(true);
    const ok = await action();
    setBusy(false);
    if (ok !== false) setMode("idle");
  };

  const submitMax = (event: FormEvent) => {
    event.preventDefault();
    const value = toDecimalInput(typed, language);
    void run(() => onMax(value === "" ? null : value));
  };

  return (
    <li className="firm-rules__item" data-testid={`rule-${rule.id}`}>
      <div className="firm-rules__head">
        <span className="firm-rules__what">
          <span className="firm-rules__name">{counterparty}</span>
          <span className="firm-muted">{t("client.rules.to_account", { account })}</span>
        </span>
        <Badge variant={STATUS_BADGE[rule.status] ?? "neutral"}>
          {t(`client.rules.status.${rule.status}`)}
        </Badge>
      </div>
      {rule.status === "suspended" && rule.suspended_reason !== null ? (
        <p className="firm-rules__reason" data-testid={`rule-reason-${rule.id}`}>
          {rule.suspended_reason}
        </p>
      ) : null}
      <p className="firm-muted firm-rules__facts">
        <span data-testid={`rule-max-${rule.id}`}>
          {rule.max_amount !== null
            ? t("client.rules.max_amount", { amount: money(rule.max_amount) })
            : t("client.rules.no_max")}
        </span>
        {" · "}
        {t("client.rules.postings_count", { count: rule.postings_count })}
        {lastPosted !== null ? (
          <>
            {" · "}
            {t("client.rules.last_posted", { date: date(lastPosted) })}
          </>
        ) : null}
        {created !== null ? (
          <>
            {" · "}
            {rule.created_by_name !== null
              ? t("client.rules.created_by", { name: rule.created_by_name, date: date(created) })
              : t("client.rules.created", { date: date(created) })}
          </>
        ) : null}
      </p>

      {retired ? null : mode === "max" ? (
        <form className="firm-rules__inline" onSubmit={submitMax}>
          <label className="ui-label" htmlFor={maxId}>
            {t("client.rules.max_label")}
          </label>
          <input
            id={maxId}
            className="ui-input firm-rules__amount"
            inputMode="decimal"
            value={typed}
            aria-describedby={hintId}
            data-testid={`rule-max-input-${rule.id}`}
            onChange={(event) => setTyped(event.target.value)}
          />
          <p id={hintId} className="ui-help">
            {t("client.rules.max_hint")}
          </p>
          <span className="firm-rules__actions">
            <Button size="sm" disabled={busy} onClick={() => setMode("idle")}>
              {t("common.action.cancel")}
            </Button>
            {rule.max_amount !== null ? (
              <Button
                size="sm"
                disabled={busy}
                data-testid={`rule-max-clear-${rule.id}`}
                onClick={() => void run(() => onMax(null))}
              >
                {t("client.rules.max_clear")}
              </Button>
            ) : null}
            <Button
              type="submit"
              size="sm"
              variant="primary"
              disabled={busy || typed.trim() === ""}
              data-testid={`rule-max-save-${rule.id}`}
            >
              {busy ? t("client.todo.working") : t("client.rules.max_save")}
            </Button>
          </span>
        </form>
      ) : mode === "retire" ? (
        <div className="firm-rules__inline" role="group" aria-label={t("client.rules.retire")}>
          <p>{t("client.rules.retire_confirm", { counterparty })}</p>
          <span className="firm-rules__actions">
            <Button size="sm" disabled={busy} onClick={() => setMode("idle")}>
              {t("common.action.cancel")}
            </Button>
            <Button
              size="sm"
              variant="primary"
              disabled={busy}
              data-testid={`rule-retire-confirm-${rule.id}`}
              onClick={() => void run(onRetire)}
            >
              {busy ? t("client.todo.working") : t("client.rules.retire")}
            </Button>
          </span>
        </div>
      ) : (
        <span className="firm-rules__actions">
          <Button
            size="sm"
            variant="ghost"
            data-testid={`rule-max-edit-${rule.id}`}
            onClick={() => {
              setTyped(rule.max_amount ?? "");
              setMode("max");
            }}
          >
            {t("client.rules.max_edit")}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            data-testid={`rule-retire-${rule.id}`}
            onClick={() => setMode("retire")}
          >
            {t("client.rules.retire")}
          </Button>
        </span>
      )}
    </li>
  );
}

function PostingItem({
  posting,
  ruleName,
  onUndo,
}: {
  posting: RulePostingView;
  ruleName: string | null;
  onUndo: () => unknown;
}) {
  const { t, date } = useI18n();
  const money = useMoney();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const undo = async () => {
    setBusy(true);
    await onUndo();
    setBusy(false);
    setConfirming(false);
  };

  return (
    <li className="firm-rules__item" data-testid={`rule-posting-${posting.proposal_id}`}>
      <div className="firm-rules__head">
        <span className="firm-rules__what">
          <span className="firm-rules__name">
            {posting.counterparty ?? ruleName ?? t("client.todo.review.no_counterparty")}
          </span>
          <span className="firm-muted">
            {date(posting.date)}
            {" · "}
            {t("client.rules.to_account", { account: posting.account_code })}
          </span>
        </span>
        <span className="ui-num">{money(posting.amount)}</span>
      </div>
      {posting.undoable ? (
        confirming ? (
          <div
            className="firm-rules__inline"
            role="group"
            aria-label={t("client.rules.postings.undo")}
          >
            <p>{t("client.rules.postings.undo_confirm")}</p>
            <span className="firm-rules__actions">
              <Button size="sm" disabled={busy} onClick={() => setConfirming(false)}>
                {t("common.action.cancel")}
              </Button>
              <Button
                size="sm"
                variant="primary"
                disabled={busy}
                data-testid={`rule-posting-undo-confirm-${posting.proposal_id}`}
                onClick={() => void undo()}
              >
                {busy ? t("client.todo.working") : t("client.rules.postings.undo")}
              </Button>
            </span>
          </div>
        ) : (
          <span className="firm-rules__actions">
            <Button
              size="sm"
              variant="ghost"
              data-testid={`rule-posting-undo-${posting.proposal_id}`}
              onClick={() => setConfirming(true)}
            >
              {t("client.rules.postings.undo")}
            </Button>
          </span>
        )
      ) : (
        <p
          className="firm-muted firm-rules__facts"
          data-testid={`rule-posting-final-${posting.proposal_id}`}
        >
          {t("client.rules.postings.not_undoable")}
        </p>
      )}
    </li>
  );
}
