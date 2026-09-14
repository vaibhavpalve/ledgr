import { fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";
import type { DashboardActionItemView, DashboardView } from "@ledgr/shared-types";

import type { DashboardApi } from "./api";
import { destinationFor } from "./DashboardRoute";
import { HomeScreen } from "./HomeScreen";

function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

const emptySummary: DashboardView = {
  cash_position: "1234.56",
  receivables: "500.00",
  vat_estimate: "160.00",
  vat_period_start: "2026-01-01",
  vat_period_end: "2026-09-12",
  items_needing_action: [],
};

const summaryWithItems: DashboardView = {
  ...emptySummary,
  items_needing_action: [
    {
      kind: "overdue_invoice",
      id: "inv-1",
      description: "Invoice INV-2024-003 to Jansen BV, 12 days overdue",
    },
    {
      kind: "draft_expense",
      id: "exp-1",
      description: "Receipt from Café Central is missing details",
    },
    {
      kind: "draft_invoice",
      id: "inv-2",
      description: "Draft invoice to Bakker BV has not been sent yet",
    },
  ],
};

function api(summary: DashboardView): DashboardApi {
  return {
    getDashboard: vi.fn(async () => summary),
  } as unknown as DashboardApi;
}

function renderHome(
  summary: DashboardView,
  onOpenItem: (item: DashboardActionItemView) => void = vi.fn(),
) {
  return render(
    <HomeScreen
      administrationId="adm-A"
      fiscalYearId="fy-2026"
      api={api(summary)}
      onOpenItem={onOpenItem}
    />,
  );
}

describe("HomeScreen — FR-UX-005/MOB-006's prioritised home screen", () => {
  it("renders the three figures with their honesty captions", async () => {
    renderHome(emptySummary);

    await waitFor(() => expect(screen.getByTestId("home-figures")).toBeDefined());

    expect(screen.getByTestId("home-cash-position").textContent).toContain("1.234,56");
    expect(screen.getByTestId("home-cash-position-caption")).toBeDefined();
    expect(screen.getByTestId("home-receivables").textContent).toContain("500,00");
    expect(screen.getByTestId("home-receivables-caption")).toBeDefined();
    expect(screen.getByTestId("home-vat-estimate").textContent).toContain("160,00");
    expect(screen.getByTestId("home-vat-estimate-caption")).toBeDefined();
    expect(screen.getByTestId("home-vat-estimate-period")).toBeDefined();
  });

  it("shows a positive empty state when nothing needs attention (FR-UX-004)", async () => {
    renderHome(emptySummary);

    await waitFor(() => expect(screen.getByTestId("home-items-empty")).toBeDefined());
    expect(screen.queryByTestId("home-items-list")).toBeNull();
  });

  it("renders items in the order the API returned them, without re-sorting", async () => {
    renderHome(summaryWithItems);

    await waitFor(() => expect(screen.getByTestId("home-items-list")).toBeDefined());

    // Each row also carries a status chip and its one action, so the check is
    // that the Nth row is about the Nth item the API returned — the ORDER,
    // which is what the API decided and this component must not second-guess.
    const rows = screen.getAllByTestId("home-item").map((item) => item.textContent ?? "");
    expect(rows).toHaveLength(summaryWithItems.items_needing_action.length);
    summaryWithItems.items_needing_action.forEach((item, index) => {
      expect(rows[index]).toContain(item.description);
    });
  });

  it("tapping an item hands the whole item up, not a destination it decided itself", async () => {
    // ADR-058 moved the decision out of this component: with URLs there is a
    // record to open, so the screen reports WHICH item was tapped and the
    // route (DashboardRoute.destinationFor) turns that into an address. A
    // screen that computed the URL would be a second place routing lives.
    const onOpenItem = vi.fn();
    renderHome(summaryWithItems, onOpenItem);

    await waitFor(() => expect(screen.getByTestId("home-item-inv-1")).toBeDefined());
    fireEvent.click(screen.getByTestId("home-item-inv-1"));

    expect(onOpenItem).toHaveBeenCalledWith(
      expect.objectContaining({ id: "inv-1", kind: "overdue_invoice" }),
    );
  });

  it("opens an invoice item at the invoice and a draft receipt at its own form", () => {
    // The gap ADR-048 recorded and the router closed: the mobile shell could
    // only switch tab, so a tap landed on a LIST and left the person to find
    // the row again.
    expect(destinationFor({ kind: "overdue_invoice", id: "inv-1", description: "" })).toBe(
      "/invoices/inv-1",
    );
    expect(destinationFor({ kind: "draft_invoice", id: "inv-2", description: "" })).toBe(
      "/invoices/inv-2",
    );
    expect(destinationFor({ kind: "draft_expense", id: "exp-1", description: "" })).toBe(
      "/review/exp-1",
    );
  });

  it("reports a tapped draft receipt as itself, kind included", async () => {
    const onOpenItem = vi.fn();
    renderHome(summaryWithItems, onOpenItem);

    await waitFor(() => expect(screen.getByTestId("home-item-exp-1")).toBeDefined());
    fireEvent.click(screen.getByTestId("home-item-exp-1"));

    expect(onOpenItem).toHaveBeenCalledWith(
      expect.objectContaining({ id: "exp-1", kind: "draft_expense" }),
    );
  });

  it("shows an error state when the dashboard cannot be loaded", async () => {
    const failingApi = {
      getDashboard: vi.fn(async () => {
        throw new Error("network down");
      }),
    } as unknown as DashboardApi;

    render(
      <HomeScreen
        administrationId="adm-A"
        fiscalYearId="fy-2026"
        api={failingApi}
        onOpenItem={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("home-error")).toBeDefined());
    expect(screen.queryByTestId("home-figures")).toBeNull();
  });
});
