import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@ledgr/i18n";
import type { VatBoxView, VatReturnView } from "@ledgr/shared-types";

import { ApiError } from "../api/http";
import { VatRoute } from "./VatScreen";

/**
 * The BTW screen (ADR-087): what a filer sees for each period, the boxes and
 * the postings behind them, the checks, and the one irreversible act.
 */

function box(code: string, turnover: string | null, vat: string | null): VatBoxView {
  return {
    code,
    kind: code === "5b" ? "input" : code === "5a" ? "subtotal" : "turnover",
    description_nl: `Rubriek ${code}`,
    description_en: `Box ${code}`,
    turnover,
    turnover_rounded: turnover,
    vat,
    vat_rounded: vat,
    treatments: [],
  };
}

const q2: VatReturnView = {
  period_id: "p2",
  fiscal_year_id: "fy-2026",
  period_number: 2,
  start_date: "2026-04-01",
  end_date: "2026-06-30",
  period_status: "open",
  due_date: "2026-07-31",
  status: "ready",
  boxes: [
    box("1a", "1000.00", "210.00"),
    box("1e", "0.00", null),
    box("4b", "50.00", "10.00"),
    box("5a", null, "220.00"),
    box("5b", null, "32.00"),
  ],
  output_vat: "220.00",
  input_vat: "32.00",
  total_due: "188.00",
  exempt_turnover: "0.00",
  checks: [
    { code: "provisional_ruleset", severity: "warning", count: null, amount: null, detail: [] },
    { code: "unposted_purchases", severity: "warning", count: 2, amount: null, detail: [] },
  ],
  can_be_filed: true,
  filed: null,
};

const q3: VatReturnView = {
  ...q2,
  period_id: "p3",
  period_number: 3,
  start_date: "2026-07-01",
  end_date: "2026-09-30",
  due_date: "2026-10-31",
  status: "open",
  total_due: "-12.00",
  checks: [
    { code: "period_not_ended", severity: "blocking", count: null, amount: null, detail: [] },
  ],
  can_be_filed: false,
};

const filedQ2: VatReturnView = {
  ...q2,
  status: "filed",
  period_status: "vat_filed",
  checks: [],
  can_be_filed: false,
  filed: {
    filed_at: "2026-07-15T10:00:00+00:00",
    filed_by_user_id: "user-1",
    filing_channel: "manual",
    filing_reference: "OB-123",
    warnings_acknowledged: ["provisional_ruleset", "unposted_purchases"],
    ruleset_provisional: true,
  },
};

const services = {
  vat: {
    listReturns: vi.fn(async () => [q2, q3]),
    getReturn: vi.fn(async (_admin: string, periodId: string) => (periodId === "p3" ? q3 : q2)),
    getBoxLines: vi.fn(async () => [
      {
        entry_id: "e1",
        entry_number: 7,
        entry_date: "2026-05-10",
        description: "Factuur 2026-001",
        document_reference: null,
        source_system: "invoicing",
        account_code: "8000",
        account_name: "Omzet hoog tarief",
        vat_treatment: "btw_21",
        column: "turnover",
        amount: "1000.00",
      },
    ]),
    fileReturn: vi.fn(async () => filedQ2),
  },
};

vi.mock("../session/SessionProvider", () => ({
  useAdministration: () => ({
    administration: { id: "adm-A" },
    fiscalYear: { id: "fy-2026", start_date: "2026-01-01", end_date: "2026-12-31" },
  }),
}));
vi.mock("../session/ServicesProvider", () => ({ useServices: () => services }));

