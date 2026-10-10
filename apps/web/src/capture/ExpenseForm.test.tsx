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
  // Every expense this flow creates is born business_account (`add_item`'s
  // INSERT default) - a person is never asked, so it is never missing.
  payment_method: "business_account",
  suggested_category: null,
  missing_fields: ["expense_date", "supplier", "gross_amount", "vat_treatment", "category"],
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
  payment_method: "business_account",
  missing_fields: [],
  can_be_marked_ready: true,
};

function api(overrides: Partial<CaptureApi> = {}) {
  return {
    updateExpense: vi.fn(async () => complete),
    markReady: vi.fn(async () => ({ ...complete, status: "ready" as const })),
    readExpenseAgain: vi.fn(async () => ({
      ...complete,
      extraction: { status: "done" as const, reason: null, fields: { supplier: 0.95 } },
    })),
    ...overrides,
  } as unknown as CaptureApi;
}

function failedWith(reason: string | null, status: ExpenseView["status"] = "draft"): ExpenseView {
  return { ...blank, status, extraction: { status: "failed", reason, fields: {} } };
}

describe("a failed reading — ADR-095", () => {
  it("says a busy provider is worth another try", () => {
    render(
      <ExpenseForm
        administrationId="adm-A"
        expense={failedWith("provider_rate_limited")}
        api={api()}
      />,
      "en",
    );

    expect(screen.getByTestId("expense-read-failed").textContent).toContain("briefly unavailable");
  });

  it("says a refused key needs an administrator, not a retry", () => {
    render(
      <ExpenseForm
        administrationId="adm-A"
        expense={failedWith("provider_auth_failed")}
        api={api()}
      />,
      "en",
    );

    expect(screen.getByTestId("expense-read-failed").textContent).toContain("administrator");
  });

  it("keeps the general sentence for a reason it has no special words for", () => {
    render(
      <ExpenseForm administrationId="adm-A" expense={failedWith("nothing_found")} api={api()} />,
      "en",
    );

    expect(screen.getByTestId("expense-read-failed").textContent).toContain(
      "We couldn't read this invoice automatically",
    );
  });

  it("reads the stored invoice again and shows what came back", async () => {
    const calls = api();
    const onChanged = vi.fn();
    render(
      <ExpenseForm
        administrationId="adm-A"
        expense={failedWith("provider_rate_limited")}
        api={calls}
        onChanged={onChanged}
      />,
    );

    fireEvent.click(screen.getByTestId("expense-read-again"));

    await waitFor(() => expect(calls.readExpenseAgain).toHaveBeenCalledWith("adm-A", "exp-1"));
    await waitFor(() =>
      expect(onChanged).toHaveBeenCalledWith(expect.objectContaining({ supplier: "Café Central" })),
    );
  });

  it("shows the server's sentence when reading again is refused", async () => {
    const calls = api({
      readExpenseAgain: vi.fn(async () => {
        throw new ApiError(409, "extraction_unavailable", "Automatisch lezen staat niet aan.");
      }) as unknown as CaptureApi["readExpenseAgain"],
    });
    render(
      <ExpenseForm
        administrationId="adm-A"
        expense={failedWith("provider_rate_limited")}
        api={calls}
      />,
    );

    fireEvent.click(screen.getByTestId("expense-read-again"));

    await waitFor(() =>
      expect(screen.getByTestId("expense-problem").textContent).toContain("staat niet aan"),
    );
  });

  it("offers no re-read once the claim has been submitted", () => {
    render(
      <ExpenseForm
        administrationId="adm-A"
        expense={failedWith("provider_rate_limited", "ready")}
        api={api()}
      />,
    );

    expect(screen.queryByTestId("expense-read-again")).toBeNull();
  });
});

describe("a certain VAT rate submits without a person — FR-EXP-001c", () => {
  const autoSubmitted: ExpenseView = {
    ...complete,
    status: "ready",
    extraction: { status: "done", reason: null, fields: { supplier: 0.95, vat_rate: 0.99 } },
  };

  it("says it was read and submitted automatically, not to check it first", () => {
    render(<ExpenseForm administrationId="adm-A" expense={autoSubmitted} api={api()} />, "en");

    expect(screen.getByTestId("expense-read-submitted").textContent).toContain(
      "nothing left for you to review",
    );
    expect(screen.queryByTestId("expense-read-done")).toBeNull();
  });

  it("shows no editable fields or buttons — there is nothing left to do", () => {
    render(<ExpenseForm administrationId="adm-A" expense={autoSubmitted} api={api()} />);

    for (const field of [
      "expense-date",
      "expense-supplier",
      "expense-gross-amount",
      "expense-vat-treatment",
      "expense-category",
      "expense-submit",
    ]) {
      expect(screen.queryByTestId(field)).toBeNull();
    }
  });

  it("still shows the ordinary reading notice for a draft that was only read, not submitted", () => {
    const readOnly: ExpenseView = {
      ...blank,
      extraction: { status: "done", reason: null, fields: { supplier: 0.95 } },
    };
    render(<ExpenseForm administrationId="adm-A" expense={readOnly} api={api()} />, "en");

    expect(screen.getByTestId("expense-read-done")).toBeDefined();
    expect(screen.queryByTestId("expense-read-submitted")).toBeNull();
    expect(screen.getByTestId("expense-date")).toBeDefined();
  });
});

