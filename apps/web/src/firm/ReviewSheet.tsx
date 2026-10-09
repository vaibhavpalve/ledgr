import { useCallback, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useI18n } from "@ledgr/i18n";
import type {
  FirmDecideResultView,
  FirmProposalDecision,
  FirmProposalGroupView,
  FirmProposalView,
} from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { EmptyState, ErrorState, LoadingSkeleton } from "../shell/ScreenState";
import { Button, useMoney } from "../ui";
import type { FirmApiShape } from "./api";
import { FailureList, FirmDialog } from "./FirmDialogs";
import { useResource } from "./useResource";

/**
 * The review sheet for booking proposals (ADR-110, FR-BNK-003/004): grouped by counterparty and
 * target account, so 48 proposals read as a handful of decisions ("KPN to 4500 Telefoon, 12
 * items over 9 clients"). Approve or reject a whole group or one proposal at a time; nothing is
 * approved by opening this. Each decision is authorized and posted per administration by the
 * server; a partial failure is listed here, never folded into one error.
 *
 * Amounts are the API's decimal strings, formatted by `useMoney` - never parsed to a float.
 */
export function ReviewSheet({
  api,
  administrationIds,
  onClose,
  onChanged,
}: {
  api: FirmApiShape;
  /** Scope to these clients (the bulk bar's selection); undefined = every granted client. */
  administrationIds?: readonly string[];
  onClose: () => void;
  /** After any decision landed: the page refreshes its counts and its list. */
  onChanged: () => unknown;
}) {
  const { t } = useI18n();
  const load = useCallback(() => api.listProposals(administrationIds), [api, administrationIds]);
  const proposals = useResource(load);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<FirmDecideResultView | null>(null);
  const [lastNames, setLastNames] = useState<ReadonlyMap<string, string>>(new Map());
  const [problem, setProblem] = useState<string | null>(null);

  const decide = async (items: readonly FirmProposalView[], decision: FirmProposalDecision) => {
    if (items.length === 0) return;
    setBusy(true);
    setProblem(null);
    setLastNames(
      new Map(items.map((item) => [item.id, `${item.display_name} · ${item.description}`])),
    );
    try {
      const answer = await api.decideProposals(
        items.map((item) => ({ proposalId: item.id, decision })),
      );
      setResult(answer);
      if (answer.approved + answer.rejected > 0) void onChanged();
      proposals.reload();
    } catch (error) {
      setProblem(describeError(error));
    } finally {
      setBusy(false);
    }
  };

  const groups = proposals.data?.groups ?? [];

  return (
    <FirmDialog
      title={t("client.todo.review.title")}
      subtitle={
        administrationIds !== undefined && administrationIds.length > 0
          ? t("client.todo.review.scope_selection", { count: administrationIds.length })
          : undefined
      }
      onClose={onClose}
      drawer
      testId="firm-review-sheet"
    >
      <p className="firm-muted">{t("client.todo.review.intro")}</p>

      {result !== null ? (
        <p role="status" data-testid="firm-review-result">
          {t("client.todo.review.result", {
            approved: result.approved,
            rejected: result.rejected,
          })}
        </p>
      ) : null}
      {result !== null && result.failed.length > 0 ? (
        <FailureList
          heading={t("client.todo.review.failed", { count: result.failed.length })}
          items={result.failed.map((failure) => ({
            key: failure.proposal_id,
            label: lastNames.get(failure.proposal_id) ?? failure.proposal_id,
            reason: failure.reason,
          }))}
          testId="firm-review-failures"
        />
      ) : null}
      {problem !== null ? (
        <p className="field-error" role="alert">
          {problem}
        </p>
      ) : null}

      <div className="firm-review__body">
        {proposals.data === null && proposals.loading ? (
          <LoadingSkeleton rows={3} testId="firm-review-loading" />
        ) : proposals.data === null && proposals.error !== null ? (
          <ErrorState
            message={proposals.error}
            onRetry={proposals.reload}
            testId="firm-review-error"
          />
        ) : groups.length === 0 ? (
          <EmptyState
            title={t("client.todo.review.empty_title")}
            body={t("client.todo.review.empty_body")}
            testId="firm-review-empty"
          />
        ) : (
          <ul className="firm-review__groups" data-testid="firm-review-groups">
            {groups.map((group) => (
              <ProposalGroup
                key={group.group_key}
                group={group}
                busy={busy}
                onDecide={(items, decision) => void decide(items, decision)}
              />
            ))}
          </ul>
        )}
      </div>
    </FirmDialog>
  );
}

