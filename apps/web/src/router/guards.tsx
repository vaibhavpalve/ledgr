import { Navigate, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../auth/AuthProvider";
import { useSession } from "../session/SessionProvider";

/**
 * The route guards — the three redirects the brief names, each a layout
 * route so that everything nested under it is covered by construction
 * rather than by each screen remembering to check.
 *
 *   RequireAuth               unauthenticated → /login, remembering the URL
 *                             (`state.from`), and MFA pending → /mfa.
 *   RequireAdministration     no administration → /onboarding for a business,
 *                             /clients (the empty portfolio) for a firm.
 *   RedirectIfAuthenticated   /login and /signup while signed in → where
 *                             the person was going, or the dashboard.
 *
 * Every one of these is presentation (CLAUDE.md rule 3): the server denies
 * on its own, and a guard here only spares someone a screen that would
 * refuse them anyway.
 */

export interface LocationState {
  readonly from?: { readonly pathname: string; readonly search: string };
}

/** Where a successful sign-in lands: the remembered URL, or the dashboard. */
export function intendedPath(state: unknown): string {
  const from = (state as LocationState | null)?.from;
  if (from === undefined || from.pathname === "/login" || from.pathname === "/signup") return "/";
  return `${from.pathname}${from.search}`;
}

export function RequireAuth() {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "anonymous") {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  if (status === "mfa_pending") {
    return <Navigate to="/mfa" replace state={(location.state as LocationState | null) ?? { from: location }} />;
  }
  return <Outlet />;
}

export function RequireAdministration() {
  const { administration, me } = useSession();
  if (administration === null) {
    return <Navigate to={me.organization.kind === "firm" ? "/clients" : "/onboarding"} replace />;
  }
  return <Outlet />;
}

export function RedirectIfAuthenticated() {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "authenticated") {
    return <Navigate to={intendedPath(location.state)} replace />;
  }
  return <Outlet />;
}
