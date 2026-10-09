import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";

import { ApiError } from "../api/http";
import { assertNoAxeViolations, axeViolations } from "../testing/axe";
import { testAdministration } from "../testing/session";
import type { FirmApiShape } from "./api";
import { fakeFirmApi, type FakeFirmApi } from "./fakeFirmApi";
import { FirmHomeScreen } from "./FirmHomeScreen";

/**
 * The firm home ("To do", docs/firm-home/contract.md) against an in-memory API: what an
 * accountant with many clients sees, how the worklist is steered, and that each panel fails on
 * its own.
 */

const session = {
  administrations: [testAdministration] as (typeof testAdministration)[],
  administration: null,
  switchAdministration: vi.fn(async () => undefined),
};

vi.mock("../session/SessionProvider", () => ({
  useSession: () => session,
}));

beforeEach(() => {
  session.administrations = [testAdministration];
  session.switchAdministration = vi.fn(async () => undefined);
});

afterEach(() => {
  vi.useRealTimers();
});

function renderHome(api: FirmApiShape, language: Language = "en") {
  return render(
    <I18nProvider initialLanguage={language}>
      <MemoryRouter initialEntries={["/todo"]}>
        <Routes>
          <Route path="/todo" element={<FirmHomeScreen api={api} />} />
          <Route path="/" element={<p data-testid="client-dashboard">dashboard</p>} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

async function ready(api: FakeFirmApi = fakeFirmApi(), language: Language = "en") {
  renderHome(api, language);
  await screen.findByTestId("firm-row-adm-1");
  return api;
}

const lastWorklistQuery = (api: FakeFirmApi) =>
  api.calls.filter((call) => call.method === "getWorklist").at(-1)?.args[0] as {
    chip: string;
    sort: string;
    dir: string;
    q: string;
    assigned: string;
  };

describe("the firm home renders the portfolio from the API", () => {
  it("shows the header, the six counts and the worklist rows", async () => {
    await ready();

    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("To do");
    expect(document.body.textContent).toMatch(
      /Welcome back\. Your last sign-in was .+, \d+ days? ago\./,
    );
    expect(screen.getByTestId("firm-review-open").textContent).toBe("Review 5 auto-bookings");

    const counts = screen.getByTestId("firm-counts");
    expect(within(counts).getAllByRole("button")).toHaveLength(6);
    const broken = screen.getByTestId("firm-count-broken_feeds");
    expect(broken.className).toContain("firm-count-tile--negative");
    // Never colour alone: the broken-feed count carries a word.
    expect(broken.textContent).toContain("Needs attention");

    const first = screen.getByTestId("firm-row-adm-1");
    expect(first.textContent).toContain("Bakkerij Jansen");
    expect(first.textContent).toContain("3 mo behind");
    expect(first.textContent).toContain("4 proposed");
    expect(first.textContent).toContain("2026-Q3");
    expect(first.textContent).toContain("To review");
    expect(first.textContent).toContain("23 d");
    // A zero is a muted dash, with words behind it for a screen reader.
    expect(first.querySelector(".firm-zero")?.textContent).toBe("–none");

    const second = screen.getByTestId("firm-row-adm-2");
    expect(within(second).getByTestId("firm-row-feed").textContent).toBe("ING bank feed expired");
    expect(second.textContent).toContain("limited by bank feed");
    expect(second.textContent).not.toContain("mo behind");
    expect(within(second).getByTestId("firm-row-due").textContent).toContain("within a week");
  });

  it("asks for My move, my clients, by risk, by default", async () => {
    const api = await ready();
    expect(lastWorklistQuery(api)).toMatchObject({
      chip: "my_move",
      assigned: "me",
      sort: "risk",
      dir: "asc",
      q: "",
    });
  });

  it("has no automated WCAG 2.2 AA violations", async () => {
    await ready();
    await screen.findByTestId("firm-reply-t-1");
    assertNoAxeViolations(await axeViolations(document.body));
  });
});

describe("steering the worklist", () => {
  it("switches chips, the assignee toggle and sorting on the server", async () => {
    const api = await ready();

    fireEvent.click(screen.getByTestId("firm-chip-waiting_on_client"));
    await waitFor(() => expect(lastWorklistQuery(api).chip).toBe("waiting_on_client"));
    expect(screen.getByTestId("firm-chip-waiting_on_client").getAttribute("aria-pressed")).toBe(
      "true",
    );

    fireEvent.click(screen.getByTestId("firm-assigned-any"));
    await waitFor(() => expect(lastWorklistQuery(api).assigned).toBe("any"));

    fireEvent.click(await screen.findByTestId("firm-sort-to_book"));
    await waitFor(() =>
      expect(lastWorklistQuery(api)).toMatchObject({ sort: "to_book", dir: "desc" }),
    );
    fireEvent.click(screen.getByTestId("firm-sort-to_book"));
    await waitFor(() => expect(lastWorklistQuery(api).dir).toBe("asc"));
  });

  it("a count points the worklist at the clients behind it", async () => {
    const api = await ready();

    fireEvent.click(screen.getByTestId("firm-count-missing_receipts"));
    await waitFor(() =>
      expect(lastWorklistQuery(api)).toMatchObject({
        chip: "waiting_on_client",
        sort: "missing_receipts",
        dir: "desc",
      }),
    );
  });

  it("filters by free text on the server, after typing settles", async () => {
    const api = await ready();

    fireEvent.change(screen.getByTestId("firm-filter"), { target: { value: "eva" } });
    await waitFor(() => expect(lastWorklistQuery(api).q).toBe("eva"));
    await waitFor(() => expect(screen.queryByTestId("firm-row-adm-1")).toBeNull());
    expect(screen.getByTestId("firm-row-adm-2")).toBeTruthy();
  });
});

describe("the keyboard: J / K move, X selects, Enter opens", () => {
  it("moves between rows and selects with X", async () => {
    await ready();

    fireEvent.keyDown(document.body, { key: "j" });
    expect(document.activeElement).toBe(screen.getByTestId("firm-open-adm-1"));
    fireEvent.keyDown(document.activeElement!, { key: "j" });
    expect(document.activeElement).toBe(screen.getByTestId("firm-open-adm-2"));
    fireEvent.keyDown(document.activeElement!, { key: "k" });
    expect(document.activeElement).toBe(screen.getByTestId("firm-open-adm-1"));

    fireEvent.keyDown(document.activeElement!, { key: "x" });
    expect((screen.getByTestId("firm-select-adm-1") as HTMLInputElement).checked).toBe(true);
    expect(screen.getByTestId("firm-bulk").textContent).toContain("1 selected");
  });

  it("ignores the keys while typing in the filter", async () => {
    await ready();
    const filter = screen.getByTestId("firm-filter");
    filter.focus();
    fireEvent.keyDown(filter, { key: "j" });
    expect(document.activeElement).toBe(filter);
  });

  it("Enter opens the client through the switcher, then its dashboard", async () => {
    await ready();

    fireEvent.keyDown(document.body, { key: "j" });
    fireEvent.keyDown(document.activeElement!, { key: "j" });
    fireEvent.keyDown(document.activeElement!, { key: "Enter" });

    await screen.findByTestId("client-dashboard");
    expect(session.switchAdministration).toHaveBeenCalledWith("adm-2");
  });
});

describe("bulk actions", () => {
  it("assigns the selection and reports a partial failure per client", async () => {
    const api = fakeFirmApi({
      assign: async () => ({
        assigned: 1,
        failed: [{ administration_id: "adm-2", reason: "No live grant on this client." }],
      }),
    });
    await ready(api);

    fireEvent.click(screen.getByTestId("firm-select-adm-1"));
    fireEvent.click(screen.getByTestId("firm-select-adm-2"));
    expect(screen.getByTestId("firm-bulk").textContent).toContain("2 selected");

    fireEvent.click(screen.getByTestId("firm-bulk-assign"));
    const dialog = await screen.findByTestId("firm-assign-dialog");
    expect(within(dialog).getByRole("heading").textContent).toBe("Assign 2 clients");
    await waitFor(() =>
      expect((screen.getByTestId("firm-assign-user") as HTMLSelectElement).value).toBe("user-1"),
    );
    fireEvent.change(screen.getByTestId("firm-assign-user"), { target: { value: "user-2" } });
    fireEvent.click(screen.getByTestId("firm-assign-submit"));

    expect((await screen.findByTestId("firm-assign-done")).textContent).toBe("1 client assigned.");
    const failures = screen.getByTestId("firm-assign-failures");
    expect(failures.textContent).toContain("1 could not be assigned:");
    expect(failures.textContent).toContain("Eva Mulder Design: No live grant on this client.");
    expect(api.calls.find((call) => call.method === "assign")?.args).toEqual([
      ["adm-1", "adm-2"],
      "user-2",
    ]);
    // The dialog stays open on a partial failure, so nothing is silently lost.
    expect(screen.getByTestId("firm-assign-dialog")).toBeTruthy();
  });

  it("scopes the review sheet to the selected clients", async () => {
    const api = await ready();

    fireEvent.click(screen.getByTestId("firm-select-adm-2"));
    fireEvent.click(screen.getByTestId("firm-bulk-review"));

    const sheet = await screen.findByTestId("firm-review-sheet");
    expect(sheet.textContent).toContain("For 1 selected client");
    await waitFor(() =>
      expect(api.calls.find((call) => call.method === "listProposals")?.args[0]).toEqual(["adm-2"]),
    );
  });

  it("snoozes one client from its row menu", async () => {
    const api = await ready();

    fireEvent.click(screen.getByTestId("firm-row-menu-adm-1"));
    fireEvent.click(screen.getByTestId("firm-row-snooze-adm-1"));
    await screen.findByTestId("firm-snooze-dialog");
    fireEvent.change(screen.getByTestId("firm-snooze-until"), { target: { value: "2026-12-01" } });
    fireEvent.change(screen.getByTestId("firm-snooze-reason"), {
      target: { value: "Wacht op jaarcijfers" },
    });
    fireEvent.click(screen.getByTestId("firm-snooze-submit"));

    await waitFor(() => expect(screen.queryByTestId("firm-snooze-dialog")).toBeNull());
    expect(api.calls.find((call) => call.method === "snooze")?.args).toEqual([
      "adm-1",
      "2026-12-01",
      "Wacht op jaarcijfers",
    ]);
  });
});

describe("the review sheet", () => {
  it("groups proposals, formats money from strings, and decides a whole group", async () => {
    const api = await ready();

    fireEvent.click(screen.getByTestId("firm-review-open"));
    const group = await screen.findByTestId("firm-review-group-kpn|4500");
    expect(group.textContent).toContain("KPN B.V.");
    expect(group.textContent).toContain("4500 Telefoon");
    expect(group.textContent).toContain("2 items");
    expect(group.textContent).toContain("2 clients");
    expect(group.textContent).toMatch(/€\s?1\.234,56/);

    // Nothing is decided by opening it.
    expect(api.calls.some((call) => call.method === "decideProposals")).toBe(false);

    fireEvent.click(screen.getByTestId("firm-review-toggle-kpn|4500"));
    expect(screen.getByTestId("firm-review-item-p-1").textContent).toContain("Bakkerij Jansen");

    const summariesBefore = api.calls.filter((call) => call.method === "getSummary").length;
    fireEvent.click(screen.getByTestId("firm-review-approve-all-kpn|4500"));

    expect((await screen.findByTestId("firm-review-result")).textContent).toBe(
      "2 approved, 0 rejected.",
    );
    expect(api.calls.find((call) => call.method === "decideProposals")?.args[0]).toEqual([
      { proposalId: "p-1", decision: "approve" },
      { proposalId: "p-2", decision: "approve" },
    ]);
    // The page refreshes its counts after a decision.
    await waitFor(() =>
      expect(api.calls.filter((call) => call.method === "getSummary").length).toBeGreaterThan(
        summariesBefore,
      ),
    );
  });

  it("shows which proposals failed, by client", async () => {
    const api = fakeFirmApi({
      decideProposals: async () => ({
        approved: 0,
        rejected: 0,
        failed: [{ proposal_id: "p-3", reason: "The period is closed." }],
      }),
    });
    await ready(api);

    fireEvent.click(screen.getByTestId("firm-review-open"));
    fireEvent.click(await screen.findByTestId("firm-review-toggle-shell|4310"));
    fireEvent.click(screen.getByTestId("firm-review-reject-p-3"));

    const failures = await screen.findByTestId("firm-review-failures");
    expect(failures.textContent).toContain("1 could not be processed:");
    expect(failures.textContent).toContain(
      "Bakkerij Jansen · Shell tankstation: The period is closed.",
    );
  });

  it("closes on Escape", async () => {
    await ready();
    fireEvent.click(screen.getByTestId("firm-review-open"));
    await screen.findByTestId("firm-review-sheet");
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByTestId("firm-review-sheet")).toBeNull();
  });
});

describe("the right column", () => {
  it("each panel fails on its own: a missing inbox endpoint costs one line", async () => {
    const api = fakeFirmApi({
      getInbox: async () => {
        throw new ApiError(404, null, "Not Found");
      },
    });
    await ready(api);

    expect(await screen.findByTestId("firm-replies-error")).toBeTruthy();
    expect(screen.getByTestId("firm-replies-error").textContent).toContain(
      "This panel could not be loaded.",
    );
    // The rest of the page is unaffected.
    expect(screen.getByTestId("firm-row-adm-1")).toBeTruthy();
    expect(await screen.findByTestId("firm-deadline-2026-Q3")).toBeTruthy();
    expect(screen.getByTestId("firm-counts")).toBeTruthy();
  });

  it("a failed worklist does not take the panels with it", async () => {
    const api = fakeFirmApi({
      getWorklist: async () => {
        throw new ApiError(500, null, "Server error");
      },
    });
    renderHome(api);

    expect(await screen.findByTestId("firm-worklist-error")).toBeTruthy();
    expect(await screen.findByTestId("firm-counts")).toBeTruthy();
    expect(await screen.findByTestId("firm-reply-t-1")).toBeTruthy();
  });

  it("labels the BTW progress bar in words", async () => {
    await ready();
    const deadline = await screen.findByTestId("firm-deadline-2026-Q3");
    expect(within(deadline).getByRole("img").getAttribute("aria-label")).toBe(
      "4 of 10 filed, 2 ready to file, 1 in review, 0 in progress, 3 not started",
    );
    expect(deadline.textContent).toContain("BTW 2026-Q3");
  });

  it("'since you were away' links into the worklist and can be marked as seen", async () => {
    const api = await ready();

    const line = await screen.findByTestId("firm-activity-receipts_uploaded");
    expect(line.textContent).toContain("12 receipts uploaded");
    expect(line.textContent).toContain("Bakkerij Jansen, Eva Mulder Design");
    fireEvent.click(line);
    await waitFor(() =>
      expect(lastWorklistQuery(api)).toMatchObject({ chip: "my_move", sort: "to_book" }),
    );

    fireEvent.click(screen.getByTestId("firm-mark-seen"));
    await waitFor(() => expect(api.calls.some((call) => call.method === "markSeen")).toBe(true));
  });

  it("collapses", async () => {
    await ready();
    const toggle = screen.getByTestId("firm-side-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(toggle);
    expect(screen.queryByTestId("firm-away")).toBeNull();
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });
});

describe("states", () => {
  it("teaches a firm with no clients what this page is for (FR-UX-004)", async () => {
    session.administrations = [];
    renderHome(fakeFirmApi());

    const empty = await screen.findByTestId("firm-home-empty");
    expect(empty.textContent).toContain("Your to-do list starts with a client");
    expect(screen.getByTestId("firm-add-first").getAttribute("href")).toBe("/onboarding");
  });

  it("hides the welcome line on a first sign-in", async () => {
    const api = fakeFirmApi({
      getSummary: async () => ({
        ...(await fakeFirmApi().getSummary()),
        previous_login_at: null,
      }),
    });
    await ready(api);
    expect(document.body.textContent).not.toContain("Welcome back");
  });

  it("is in Dutch for a Dutch reader", async () => {
    await ready(fakeFirmApi(), "nl");

    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Te doen");
    expect(screen.getByTestId("firm-chip-waiting_on_client").textContent).toContain(
      "Wacht op klant",
    );
    expect(screen.getByTestId("firm-review-open").textContent).toBe(
      "5 boekingsvoorstellen beoordelen",
    );
    expect(screen.getByTestId("firm-row-adm-1").textContent).toContain("3 mnd achter");
    expect(document.body.textContent).toContain("Welkom terug.");
  });
});

describe("debounce", () => {
  it("sends one request for a burst of keystrokes", async () => {
    const api = await ready();
    vi.useFakeTimers();
    const before = api.calls.filter((call) => call.method === "getWorklist").length;
    const filter = screen.getByTestId("firm-filter");
    fireEvent.change(filter, { target: { value: "b" } });
    fireEvent.change(filter, { target: { value: "ba" } });
    fireEvent.change(filter, { target: { value: "bak" } });
    await act(async () => {
      vi.advanceTimersByTime(350);
    });
    vi.useRealTimers();
    await waitFor(() =>
      expect(api.calls.filter((call) => call.method === "getWorklist").length).toBe(before + 1),
    );
    expect(lastWorklistQuery(api).q).toBe("bak");
  });
});
