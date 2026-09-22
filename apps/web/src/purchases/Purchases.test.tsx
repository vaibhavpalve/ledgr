import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CaptureQueue } from "@ledgr/offline-queue";
import type { QueueStore, StoredCapture } from "@ledgr/offline-queue";
import type { ExpenseSummaryView, ExpenseView } from "@ledgr/shared-types";

import { MemoryKeyVault, WebCryptoCipher } from "../capture/webCryptoCipher";
import { assertNoAxeViolations, axeViolations } from "../testing/axe";
import { jsonResponse } from "../testing/fakeFetch";
import { renderApp } from "../testing/renderApp";
import { meFixture } from "../testing/session";

/**
 * The purchases screen: the list, with uploading at the top, and one invoice
 * opened for review. What is asserted is what the screen is FOR - what a row
 * says, what an invoice says about how it was filled in, that the original sits
 * beside the fields - not its styling.
 */

class MemoryStore implements QueueStore {
  private readonly records = new Map<string, StoredCapture>();
  async put(record: StoredCapture) {
    this.records.set(record.id, record);
  }
  async all() {
    return [...this.records.values()];
  }
  async remove(id: string) {
    this.records.delete(id);
  }
  async clear() {
    this.records.clear();
  }
}

// The real queue opens IndexedDB, which jsdom does not have (see App.test.tsx).
vi.mock("../capture/queue", () => ({
  captureQueue: () =>
    new CaptureQueue({
      store: new MemoryStore(),
      cipher: new WebCryptoCipher(new MemoryKeyVault()),
      clock: { now: () => Date.now() },
      newId: () => crypto.randomUUID(),
    }),
  capturesAtRisk: vi.fn(async () => 0),
  purgeCaptureQueue: vi.fn(async (reason: string) => ({ reason, discarded: 0 })),
  startCaptureUploads: vi.fn(() => ({ stop: () => {} })),
}));

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  window.history.replaceState(null, "", "/");
});

const row = (overrides: Partial<ExpenseSummaryView> = {}): ExpenseSummaryView => ({
  id: "exp-1",
  status: "draft",
  capture_item_id: "item-1",
  expense_date: "2026-09-18",
  supplier: "Meelfabriek Zeeland",
  gross_amount: "1240.00",
  category: "Inventory & stock",
  category_key: "inventory_stock",
  rgs_code: "7000",
  invoice_number: "MFZ-9921",
  extraction_status: "done",
  missing_fields: [],
  can_be_marked_ready: false,
  ...overrides,
});

const detail = (overrides: Partial<ExpenseView> = {}): ExpenseView => ({
  id: "exp-1",
  status: "draft",
  capture_item_id: "item-1",
  expense_date: "2026-09-18",
  supplier: "Meelfabriek Zeeland",
  gross_amount: "1240.00",
  vat_treatment: "btw_21",
  vat_rate: "21.00",
  vat_amount: "215.21",
  net_amount: "1024.79",
  category: "Inventory & stock",
  invoice_number: "MFZ-9921",
  payment_method: null,
  suggested_category: null,
  missing_fields: ["payment_method"],
  can_be_marked_ready: false,
  duplicate_warnings: [],
  extraction: {
    status: "done",
    reason: null,
    fields: { supplier: 0.99, invoice_date: 0.97, gross_amount: 0.62, invoice_number: 0.9 },
  },
  document_id: "doc-1",
  ...overrides,
});

function stubApi(routes: Record<string, () => Response>) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const key = `${init?.method ?? "GET"} ${String(input)}`;
      calls.push(key);
      const handler = routes[key];
      if (handler) return handler();
      if (key === "GET /v1/me") return jsonResponse(meFixture());
      if (key === "GET /v1/me/language") {
        return jsonResponse({ language: null, supported: ["en", "nl"] });
      }
      if (key === "POST /v1/administrations/adm-A/capture-sessions") {
        return jsonResponse({
          id: "sess-1",
          open: true,
          opened_at: null,
          finalised_at: null,
          items: [],
          expenses_to_create: 0,
        });
      }
      return new Response(null, { status: 500 });
    }) as unknown as typeof fetch,
  );
  return calls;
}

const LIST = "GET /v1/administrations/adm-A/expenses";

async function openPurchases(rows: readonly ExpenseSummaryView[]) {
  stubApi({ [LIST]: () => jsonResponse(rows) });
  renderApp({ authenticated: true, language: "en" }, { route: "/purchases" });
  return screen.findByTestId("purchases");
}