describe("the form asks for the minimum — FR-EXP-001b", () => {
  it("offers exactly the five fields a person still has to fill in", async () => {
    // Payment method (FR-EXP-001e) is no longer one of them: this flow is for
    // company-funded purchases only, so every expense is born business_account
    // (`add_item`'s INSERT default) and a person is never asked.
    render(<ExpenseForm administrationId="adm-A" expense={blank} api={api()} />);

    for (const field of [
      "expense-date",
      "expense-supplier",
      "expense-gross-amount",
      "expense-vat-treatment",
      "expense-category",
    ]) {
      expect(screen.getByTestId(field)).toBeDefined();
    }
    expect(screen.queryByTestId("expense-payment-method")).toBeNull();
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
});

describe("nothing blocks filling the form in, only submitting it — FR-EXP-001c", () => {
  it("sends only the fields that were touched, alongside submitting", async () => {
    // PATCH, not PUT: an omitted field must not mean "clear it", or one
    // changed field would wipe the other five. There is one button now
    // (submit), so touching a field and submitting is the only path there is.
    const calls = api();
    render(<ExpenseForm administrationId="adm-A" expense={complete} api={calls} />);

    fireEvent.change(screen.getByTestId("expense-category"), { target: { value: "Reiskosten" } });
    fireEvent.click(screen.getByTestId("expense-submit"));

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
    expect(missing).toContain("categorie");
  });
});

describe("money never goes through a float — NFR-031", () => {
  it("sends the amount as the string that was typed", async () => {
    // JSON has one number type and it is a double. A round trip through
    // `Number()` loses the value before the server can see it.
    const calls = api();
    render(<ExpenseForm administrationId="adm-A" expense={complete} api={calls} />);

    fireEvent.change(screen.getByTestId("expense-gross-amount"), {
      target: { value: "1234.56" },
    });
    fireEvent.click(screen.getByTestId("expense-submit"));

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
        invoice_number_match: "different",
      },
    ],
  });

  it("leaves the submit button enabled", async () => {
    // Two identical receipts can be legitimate. The API sends these alongside
    // `can_be_marked_ready: true` on purpose, and a client that gated on them
    // would refuse a claim the server is willing to accept.
    render(<ExpenseForm administrationId="adm-A" expense={withDuplicate(false)} api={api()} />);

    expect(screen.getByTestId("expense-submit").hasAttribute("disabled")).toBe(false);
  });

  it("does not repeat the warning here - it was said at the upload (ADR-116)", async () => {
    // The list of look-alikes used to live in this form, once per matching
    // invoice. An invoice uploaded four times showed the same line four times,
    // to whoever opened it afterwards. It is announced once, when the file
    // lands (DuplicateUploadNotices), and not again on the invoice.
    render(<ExpenseForm administrationId="adm-A" expense={withDuplicate(true)} api={api()} />);

    expect(screen.queryByTestId("expense-duplicates")).toBeNull();
    expect(screen.queryByTestId("expense-duplicate")).toBeNull();
    expect(screen.queryByText(/Café Central/)).toBeNull();
    expect(screen.queryByTestId("expense-duplicate-blocked")).toBeNull();
  });
});

describe("a confirmed duplicate blocks submission — ADR-101", () => {
  const confirmed: ExpenseView = {
    ...complete,
    can_be_marked_ready: false,
    duplicate_warnings: [
      {
        expense_id: "exp-9",
        strength: "strong",
        supplier: "Mistral AI SAS",
        expense_date: "2026-09-28",
        gross_amount: "10.00",
        status: "ready",
        same_submitter: true,
        similarity: 1,
        invoice_number_match: "same",
      },
    ],
  };

  it("says why Submit is disabled, in one sentence, and lists nothing", async () => {
    // ADR-116 moved every duplicate warning to the upload. What stays is the one
    // line a disabled button needs: a button that refuses without saying why is
    // worse than any banner.
    render(<ExpenseForm administrationId="adm-A" expense={confirmed} api={api()} />, "en");

    expect(screen.getByTestId("expense-duplicate-blocked").textContent).toContain(
      "same invoice number is already on your list",
    );
    expect(screen.queryByTestId("expense-duplicates")).toBeNull();
    expect(screen.queryByTestId("expense-duplicate")).toBeNull();
  });

  it("disables the submit button, unlike an ordinary duplicate", async () => {
    render(<ExpenseForm administrationId="adm-A" expense={confirmed} api={api()} />);

    expect(screen.getByTestId("expense-submit").hasAttribute("disabled")).toBe(true);
  });
});

