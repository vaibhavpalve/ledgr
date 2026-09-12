import { useEffect, useRef, useState } from "react";
import { useI18n } from "@ledgr/i18n";
import type { CaptureQueue } from "@ledgr/offline-queue";
import type { ClientBadge } from "@ledgr/shared-types";

import "./MobileShell.css";
import { ApproveList } from "./approve/ApproveList";
import type { CaptureApi } from "./capture/api";
import { CaptureScreen } from "./capture/CaptureScreen";
import type { DecodeFile } from "./capture/decode";
import type { Sitting } from "./capture/useSitting";
import { ClientHeader } from "./client/ClientHeader";
import type { DashboardApi } from "./home/api";
import { HomeScreen } from "./home/HomeScreen";
import type { SalesInvoiceApi } from "./invoicing/api";
import { SendInvoiceForm } from "./invoicing/SendInvoiceForm";
import { ViewList } from "./view/ViewList";

export type MobileTab = "home" | "capture" | "approve" | "view" | "invoice";

/**
 * The P0 mobile experience — §5.2, §7.4, MOB-002/004/005 — plus FR-UX-005/
 * MOB-006's home screen, added as a fifth section by this task.
 *
 *     §7.4       "Mobile is deliberately not a full clone of the web app. It
 *                covers capture, approve, view and invoice — the tasks
 *                people do away from a desk."
 *     FR-UX-005  The home screen is a prioritised list of what needs the
 *                user's attention, not a menu of everything the product can
 *                do.
 *
 * A five-tab, state-based switcher — no router, consistent with this
 * codebase's existing minimal style (`App.tsx` has none either) and with the
 * scope this shell is deliberately narrow to. Each tab is one of the things
 * §7.4/FR-UX-005 name, and nothing else:
 *
 *     home      FR-UX-005/MOB-006: a prioritised summary (cash position,
 *               receivables, VAT estimate, items needing attention) — a
 *               genuinely different thing from the four task tabs below, and
 *               the tab someone lands on, not a fifth task to pick from a
 *               menu. Tapping an item switches to whichever tab below is
 *               relevant.
 *     capture   the EXISTING FR-EXP-001/MOB-002 mechanism, unmodified below
 *               (CaptureScreen, CaptureQueueStatus, ExpenseForm, the offline
 *               queue) — see CaptureScreen.tsx for why none of that is
 *               rebuilt here.
 *     approve   MOB-004: a revisitable list of DRAFT expenses into the same
 *               ExpenseForm/mark_expense_ready flow — not a new multi-actor
 *               approval workflow (this task's ADR records that scope
 *               decision explicitly; FR-AP-006/007's routing is P1).
 *     view      MOB-004's "document viewable at full resolution": read-only
 *               posted/ready expenses and sales invoices.
 *     invoice   MOB-005: create and send a sales invoice using the four
 *               existing endpoints in sequence (create, set lines, issue,
 *               send). Template EDITING stays web-only in TemplateDesigner.
 *
 * `administrationId`/`fiscalYearId` are props for the same reason
 * `SittingContext` is one on `useSitting`: there is no session/active-client
 * endpoint wired yet (see App.tsx and auth/SignInPending), and a shell that
 * reached for "the currently open client" itself would be the one that opens
 * a capture, an approval or an invoice against the wrong administration.
 *
 * --- `badge` and `ClientHeader` (FR-FRM-000a) ---
 *
 * `badge` is fetched once by `AuthenticatedMobileShell` (App.tsx) — never by
 * this component — for the same reason `administrationId`/`fiscalYearId`
 * above are props rather than something fetched here: this shell's job is to
 * render what it is given, not to be one of several places that could each
 * independently get "the active client" wrong or stale.
 *
 * `<ClientHeader>` is rendered exactly ONCE, above the per-tab conditional
 * below and before the tab bar in DOM order, specifically so it is on screen
 * for all five tabs without exception — "unmistakable at all times" (the
 * requirement's own words) cannot be a per-tab responsibility, or a future
 * sixth tab could silently omit it. See `MobileShell.css` for the sticky
 * positioning that keeps it in view while a tab's own content scrolls, and
 * `ClientHeader`'s own docstring for why `badge` is a three-state
 * `ClientBadge | null | undefined` rather than the two-state type it
 * started with (ADR-049).
 */
