import { render as renderBare, screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";
import type { ExpenseView } from "@ledgr/shared-types";

import { PurchaseDetails } from "./PurchaseDetails";

function render(ui: ReactElement, language: Language = "en") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

/** An invoice that was read automatically and submitted without a person. */
const submitted: ExpenseView = {
  id: "exp-1",
  status: "ready",
  capture_item_id: "item-1",
  expense_date: "2026-05-05",
  supplier: "KPN BV",
  gross_amount: "34.44",
  vat_treatment: "btw_21",
  vat_rate: "21.00",
  vat_amount: "5.98",
  net_amount: "28.46",
  category: "Phone & internet",
  invoice_number: "1377145720",
  payment_method: "business_account",
  suggested_category: null,
  missing_fields: [],
  can_be_marked_ready: true,
  duplicate_warnings: [],
  document_id: "doc-1",
  created_at: "2026-05-12T08:31:00+00:00",
  posted_at: null,
  original: {
    filename: "factuur-kpn-mei.pdf",
    content_type: "application/pdf",
    byte_size: 183_500,
    page_count: 4,
    source: "upload",
    captured_at: "2026-05-12T08:31:00+00:00",
  },
  extraction: {
    status: "done",
    reason: null,
    fields: { supplier: 0.99, gross_amount: 0.98 },
    read_at: "2026-05-12T08:31:04+00:00",
    submitted: true,
  },
};

const text = (id: string) => screen.getByTestId(id).textContent ?? "";

describe("a submitted invoice says everything that was read from it", () => {
  it("lists the invoice's own data, not just that nothing is left to do", () => {
    render(<PurchaseDetails expense={submitted} />);

    expect(text("detail-supplier")).toBe("KPN BV");
    expect(text("detail-invoice-number")).toBe("1377145720");
    expect(text("detail-date")).toContain("2026");
    expect(text("detail-total")).toContain("34,44");
    expect(text("detail-net")).toContain("28,46");
    expect(text("detail-vat")).toContain("5,98");
    expect(text("detail-category")).toBe("Phone & internet");
    expect(text("detail-payment")).toBe("Business account");
    expect(text("detail-status")).toBe("Ready to book");
  });

  it("describes the file: name, type, size and pages, and how it arrived", () => {
    render(<PurchaseDetails expense={submitted} />);

    expect(text("detail-file-name")).toBe("factuur-kpn-mei.pdf");
    const type = text("detail-file-type");
    expect(type).toContain("PDF");
    expect(type).toContain("179,2 KB");
    expect(type).toContain("4 pages");
    expect(text("detail-source")).toBe("Upload");
    expect(text("detail-reading")).toBe("Read automatically");
  });

  it("tells the story in order: added, read, submitted - and says it was automatic", () => {
    render(<PurchaseDetails expense={submitted} />);

    const history = within(screen.getByTestId("purchase-history"));
    const entries = history.getAllByRole("listitem").map((item) => item.textContent ?? "");
    expect(entries).toHaveLength(3);
    expect(entries[0]).toContain("Uploaded");
    expect(entries[1]).toContain("Read automatically");
    expect(entries[2]).toContain("Submitted automatically");
  });

  it("adds the booking to the history once it is booked, with the day it was", () => {
    render(
      <PurchaseDetails
        expense={{ ...submitted, status: "posted", posted_at: "2026-05-13T10:00:00+00:00" }}
      />,
    );

    const posted = screen.getByTestId("history-posted");
    expect(posted.textContent).toContain("Booked");
    expect(posted.textContent).toContain("13");
  });

  it("keeps no time for a submission a person made, and does not invent one", () => {
    const byPerson: ExpenseView = {
      ...submitted,
      extraction: { ...submitted.extraction!, submitted: false },
    };
    render(<PurchaseDetails expense={byPerson} />);

    const entry = screen.getByTestId("history-submitted");
    expect(entry.textContent).toBe("Submitted");
  });

  it("says a gap is a gap, in the invoice's own rows", () => {
    render(
      <PurchaseDetails
        expense={{ ...submitted, invoice_number: null, supplier: null, category: null }}
      />,
    );

    expect(text("detail-supplier")).toBe("—");
    expect(text("detail-invoice-number")).toBe("—");
    expect(text("detail-category")).toBe("—");
  });

  it("says a failed reading failed, and keeps the file facts", () => {
    render(
      <PurchaseDetails
        expense={{
          ...submitted,
          extraction: {
            status: "failed",
            reason: "nothing_found",
            fields: {},
            read_at: "2026-05-12T08:31:04+00:00",
          },
        }}
      />,
    );

    expect(text("detail-reading")).toBe("Could not be read");
    expect(screen.getByTestId("history-read_failed").textContent).toContain("Reading failed");
    expect(text("detail-file-name")).toBe("factuur-kpn-mei.pdf");
  });

  it("speaks Dutch", () => {
    render(<PurchaseDetails expense={submitted} />, "nl");

    expect(screen.getByTestId("purchase-invoice-data").textContent).toContain("Factuurgegevens");
    expect(text("detail-status")).toBe("Klaar om te boeken");
    expect(text("detail-reading")).toBe("Automatisch uitgelezen");
    expect(text("detail-file-type")).toContain("4 pagina's");
  });
});

describe("a draft is still being filled in", () => {
  const draft: ExpenseView = { ...submitted, status: "draft", extraction: { ...submitted.extraction!, submitted: false } };

  it("does not repeat the form's own fields as a read-only copy", () => {
    // A copy of a field somebody is typing into is stale by the next keystroke.
    render(<PurchaseDetails expense={draft} />);

    expect(screen.queryByTestId("purchase-invoice-data")).toBeNull();
    expect(screen.queryByTestId("detail-supplier")).toBeNull();
  });

  it("still shows the file and what happened to it", () => {
    render(<PurchaseDetails expense={draft} />);

    expect(text("detail-file-name")).toBe("factuur-kpn-mei.pdf");
    expect(screen.queryByTestId("history-submitted")).toBeNull();
    expect(screen.getByTestId("history-read")).toBeTruthy();
  });

  it("shows only what it knows for an invoice with no stored original", () => {
    render(<PurchaseDetails expense={{ ...draft, original: null, extraction: null }} />);

    expect(screen.queryByTestId("detail-file-name")).toBeNull();
    expect(text("detail-reading")).toBe("Not read");
  });
});
