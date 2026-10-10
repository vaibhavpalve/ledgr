import { StrictMode } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";

import { testAdministration } from "../testing/session";
import type { FirmApiShape } from "./api";
import { ClientRulesScreen } from "./ClientRulesScreen";
import {
  fakeFirmApi,
  fakeRow,
  fakeRows,
  fakeWorklist,
  FIRM_MUTATIONS,
  type FakeFirmApi,
} from "./fakeFirmApi";
import { FirmHomeScreen } from "./FirmHomeScreen";
import { clearLastQuery, readLastQuery, storeLastQuery } from "./lastQuery";
import { NextClientControl } from "./NextClient";
import { SavedViewList } from "./SavedViews";
import { isoDay, addDays } from "./useResource";

/**
 * Firm home, wave 2 (docs/firm-home/contract-wave2.md) against the in-memory API: "Always do
 * this" rules, requesting missing receipts, saved views, "Next client with work", and the
 * client's own rules screen.
 */

const session = {
  me: { organization: { kind: "firm" as "firm" | "business" } },
  administrations: [testAdministration] as (typeof testAdministration)[],
  administration: { id: "adm-2" } as { id: string } | null,
  switchAdministration: vi.fn(async (id: string) => {
    session.administration = { id };
  }),
};
const services: { firm: FirmApiShape } = { firm: fakeFirmApi() };

vi.mock("../session/SessionProvider", () => ({
  useSession: () => session,
}));
vi.mock("../session/ServicesProvider", () => ({
  useServices: () => services,
}));

beforeEach(() => {
  session.me = { organization: { kind: "firm" } };
  session.administrations = [testAdministration];
  session.administration = { id: "adm-2" };
  session.switchAdministration = vi.fn(async (id: string) => {
    session.administration = { id };
  });
  clearLastQuery();
});

afterEach(() => {
  clearLastQuery();
});

function wrap(
  children: React.ReactNode,
  language: Language = "en",
  entries: unknown[] = ["/todo"],
) {
  return render(
    <I18nProvider initialLanguage={language}>
      <MemoryRouter initialEntries={entries as never}>{children}</MemoryRouter>
    </I18nProvider>,
  );
}

async function home(api: FakeFirmApi = fakeFirmApi(), language: Language = "en") {
  wrap(
    <>
      <SavedViewList api={api} variant="rail" />
      <Routes>
        <Route path="/todo" element={<FirmHomeScreen api={api} />} />
        <Route path="/" element={<p data-testid="client-dashboard">dashboard</p>} />
      </Routes>
    </>,
    language,
  );
  await screen.findByTestId("firm-row-adm-1");
  return api;
}

const lastWorklistQuery = (api: FakeFirmApi) =>
  api.calls.filter((call) => call.method === "getWorklist").at(-1)?.args[0] as Record<
    string,
    unknown
  >;

describe("review sheet: Always do this for these clients", () => {
  it("explains a rule, sends remember on approve, and shows the rules made", async () => {
    const api = await home();
    fireEvent.click(screen.getByTestId("firm-review-open"));
    const group = await screen.findByTestId("firm-review-group-kpn|4500");
    expect(group.textContent).toContain("Always do this for these clients");
    expect(group.textContent).toContain("Never without a document");
    expect(group.textContent).toContain("booked to 4500 Telefoon without asking");

    fireEvent.click(screen.getByTestId("firm-review-remember-kpn|4500"));
    fireEvent.click(screen.getByTestId("firm-review-approve-all-kpn|4500"));

    const result = await screen.findByTestId("firm-review-result");
    expect(result.textContent).toBe("2 approved, 0 rejected. 2 rules created.");
    expect(api.calls.find((call) => call.method === "decideProposals")?.args[0]).toEqual([
      { proposalId: "p-1", decision: "approve", remember: true },
      { proposalId: "p-2", decision: "approve", remember: true },
    ]);
  });

  it("never sends remember on a rejection, nor when unticked", async () => {
    const api = await home();
    fireEvent.click(screen.getByTestId("firm-review-open"));
    await screen.findByTestId("firm-review-group-shell|4310");
    fireEvent.click(screen.getByTestId("firm-review-remember-shell|4310"));
    fireEvent.click(screen.getByTestId("firm-review-reject-all-shell|4310"));
    await screen.findByTestId("firm-review-result");
    expect(screen.queryByTestId("firm-review-rules-created")).toBeNull();
    expect(api.calls.find((call) => call.method === "decideProposals")?.args[0]).toEqual([
      { proposalId: "p-3", decision: "reject" },
    ]);
  });
});

