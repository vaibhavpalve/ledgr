import { useCallback, useEffect, useState } from "react";

import { describeError } from "../api/http";

export interface Resource<T> {
  /** The last answer, kept while a reload is in flight so a list does not blank on every filter. */
  readonly data: T | null;
  readonly error: string | null;
  readonly loading: boolean;
  reload(): void;
}

/**
 * One GET, owned by one panel: its own loading, its own error, its own retry - so the firm home's
 * panels fail independently (one 404 costs that panel, not the page).
 *
 * `load` must be stable (`useCallback`) and is re-run whenever it changes identity. Reads only:
 * StrictMode runs this effect twice in development, which for a GET costs one extra request and
 * nothing else (the first answer is discarded by `cancelled`). No mutation ever goes through here.
 */
export function useResource<T>(load: () => Promise<T>): Resource<T> {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({
    data: null,
    error: null,
    loading: true,
  });
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setState((previous) => ({ ...previous, loading: true }));
    load().then(
      (data) => {
        if (!cancelled) setState({ data, error: null, loading: false });
      },
      (error: unknown) => {
        if (!cancelled) {
          setState((previous) => ({
            data: previous.data,
            error: describeError(error),
            loading: false,
          }));
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [load, version]);

  const reload = useCallback(() => setVersion((value) => value + 1), []);
  return { ...state, reload };
}

/** A query value that settles `delay` ms after the last keystroke (the worklist's `q`). */
export function useDebounced<T>(value: T, delay = 300): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return settled;
}

/** Whether a media query matches, live. False where `matchMedia` does not exist (jsdom). */
export function useMediaQuery(query: string): boolean {
  const get = () =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(query).matches
      : false;
  const [matches, setMatches] = useState(get);
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const list = window.matchMedia(query);
    const onChange = () => setMatches(list.matches);
    onChange();
    list.addEventListener("change", onChange);
    return () => list.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

/**
 * The calendar date (YYYY-MM-DD) an ISO timestamp falls on for the reader, for `date()` - which
 * accepts calendar dates only and refuses an instant on purpose.
 */
export function localDateOf(timestamp: string): string | null {
  const instant = new Date(timestamp);
  if (Number.isNaN(instant.getTime())) return null;
  return isoDay(instant);
}

export function isoDay(day: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`;
}

/** Whole calendar days from `isoDate` to today (0 = today). */
export function daysSince(isoDate: string, today: Date = new Date()): number {
  const [year, month, day] = isoDate.split("-").map(Number);
  const then = new Date(year ?? 1970, (month ?? 1) - 1, day ?? 1);
  const midnight = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  return Math.round((midnight.getTime() - then.getTime()) / 86_400_000);
}

export function addDays(day: Date, days: number): Date {
  const next = new Date(day.getFullYear(), day.getMonth(), day.getDate());
  next.setDate(next.getDate() + days);
  return next;
}
