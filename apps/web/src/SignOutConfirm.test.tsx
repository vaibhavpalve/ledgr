import { fireEvent, render as renderBare, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";

import { SignOutConfirm } from "./SignOutConfirm";
import { assertNoAxeViolations, axeViolations } from "./testing/axe";

function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

describe("SignOutConfirm — MOB-009's warning", () => {
  it("names the count of captures at risk", () => {
    render(<SignOutConfirm count={3} onCancel={vi.fn()} onConfirm={vi.fn()} />);
    expect(screen.getByTestId("sign-out-confirm").textContent).toContain("3");
  });

  it("pluralises correctly for exactly one capture", () => {
    render(<SignOutConfirm count={1} onCancel={vi.fn()} onConfirm={vi.fn()} />, "en");
    expect(screen.getByTestId("sign-out-confirm").textContent).toContain("1 receipt in the queue");
  });

  it("cancel calls back without confirming", () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    render(<SignOutConfirm count={1} onCancel={onCancel} onConfirm={onConfirm} />);

    fireEvent.click(screen.getByTestId("sign-out-confirm-cancel"));

    expect(onCancel).toHaveBeenCalledOnce();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("confirm calls back", () => {
    const onConfirm = vi.fn();
    render(<SignOutConfirm count={1} onCancel={vi.fn()} onConfirm={onConfirm} />);

    fireEvent.click(screen.getByTestId("sign-out-confirm-anyway"));

    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it("is an alertdialog with an accessible name, WCAG 2.2 SC 2.4.3", () => {
    render(<SignOutConfirm count={1} onCancel={vi.fn()} onConfirm={vi.fn()} />);
    const dialog = screen.getByTestId("sign-out-confirm");
    expect(dialog.getAttribute("role")).toBe("alertdialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.getAttribute("aria-label")).toBeTruthy();
  });

  it("no automated WCAG 2.2 AA violations, in each shipped language", async () => {
    for (const language of ["nl", "en"] as const) {
      const { container, unmount } = render(
        <SignOutConfirm count={2} onCancel={vi.fn()} onConfirm={vi.fn()} />,
        language,
      );
      assertNoAxeViolations(await axeViolations(container));
      unmount();
    }
  });
});