describe("bulk: Request missing receipts", () => {
  it("previews each client with count, last chased and a reason in words, then sends", async () => {
    const api = await home();
    for (const id of ["adm-1", "adm-2", "adm-3"]) {
      fireEvent.click(screen.getByTestId(`firm-select-${id}`));
    }
    fireEvent.click(screen.getByTestId("firm-bulk-chase"));

    const dialog = await screen.findByTestId("firm-chase-dialog");
    expect(dialog.textContent).toContain("For 3 selected clients");
    expect(dialog.textContent).toContain("names no amounts or suppliers");
    const eva = await within(dialog).findByTestId("firm-chase-item-adm-2");
    expect(eva.textContent).toContain("6 receipts missing");
    expect(eva.textContent).toContain("never reminded");
    expect(eva.textContent).toContain("Will be sent");
    const jansen = within(dialog).getByTestId("firm-chase-item-adm-1");
    expect(jansen.textContent).toContain("3 receipts missing");
    expect(jansen.textContent).toMatch(/last reminded on .+/);
    expect(jansen.textContent).toContain("Skipped: already reminded in the last 24 hours");
    expect(within(dialog).getByTestId("firm-chase-item-adm-3").textContent).toContain(
      "Skipped: nothing is missing",
    );
    // Nothing goes out by opening the preview.
    expect(api.calls.some((call) => call.method === "sendChase")).toBe(false);

    const send = screen.getByTestId("firm-chase-send");
    expect(send.textContent).toBe("Send to 1 client");
    fireEvent.click(send);

    expect((await screen.findByTestId("firm-chase-result")).textContent).toBe("Sent to 1 client.");
    const skipped = screen.getByTestId("firm-chase-skipped");
    expect(skipped.textContent).toContain("2 skipped:");
    expect(skipped.textContent).toContain("Bakkerij Jansen: already reminded in the last 24 hours");
    expect(skipped.textContent).toContain("Hoveniersbedrijf De Linde: nothing is missing");
    expect(api.calls.find((call) => call.method === "sendChase")?.args[0]).toEqual([
      "adm-1",
      "adm-2",
      "adm-3",
    ]);
  });

  it("a row says when the client was last chased", async () => {
    const threeDaysAgo = `${isoDay(addDays(new Date(), -3))}T09:00:00`;
    const api = fakeFirmApi({
      getWorklist: async (query) =>
        fakeWorklist(query, [fakeRow({ last_chased_at: threeDaysAgo }), ...fakeRows.slice(1)]),
    });
    await home(api);
    const row = screen.getByTestId("firm-row-adm-1");
    expect(within(row).getByTestId("firm-row-chased").textContent).toBe("Chased 3 d ago");
    expect(
      within(screen.getByTestId("firm-row-adm-2")).queryByTestId("firm-row-chased"),
    ).toBeNull();
  });
});

