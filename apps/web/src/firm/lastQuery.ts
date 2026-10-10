import type { FirmSavedViewQuery } from "@ledgr/shared-types";

/**
 * The firm worklist's last query, kept for this tab only (sessionStorage), so "Next client with
 * work" inside a client follows the same filters and sort the accountant was working through
 * (contract-wave2 decision 7). It is a convenience, never a control: every read and write is
 * wrapped, and storage that is blocked, full or absent simply hides the control.
 */

const KEY = "boeklite.firm.worklist-query";

export function storeLastQuery(query: FirmSavedViewQuery): void {
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify(query));
  } catch {
    // Private mode, quota, or no storage at all: the next-client control just stays hidden.
  }
}

/** The stored text as is; cheap enough to read on every render. */
export function readLastQueryRaw(): string | null {
  try {
    return window.sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function readLastQuery(): FirmSavedViewQuery | null {
  return parseLastQuery(readLastQueryRaw());
}

export function parseLastQuery(raw: string | null): FirmSavedViewQuery | null {
  try {
    if (raw === null) return null;
    const parsed = JSON.parse(raw) as Partial<FirmSavedViewQuery> | null;
    if (
      parsed === null ||
      typeof parsed !== "object" ||
      typeof parsed.chip !== "string" ||
      typeof parsed.sort !== "string" ||
      typeof parsed.assigned !== "string"
    ) {
      return null;
    }
    return {
      chip: parsed.chip,
      q: typeof parsed.q === "string" ? parsed.q : "",
      assigned: parsed.assigned,
      sort: parsed.sort,
      dir: parsed.dir === "desc" ? "desc" : "asc",
      vat_frequency: parsed.vat_frequency ?? null,
    };
  } catch {
    return null;
  }
}

export function clearLastQuery(): void {
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // Nothing to clear where there is no storage.
  }
}

/** Two stored queries ask the server for the same list. */
export function sameQuery(a: FirmSavedViewQuery, b: FirmSavedViewQuery): boolean {
  return (
    a.chip === b.chip &&
    a.q.trim() === b.q.trim() &&
    a.assigned === b.assigned &&
    a.sort === b.sort &&
    a.dir === b.dir &&
    (a.vat_frequency ?? null) === (b.vat_frequency ?? null)
  );
}

/**
 * Saved views change in one place (the firm home) and are listed in another (the rail): a tiny
 * in-tab signal tells the list to read again. No state lives here.
 */
const listeners = new Set<() => void>();

export function onViewsChanged(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function viewsChanged(): void {
  for (const listener of [...listeners]) listener();
}
