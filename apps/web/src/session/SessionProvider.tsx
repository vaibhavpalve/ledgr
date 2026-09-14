import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useI18n } from "@ledgr/i18n";
import type { AdministrationView, ClientBadge, FiscalYearView, MeView } from "@ledgr/shared-types";

import { describeError } from "../api/http";
import { useAuth } from "../auth/AuthProvider";
import type { SittingContext } from "../capture/useSitting";
import { startCaptureUploads } from "../capture/queue";
import { useServices } from "./ServicesProvider";

/**
 * The session's tenant context, loaded once from `GET /v1/me` after MFA and
 * held in MEMORY for as long as the person is signed in — FR-WEB-006 ("no
 * financial data persisted in browser local storage; session state is
 * memory-resident and cleared on logout"). The only thing in storage is the
 * access token (`auth/session.ts`), exactly as before.
 *
 * This is the seam every screen used to be waiting for. `useSitting`'s
 * `SittingContext`, `MobileShell`'s `administrationId`/`fiscalYearId`, the
 * mobile shell's badge — each of their docstrings said "supplied by whatever
 * knows the session, when there is one". This provider is what knows the
 * session, and `sittingContext`/`badge` below are those props, derived from
 * one answer rather than fetched three times.
 *
 * --- The active administration is the SESSION's, never a screen's ---
 *
 * `active_administration_id` comes from the session row (§4.1) — the same
 * value `require_permission`'s wrong-client guard checks every write
 * against. Switching goes through `PUT /v1/switcher/{id}` and then a fresh
 * `GET /v1/me`, so the header, the dashboard and the next posting all read
 * the same fact from the same place. A screen that kept its own idea of the
 * open client would be the one that posts to the wrong one (FR-FRM-000a).
 *
 * A business (Model B) with an administration and no active one yet — the
 * state a session is in the first time after onboarding on another device —
 * is switched into it automatically here: there is exactly one it could
 * mean. A firm is never auto-switched; choosing a client is the deliberate
 * act the portfolio screen exists for.
 *
 * --- The fiscal year is a view preference, not tenant context ---
 *
 * It defaults to the year the API marks current and lives in this provider's
 * state only. The header's selector changes it for the session; a reload
 * returns to the current year. Nothing is written anywhere.
 */
export interface SessionContextValue {
  readonly me: MeView;
  readonly administrations: readonly AdministrationView[];
  /** The session's active administration, or null when it has none (a firm at the portfolio). */
  readonly administration: AdministrationView | null;
  readonly fiscalYear: FiscalYearView | null;
  readonly fiscalYears: readonly FiscalYearView[];
  /** FR-FRM-000a: what `ClientHeader` renders, derived from `administration`. */
  readonly badge: ClientBadge | null;
  /** What `useSitting` and the capture/invoice screens need; null without an administration. */
  readonly sittingContext: SittingContext | null;
  selectFiscalYear(id: string): void;
  switchAdministration(id: string): Promise<void>;
  /** Re-reads `GET /v1/me` — after onboarding, a settings change, or a switch. */
  refresh(): Promise<MeView>;
}

const SessionContext = createContext<SessionContextValue | null>(null);

type Phase =
  | { readonly kind: "loading" }
  | { readonly kind: "error"; readonly message: string }
  | { readonly kind: "ready"; readonly me: MeView };