function ProposalGroup({
  group,
  busy,
  onDecide,
}: {
  group: FirmProposalGroupView;
  busy: boolean;
  onDecide: (items: readonly FirmProposalView[], decision: FirmProposalDecision) => void;
}) {
  const { t, date } = useI18n();
  const money = useMoney();
  const [open, setOpen] = useState(false);
  const counterparty = group.counterparty ?? t("client.todo.review.no_counterparty");
  const account =
    group.account_code !== null
      ? [group.account_code, group.account_name].filter(Boolean).join(" ")
      : t("client.todo.review.no_account");
  const itemsId = `firm-review-items-${group.group_key}`;

  return (
    <li className="firm-review__group" data-testid={`firm-review-group-${group.group_key}`}>
      <div className="firm-review__group-head">
        <button
          type="button"
          className="firm-review__toggle"
          aria-expanded={open}
          aria-controls={itemsId}
          onClick={() => setOpen((value) => !value)}
          data-testid={`firm-review-toggle-${group.group_key}`}
        >
          {open ? (
            <ChevronDown size={16} aria-hidden="true" />
          ) : (
            <ChevronRight size={16} aria-hidden="true" />
          )}
          <span className="firm-review__who">
            <span className="firm-review__counterparty">{counterparty}</span>
            <span className="firm-review__account">{account}</span>
          </span>
          <span className="ledgr-visually-hidden">
            {open ? t("client.todo.review.hide_items") : t("client.todo.review.show_items")}
          </span>
        </button>
        <span className="firm-review__meta firm-muted">
          {t("client.todo.review.items", { count: group.count })}
          {" · "}
          {t("client.todo.review.clients", { count: group.client_count })}
        </span>
        <span className="ui-num firm-review__total">{money(group.total_amount)}</span>
        <span className="firm-review__actions">
          <Button
            size="sm"
            disabled={busy}
            aria-label={t("client.todo.review.reject_all_label", { counterparty })}
            onClick={() => onDecide(group.proposals, "reject")}
            data-testid={`firm-review-reject-all-${group.group_key}`}
          >
            {t("client.todo.review.reject_all")}
          </Button>
          <Button
            size="sm"
            disabled={busy}
            aria-label={t("client.todo.review.approve_all_label", { counterparty })}
            onClick={() => onDecide(group.proposals, "approve")}
            data-testid={`firm-review-approve-all-${group.group_key}`}
          >
            {t("client.todo.review.approve_all")}
          </Button>
        </span>
      </div>
      {open ? (
        <ul id={itemsId} className="firm-review__items">
          {group.proposals.map((item) => (
            <li
              key={item.id}
              className="firm-review__item"
              data-testid={`firm-review-item-${item.id}`}
            >
              <span className="firm-review__item-text">
                <span>{item.display_name}</span>
                <span className="firm-muted">
                  {date(item.date)}
                  {" · "}
                  {item.description}
                </span>
              </span>
              <span className="ui-num">{money(item.amount)}</span>
              <span className="firm-review__actions">
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  aria-label={t("client.todo.review.reject_one_label", {
                    client: item.display_name,
                    amount: money(item.amount),
                  })}
                  onClick={() => onDecide([item], "reject")}
                  data-testid={`firm-review-reject-${item.id}`}
                >
                  {t("client.todo.review.reject")}
                </Button>
                <Button
                  size="sm"
                  disabled={busy}
                  aria-label={t("client.todo.review.approve_one_label", {
                    client: item.display_name,
                    amount: money(item.amount),
                  })}
                  onClick={() => onDecide([item], "approve")}
                  data-testid={`firm-review-approve-${item.id}`}
                >
                  {t("client.todo.review.approve")}
                </Button>
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}
