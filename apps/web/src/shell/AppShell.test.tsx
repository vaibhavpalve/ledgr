import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CaptureQueue } from "@ledgr/offline-queue";
import type { QueueStore, StoredCapture } from "@ledgr/offline-queue";

import { MemoryKeyVault, WebCryptoCipher } from "../capture/webCryptoCipher";
import { assertNoAxeViolations, axeViolations } from "../testing/axe";
import { jsonResponse } from "../testing/fakeFetch";
import { renderApp } from "../testing/renderApp";
import { meFixture } from "../testing/session";

/**
 * The rail, in the Boekje design's shape: one "Bookkeeping" group of nine
 * sections, Settings, and who is signed in at the foot. What is asserted is the
 * menu's CONTENT and ORDER, because that is what the design specifies and what
 * a restyle could silently drop.
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

// The real queue opens IndexedDB, which jsdom does not have (see App.test.tsx).
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

function stubApi() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const key = `${init?.method ?? "GET"} ${String(input)}`;
      if (key === "GET /v1/me") return jsonResponse(meFixture());
      if (key === "GET /v1/me/language") {
        return jsonResponse({ language: null, supported: ["en", "nl"] });
      }
      return new Response(null, { status: 500 });
    }) as unknown as typeof fetch,
  );
}

async function railAt(route: string, language: "en" | "nl" = "en") {
  stubApi();
  renderApp({ authenticated: true, language }, { route });
  return screen.findByTestId("shell-rail");
}

const labelsIn = (rail: HTMLElement) =>
  within(rail)
    .getAllByRole("link")
    .map((link) => link.textContent?.trim());

describe("the rail — the Boekje design's menu", () => {
  it("lists the nine sections in the design's order, then Settings", async () => {
    const rail = await railAt("/");

    expect(within(rail).getByText("Bookkeeping")).toBeTruthy();
    expect(labelsIn(rail)).toEqual([
      "Overview",
      "Sales",
      "Purchases",
      "Bank",
      "Journal",
      "Grootboek",
      "Assets",
      "Contacts",
      "Reports",
      "Settings",
    ]);
  });

  it("has ONE Purchases item and no sub-menu: capture and review live inside it", async () => {
    const rail = await railAt("/");

    const purchases = within(rail).getByRole("link", { name: /^Purchases/ });
    expect(purchases.getAttribute("href")).toBe("/purchases");
    // The three screens it replaced have no menu entries of their own.
    for (const gone of ["Capture", "Review", "All purchases"]) {
      expect(within(rail).queryByRole("link", { name: gone })).toBeNull();
    }
  });

  it("does not use one name for two rows", async () => {
    const rail = await railAt("/");

    const names = labelsIn(rail);
    expect(new Set(names).size).toBe(names.length);
  });

  it("shows who is signed in and their role, with sign out one click away", async () => {
    const rail = await railAt("/");

    const card = within(rail).getByTestId("shell-user");
    expect(card.textContent).toContain(meFixture().user.email);
    expect(card.textContent).toContain("Owner");
    expect(within(card).getByTestId("nav-sign-out").getAttribute("aria-label")).toBe("Sign out");
  });

  it("is in Dutch when the reader is", async () => {
    const rail = await railAt("/", "nl");

    expect(within(rail).getByText("Boekhouding")).toBeTruthy();
    expect(within(rail).getByText("Inkoop")).toBeTruthy();
    expect(within(rail).getByText("Memoriaal")).toBeTruthy();
    expect(within(rail).getByText("Relaties")).toBeTruthy();
  });

  it("has no automated WCAG 2.2 AA violations", async () => {
    await railAt("/");

    assertNoAxeViolations(await axeViolations(document.body));
  });
});

describe("the old purchase addresses", () => {
  it.each([
    ["/capture", "purchases"],
    ["/review", "purchases"],
    ["/overview", "purchases"],
  ])("%s lands on the purchases screen", async (route, testId) => {
    stubApi();
    renderApp({ authenticated: true, language: "en" }, { route });

    expect(await screen.findByTestId(testId)).toBeTruthy();
  });
});

describe("rail items whose screens are not built yet", () => {
  it.each([
    ["/bank", "coming-soon-bank", "Bank"],
    ["/reports", "coming-soon-reports", "Reports"],
  ])("%s says so instead of opening an empty screen", async (route, testId, name) => {
    stubApi();
    renderApp({ authenticated: true, language: "en" }, { route });

    const screenEl = await screen.findByTestId(testId);
    expect(within(screenEl).getByRole("heading", { name })).toBeTruthy();
    expect(screenEl.textContent).toContain("Not available yet");
    expect(screenEl.textContent).toContain(`The ${name} screen has not been built yet`);
  });
});
