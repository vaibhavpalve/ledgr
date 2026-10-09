import { StrictMode } from "react";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CaptureQueue } from "@ledgr/offline-queue";
import type { QueueStore, StoredCapture } from "@ledgr/offline-queue";

import { App } from "../App";
import { MemoryKeyVault, WebCryptoCipher } from "../capture/webCryptoCipher";
import { jsonResponse } from "../testing/fakeFetch";
import { renderApp } from "../testing/renderApp";
import { meFixture, testAdministration } from "../testing/session";
import { fakeFirmApi } from "./fakeFirmApi";

/**
 * Where a firm user lands: with no client open, the firm home ("To do"); it is in the rail with
 * the client inbox and its unread count. A business user keeps their own dashboard.
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

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  window.history.replaceState(null, "", "/");
});

const firmMe = meFixture({
  organization: { id: "org-f", name: "Bakker & Co", kind: "firm", kvk_number: "11223344" },
  administrations: [testAdministration],
  active_administration_id: null,
});

function stubMe(me: ReturnType<typeof meFixture>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const key = `${init?.method ?? "GET"} ${String(input)}`;
      if (key === "GET /v1/me") return jsonResponse(me);
      if (key === "GET /v1/me/language") {
        return jsonResponse({ language: null, supported: ["en", "nl"] });
      }
      return new Response(null, { status: 500 });
    }) as unknown as typeof fetch,
  );
}

describe("routing the firm home", () => {
  it("a firm user with no client open lands on To do", async () => {
    stubMe(firmMe);
    renderApp(
      { authenticated: true, language: "en", services: { firm: fakeFirmApi() } },
      { route: "/" },
    );

    expect(await screen.findByTestId("firm-home")).toBeTruthy();
    const rail = screen.getByTestId("shell-rail");
    const todo = within(rail).getByTestId("nav-todo");
    expect(todo.getAttribute("href")).toBe("/todo");
    expect(todo.getAttribute("aria-current")).toBe("page");
    expect(within(rail).getByTestId("nav-inbox").textContent).toContain("Client inbox");
    expect((await within(rail).findByTestId("nav-inbox-count")).textContent).toBe("2");
  });

  it("the inbox lists client replies", async () => {
    stubMe(firmMe);
    renderApp(
      { authenticated: true, language: "en", services: { firm: fakeFirmApi() } },
      { route: "/inbox" },
    );

    expect(await screen.findByTestId("firm-inbox")).toBeTruthy();
    expect(await screen.findByText("Bonnetje Coolblue")).toBeTruthy();
  });

  it("a business user has no firm home: /todo goes to their dashboard", async () => {
    stubMe(meFixture());
    const firm = fakeFirmApi();
    renderApp({ authenticated: true, language: "en", services: { firm } }, { route: "/todo" });

    const rail = await screen.findByTestId("shell-rail");
    expect(within(rail).queryByTestId("nav-todo")).toBeNull();
    expect(screen.queryByTestId("firm-home")).toBeNull();
    expect(firm.calls).toHaveLength(0);
  });

  it("works under StrictMode (as `pnpm dev` runs it) and mutates nothing on mount", async () => {
    stubMe(firmMe);
    const firm = fakeFirmApi();
    render(
      <StrictMode>
        <MemoryRouter initialEntries={["/"]}>
          <App authenticated language="en" services={{ firm }} />
        </MemoryRouter>
      </StrictMode>,
    );

    expect(await screen.findByTestId("firm-row-adm-1")).toBeTruthy();
    expect(await screen.findByTestId("firm-reply-t-1")).toBeTruthy();
    expect(await screen.findByTestId("firm-deadline-2026-Q3")).toBeTruthy();
    const mutations = ["markSeen", "snooze", "assign", "decideProposals"];
    expect(firm.calls.filter((call) => mutations.includes(call.method))).toEqual([]);
  });
});
