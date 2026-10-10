import { act, fireEvent, render as renderBare, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";
import type { CaptureDuplicate, CaptureQueue, DeliveredCapture } from "@ledgr/offline-queue";

import { DuplicateUploadNotices, useUploadNotices } from "./DuplicateUploadNotices";

/**
 * The part of a queue this feature touches: somebody to tell. The real queue's
 * `markDelivered` -> `onDelivered` is covered in packages/offline-queue; what is
 * asserted here is what the screen does with the answer.
 */
class FakeQueue {
  private readonly listeners = new Set<(event: DeliveredCapture) => void>();
  onDelivered(listener: (event: DeliveredCapture) => void) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  deliver(event: DeliveredCapture) {
    for (const listener of this.listeners) listener(event);
  }
}

const duplicate = (overrides: Partial<CaptureDuplicate> = {}): CaptureDuplicate => ({
  match: "same_file",
  expenseId: "exp-old",
  supplier: "Mistral AI SAS",
  expenseDate: "2026-09-28",
  grossAmount: "10.00",
  invoiceNumber: null,
  status: "ready",
  sameSubmitter: true,
  count: 1,
  invoiceNumberMatch: null,
  ...overrides,
});

const delivered = (overrides: Partial<DeliveredCapture> = {}): DeliveredCapture => ({
  receiptRef: "r-1",
  administrationId: "adm-A",
  filename: "mistral.pdf",
  expenseId: "exp-new",
  duplicate: duplicate(),
  ...overrides,
});

function Harness({ queue, administrationId = "adm-A" }: { queue: CaptureQueue; administrationId?: string }) {
  const { notices, dismiss } = useUploadNotices(queue, administrationId);
  return <DuplicateUploadNotices notices={notices} onDismiss={dismiss} />;
}

function mount(queue: FakeQueue, administrationId = "adm-A", language: Language = "en") {
  const ui: ReactElement = (
    <MemoryRouter>
      <I18nProvider initialLanguage={language}>
        <Harness queue={queue as unknown as CaptureQueue} administrationId={administrationId} />
      </I18nProvider>
    </MemoryRouter>
  );
  return renderBare(ui);
}

describe("telling the uploader at the upload — ADR-116", () => {
  it("says nothing until a duplicate arrives", () => {
    mount(new FakeQueue());

    expect(screen.queryByTestId("upload-duplicates")).toBeNull();
  });

  it("names the file and the invoice it repeats, once the file has landed", () => {
    const queue = new FakeQueue();
    mount(queue);

    act(() => queue.deliver(delivered()));

    const text = screen.getByTestId("upload-duplicate-text").textContent ?? "";
    expect(text).toContain("mistral.pdf");
    expect(text).toContain("Mistral AI SAS");
    expect(text).toContain("10,00");
    expect(screen.getByTestId("upload-duplicate").getAttribute("data-match")).toBe("same_file");
  });

  it("links to the invoice it repeats and to the new upload", () => {
    const queue = new FakeQueue();
    mount(queue);

    act(() => queue.deliver(delivered()));

    expect(screen.getByTestId("upload-duplicate-open-existing").getAttribute("href")).toBe(
      "/purchases/exp-old",
    );
    expect(screen.getByTestId("upload-duplicate-open-new").getAttribute("href")).toBe(
      "/purchases/exp-new",
    );
  });

  it("says nothing for an upload that is not a duplicate", () => {
    const queue = new FakeQueue();
    mount(queue);

    act(() => queue.deliver(delivered({ duplicate: null })));

    expect(screen.queryByTestId("upload-duplicates")).toBeNull();
  });

  it("is quiet about matching details with a different number - it may be a second invoice", () => {
    const queue = new FakeQueue();
    mount(queue);

    act(() =>
      queue.deliver(
        delivered({
          duplicate: duplicate({ match: "same_details", invoiceNumberMatch: "different" }),
        }),
      ),
    );

    const card = screen.getByTestId("upload-duplicate");
    expect(card.className).not.toContain("--strong");
    expect(screen.getByTestId("upload-duplicate-text").textContent).toContain(
      "different invoice number",
    );
  });

  it("is loud about the same invoice number, and says it cannot be submitted", () => {
    const queue = new FakeQueue();
    mount(queue);

    act(() =>
      queue.deliver(delivered({ duplicate: duplicate({ match: "same_invoice", count: 1 }) })),
    );

    expect(screen.getByTestId("upload-duplicate").className).toContain("--strong");
    expect(screen.getByTestId("upload-duplicate-text").textContent).toContain(
      "can't be submitted",
    );
  });

  it("counts the other invoices that look like it too", () => {
    const queue = new FakeQueue();
    mount(queue);

    act(() => queue.deliver(delivered({ duplicate: duplicate({ count: 4 }) })));

    expect(screen.getByTestId("upload-duplicate").textContent).toContain("3 other invoices");
  });

  it("says whether it was their own entry without naming a colleague", () => {
    const queue = new FakeQueue();
    mount(queue);

    act(() => queue.deliver(delivered({ duplicate: duplicate({ sameSubmitter: false }) })));

    expect(screen.getByTestId("upload-duplicate").textContent).toContain(
      "Someone else submitted an almost identical expense",
    );
  });

  it("stays until it is dismissed, and then goes", () => {
    const queue = new FakeQueue();
    mount(queue);
    act(() => queue.deliver(delivered()));

    fireEvent.click(screen.getByTestId("upload-duplicate-dismiss"));

    expect(screen.queryByTestId("upload-duplicate")).toBeNull();
  });

  it("does not announce another client's duplicate on this client's screen", () => {
    const queue = new FakeQueue();
    mount(queue, "adm-A");

    act(() => queue.deliver(delivered({ administrationId: "adm-B" })));

    expect(screen.queryByTestId("upload-duplicates")).toBeNull();
  });

  it("keeps one notice per upload when the same receipt is told again", () => {
    const queue = new FakeQueue();
    mount(queue);

    act(() => queue.deliver(delivered()));
    act(() => queue.deliver(delivered({ duplicate: duplicate({ match: "same_invoice" }) })));

    expect(screen.getAllByTestId("upload-duplicate")).toHaveLength(1);
    expect(screen.getByTestId("upload-duplicate").getAttribute("data-match")).toBe("same_invoice");
  });

  it("calls a photograph by something a person can recognise", () => {
    const queue = new FakeQueue();
    mount(queue);

    act(() => queue.deliver(delivered({ filename: null })));

    expect(screen.getByTestId("upload-duplicate-text").textContent).toContain("This upload");
  });

  it("speaks Dutch", () => {
    const queue = new FakeQueue();
    mount(queue, "adm-A", "nl");

    act(() => queue.deliver(delivered()));

    expect(screen.getByTestId("upload-duplicate").textContent).toContain(
      "Dit bestand staat al in uw lijst",
    );
  });
});
