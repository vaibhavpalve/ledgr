import { StrictMode } from "react";
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CaptureQueue } from "@ledgr/offline-queue";
import type { QueueStore, StoredCapture } from "@ledgr/offline-queue";

import { App } from "../App";
import { MemoryKeyVault, WebCryptoCipher } from "../capture/webCryptoCipher";
import { jsonResponse } from "../testing/fakeFetch";
import { renderApp } from "../testing/renderApp";
import { meFixture } from "../testing/session";
import { fakeReceiptsApi } from "./fakeReceiptsApi";

/**
 * `/receipts-needed`: the bank payments still missing a receipt, each with the EXISTING capture
 * flow one press away - and nothing posted until that press.
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

// Whole-app renders: give the session bootstrap room on a loaded CI runner.
configure({ asyncUtilTimeout: 4000 });

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  window.history.replaceState(null, "", "/");
});

const SESSION = "POST /v1/administrations/adm-A/capture-sessions";

function stubApi() {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const key = `${init?.method ?? "GET"} ${String(input)}`;
      calls.push(key);
      if (key === "GET /v1/me") return jsonResponse(meFixture());
      if (key === "GET /v1/me/language") {
        return jsonResponse({ language: null, supported: ["en", "nl"] });
      }
      if (key === SESSION) {
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

describe("receipts needed", () => {
  it("lists each payment with its date, amount, payee and a status word", async () => {
    stubApi();
    const receipts = fakeReceiptsApi();
    renderApp(
      { authenticated: true, language: "en", services: { receipts } },
      { route: "/receipts-needed" },
    );

    const line = await screen.findByTestId("receipts-line-bt-1");
    expect(line.textContent).toContain("KPN B.V.");
    expect(line.textContent).toMatch(/70[.,]27/);
    expect(line.textContent).toContain("Receipt missing");
    expect(line.textContent).toContain("KPN factuur september");
    expect(line.textContent).toMatch(/2026|12/);
    // Exact decimals, never through a float: 1234.56 keeps both cents.
    const other = screen.getByTestId("receipts-line-bt-2");
    expect(other.textContent).toMatch(/1\D?234[.,]56/);
    expect(other.textContent).toContain("Unknown payee");
    expect(screen.getByText("2 payments need a receipt")).toBeTruthy();
    expect(receipts.calls).toEqual([{ method: "listMissing", args: ["adm-A"] }]);
  });

  it("says Nothing missing when every payment has a receipt", async () => {
    stubApi();
    renderApp(
      { authenticated: true, language: "en", services: { receipts: fakeReceiptsApi([]) } },
      { route: "/receipts-needed" },
    );
    expect((await screen.findByTestId("receipts-empty")).textContent).toContain("Nothing missing");
  });

  it("Upload receipt opens the existing capture flow, and only then starts a sitting", async () => {
    const calls = stubApi();
    renderApp(
      { authenticated: true, language: "en", services: { receipts: fakeReceiptsApi() } },
      { route: "/receipts-needed" },
    );

    const upload = await screen.findByTestId("receipts-upload-bt-1");
    expect(upload.getAttribute("aria-label")).toContain("KPN B.V.");
    expect(screen.queryByTestId("receipts-capture")).toBeNull();
    expect(calls).not.toContain(SESSION);

    fireEvent.click(upload);
    const panel = await screen.findByTestId("receipts-capture");
    expect(within(panel).getByRole("heading").textContent).toContain("KPN B.V.");
    // The capture screen's own intake (CaptureScreen), not a second uploader.
    expect(panel.querySelector('input[type="file"]')).not.toBeNull();
    await waitFor(() => expect(calls).toContain(SESSION));

    fireEvent.click(screen.getByTestId("receipts-capture-close"));
    await waitFor(() => expect(screen.queryByTestId("receipts-capture")).toBeNull());
  });

  it("reads in Dutch", async () => {
    stubApi();
    renderApp(
      { authenticated: true, language: "nl", services: { receipts: fakeReceiptsApi([]) } },
      { route: "/receipts-needed" },
    );
    expect((await screen.findByTestId("receipts-empty")).textContent).toContain("Niets ontbreekt");
  });

  it("works under StrictMode and posts nothing on mount", async () => {
    const calls = stubApi();
    render(
      <StrictMode>
        <MemoryRouter initialEntries={["/receipts-needed"]}>
          <App authenticated language="en" services={{ receipts: fakeReceiptsApi() }} />
        </MemoryRouter>
      </StrictMode>,
    );
    expect(await screen.findByTestId("receipts-line-bt-1")).toBeTruthy();
    // The account-language sync (IAM-010g, App.tsx) is the app's own; this screen posts nothing.
    expect(
      calls.filter((call) => !call.startsWith("GET ") && call !== "PUT /v1/me/language"),
    ).toEqual([]);
  });
});
