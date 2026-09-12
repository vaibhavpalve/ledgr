import { fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { hasMessage, I18nProvider, type Language } from "@ledgr/i18n";
import { BLOCKED_REASONS, CaptureQueue, QUEUE_STATES } from "@ledgr/offline-queue";
import type { NewCapture, QueueStore, StoredCapture } from "@ledgr/offline-queue";

import { CaptureQueuePurgeWarning, CaptureQueueStatus } from "./CaptureQueueStatus";
import { MemoryKeyVault, WebCryptoCipher } from "./webCryptoCipher";

/**
 * Every string comes from the catalogue (FR-LOC-001), so the panel needs a
 * language. Dutch by default, matching the product; the test that is ABOUT
 * language passes one explicitly.
 */
function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

/** IndexedDB does not exist in jsdom; the store is a port for this reason. */
class MemoryStore implements QueueStore {
  readonly records = new Map<string, StoredCapture>();

  async put(record: StoredCapture): Promise<void> {
    this.records.set(record.id, record);
  }

  async all(): Promise<readonly StoredCapture[]> {
    return [...this.records.values()];
  }

  async remove(id: string): Promise<void> {
    this.records.delete(id);
  }

  async clear(): Promise<void> {
    this.records.clear();
  }
}

const capture: NewCapture = {
  organizationId: "org-1",
  administrationId: "adm-A",
  fiscalYearId: "fy-2026",
  userId: "user-1",
  sessionId: "sess-1",
  receiptRef: "receipt-1",
  pageIndex: 0,
  source: "camera",
  filename: "receipt.jpg",
  contentType: "image/jpeg",
  image: new Uint8Array([0xff, 0xd8, 0xff, 0xe0]),
};

function build() {
  let millis = Date.parse("2026-09-06T10:00:00.000Z");
  const store = new MemoryStore();
  const queue = new CaptureQueue({
    store,
    // The real cipher: the panel is asserted to work over genuinely encrypted
    // records, not over a stub that would hide a decryption the snapshot must
    // not be doing.
    cipher: new WebCryptoCipher(new MemoryKeyVault()),
    clock: { now: () => (millis += 1_000) },
    newId: () => `id-${store.records.size}-${millis}`,
  });
  return { store, queue };
}

describe("CaptureQueueStatus — MOB-003's visible queue state", () => {
  it("says so plainly when nothing is waiting", async () => {
    const { queue } = build();

    render(<CaptureQueueStatus queue={queue} />);

    await waitFor(() =>
      expect(screen.getByTestId("capture-queue-summary").textContent).toContain(
        "Alle bonnen zijn geüpload.",
      ),
    );
  });

  it("counts what is waiting", async () => {
    const { queue } = build();
    await queue.enqueue(capture);
    await queue.enqueue(capture);

    render(<CaptureQueueStatus queue={queue} />);

    await waitFor(() =>
      expect(screen.getByTestId("capture-queue-summary").textContent).toContain(
        "2 bonnen wachten op uploaden",
      ),
    );
  });

  it("uses the singular for one receipt", async () => {
    const { queue } = build();
    await queue.enqueue(capture);

    render(<CaptureQueueStatus queue={queue} />);

    await waitFor(() =>
      expect(screen.getByTestId("capture-queue-summary").textContent).toContain(
        "1 bon wacht op uploaden",
      ),
    );
  });

  it("explains why nothing is moving when the device is offline", async () => {
    // The panel's most important state: a person who has just photographed six
    // receipts wants to know they survived, and silence is indistinguishable
    // from having lost them.
    const { queue } = build();
    await queue.enqueue(capture);
    queue.setOffline(true);

    render(<CaptureQueueStatus queue={queue} />);

    await waitFor(() =>
      expect(screen.getByTestId("capture-queue-offline").textContent).toContain(
        "Bonnen worden vanzelf geüpload zodra er weer verbinding is",
      ),
    );
  });

  it("renders in English when that is the language", async () => {
    const { queue } = build();
    await queue.enqueue(capture);

    render(<CaptureQueueStatus queue={queue} />, "en");

    await waitFor(() =>
      expect(screen.getByTestId("capture-queue-summary").textContent).toContain(
        "1 receipt waiting to upload",
      ),
    );
  });

  it("updates as the queue drains, without being re-rendered", async () => {
    const { queue } = build();
    await queue.enqueue(capture);
    render(<CaptureQueueStatus queue={queue} />);
    await waitFor(() => expect(screen.getAllByTestId("capture-queue-item")).toHaveLength(1));

    const [record] = await queue.records();
    await queue.markDelivered(record!);

    await waitFor(() =>
      expect(screen.getByTestId("capture-queue-delivered").textContent).toContain("1 bon geüpload"),
    );
    expect(screen.queryAllByTestId("capture-queue-item")).toHaveLength(0);
  });

  it("shows how much of the cap is spent — MOB-009", async () => {
    const { queue } = build();

    render(<CaptureQueueStatus queue={queue} />);

    await waitFor(() =>
      expect(screen.getByTestId("capture-queue-storage").textContent).toContain(
        "van 200,0 MB gebruikt",
      ),
    );
  });

  it("needs no encryption key to render — MOB-008's auto-lock must not hide the queue", async () => {
    const store = new MemoryStore();
    const cipher = new WebCryptoCipher(new MemoryKeyVault());
    const queue = new CaptureQueue({
      store,
      cipher,
      clock: { now: () => Date.parse("2026-09-06T10:00:00.000Z") },
      newId: () => `id-${store.records.size}`,
    });
    await queue.enqueue(capture);
    await cipher.destroyKey();

    render(<CaptureQueueStatus queue={queue} />);

    await waitFor(() =>
      expect(screen.getByTestId("capture-queue-summary").textContent).toContain("1 bon wacht"),
    );
  });
});

describe("every state and every refusal has a sentence (FR-UX-007, FR-LOC-001)", () => {
  // The panel builds these keys at runtime, so scripts/check_translations.py
  // cannot see them as literals and a new member would ship a raw identifier
  // onto somebody's screen. Walking the runtime arrays is what makes that a
  // build failure instead.
  it.each(QUEUE_STATES)("names the %s state", (state) => {
    expect(hasMessage(`capture.queue.state.${state}`)).toBe(true);
  });

  it.each(BLOCKED_REASONS)("explains %s", (reason) => {
    expect(hasMessage(`capture.queue.reason.${reason}`)).toBe(true);
  });
});

describe("captures the server refused", () => {
  async function withBlocked(reason: Parameters<CaptureQueue["markBlocked"]>[1]) {
    const { queue } = build();
    await queue.enqueue(capture);
    const [record] = await queue.records();
    // The attempted record, not the caller's earlier copy — the same thing the
    // uploader does, and what keeps the attempt count from being written back
    // to zero.
    const attempted = await queue.markUploading(record!);
    await queue.markBlocked(attempted, reason);
    return queue;
  }

  it("names the reason rather than showing a bare failure", async () => {
    // A photograph the person cannot use and no idea why is the worst state
    // this panel can leave somebody in.
    const queue = await withBlocked("unsupported_type");

    render(<CaptureQueueStatus queue={queue} />);

    await waitFor(() =>
      expect(screen.getByTestId("capture-queue-item").textContent).toContain(
        "Gebruik JPEG, PNG, HEIC of PDF",
      ),
    );
  });

  it("says a lost permission is a lost permission", async () => {
    const queue = await withBlocked("not_permitted");

    render(<CaptureQueueStatus queue={queue} />);

    await waitFor(() =>
      expect(screen.getByTestId("capture-queue-item").textContent).toContain(
        "Je mag geen bonnen meer indienen voor deze klant.",
      ),
    );
  });

  it("shows how many attempts have been made", async () => {
    const queue = await withBlocked("rejected");

    render(<CaptureQueueStatus queue={queue} />);

    await waitFor(() =>
      expect(screen.getByTestId("capture-queue-item").textContent).toContain("1 poging"),
    );
  });

  it("queues it again when the person retries, and asks for a drain", async () => {
    const queue = await withBlocked("not_permitted");
    const onRetry = vi.fn();
    render(<CaptureQueueStatus queue={queue} onRetry={onRetry} />);
    await waitFor(() => screen.getByTestId("capture-queue-retry"));

    fireEvent.click(screen.getByTestId("capture-queue-retry"));

    await waitFor(() =>
      expect(screen.getByTestId("capture-queue-item").getAttribute("data-state")).toBe("queued"),
    );
    expect(onRetry).toHaveBeenCalled();
  });

  it("removes it when the person discards it", async () => {
    const queue = await withBlocked("infected");
    render(<CaptureQueueStatus queue={queue} />);
    await waitFor(() => screen.getByTestId("capture-queue-discard"));

    fireEvent.click(screen.getByTestId("capture-queue-discard"));

    await waitFor(() => expect(screen.queryAllByTestId("capture-queue-item")).toHaveLength(0));
  });

  it("warns in the discard button's own name that the photograph goes too", async () => {
    // On an offline phone this queue holds the only copy, and a screen reader
    // user must hear that before the button, not after it.
    const queue = await withBlocked("rejected");

    render(<CaptureQueueStatus queue={queue} />);

    await waitFor(() =>
      expect(screen.getByTestId("capture-queue-discard").getAttribute("aria-label")).toBe(
        "Bon 1 weggooien; de foto wordt van dit apparaat verwijderd",
      ),
    );
  });

  it("gives each row's buttons a name that identifies which receipt", async () => {
    const { queue } = build();
    await queue.enqueue(capture);
    await queue.enqueue(capture);
    for (const record of await queue.records()) await queue.markBlocked(record, "rejected");

    render(<CaptureQueueStatus queue={queue} />);

    await waitFor(() => expect(screen.getAllByTestId("capture-queue-retry")).toHaveLength(2));
    const labels = screen
      .getAllByTestId("capture-queue-retry")
      .map((button) => button.getAttribute("aria-label"));
    expect(new Set(labels).size).toBe(2);
  });
});

describe("CaptureQueuePurgeWarning — MOB-009", () => {
  it("says what signing out would destroy", async () => {
    // MOB-009 requires the purge; it does not require it to be a surprise.
    render(<CaptureQueuePurgeWarning pending={3} />);

    expect(screen.getByTestId("capture-queue-purge-warning").textContent).toContain(
      "3 bonnen zijn nog niet geüpload en gaan verloren als je uitlogt.",
    );
  });

  it("uses the singular for one", () => {
    render(<CaptureQueuePurgeWarning pending={1} />);

    expect(screen.getByTestId("capture-queue-purge-warning").textContent).toContain(
      "1 bon is nog niet geüpload",
    );
  });

  it("says nothing when there is nothing at risk", () => {
    render(<CaptureQueuePurgeWarning pending={0} />);

    expect(screen.queryByTestId("capture-queue-purge-warning")).toBeNull();
  });
});