export function MobileShell({
  administrationId,
  fiscalYearId,
  badge,
  sitting,
  queue,
  decode,
  captureApi,
  invoiceApi,
  dashboardApi,
}: {
  administrationId: string;
  fiscalYearId: string;
  badge: ClientBadge | null | undefined;
  sitting: Sitting;
  queue: CaptureQueue;
  decode: DecodeFile;
  captureApi: CaptureApi;
  invoiceApi: SalesInvoiceApi;
  dashboardApi: DashboardApi;
}) {
  const { t } = useI18n();
  // FR-UX-005: someone opening the app lands on the prioritised summary, not
  // straight into a task - "home", not "capture", is the initial tab.
  const [tab, setTab] = useState<MobileTab>("home");

  // WCAG 2.2 SC 2.4.3 (Focus Order) / SC 4.1.2, applied to a shell that has no
  // router: switching tabs replaces the whole screen the way a page navigation
  // would, so it needs the same focus behaviour one gets for free — moving
  // focus onto the new content, rather than leaving it sitting on the tab
  // button a sighted user can see was already activated but a screen reader
  // user has no signal actually changed anything past its own announcement.
  // `tabIndex={-1}` makes `<main>` a valid, non-Tab-order focus target for
  // exactly this; the ref skips the initial mount so landing on "home" does
  // not steal focus from whatever the page already had (e.g. a skip link).
  const mainRef = useRef<HTMLElement>(null);
  const mounted = useRef(false);
  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    mainRef.current?.focus();
  }, [tab]);

  return (
    <div className="mobile-shell">
      <ClientHeader badge={badge} />

      <main
        ref={mainRef}
        className="mobile-shell__content"
        tabIndex={-1}
        data-testid="mobile-shell-content"
      >
        {tab === "home" ? (
          <HomeScreen
            administrationId={administrationId}
            fiscalYearId={fiscalYearId}
            api={dashboardApi}
            onNavigate={setTab}
          />
        ) : null}
        {tab === "capture" ? (
          <CaptureScreen sitting={sitting} queue={queue} decode={decode} />
        ) : null}
        {tab === "approve" ? (
          <ApproveList administrationId={administrationId} api={captureApi} />
        ) : null}
        {tab === "view" ? (
          <ViewList
            administrationId={administrationId}
            captureApi={captureApi}
            invoiceApi={invoiceApi}
          />
        ) : null}
        {tab === "invoice" ? (
          <SendInvoiceForm
            administrationId={administrationId}
            fiscalYearId={fiscalYearId}
            api={invoiceApi}
            onSent={() => setTab("view")}
          />
        ) : null}
      </main>

      {/*
        A bottom tab bar — the mobile convention — with exactly five tabs,
        deliberately narrower than a full desktop nav (§7.4). `aria-current`
        rather than only a visual style, so the current section is announced
        to a screen reader and not merely coloured.
      */}
      <nav
        aria-label={t("mobile.shell.nav_label")}
        className="mobile-shell__tabs"
        data-testid="mobile-shell-tabs"
      >
        <button
          type="button"
          aria-current={tab === "home" ? "page" : undefined}
          data-testid="mobile-tab-home"
          onClick={() => setTab("home")}
        >
          {t("mobile.shell.tab.home")}
        </button>
        <button
          type="button"
          aria-current={tab === "capture" ? "page" : undefined}
          data-testid="mobile-tab-capture"
          onClick={() => setTab("capture")}
        >
          {t("mobile.shell.tab.capture")}
        </button>
        <button
          type="button"
          aria-current={tab === "approve" ? "page" : undefined}
          data-testid="mobile-tab-approve"
          onClick={() => setTab("approve")}
        >
          {t("mobile.shell.tab.approve")}
        </button>
        <button
          type="button"
          aria-current={tab === "view" ? "page" : undefined}
          data-testid="mobile-tab-view"
          onClick={() => setTab("view")}
        >
          {t("mobile.shell.tab.view")}
        </button>
        <button
          type="button"
          aria-current={tab === "invoice" ? "page" : undefined}
          data-testid="mobile-tab-invoice"
          onClick={() => setTab("invoice")}
        >
          {t("mobile.shell.tab.send_invoice")}
        </button>
      </nav>
    </div>
  );
}