describe("saved views", () => {
  it("lists views with their count, and a click applies the view's query", async () => {
    const api = await home();
    const item = await screen.findByTestId("nav-view-view-1");
    expect(item.textContent).toContain("BTW maand");
    expect(item.textContent).toContain("1");

    fireEvent.click(item);
    await waitFor(() =>
      expect(lastWorklistQuery(api)).toMatchObject({
        chip: "vat_not_filed",
        assigned: "any",
        sort: "vat_due",
        dir: "asc",
        vatFrequency: "monthly",
      }),
    );
    expect(screen.getByTestId("firm-chip-vat_not_filed").getAttribute("aria-pressed")).toBe("true");
    // The applied query is a saved view, so it can be renamed or archived from here.
    expect(screen.getByTestId("firm-view-manage").textContent).toBe("Edit view: BTW maand");
    expect(screen.getByTestId("firm-more-filters").textContent).toBe("More filters (1)");
  });

  it("saves the query on screen as a new view, and the rail shows it", async () => {
    const api = await home();
    fireEvent.click(screen.getByTestId("firm-chip-waiting_on_client"));
    await waitFor(() => expect(lastWorklistQuery(api).chip).toBe("waiting_on_client"));

    fireEvent.click(screen.getByTestId("firm-view-save"));
    await screen.findByTestId("firm-view-dialog");
    fireEvent.change(screen.getByTestId("firm-view-name"), { target: { value: "  Wacht  " } });
    fireEvent.click(screen.getByTestId("firm-view-submit"));

    await waitFor(() => expect(screen.queryByTestId("firm-view-dialog")).toBeNull());
    expect(api.calls.find((call) => call.method === "createView")?.args).toEqual([
      "Wacht",
      {
        chip: "waiting_on_client",
        q: "",
        assigned: "me",
        sort: "risk",
        dir: "asc",
        vat_frequency: null,
      },
    ]);
    expect((await screen.findByTestId("nav-view-view-2")).textContent).toContain("Wacht");
    expect(screen.getByTestId("firm-view-manage").textContent).toBe("Edit view: Wacht");
  });

  it("renames and archives a view", async () => {
    const api = await home();
    fireEvent.click(await screen.findByTestId("nav-view-view-1"));
    fireEvent.click(await screen.findByTestId("firm-view-manage"));
    fireEvent.change(await screen.findByTestId("firm-view-name"), {
      target: { value: "Maandaangiftes" },
    });
    fireEvent.click(screen.getByTestId("firm-view-submit"));
    await waitFor(() =>
      expect(screen.getByTestId("nav-view-view-1").textContent).toContain("Maandaangiftes"),
    );
    expect(api.calls.find((call) => call.method === "renameView")?.args).toEqual([
      "view-1",
      "Maandaangiftes",
    ]);

    fireEvent.click(screen.getByTestId("firm-view-manage"));
    fireEvent.click(await screen.findByTestId("firm-view-archive"));
    await waitFor(() => expect(screen.queryByTestId("nav-view-view-1")).toBeNull());
    expect(api.calls.find((call) => call.method === "archiveView")?.args).toEqual(["view-1"]);
    expect(screen.getByTestId("firm-view-save")).toBeTruthy();
  });

  it("a view whose stored query no longer validates says so instead of a count", async () => {
    const api = fakeFirmApi({
      listViews: async () => [
        {
          id: "view-9",
          name: "Oud",
          query: { chip: "my_move", q: "", assigned: "user-gone", sort: "risk", dir: "asc" },
          count: null,
        },
      ],
    });
    await home(api);
    const item = await screen.findByTestId("nav-view-view-9");
    expect(within(item).getByTestId("firm-view-stale").textContent).toBe("needs updating");
  });

  it("More filters narrows the list to a BTW frequency on the server", async () => {
    const api = await home();
    fireEvent.click(screen.getByTestId("firm-more-filters"));
    fireEvent.change(screen.getByTestId("firm-filter-vat-frequency"), {
      target: { value: "quarterly" },
    });
    await waitFor(() => expect(lastWorklistQuery(api).vatFrequency).toBe("quarterly"));
    // And it is what "Next client with work" will follow.
    expect(readLastQuery()).toMatchObject({ chip: "my_move", vat_frequency: "quarterly" });
  });

  it("reads in Dutch", async () => {
    await home(fakeFirmApi(), "nl");
    expect(screen.getByTestId("firm-view-save").textContent).toBe("Weergave opslaan");
    expect(screen.getByTestId("firm-more-filters").textContent).toBe("Meer filters");
    expect((await screen.findByTestId("nav-views")).textContent).toContain("Opgeslagen weergaven");
  });
});

