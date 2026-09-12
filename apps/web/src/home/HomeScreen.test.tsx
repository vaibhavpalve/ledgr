import { fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";
import type { DashboardView } from "@ledgr/shared-types";

import type { MobileTab } from "../MobileShell";
import type { DashboardApi } from "./api";
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

function renderHome(summary: DashboardView, onNavigate: (tab: MobileTab) => void = vi.fn()) {
  return render(
    <HomeScreen
      administrationId="adm-A"
      fiscalYearId="fy-2026"
      api={api(summary)}
      onNavigate={onNavigate}
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

    const items = screen.getAllByTestId("home-item");
    expect(items.map((item) => item.textContent)).toEqual([
      "Invoice INV-2024-003 to Jansen BV, 12 days overdue",
      "Receipt from Café Central is missing details",
      "Draft invoice to Bakker BV has not been sent yet",
    ]);
  });

  it("tapping an overdue-invoice or draft-invoice item navigates to the View tab", async () => {
    const onNavigate = vi.fn();
    renderHome(summaryWithItems, onNavigate);

    await waitFor(() => expect(screen.getByTestId("home-item-inv-1")).toBeDefined());
    fireEvent.click(screen.getByTestId("home-item-inv-1"));
    expect(onNavigate).toHaveBeenCalledWith("view");

    fireEvent.click(screen.getByTestId("home-item-inv-2"));
    expect(onNavigate).toHaveBeenCalledWith("view");
  });

  it("tapping a draft-expense item navigates to the Approve tab", async () => {
    const onNavigate = vi.fn();
    renderHome(summaryWithItems, onNavigate);

    await waitFor(() => expect(screen.getByTestId("home-item-exp-1")).toBeDefined());
    fireEvent.click(screen.getByTestId("home-item-exp-1"));

    expect(onNavigate).toHaveBeenCalledWith("approve");
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
        onNavigate={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("home-error")).toBeDefined());
    expect(screen.queryByTestId("home-figures")).toBeNull();
  });
});
