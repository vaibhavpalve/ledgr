import { useCallback, useEffect, useId, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "@ledgr/i18n";
import type { FirmSavedView, FirmSavedViewQuery } from "@ledgr/shared-types";

import "./FirmHome.css";
import { describeError } from "../api/http";
import { Button } from "../ui";
import type { FirmApiShape } from "./api";
import { FirmDialog } from "./FirmDialogs";
import { onViewsChanged, viewsChanged } from "./lastQuery";
import { useResource, type Resource } from "./useResource";

/**
 * Saved views (FR-FRM-000b, ADR-115): a named worklist query per user, with a live count. They
 * are listed in the rail's Firm area (and on a phone, where the rail is gone, above the list);
 * clicking one opens To do with its query applied. Saving, renaming and archiving happen on the
 * firm home, against the query on screen.
 */

/** What a click on a view hands the firm home, through the router's location state. */
export interface ApplyViewState {
  readonly applyView: { readonly id: string; readonly query: FirmSavedViewQuery };
}

export function appliedViewOf(state: unknown): ApplyViewState["applyView"] | null {
  const candidate = (state as Partial<ApplyViewState> | null)?.applyView;
  if (candidate === undefined || candidate === null || typeof candidate !== "object") return null;
  if (typeof candidate.query !== "object" || candidate.query === null) return null;
  return candidate;
}

/** The caller's views, read again whenever one is saved, renamed or archived in this tab. */
export function useSavedViews(api: FirmApiShape): Resource<FirmSavedView[]> {
  const load = useCallback(() => api.listViews(), [api]);
  const views = useResource(load);
  const { reload } = views;
  useEffect(() => onViewsChanged(reload), [reload]);
  return views;
}

/**
 * The list itself: a button per view, name and count. Secondary chrome, so an endpoint that
 * fails or is not deployed yet renders nothing rather than an error in the rail.
 */
export function SavedViewList({
  api,
  variant,
  activeId,
}: {
  api: FirmApiShape;
  variant: "rail" | "chips";
  activeId?: string | null;
}) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const views = useSavedViews(api);
  const list = views.data ?? [];
  if (list.length === 0) return null;

  const open = (view: FirmSavedView) => {
    const state: ApplyViewState = { applyView: { id: view.id, query: view.query } };
    navigate("/todo", { state });
  };

  if (variant === "chips") {
    return (
      <div
        className="firm-chips firm-views-chips"
        role="group"
        aria-label={t("client.todo.views.title")}
        data-testid="firm-views-chips"
      >
        {list.map((view) => (
          <button
            key={view.id}
            type="button"
            className="firm-chip"
            aria-pressed={activeId === view.id}
            data-testid={`firm-view-chip-${view.id}`}
            onClick={() => open(view)}
          >
            <span>{view.name}</span>
            <ViewCount count={view.count} className="firm-chip__count ui-num" />
          </button>
        ))}
      </div>
    );
  }

  return (
    <div className="shell__nav-group firm-views" data-testid="nav-views">
      <div className="ui-navgroup">{t("client.todo.views.title")}</div>
      <ul className="firm-views__list">
        {list.map((view) => (
          <li key={view.id}>
            <button
              type="button"
              className="ui-nav firm-views__item"
              data-testid={`nav-view-${view.id}`}
              onClick={() => open(view)}
            >
              <span className="ui-nav__label">{view.name}</span>
              <ViewCount count={view.count} className="firm-views__count ui-num" />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * A view's live count, with its noun for a screen reader. `null` means the stored query no
 * longer validates (ADR-115): said in words, never shown as an empty or zero count.
 */
function ViewCount({ count, className }: { count: number | null; className: string }) {
  const { t } = useI18n();
  if (count === null) {
    return (
      <span className={`${className} firm-views__stale`} data-testid="firm-view-stale">
        {t("client.todo.views.count_unavailable")}
      </span>
    );
  }
  return (
    <span className={className}>
      {count}
      <span className="ledgr-visually-hidden">
        {" "}
        {t("client.todo.views.count_label", { count })}
      </span>
    </span>
  );
}

/** "Save view" and, when the query on screen is a saved view, "Rename or archive". */
export function ViewControls({
  api,
  query,
  activeView,
}: {
  api: FirmApiShape;
  query: FirmSavedViewQuery;
  activeView: FirmSavedView | null;
}) {
  const { t } = useI18n();
  const [mode, setMode] = useState<"save" | "manage" | null>(null);
  return (
    <>
      {activeView !== null ? (
        <Button
          size="sm"
          variant="ghost"
          data-testid="firm-view-manage"
          onClick={() => setMode("manage")}
        >
          {t("client.todo.views.manage", { name: activeView.name })}
        </Button>
      ) : (
        <Button
          size="sm"
          variant="ghost"
          data-testid="firm-view-save"
          onClick={() => setMode("save")}
        >
          {t("client.todo.views.save")}
        </Button>
      )}
      {mode !== null ? (
        <ViewDialog
          api={api}
          query={query}
          view={mode === "manage" ? activeView : null}
          onClose={() => setMode(null)}
        />
      ) : null}
    </>
  );
}

function ViewDialog({
  api,
  query,
  view,
  onClose,
}: {
  api: FirmApiShape;
  query: FirmSavedViewQuery;
  /** null = save the query on screen as a new view. */
  view: FirmSavedView | null;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const [name, setName] = useState(view?.name ?? "");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const nameId = useId();
  const hintId = useId();
  const trimmed = name.trim();
  const valid = trimmed.length >= 1 && trimmed.length <= 60;

  const run = async (action: () => unknown) => {
    setBusy(true);
    setProblem(null);
    try {
      await action();
      viewsChanged();
      onClose();
    } catch (error) {
      setProblem(describeError(error));
      setBusy(false);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!valid) return;
    void run(() =>
      view === null ? api.createView(trimmed, query) : api.renameView(view.id, trimmed),
    );
  };

  return (
    <FirmDialog
      title={
        view === null ? t("client.todo.views.save_title") : t("client.todo.views.manage_title")
      }
      onClose={onClose}
      testId="firm-view-dialog"
    >
      <form className="firm-form" onSubmit={submit}>
        <div className="ui-field">
          <label className="ui-label" htmlFor={nameId}>
            {t("client.todo.views.name")}
          </label>
          <input
            id={nameId}
            className="ui-input"
            value={name}
            maxLength={60}
            required
            aria-describedby={hintId}
            data-testid="firm-view-name"
            onChange={(event) => setName(event.target.value)}
          />
          <p id={hintId} className="ui-help">
            {view === null ? t("client.todo.views.save_hint") : t("client.todo.views.manage_hint")}
          </p>
        </div>
        {problem !== null ? (
          <p className="field-error" role="alert">
            {problem}
          </p>
        ) : null}
        <div className="dialog__actions">
          {view !== null ? (
            <Button
              disabled={busy}
              data-testid="firm-view-archive"
              onClick={() => void run(() => api.archiveView(view.id))}
            >
              {t("client.todo.views.archive")}
            </Button>
          ) : null}
          <Button onClick={onClose}>{t("common.action.cancel")}</Button>
          <Button
            type="submit"
            variant="primary"
            disabled={busy || !valid}
            data-testid="firm-view-submit"
          >
            {busy
              ? t("client.todo.working")
              : view === null
                ? t("client.todo.views.save")
                : t("client.todo.views.rename")}
          </Button>
        </div>
      </form>
    </FirmDialog>
  );
}