describe("Next client with work", () => {
  const query = {
    chip: "my_move" as const,
    q: "",
    assigned: "me",
    sort: "risk" as const,
    dir: "asc" as const,
    vat_frequency: null,
  };

  function inClient(api: FakeFirmApi, language: Language = "en") {
    services.firm = api;
    return wrap(
      <>
        <NextClientControl />
        <Routes>
          <Route path="/bank" element={<p data-testid="bank">bank</p>} />
          <Route path="/" element={<p data-testid="client-dashboard">dashboard</p>} />
        </Routes>
      </>,
      language,
      ["/bank"],
    );
  }

  it("moves through the worklist's clients, then says the list is done", async () => {
    storeLastQuery(query);
    const api = fakeFirmApi();
    inClient(api);

    expect((await screen.findByTestId("next-client-remaining")).textContent).toBe("2 left");
    expect(screen.getByTestId("next-client-button").textContent).toContain(
      "Next client with work →",
    );
    expect(api.calls.find((call) => call.method === "nextClient")?.args).toEqual(["adm-2", query]);

    fireEvent.click(screen.getByTestId("next-client-button"));
    await screen.findByTestId("client-dashboard");
    expect(session.switchAdministration).toHaveBeenCalledWith("adm-1");
    expect((await screen.findByTestId("next-client-remaining")).textContent).toBe("1 left");

    fireEvent.click(screen.getByTestId("next-client-button"));
    await waitFor(() => expect(session.switchAdministration).toHaveBeenCalledWith("adm-3"));

    const done = await screen.findByTestId("next-client-done");
    expect(done.textContent).toContain("No more clients with work in this list.");
    expect(within(done).getByTestId("next-client-back").getAttribute("href")).toBe("/todo");
  });

  it("is hidden without a stored worklist query", async () => {
    const api = fakeFirmApi();
    inClient(api);
    await screen.findByTestId("bank");
    expect(screen.queryByTestId("next-client")).toBeNull();
    expect(api.calls).toHaveLength(0);
  });

  it("is hidden for a business user", async () => {
    storeLastQuery(query);
    session.me = { organization: { kind: "business" } };
    const api = fakeFirmApi();
    inClient(api);
    await screen.findByTestId("bank");
    expect(screen.queryByTestId("next-client")).toBeNull();
    expect(api.calls).toHaveLength(0);
  });

  it("survives storage that refuses (private mode)", async () => {
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    try {
      inClient(fakeFirmApi());
      await screen.findByTestId("bank");
      expect(screen.queryByTestId("next-client")).toBeNull();
    } finally {
      spy.mockRestore();
    }
  });

  it("reads in Dutch", async () => {
    storeLastQuery(query);
    inClient(fakeFirmApi(), "nl");
    expect((await screen.findByTestId("next-client-remaining")).textContent).toBe("nog 2");
    expect(screen.getByTestId("next-client-button").textContent).toContain(
      "Volgende klant met werk →",
    );
  });
});

