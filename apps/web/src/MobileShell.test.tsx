import { act, fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";
import { CaptureQueue } from "@ledgr/offline-queue";
import type { QueueStore, StoredCapture } from "@ledgr/offline-queue";
import type { ClientBadge } from "@ledgr/shared-types";

import type { CaptureApi } from "./capture/api";
import type { DecodeFile } from "./capture/decode";
import { useSitting, type SittingContext } from "./capture/useSitting";
import { MemoryKeyVault, WebCryptoCipher } from "./capture/webCryptoCipher";
import type { DashboardApi } from "./home/api";
import type { SalesInvoiceApi } from "./invoicing/api";
import { MobileShell } from "./MobileShell";

function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

class MemoryStore implements QueueStore {
  readonly records = new Map<string, StoredCapture>();
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

const context: SittingContext = {
  organizationId: "org-1",
  administrationId: "adm-A",
  fiscalYearId: "fy-2026",
  userId: "user-1",
};

const decode: DecodeFile = async (file) => ({
  bytes: new Uint8Array([1, 2, 3]),
  contentType: file.type || "image/jpeg",
  quality: null,
});

function captureApi(): CaptureApi {
  return {
    listExpenses: vi.fn(async () => []),
  } as unknown as CaptureApi;
}

function invoiceApi(): SalesInvoiceApi {
  return {
    listInvoices: vi.fn(async () => []),
  } as unknown as SalesInvoiceApi;
}

function dashboardApi(): DashboardApi {
  return {
    getDashboard: vi.fn(async () => ({
      cash_position: "0.00",
      receivables: "0.00",
      vat_estimate: "0.00",
      vat_period_start: "2026-01-01",
      vat_period_end: "2026-09-09",
      items_needing_action: [],
    })),
  } as unknown as DashboardApi;
}

// ADR-047: mounting the Capture tab (the default) now starts a sitting on its
// own. `openSession` here resolves successfully, and `sessionPromise` is
// exposed so a test can await that settling explicitly — rather than leaving
// it to resolve on its own schedule and surface later as a state update
// nothing in the test was waiting for.
let sessionPromise: Promise<unknown> = Promise.resolve();
const openSession = vi.fn(() => {
  sessionPromise = Promise.resolve({
    id: "sess-1",
    open: true,
    opened_at: null,
    finalised_at: null,
    items: [],
    expenses_to_create: 0,
  });
  return sessionPromise;
});

// FR-FRM-000a: a resolved badge, used wherever a test isn't specifically
// about the loading/none-selected states.
const badge: ClientBadge = {
  administrationId: "adm-A",
  displayName: "Bakker IT",
  legalName: "Bakker Consultancy B.V.",
  tradeName: "Bakker IT",
  kvkNumber: "12345678",
  colour: "indigo",
  initials: "BI",
  colourIsAmbiguous: false,
};

function Harness({ badge: badgeProp }: { badge: ClientBadge | null | undefined }) {
  const queue = new CaptureQueue({
    store: new MemoryStore(),
    cipher: new WebCryptoCipher(new MemoryKeyVault()),
    clock: { now: () => Date.parse("2026-09-09T10:00:00.000Z") },
    newId: () => crypto.randomUUID(),
  });
  const sitting = useSitting({
    context,
    queue,
    api: { openSession, finaliseSession: vi.fn() } as unknown as CaptureApi,
  });
  return (
    <MobileShell
      administrationId="adm-A"
      fiscalYearId="fy-2026"
      badge={badgeProp}
      sitting={sitting}
      queue={queue}
      decode={decode}
      captureApi={captureApi()}
      invoiceApi={invoiceApi()}
      dashboardApi={dashboardApi()}
    />
  );
}

/**
 * Renders the harness and lets its auto-started sitting settle before
 * returning. Defaults to a resolved badge when the caller doesn't specify
 * one at all — but `{ badge: undefined }` (an explicit key, distinct from a
 * missing one via `in`) is honoured as-is, because that IS the "not yet
 * known" state under test below, and a default parameter on `badge` itself
 * would silently substitute the resolved fixture for it (default values
 * apply whenever the destructured value is `undefined`, which cannot tell
 * "omitted" apart from "explicitly undefined").
 */
async function renderHarness(props: { badge?: ClientBadge | null | undefined } = {}) {
  const badgeProp = "badge" in props ? props.badge : badge;
  const rendered = render(<Harness badge={badgeProp} />);
  await act(async () => {
    await sessionPromise;
  });
  return rendered;
}

describe("MobileShell — the five-section mobile navigation (§7.4, FR-UX-005, MOB-002/004/005/006)", () => {
  it("renders exactly five tabs", async () => {
    await renderHarness();

    expect(screen.getByTestId("mobile-tab-home")).toBeDefined();
    expect(screen.getByTestId("mobile-tab-capture")).toBeDefined();
    expect(screen.getByTestId("mobile-tab-approve")).toBeDefined();
    expect(screen.getByTestId("mobile-tab-view")).toBeDefined();
    expect(screen.getByTestId("mobile-tab-invoice")).toBeDefined();
  });

  it("opens on the Home tab by default (FR-UX-005: a summary, not straight into a task)", async () => {
    await renderHarness();

    await waitFor(() => expect(screen.getByTestId("home-figures")).toBeDefined());
    expect(screen.getByTestId("mobile-tab-home").getAttribute("aria-current")).toBe("page");
    expect(screen.queryByTestId("capture-take-photo")).toBeNull();
  });

  it("switches to the Capture tab and renders the existing CaptureScreen unchanged", async () => {
    await renderHarness();

    fireEvent.click(screen.getByTestId("mobile-tab-capture"));

    expect(screen.getByTestId("capture-take-photo")).toBeDefined();
    expect(screen.getByTestId("mobile-tab-capture").getAttribute("aria-current")).toBe("page");
  });

  it("switches to the Approve tab and renders its list", async () => {
    await renderHarness();

    fireEvent.click(screen.getByTestId("mobile-tab-approve"));

    await waitFor(() => expect(screen.getByTestId("approve-empty")).toBeDefined());
    expect(screen.queryByTestId("capture-take-photo")).toBeNull();
    expect(screen.getByTestId("mobile-tab-approve").getAttribute("aria-current")).toBe("page");
  });

  it("switches to the View tab and renders its lists", async () => {
    await renderHarness();

    fireEvent.click(screen.getByTestId("mobile-tab-view"));

    await waitFor(() => expect(screen.getByTestId("view-expenses-empty")).toBeDefined());
    expect(screen.getByTestId("view-invoices-empty")).toBeDefined();
  });

  it("switches to the Send-invoice tab and renders the form", async () => {
    await renderHarness();

    fireEvent.click(screen.getByTestId("mobile-tab-invoice"));

    expect(screen.getByTestId("invoice-submit")).toBeDefined();
  });

  it("only one tab's content is mounted at a time", async () => {
    await renderHarness();

    fireEvent.click(screen.getByTestId("mobile-tab-invoice"));
    expect(screen.queryByTestId("capture-take-photo")).toBeNull();

    fireEvent.click(screen.getByTestId("mobile-tab-capture"));
    expect(screen.getByTestId("capture-take-photo")).toBeDefined();
    expect(screen.queryByTestId("invoice-submit")).toBeNull();
  });
});

describe("FR-FRM-000a: the client header is persistent, not per-tab", () => {
  const TABS = ["home", "capture", "approve", "view", "invoice"] as const;

  it("is present on every one of the five tabs without exception", async () => {
    await renderHarness();

    for (const tab of TABS) {
      if (tab !== "home") fireEvent.click(screen.getByTestId(`mobile-tab-${tab}`));
      expect(screen.getByTestId("client-header")).toBeDefined();
      expect(screen.getByTestId("client-name").textContent).toBe("Bakker IT");
    }
  });

  it("renders before the tab content in DOM order", async () => {
    await renderHarness();

    const header = screen.getByTestId("client-header");
    const content = screen.getByTestId("mobile-shell-content");
    // DOCUMENT_POSITION_FOLLOWING on `content` relative to `header` means
    // `header` comes first - the header must never end up mounted after (and
    // therefore visually below) a tab's own content.
    const relativePosition = header.compareDocumentPosition(content);
    expect(Boolean(relativePosition & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true);
  });

  it("shows a neutral loading state, never 'no client selected', while the badge is still unknown", async () => {
    await renderHarness({ badge: undefined });

    expect(screen.getByTestId("client-header").dataset.state).toBe("loading");
    expect(screen.queryByText("Geen klant geselecteerd")).toBeNull();
  });

  it("shows 'no client selected' only once the API has confirmed there is none", async () => {
    await renderHarness({ badge: null });

    expect(screen.getByTestId("client-header").dataset.state).toBe("none");
  });

  it("shows the resolved client once the badge has arrived", async () => {
    await renderHarness({ badge });

    expect(screen.getByTestId("client-header").dataset.state).toBe("active");
    expect(screen.getByTestId("client-name").textContent).toBe("Bakker IT");
  });
});
