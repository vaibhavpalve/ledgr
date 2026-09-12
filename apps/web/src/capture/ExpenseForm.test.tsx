import { fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";
import type { ExpenseView } from "@ledgr/shared-types";

import { ApiError, type CaptureApi } from "./api";
import { ExpenseForm } from "./ExpenseForm";

function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

const blank: ExpenseView = {
  id: "exp-1",
  status: "draft",
  capture_item_id: "item-1",
  expense_date: null,
  supplier: null,
  gross_amount: null,
  vat_treatment: null,
  vat_rate: null,
  vat_amount: null,
  net_amount: null,
  category: null,
  payment_method: null,
  suggested_category: null,
  missing_fields: [
    "expense_date",
    "supplier",
    "gross_amount",
    "vat_treatment",
    "category",
    "payment_method",
  ],
  can_be_marked_ready: false,
  duplicate_warnings: [],
};

const complete: ExpenseView = {
  ...blank,
  expense_date: "2026-09-01",
  supplier: "Café Central",
  gross_amount: "121.00",
  vat_treatment: "btw_21",
  vat_rate: "21.00",
  vat_amount: "21.00",
  net_amount: "100.00",
  category: "Representatie",
  payment_method: "personal_reimbursable",
  missing_fields: [],
  can_be_marked_ready: true,
};

function api(overrides: Partial<CaptureApi> = {}) {
  return {
    updateExpense: vi.fn(async () => complete),
    markReady: vi.fn(async () => ({ ...complete, status: "ready" as const })),
    ...overrides,
  } as unknown as CaptureApi;
}

describe("the form asks for the minimum — FR-EXP-001b, FR-EXP-001e", () => {
  it("offers exactly the six fields the requirement names", async () => {
    render(<ExpenseForm administrationId="adm-A" expense={blank} api={api()} />);

    for (const field of [
      "expense-date",
      "expense-supplier",
      "expense-gross-amount",
      "expense-vat-treatment",
      "expense-category",
      "expense-payment-method",
    ]) {
      expect(screen.getByTestId(field)).toBeDefined();
    }
  });

  it("shows VAT, net and the rate without offering to type them", async () => {
    // The rate comes from the treatment and the date (CMP-014) and the amounts
    // are computed. An input for any of them would let this screen assert a
    // figure the database is about to disagree with.
    render(<ExpenseForm administrationId="adm-A" expense={complete} api={api()} />);

    expect(screen.getByTestId("expense-derived").textContent).toContain("21,00");
    expect(screen.getByTestId("expense-derived").textContent).toContain("100,00");
    expect(screen.queryByTestId("expense-vat-amount")).toBeNull();
    expect(screen.queryByTestId("expense-net-amount")).toBeNull();
  });

  it("names FR-EXP-001e's reimbursable option by its consequence", async () => {
    render(<ExpenseForm administrationId="adm-A" expense={blank} api={api()} />);

    expect(screen.getByTestId("expense-payment-method").textContent).toContain(
      "Zelf voorgeschoten",
    );
  });
});

describe("nothing blocks — FR-EXP-001c", () => {
  it("saves a form with only one field filled in", async () => {
    // "The product never blocks on extraction being available." A partly
    // filled expense is the normal state, not an error.
    const calls = api();
    render(<ExpenseForm administrationId="adm-A" expense={blank} api={calls} />);

    fireEvent.change(screen.getByTestId("expense-supplier"), { target: { value: "Café Central" } });
    fireEvent.click(screen.getByTestId("expense-save"));

    await waitFor(() =>
      expect(calls.updateExpense).toHaveBeenCalledWith("adm-A", "exp-1", {
        supplier: "Café Central",
      }),
    );
  });

  it("sends only the fields that were touched", async () => {
    // PATCH, not PUT: an omitted field must not mean "clear it", or saving one
    // change would wipe the other five.
    const calls = api();
    render(<ExpenseForm administrationId="adm-A" expense={complete} api={calls} />);

    fireEvent.change(screen.getByTestId("expense-category"), { target: { value: "Reiskosten" } });
    fireEvent.click(screen.getByTestId("expense-save"));

    await waitFor(() =>
      expect(calls.updateExpense).toHaveBeenCalledWith("adm-A", "exp-1", {
        category: "Reiskosten",
      }),
    );
  });

  it("lists everything still outstanding at once", async () => {
    render(<ExpenseForm administrationId="adm-A" expense={blank} api={api()} />);

    const missing = screen.getByTestId("expense-missing").textContent ?? "";
    expect(missing).toContain("datum");
    expect(missing).toContain("leverancier");
    expect(missing).toContain("betaalmethode");
  });
});

describe("money never goes through a float — NFR-031", () => {
  it("sends the amount as the string that was typed", async () => {
    // JSON has one number type and it is a double. A round trip through
    // `Number()` loses the value before the server can see it.
    const calls = api();
    render(<ExpenseForm administrationId="adm-A" expense={blank} api={calls} />);

    fireEvent.change(screen.getByTestId("expense-gross-amount"), {
      target: { value: "1234.56" },
    });
    fireEvent.click(screen.getByTestId("expense-save"));

    await waitFor(() => {
      const patch = vi.mocked(calls.updateExpense).mock.calls[0]?.[2];
      expect(patch?.gross_amount).toBe("1234.56");
      expect(typeof patch?.gross_amount).toBe("string");
    });
  });

  it("uses a text input rather than a number input", async () => {
    // `type="number"` hands back a value the browser has already parsed as a
    // double, which is the same loss one step earlier.
    render(<ExpenseForm administrationId="adm-A" expense={blank} api={api()} />);

    const input = screen.getByTestId("expense-gross-amount");
    expect(input.getAttribute("type")).toBe("text");
    expect(input.getAttribute("inputMode")).toBe("decimal");
  });

  it("renders stored amounts through the administration's locale", async () => {
    render(<ExpenseForm administrationId="adm-A" expense={complete} api={api()} />);

    expect(screen.getByTestId("expense-derived").textContent).toContain("21,00");
  });
});

describe("the category suggestion — FR-EXP-001b", () => {
  it("offers the previous choice rather than applying it", async () => {
    const suggested = { ...blank, suggested_category: "Representatie" };
    render(<ExpenseForm administrationId="adm-A" expense={suggested} api={api()} />);

    expect(screen.getByTestId("expense-suggestion").textContent).toContain("Representatie");
    expect((screen.getByTestId("expense-category") as HTMLInputElement).value).toBe("");
  });

  it("fills the field when the suggestion is accepted", async () => {
    const suggested = { ...blank, suggested_category: "Representatie" };
    render(<ExpenseForm administrationId="adm-A" expense={suggested} api={api()} />);

    fireEvent.click(screen.getByTestId("expense-use-suggestion"));

    expect((screen.getByTestId("expense-category") as HTMLInputElement).value).toBe(
      "Representatie",
    );
  });

  it("shows nothing once a category has been chosen", async () => {
    // The API sends null then, so a default cannot reappear to argue with a
    // decision already made.
    render(<ExpenseForm administrationId="adm-A" expense={complete} api={api()} />);

    expect(screen.queryByTestId("expense-suggestion")).toBeNull();
  });
});

describe("duplicate warnings warn and never block — FR-EXP-001g", () => {
  const withDuplicate = (sameSubmitter: boolean): ExpenseView => ({
    ...complete,
    duplicate_warnings: [
      {
        expense_id: "exp-9",
        strength: "strong",
        supplier: "Café Central",
        expense_date: "2026-09-01",
        gross_amount: "121.00",
        status: "ready",
        same_submitter: sameSubmitter,
        similarity: 0.97,
      },
    ],
  });

  it("shows the matching expense", async () => {
    render(<ExpenseForm administrationId="adm-A" expense={withDuplicate(true)} api={api()} />);

    const entry = screen.getByTestId("expense-duplicate").textContent ?? "";
    expect(entry).toContain("Café Central");
    expect(entry).toContain("121,00");
  });

  it("leaves the submit button enabled", async () => {
    // Two identical receipts can be legitimate. The API sends these alongside
    // `can_be_marked_ready: true` on purpose, and a client that gated on them
    // would refuse a claim the server is willing to accept.
    render(<ExpenseForm administrationId="adm-A" expense={withDuplicate(false)} api={api()} />);

    expect(screen.getByTestId("expense-submit").hasAttribute("disabled")).toBe(false);
  });

  it("says whether it was their own entry without naming a colleague", async () => {
    render(<ExpenseForm administrationId="adm-A" expense={withDuplicate(false)} api={api()} />);

    expect(screen.getByTestId("expense-duplicate").textContent).toContain("Iemand anders");
  });
});

describe("submitting", () => {
  it("is refused by the button until the server says the minimum is there", async () => {
    render(<ExpenseForm administrationId="adm-A" expense={blank} api={api()} />);

    expect(screen.getByTestId("expense-submit").hasAttribute("disabled")).toBe(true);
  });

  it("saves pending edits before releasing the claim", async () => {
    const calls = api();
    render(<ExpenseForm administrationId="adm-A" expense={complete} api={calls} />);

    fireEvent.change(screen.getByTestId("expense-supplier"), { target: { value: "Hotel Zon" } });
    fireEvent.click(screen.getByTestId("expense-submit"));

    await waitFor(() => expect(calls.markReady).toHaveBeenCalledWith("adm-A", "exp-1"));
    expect(calls.updateExpense).toHaveBeenCalledWith("adm-A", "exp-1", { supplier: "Hotel Zon" });
  });

  it("shows the server's own sentence when it refuses", async () => {
    // Already translated into the reader's language by the server (FR-UX-007),
    // and it names the specific refusal — this screen would only be guessing.
    const calls = api({
      markReady: vi.fn(async () => {
        throw new ApiError(422, "expense_incomplete", "Deze uitgave mist nog een categorie.");
      }) as unknown as CaptureApi["markReady"],
    });
    render(<ExpenseForm administrationId="adm-A" expense={complete} api={calls} />);

    fireEvent.click(screen.getByTestId("expense-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("expense-problem").textContent).toContain("mist nog een categorie"),
    );
  });

  it("reports the saved state so a person knows the work is not lost", async () => {
    const calls = api();
    render(<ExpenseForm administrationId="adm-A" expense={complete} api={calls} />);

    fireEvent.change(screen.getByTestId("expense-supplier"), { target: { value: "Hotel Zon" } });
    fireEvent.click(screen.getByTestId("expense-save"));

    await waitFor(() => expect(screen.getByTestId("expense-saved")).toBeDefined());
  });
});