export function SessionProvider({
  children,
  renderLoading,
  renderError,
}: {
  children: ReactNode;
  /** What to show while `/v1/me` is in flight — the shell's skeleton. */
  renderLoading: () => ReactNode;
  /** D5: what happened and what to do next; `retry` re-reads, and the caller offers sign-out. */
  renderError: (message: string, retry: () => void) => ReactNode;
}) {
  const { language } = useI18n();
  const { fetchImpl } = useAuth();
  const services = useServices();
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const [fiscalYearId, setFiscalYearId] = useState<string | null>(null);

  const load = useCallback(async (): Promise<MeView> => {
    let me = await services.account.getMe();
    if (
      me.active_administration_id === null &&
      me.organization.kind === "business" &&
      me.administrations.length > 0
    ) {
      // The one administration a business session could mean — see the
      // module docstring. Re-read afterwards rather than patching the
      // answer locally: the session row is the truth, this is a cache of it.
      const only = me.administrations[0];
      if (only !== undefined) {
        await services.client.switchTo(only.id);
        me = await services.account.getMe();
      }
    }
    return me;
  }, [services]);

  // Loads once per sign-in. `services` changes identity with the language
  // (its clients carry `Accept-Language`), and a language switch must NOT
  // re-fetch the session: the ref keeps the first load's answer and only an
  // explicit `refresh()` replaces it.
  const loaded = useRef(false);
  //: The load that is already running, so a second effect invocation ADOPTS it
  //: rather than being turned away by `loaded` or starting a duplicate.
  //:
  //: This is what StrictMode requires. In development React mounts, runs the
  //: effect, runs its cleanup, and runs the effect again — deliberately, to
  //: surface exactly this class of bug. A guard that only says "already
  //: started" turns the second run away, while the first run's cleanup has
  //: already set its `cancelled` flag, so the answer that eventually arrives
  //: is discarded and NOTHING sets the phase: the app sits on its loading
  //: skeleton forever. It did, for every `pnpm dev` session, while the
  //: production build (no StrictMode) worked — which is why no test caught it
  //: until the app was opened in a real browser.
  const inFlight = useRef<Promise<MeView> | null>(null);

  const refresh = useCallback(async (): Promise<MeView> => {
    const me = await load();
    setPhase({ kind: "ready", me });
    return me;
  }, [load]);

  useEffect(() => {
    if (loaded.current) return;
    let cancelled = false;
    const pending = inFlight.current ?? load();
    inFlight.current = pending;
    pending
      .then((me) => {
        loaded.current = true;
        inFlight.current = null;
        if (!cancelled) setPhase({ kind: "ready", me });
      })
      .catch((error: unknown) => {
        // `loaded` stays false: a failed load is not a load, and the retry
        // below (or a later `load` identity) must be able to start another.
        inFlight.current = null;
        if (!cancelled) setPhase({ kind: "error", message: describeError(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [load]);

  const retry = useCallback(() => {
    setPhase({ kind: "loading" });
    const pending = load();
    inFlight.current = pending;
    pending
      .then((me) => {
        loaded.current = true;
        inFlight.current = null;
        setPhase({ kind: "ready", me });
      })
      .catch((error: unknown) => {
        inFlight.current = null;
        setPhase({ kind: "error", message: describeError(error) });
      });
  }, [load]);

  // MOB-003: queued captures drain for as long as someone is signed in, on
  // the SAME authenticated fetch as everything else — the uploader used to
  // send no bearer token at all, which was the named gap in auth/session.ts.
  useEffect(() => {
    if (phase.kind !== "ready") return;
    const uploader = startCaptureUploads(() => language, fetchImpl);
    return () => uploader.stop();
    // `language` is read through the callback; re-running on every switch
    // would restart the uploader mid-upload for nothing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase.kind, fetchImpl]);

  const switchAdministration = useCallback(
    async (id: string) => {
      await services.client.switchTo(id);
      setFiscalYearId(null);
      await refresh();
    },
    [services, refresh],
  );

  const value = useMemo<SessionContextValue | null>(() => {
    if (phase.kind !== "ready") return null;
    const { me } = phase;
    const administration =
      me.administrations.find((entry) => entry.id === me.active_administration_id) ?? null;
    const fiscalYears = administration?.fiscal_years ?? [];
    const fiscalYear =
      fiscalYears.find((year) => year.id === fiscalYearId) ??
      fiscalYears.find((year) => year.is_current) ??
      fiscalYears[fiscalYears.length - 1] ??
      null;
    return {
      me,
      administrations: me.administrations,
      administration,
      fiscalYear,
      fiscalYears,
      badge: administration === null ? null : badgeOf(administration, me.administrations),
      sittingContext:
        administration === null || fiscalYear === null
          ? null
          : {
              organizationId: me.organization.id,
              administrationId: administration.id,
              fiscalYearId: fiscalYear.id,
              userId: me.user.id,
            },
      selectFiscalYear: setFiscalYearId,
      switchAdministration,
      refresh,
    };
  }, [phase, fiscalYearId, switchAdministration, refresh]);

  if (phase.kind === "loading") return <>{renderLoading()}</>;
  if (phase.kind === "error") return <>{renderError(phase.message, retry)}</>;
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

/**
 * FR-FRM-000a's badge from the session's own administration list. The
 * ambiguity flag is computed from the same list the server would use — the
 * caller's own switcher — so it cannot disagree with `/v1/switcher/active`.
 */
function badgeOf(
  administration: AdministrationView,
  all: readonly AdministrationView[],
): ClientBadge {
  return {
    administrationId: administration.id,
    displayName: administration.trade_name ?? administration.legal_name,
    legalName: administration.legal_name,
    tradeName: administration.trade_name,
    kvkNumber: administration.kvk_number,
    colour: administration.colour,
    initials: administration.initials,
    colourIsAmbiguous: all.some(
      (other) => other.id !== administration.id && other.colour === administration.colour,
    ),
  };
}

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (value === null) {
    throw new Error("useSession() outside a ready <SessionProvider>.");
  }
  return value;
}

/** The brief's name for it — the caller's own account, organization and memberships. */
export const useMe = useSession;

/**
 * For screens that only exist inside an administration: the guard
 * (`RequireAdministration`) has already redirected when there is none, so
 * this throws rather than returning null — a screen reaching for a tenant
 * context it does not have is exactly the wrong-client failure
 * FR-FRM-000a names.
 */
export function useAdministration(): {
  administration: AdministrationView;
  fiscalYear: FiscalYearView;
  sittingContext: SittingContext;
} {
  const { administration, fiscalYear, sittingContext } = useSession();
  if (administration === null || fiscalYear === null || sittingContext === null) {
    throw new Error("useAdministration() on a route that is not behind RequireAdministration.");
  }
  return { administration, fiscalYear, sittingContext };
}