describe("the way out of a confirmed duplicate - ADR-117", () => {
  const blocked: ExpenseView = {
    ...complete,
    can_be_marked_ready: false,
    duplicate_warnings: [
      {
        expense_id: "exp-9",
        strength: "strong",
        supplier: "Mistral AI SAS",
        expense_date: "2026-09-28",
        gross_amount: "10.00",
        status: "ready",
        same_submitter: true,
        similarity: 1,
        invoice_number_match: "same",
      },
    ],
  };

  it("offers to discard the invoice beside the sentence that explains the disabled button", () => {
    render(
      <ExpenseForm
        administrationId="adm-A"
        expense={blocked}
        api={api()}
        onDiscard={vi.fn(async () => {})}
      />,
      "en",
    );

    const note = screen.getByTestId("expense-duplicate-blocked");
    expect(note.textContent).toContain("can't be submitted");
    expect(note.contains(screen.getByTestId("expense-discard"))).toBe(true);
  });

  it("offers nothing the screen cannot finish", () => {
    render(<ExpenseForm administrationId="adm-A" expense={blocked} api={api()} />, "en");

    expect(screen.queryByTestId("expense-discard")).toBeNull();
  });

  it("asks before it removes the invoice, and keeps it if the answer is no", async () => {
    const onDiscard = vi.fn(async () => {});
    render(
      <ExpenseForm administrationId="adm-A" expense={blocked} api={api()} onDiscard={onDiscard} />,
      "en",
    );

    fireEvent.click(screen.getByTestId("expense-discard"));
    expect(onDiscard).not.toHaveBeenCalled();
    expect(screen.getByTestId("expense-duplicate-blocked").textContent).toContain("The file is kept");

    fireEvent.click(screen.getByTestId("expense-discard-keep"));
    expect(onDiscard).not.toHaveBeenCalled();
    expect(screen.getByTestId("expense-discard")).toBeTruthy();
  });

  it("discards once confirmed", async () => {
    const onDiscard = vi.fn(async () => {});
    render(
      <ExpenseForm administrationId="adm-A" expense={blocked} api={api()} onDiscard={onDiscard} />,
      "en",
    );

    fireEvent.click(screen.getByTestId("expense-discard"));
    fireEvent.click(screen.getByTestId("expense-discard-confirm"));

    await waitFor(() => expect(onDiscard).toHaveBeenCalledTimes(1));
  });

  it("shows the server's own sentence when it refuses, and leaves the invoice there", async () => {
    const onDiscard = vi.fn(async () => {
      throw new ApiError(409, "expense_not_discardable", "Only a draft can be thrown away.");
    });
    render(
      <ExpenseForm administrationId="adm-A" expense={blocked} api={api()} onDiscard={onDiscard} />,
      "en",
    );

    fireEvent.click(screen.getByTestId("expense-discard"));
    fireEvent.click(screen.getByTestId("expense-discard-confirm"));

    expect((await screen.findByTestId("expense-problem")).textContent).toContain(
      "Only a draft can be thrown away.",
    );
    expect(screen.getByTestId("expense-discard")).toBeTruthy();
  });

  it("does not offer it where nothing is blocked", () => {
    render(
      <ExpenseForm
        administrationId="adm-A"
        expense={complete}
        api={api()}
        onDiscard={vi.fn(async () => {})}
      />,
    );

    expect(screen.queryByTestId("expense-discard")).toBeNull();
  });
});

describe("a duplicate with no invoice number to compare - ADR-101", () => {
  const missingNumber: ExpenseView = {
    ...complete,
    duplicate_warnings: [
      {
        expense_id: "exp-9",
        strength: "strong",
        supplier: "Parkeergarage Centrum",
        expense_date: "2026-09-28",
        gross_amount: "4.50",
        status: "ready",
        same_submitter: true,
        similarity: 1,
        invoice_number_match: "missing",
      },
    ],
  };

  it("leaves submitting possible and adds nothing to the form", async () => {
    // Nothing rules a duplicate out, which the upload notice says; the form has
    // no reason to refuse and so no sentence to give.
    render(<ExpenseForm administrationId="adm-A" expense={missingNumber} api={api()} />, "en");

    expect(screen.queryByTestId("expense-duplicates")).toBeNull();
    expect(screen.queryByTestId("expense-duplicate-blocked")).toBeNull();
    expect(screen.getByTestId("expense-submit").hasAttribute("disabled")).toBe(false);
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
});