function open(route: string) {
  render(
    <I18nProvider initialLanguage="en">
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/vat" element={<VatRoute />} />
          <Route path="/vat/:periodId" element={<VatRoute />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("the BTW overview", () => {
  it("lists each period with its status, what it comes to and when it is due", async () => {
    open("/vat");
    const q2Card = await screen.findByTestId("vat-period-p2");
    expect(q2Card.textContent).toContain("Q2 2026");
    expect(q2Card.textContent).toContain("Ready to file");
    expect(q2Card.textContent).toContain("To pay");
    expect(q2Card.textContent).toMatch(/188/);

    const q3Card = screen.getByTestId("vat-period-p3");
    expect(q3Card.textContent).toContain("In progress");
    // A negative total is a refund, shown as a positive amount under "To reclaim".
    expect(q3Card.textContent).toContain("To reclaim");
    expect(q3Card.textContent).not.toMatch(/−12|-12/);
  });
});

describe("one BTW return", () => {
  it("shows the boxes, with no figure where a box has no such column", async () => {
    open("/vat/p2");
    const row1e = await screen.findByTestId("vat-box-1e");
    // The empty cell is hidden from assistive technology too: "no such column" is not a zero.
    const cells = row1e.querySelectorAll("td");
    expect(cells[2]?.textContent).toBe("");
    expect(cells[2]?.getAttribute("aria-hidden")).toBe("true");
    expect(screen.getByTestId("vat-total").textContent).toMatch(/188/);
  });

  it("drills a box down to the postings behind it", async () => {
    open("/vat/p2");
    fireEvent.click(await screen.findByTestId("vat-box-toggle-1a"));
    const drill = await screen.findByTestId("vat-drill-1a");
    expect(drill.textContent).toContain("Factuur 2026-001");
    expect(services.vat.getBoxLines).toHaveBeenCalledWith("adm-A", "p2", "1a");
  });

  it("names each check and links to where it is fixed", async () => {
    open("/vat/p2");
    const check = await screen.findByTestId("vat-check-unposted_purchases");
    expect(check.textContent).toContain("2 purchase invoices or receipts are not booked yet");
    expect(within(check).getByRole("link").getAttribute("href")).toBe("/purchases");
  });

  it("does not offer filing while the period is still running", async () => {
    open("/vat/p3");
    await screen.findByTestId("vat-check-period_not_ended");
    expect(screen.queryByTestId("vat-file")).toBeNull();
  });

  it("files only once every warning is acknowledged and the lock is confirmed", async () => {
    open("/vat/p2");
    const submit = (await screen.findByTestId("vat-file-submit")) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    fireEvent.click(screen.getByTestId("vat-ack-provisional_ruleset"));
    expect(submit.disabled).toBe(true);
    fireEvent.click(screen.getByTestId("vat-ack-unposted_purchases"));
    expect(submit.disabled).toBe(false);

    fireEvent.change(screen.getByTestId("vat-reference"), { target: { value: " OB-123 " } });
    fireEvent.click(submit);
    expect(services.vat.fileReturn).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByTestId("vat-file-confirm-yes"));
    await waitFor(() => expect(screen.getByTestId("vat-filed")).not.toBeNull());
    expect(services.vat.fileReturn).toHaveBeenCalledWith("adm-A", "p2", {
      filing_reference: "OB-123",
      acknowledged_warnings: ["provisional_ruleset", "unposted_purchases"],
      expected_total: "188.00",
    });
    expect(screen.getByTestId("vat-filed").textContent).toContain("OB-123");
    expect(screen.queryByTestId("vat-file")).toBeNull();
  });

  it("says who may file when the filer is not allowed to", async () => {
    services.vat.fileReturn.mockRejectedValueOnce(new ApiError(403, "no_matching_grant", "nope"));
    open("/vat/p2");
    fireEvent.click(await screen.findByTestId("vat-ack-provisional_ruleset"));
    fireEvent.click(screen.getByTestId("vat-ack-unposted_purchases"));
    fireEvent.click(screen.getByTestId("vat-file-submit"));
    fireEvent.click(await screen.findByTestId("vat-file-confirm-yes"));
    const error = await screen.findByTestId("vat-file-error");
    expect(error.textContent).toContain("Only the owner or the accountant");
  });
});