describe("the client rules screen", () => {
  function rules(api: FakeFirmApi, language: Language = "en") {
    return wrap(<ClientRulesScreen api={api} administrationId="adm-1" />, language, ["/rules"]);
  }

  it("lists rules with their status in words, and the bookings they made", async () => {
    rules(fakeFirmApi());
    const active = await screen.findByTestId("rule-rule-1");
    expect(active.textContent).toContain("KPN B.V.");
    expect(active.textContent).toContain("to 4500 Telefoon");
    expect(active.textContent).toContain("Active");
    expect(active.textContent).toContain("No maximum");
    expect(active.textContent).toContain("2 bookings");
    expect(active.textContent).toContain("made by Sanne de Vries");

    const paused = screen.getByTestId("rule-rule-2");
    expect(paused.textContent).toContain("Paused");
    expect(screen.getByTestId("rule-reason-rule-2").textContent).toContain(
      "can no longer reconcile",
    );
    expect(paused.textContent).toMatch(/Up to €\s?150,00/);

    const posting = await screen.findByTestId("rule-posting-p-10");
    expect(posting.textContent).toMatch(/€\s?70,27/);
    expect(screen.getByTestId("rule-posting-final-p-11").textContent).toBe(
      "Can no longer be undone here.",
    );
  });

  it("sets a maximum from what a Dutch reader types, as a decimal string", async () => {
    const api = fakeFirmApi();
    rules(api);
    fireEvent.click(await screen.findByTestId("rule-max-edit-rule-1"));
    fireEvent.change(screen.getByTestId("rule-max-input-rule-1"), {
      target: { value: "1.250,50" },
    });
    fireEvent.click(screen.getByTestId("rule-max-save-rule-1"));

    await waitFor(() =>
      expect(screen.getByTestId("rule-max-rule-1").textContent).toMatch(/Up to €\s?1\.250,50/),
    );
    expect(api.calls.find((call) => call.method === "setRuleMaxAmount")?.args).toEqual([
      "adm-1",
      "rule-1",
      "1250.50",
    ]);
    expect(screen.getByTestId("rules-notice").textContent).toBe("Maximum saved.");
  });

  it("removes a maximum", async () => {
    const api = fakeFirmApi();
    rules(api);
    fireEvent.click(await screen.findByTestId("rule-max-edit-rule-2"));
    fireEvent.click(screen.getByTestId("rule-max-clear-rule-2"));
    await waitFor(() =>
      expect(screen.getByTestId("rule-max-rule-2").textContent).toBe("No maximum"),
    );
    expect(api.calls.find((call) => call.method === "setRuleMaxAmount")?.args).toEqual([
      "adm-1",
      "rule-2",
      null,
    ]);
  });

  it("retires a rule after a confirmation", async () => {
    const api = fakeFirmApi();
    rules(api);
    fireEvent.click(await screen.findByTestId("rule-retire-rule-1"));
    expect(api.calls.some((call) => call.method === "retireRule")).toBe(false);
    expect(screen.getByTestId("rule-rule-1").textContent).toContain(
      "What it already booked stays as it is",
    );
    fireEvent.click(screen.getByTestId("rule-retire-confirm-rule-1"));

    await waitFor(() => expect(screen.getByTestId("rule-rule-1").textContent).toContain("Retired"));
    expect(api.calls.find((call) => call.method === "retireRule")?.args).toEqual([
      "adm-1",
      "rule-1",
    ]);
    // A retired rule has nothing left to act on.
    expect(screen.queryByTestId("rule-retire-rule-1")).toBeNull();
  });

  it("undoes a rule's booking only where it is undoable", async () => {
    const api = fakeFirmApi();
    rules(api);
    expect(await screen.findByTestId("rule-posting-p-10")).toBeTruthy();
    expect(screen.queryByTestId("rule-posting-undo-p-11")).toBeNull();

    fireEvent.click(screen.getByTestId("rule-posting-undo-p-10"));
    fireEvent.click(screen.getByTestId("rule-posting-undo-confirm-p-10"));
    await waitFor(() => expect(screen.queryByTestId("rule-posting-p-10")).toBeNull());
    expect(api.calls.find((call) => call.method === "undoRulePosting")?.args).toEqual([
      "adm-1",
      "p-10",
    ]);
    expect(screen.getByTestId("rules-notice").textContent).toBe("Booking undone.");
  });

  it("turns receipt reminders on with a cadence on the same screen", async () => {
    const api = fakeFirmApi();
    rules(api);
    const section = await screen.findByTestId("rules-chase");
    await waitFor(() => expect(section.textContent).toContain("Off"));
    const save = screen.getByTestId("rules-chase-save") as HTMLButtonElement;
    expect(save.disabled).toBe(true);

    fireEvent.click(screen.getByTestId("rules-chase-enabled"));
    fireEvent.change(screen.getByTestId("rules-chase-cadence"), {
      target: { value: "fortnightly" },
    });
    fireEvent.click(save);

    expect((await screen.findByTestId("rules-chase-saved")).textContent).toBe("Saved.");
    await waitFor(() => expect(section.textContent).toContain("On · Every two weeks"));
    expect(api.calls.find((call) => call.method === "setChaseSetting")?.args).toEqual([
      "adm-1",
      { enabled: true, cadence: "fortnightly" },
    ]);
  });

  it("reads in Dutch", async () => {
    rules(fakeFirmApi(), "nl");
    expect((await screen.findByTestId("rule-rule-2")).textContent).toContain("Gepauzeerd");
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Regels en herinneringen");
    expect(screen.getByTestId("rules-chase").textContent).toContain("Bonherinneringen");
  });
});

describe("StrictMode: nothing mutates on mount", () => {
  it("the firm home, the rules screen and the next-client control only read", async () => {
    storeLastQuery({
      chip: "my_move",
      q: "",
      assigned: "me",
      sort: "risk",
      dir: "asc",
      vat_frequency: null,
    });
    const api = fakeFirmApi();
    services.firm = api;
    render(
      <StrictMode>
        <I18nProvider initialLanguage="en">
          <MemoryRouter initialEntries={["/todo"]}>
            <NextClientControl />
            <SavedViewList api={api} variant="rail" />
            <Routes>
              <Route path="/todo" element={<FirmHomeScreen api={api} />} />
            </Routes>
            <ClientRulesScreen api={api} administrationId="adm-1" />
          </MemoryRouter>
        </I18nProvider>
      </StrictMode>,
    );
    expect(await screen.findByTestId("firm-row-adm-1")).toBeTruthy();
    expect(await screen.findByTestId("rule-rule-1")).toBeTruthy();
    expect(await screen.findByTestId("nav-view-view-1")).toBeTruthy();
    expect(api.calls.filter((call) => FIRM_MUTATIONS.includes(call.method))).toEqual([]);
  });
});
