import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CaptureQueue } from "@ledgr/offline-queue";
import type { QueueStore, StoredCapture } from "@ledgr/offline-queue";

import { App } from "./App";
import { MemoryKeyVault, WebCryptoCipher } from "./capture/webCryptoCipher";
import type { SittingContext } from "./capture/useSitting";

afterEach(() => {
  vi.unstubAllGlobals();
});

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

// `AuthenticatedMobileShell` calls `captureQueue()`, whose real composition
// (capture/queue.ts) opens an IndexedDB database — unavailable in jsdom, the
// same reason every other suite touching the capture screen (e.g.
// CaptureScreen.test.tsx, MobileShell.test.tsx) builds its own `CaptureQueue`
// over a `MemoryStore` rather than going through the module singleton. Mocked
// here so App.test.tsx can exercise the real composition root's WIRING
// (does authenticated + a mobile context reach MobileShell) without needing a
// real IndexedDB.
vi.mock("./capture/queue", () => ({
  captureQueue: () =>
    new CaptureQueue({
      store: new MemoryStore(),
      cipher: new WebCryptoCipher(new MemoryKeyVault()),
      clock: { now: () => Date.now() },
      newId: () => crypto.randomUUID(),
    }),
}));

const mobileContext: SittingContext = {
  organizationId: "org-1",
  administrationId: "adm-A",
  fiscalYearId: "fy-2026",
  userId: "user-1",
};

describe("App", () => {
  it("renders without crashing", () => {
    render(<App />);
    expect(screen.getByText("LEDGR")).toBeDefined();
  });

  it("authenticated with no mobile context renders today's bare header only — the pre-existing, documented gap", () => {
    render(<App authenticated />);
    expect(screen.getByText("LEDGR")).toBeDefined();
    expect(screen.queryByTestId("mobile-shell-tabs")).toBeNull();
  });

  it("authenticated with a mobile context renders the five-tab mobile shell, opening on Home", async () => {
    render(<App authenticated mobileContext={mobileContext} />);
    expect(screen.getByTestId("mobile-shell-tabs")).toBeDefined();
    expect(screen.getByTestId("mobile-tab-home")).toBeDefined();
    expect(screen.getByTestId("mobile-tab-capture")).toBeDefined();
    // FR-UX-005: Home, not Capture, is where the shell opens.
    expect(screen.getByTestId("mobile-tab-home").getAttribute("aria-current")).toBe("page");

    // HomeScreen fetches the dashboard as soon as it mounts, which here means
    // a real `fetch` against a relative URL — the one thing this
    // composition-root test does not mock. Waited out so that its
    // (necessarily failing) settlement lands inside this test rather than as
    // an unwrapped state update blamed on whichever test runs next.
    await waitFor(() => expect(screen.getByTestId("home-error")).toBeDefined());
  });

  it("switching to Capture from Home still starts a sitting on its own (ADR-047)", async () => {
    render(<App authenticated mobileContext={mobileContext} />);
    await waitFor(() => expect(screen.getByTestId("home-error")).toBeDefined());

    fireEvent.click(screen.getByTestId("mobile-tab-capture"));

    // ADR-047: the capture screen starts a sitting on its own as soon as it
    // mounts, which here means a real `fetch` against a relative URL — the
    // one thing this composition-root test does not mock.
    await waitFor(() =>
      expect(screen.getByTestId("capture-problem").getAttribute("data-reason")).toBe(
        "offline_cannot_start",
      ),
    );
  });
});

/** Routes a stubbed `fetch` by URL, matching the badge endpoint and failing everything else. */
function fetchRoutedTo(activeClientResponse: () => Promise<Response> | Response) {
  return vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === "/v1/switcher/active") return activeClientResponse();
    // Every other call (dashboard, capture-sessions, ...) fails the same way
    // it does in the tests above, which don't stub fetch at all - this
    // describe block is only about the badge fetch.
    return new Response(null, { status: 500 });
  });
}

const activeBadgeWire = {
  administration_id: "adm-A",
  display_name: "Bakker IT",
  legal_name: "Bakker Consultancy B.V.",
  trade_name: "Bakker IT",
  kvk_number: "12345678",
  colour: "indigo",
  initials: "BI",
  colour_is_ambiguous: false,
  role: "Accountant",
  role_is_system: true,
  expires_at: null,
};

describe("AuthenticatedMobileShell: fetches the active-client badge on mount (FR-FRM-000a)", () => {
  it("fetches GET /v1/switcher/active and passes the resolved badge through to the header", async () => {
    vi.stubGlobal(
      "fetch",
      fetchRoutedTo(
        () =>
          new Response(JSON.stringify(activeBadgeWire), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    );

    render(<App authenticated mobileContext={mobileContext} language="nl" />);

    await waitFor(() => expect(screen.getByTestId("client-name").textContent).toBe("Bakker IT"));
    expect(screen.getByTestId("client-header").dataset.state).toBe("active");
  });

  it(
    "shows the neutral loading state while the fetch is in flight, and only renders " +
      "'no client selected' once the API has confirmed there is none - never the reverse order",
    async () => {
      let resolveFetch: (response: Response) => void = () => {};
      const pending = new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      });
      vi.stubGlobal(
        "fetch",
        fetchRoutedTo(() => pending),
      );

      render(<App authenticated mobileContext={mobileContext} language="nl" />);

      // The fetch has not answered yet: the header must show the neutral
      // loading state, not "no client selected" - that would be a false
      // signal for the entire window the request is in flight.
      expect(screen.getByTestId("client-header").dataset.state).toBe("loading");
      expect(screen.queryByText("Geen klant geselecteerd")).toBeNull();

      resolveFetch(
        new Response("null", { status: 200, headers: { "Content-Type": "application/json" } }),
      );

      await waitFor(() => expect(screen.getByTestId("client-header").dataset.state).toBe("none"));
      expect(screen.getByText("Geen klant geselecteerd")).toBeDefined();
    },
  );
});