describe("the purchases list", () => {
  it("shows what each invoice is, where it is booked, where it stands and what it costs", async () => {
    const page = await openPurchases([
      row(),
      row({
        id: "exp-2",
        supplier: "Bakkersgroothandel Jong",
        invoice_number: "BGJ-30184",
        status: "posted",
        category: "Utilities",
        category_key: "utilities",
        rgs_code: "4600",
        gross_amount: "745.60",
      }),
    ]);

    const rows = await within(page).findAllByTestId("purchase-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]?.textContent).toContain("Meelfabriek Zeeland");
    expect(rows[0]?.textContent).toContain("MFZ-9921");
    expect(rows[0]?.textContent).toContain("7000 · Inventory & stock");
    expect(rows[0]?.textContent).toContain("To review");
    expect(rows[0]?.textContent).toContain("1.240,00");
    expect(rows[1]?.textContent).toContain("Booked");
    expect(rows[1]?.textContent).toContain("4600 · Utilities");
  });

  it("counts the invoices beside the heading", async () => {
    const page = await openPurchases([row(), row({ id: "exp-2" }), row({ id: "exp-3" })]);

    await within(page).findAllByTestId("purchase-row");
    expect(page.textContent).toContain("3 purchase invoices");
  });

  it("opens an invoice from anywhere on its row, by a real link", async () => {
    const page = await openPurchases([row()]);

    const link = await within(page).findByTestId("purchase-open-exp-1");
    expect(link.getAttribute("href")).toBe("/purchases/exp-1");
  });

  it("gives an invoice nobody has filled in a row that says why, not a blank one", async () => {
    const page = await openPurchases([
      row({
        id: "exp-9",
        supplier: null,
        gross_amount: null,
        category: null,
        category_key: null,
        rgs_code: null,
        invoice_number: null,
        extraction_status: "failed",
      }),
    ]);

    const rows = await within(page).findAllByTestId("purchase-row");
    expect(rows[0]?.textContent).toContain("Invoice to fill in");
    expect(rows[0]?.textContent).toContain("Could not be read automatically");
  });

  it("shows a category typed into the form as typed, with no account beside it", async () => {
    const page = await openPurchases([
      row({ category: "Kantoor", category_key: null, rgs_code: null }),
    ]);

    const rows = await within(page).findAllByTestId("purchase-row");
    expect(rows[0]?.textContent).toContain("Kantoor");
    expect(rows[0]?.textContent).not.toContain("·");
  });

  it("teaches the next step when there is nothing yet", async () => {
    const page = await openPurchases([]);

    const empty = await within(page).findByTestId("purchases-empty");
    expect(empty.textContent).toContain("No purchase invoices yet");
    expect(empty.textContent).toContain("Add your first invoice above");
  });

  it("puts uploading at the top, above the list, and has no sub-menu", async () => {
    const page = await openPurchases([row()]);
    await within(page).findAllByTestId("purchase-row");

    const drop = within(page).getByTestId("capture-drop");
    const table = within(page).getByRole("table");
    // Uploading first, then the list it adds to.
    expect(drop.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(page).getByTestId("capture-choose-files")).toBeTruthy();
    // Embedded: no second heading, and no "finish this batch".
    expect(within(page).queryByTestId("capture-finalise")).toBeNull();
    expect(within(page).getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("asks for the category when files are added, as before", async () => {
    const page = await openPurchases([]);
    await within(page).findByTestId("purchases-empty");

    const file = new File([new Uint8Array([37, 80, 68, 70])], "invoice.pdf", {
      type: "application/pdf",
    });
    fireEvent.change(within(page).getByTestId("capture-file-input"), { target: { files: [file] } });

    expect(await screen.findByTestId("capture-category-picker")).toBeTruthy();
  });

  it("says so, with a way to retry, when the list cannot be loaded", async () => {
    stubApi({ [LIST]: () => new Response(null, { status: 503 }) });
    renderApp({ authenticated: true, language: "en" }, { route: "/purchases" });

    expect(await screen.findByTestId("screen-error")).toBeTruthy();
    expect(screen.getByTestId("screen-error-retry")).toBeTruthy();
  });

  it("has no automated WCAG 2.2 AA violations", async () => {
    const page = await openPurchases([
      row(),
      row({ id: "exp-2", status: "posted" }),
      row({ id: "exp-3", status: "ready", supplier: null }),
    ]);
    await within(page).findAllByTestId("purchase-row");

    assertNoAxeViolations(await axeViolations(document.body));
  });
});

const EXPENSE = "GET /v1/administrations/adm-A/expenses/exp-1";
const DOCUMENT = "GET /v1/administrations/adm-A/documents/doc-1/content";

async function openInvoice(view: ExpenseView) {
  // jsdom has no object URLs; the viewer needs one to show the original.
  vi.stubGlobal(
    "URL",
    Object.assign(URL, {
      createObjectURL: () => "blob:original",
      revokeObjectURL: () => {},
    }),
  );
  stubApi({
    [EXPENSE]: () => jsonResponse(view),
    [DOCUMENT]: () => new Response(new Blob(["%PDF-1.7"], { type: "application/pdf" })),
  });
  renderApp({ authenticated: true, language: "en" }, { route: "/purchases/exp-1" });
  return screen.findByTestId("purchase-detail");
}

describe("one invoice, opened for review", () => {
  it("says the invoice was read automatically and asks for a check", async () => {
    const page = await openInvoice(detail());

    expect(await within(page).findByTestId("expense-read-done")).toBeTruthy();
    expect(within(page).getByTestId("expense-read-done").textContent).toContain(
      "check the marked fields",
    );
  });

  it("marks only the fields the reading was not sure of", async () => {
    const page = await openInvoice(detail());

    // The amount (0.62) is flagged; the supplier (0.99) and date (0.97) are not.
    expect(await within(page).findByTestId("expense-gross-amount-check")).toBeTruthy();
    expect(within(page).queryByTestId("expense-supplier-check")).toBeNull();
    expect(within(page).queryByTestId("expense-date-check")).toBeNull();
  });

  it("shows the invoice number as a field of the invoice", async () => {
    const page = await openInvoice(detail());

    const input = (await within(page).findByTestId("expense-invoice-number")) as HTMLInputElement;
    expect(input.value).toBe("MFZ-9921");
  });

  it("says plainly when the invoice could not be read, rather than showing a form nobody tried to fill", async () => {
    const page = await openInvoice(
      detail({
        supplier: null,
        gross_amount: null,
        expense_date: null,
        extraction: { status: "failed", reason: "provider_refused", fields: {} },
      }),
    );

    expect(await within(page).findByTestId("expense-read-failed")).toBeTruthy();
    expect(within(page).queryByTestId("expense-read-done")).toBeNull();
    expect(within(page).queryByTestId("expense-gross-amount-check")).toBeNull();
  });

  it("says nothing about reading for an invoice nobody read", async () => {
    const page = await openInvoice(detail({ extraction: null }));

    await within(page).findByTestId("expense-supplier");
    expect(within(page).queryByTestId("expense-read-done")).toBeNull();
    expect(within(page).queryByTestId("expense-read-failed")).toBeNull();
  });

  it("shows the original beside the fields it was read from", async () => {
    const page = await openInvoice(detail());

    const frame = await within(page).findByTestId("purchase-original-frame");
    expect(frame.getAttribute("src")).toBe("blob:original");
  });

  it("has no original to show for an invoice without a stored document", async () => {
    const page = await openInvoice(detail({ document_id: null }));

    await within(page).findByTestId("expense-supplier");
    expect(within(page).queryByTestId("purchase-original-loading")).toBeNull();
    expect(within(page).queryByTestId("purchase-original-frame")).toBeNull();
  });

  it("goes back to the list", async () => {
    const page = await openInvoice(detail());

    const back = await within(page).findByTestId("purchase-back");
    expect(back.getAttribute("href")).toBe("/purchases");
  });

  it("returns to the list once the invoice is submitted", async () => {
    stubApi({
      [EXPENSE]: () => jsonResponse(detail({ missing_fields: [], can_be_marked_ready: true })),
      [LIST]: () => jsonResponse([row({ status: "ready" })]),
      "POST /v1/administrations/adm-A/expenses/exp-1/ready": () =>
        jsonResponse(detail({ status: "ready", missing_fields: [], can_be_marked_ready: true })),
    });
    renderApp({ authenticated: true, language: "en" }, { route: "/purchases/exp-1" });

    fireEvent.click(await screen.findByTestId("expense-submit"));

    await waitFor(() => expect(screen.getByTestId("purchases")).toBeTruthy());
  });

  it("has no automated WCAG 2.2 AA violations", async () => {
    // Without the original: axe cannot reach into a blob-URL frame under jsdom
    // ("Respondable target must be a frame in the current window"), and that is
    // a limit of the tool here, not of the page. The frame is checked for the one
    // thing this page controls about it - an accessible name - just below.
    const page = await openInvoice(detail({ document_id: null }));
    await within(page).findByTestId("expense-supplier");

    assertNoAxeViolations(await axeViolations(document.body));
  });

  it("gives the original an accessible name", async () => {
    const page = await openInvoice(detail());

    const frame = await within(page).findByTestId("purchase-original-frame");
    expect(frame.getAttribute("title")).toBe("Original invoice");
  });
});
