import { StrictMode } from "react";
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CaptureQueue } from "@ledgr/offline-queue";
import type { QueueStore, StoredCapture } from "@ledgr/offline-queue";
import type { MeView } from "@ledgr/shared-types";

import { App } from "../App";
import { ApiError } from "../api/http";
import { MemoryKeyVault, WebCryptoCipher } from "../capture/webCryptoCipher";
import { fakeFirmApi } from "../firm/fakeFirmApi";
import { jsonResponse } from "../testing/fakeFetch";
import { renderApp } from "../testing/renderApp";
import { meFixture, testAdministration } from "../testing/session";
import { fakeQuestionsApi, fakeThread } from "./fakeQuestionsApi";

/**
 * Question threads (FR-FRM-005, ADR-111) inside a client: the list and its tabs, opening a thread
 * (which the GET itself marks read), replying and resolving as the API permits, the two 409s a
 * person can meet, the firm's "New question" attached to a record, and the firm inbox's way in.
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

const firmInsideClient = meFixture({
  user: { id: "firm-user-2", email: "tom@kantoor.nl", language: null, email_verified: true },
  organization: { id: "org-f", name: "Bakker & Co", kind: "firm", kvk_number: "11223344" },
});

function stubMe(initial: MeView, onSwitch?: (id: string) => MeView) {
  let me = initial;
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const key = `${init?.method ?? "GET"} ${String(input)}`;
      calls.push(key);
      if (key === "GET /v1/me") return jsonResponse(me);
      if (key === "GET /v1/me/language") {
        return jsonResponse({ language: null, supported: ["en", "nl"] });
      }
      const switched = /^PUT \/v1\/switcher\/(.+)$/.exec(key);
      if (switched !== null && onSwitch !== undefined) {
        me = onSwitch(decodeURIComponent(switched[1]!));
        return jsonResponse({ administration_id: switched[1], legal_name: "x" });
      }
      return new Response(null, { status: 500 });
    }) as unknown as typeof fetch,
  );
  return calls;
}

const MUTATIONS = ["createThread", "reply", "resolve"];

describe("questions inside a client", () => {
  it("lists open threads with an unread marker and whose move it is, and switches tabs", async () => {
    stubMe(meFixture());
    const questions = fakeQuestionsApi({
      threads: [
        fakeThread(),
        fakeThread({ id: "t-old", subject: "Huurcontract", status: "resolved", unread: false }),
      ],
    });
    renderApp(
      { authenticated: true, language: "en", services: { questions } },
      { route: "/questions" },
    );

    const row = await screen.findByTestId("questions-thread-t-1");
    expect(row.textContent).toContain("Betaling KPN");
    expect(row.textContent).toContain("Unread");
    expect(row.textContent).toContain("Open");
    expect(row.textContent).toContain("Waiting for your reply");
    expect(screen.queryByText("Huurcontract")).toBeNull();
    // A client user cannot open a thread from this list; that is the firm's.
    expect(screen.queryByTestId("questions-new")).toBeNull();

    fireEvent.click(screen.getByTestId("questions-tab-resolved"));
    expect(await screen.findByText("Huurcontract")).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("Betaling KPN")).toBeNull());
    expect(screen.getByTestId("questions-tab-resolved").getAttribute("aria-selected")).toBe("true");
    expect(questions.calls.map((call) => call.args[1])).toContain("resolved");
  });

  it("shows a teaching empty state per tab", async () => {
    stubMe(meFixture());
    const questions = fakeQuestionsApi({ threads: [] });
    renderApp(
      { authenticated: true, language: "en", services: { questions } },
      { route: "/questions" },
    );
    expect((await screen.findByTestId("questions-empty")).textContent).toContain(
      "No open questions",
    );
  });

  it("opening a thread is the GET that marks it read; the author side is a word", async () => {
    stubMe(meFixture());
    const questions = fakeQuestionsApi();
    renderApp(
      { authenticated: true, language: "en", services: { questions } },
      { route: "/questions/t-1" },
    );

    const message = await screen.findByTestId("question-message-t-1-m1");
    expect(message.textContent).toContain("Your accountant");
    expect(questions.calls.filter((call) => call.method === "openThread")[0]?.args).toEqual([
      "adm-A",
      "t-1",
    ]);
    expect(questions.calls.filter((call) => MUTATIONS.includes(call.method))).toEqual([]);

    fireEvent.click(screen.getByTestId("question-back"));
    const row = await screen.findByTestId("questions-thread-t-1");
    expect(within(row).queryByText("Unread")).toBeNull();
  });

  it("a reply appears as yours and flips whose move it is", async () => {
    stubMe(meFixture());
    const questions = fakeQuestionsApi();
    renderApp(
      { authenticated: true, language: "en", services: { questions } },
      { route: "/questions/t-1" },
    );

    expect((await screen.findByTestId("question-awaiting")).textContent).toBe(
      "Waiting for your reply",
    );
    fireEvent.change(screen.getByTestId("question-reply"), {
      target: { value: "Dat is het telefoonabonnement." },
    });
    fireEvent.click(screen.getByTestId("question-send"));

    await waitFor(() =>
      expect(screen.getByTestId("question-awaiting").textContent).toBe(
        "Waiting for your accountant",
      ),
    );
    const mine = screen.getByText("Dat is het telefoonabonnement.").closest("li");
    expect(mine?.textContent).toContain("You");
    expect((screen.getByTestId("question-reply") as HTMLTextAreaElement).value).toBe("");
  });

  it("either side may resolve", async () => {
    stubMe(meFixture());
    const questions = fakeQuestionsApi();
    renderApp(
      { authenticated: true, language: "en", services: { questions } },
      { route: "/questions/t-1" },
    );
    fireEvent.click(await screen.findByTestId("question-resolve"));
    expect((await screen.findByTestId("question-resolved-note")).textContent).toContain("resolved");
    expect(screen.getByTestId("question-status").textContent).toBe("Resolved");
    expect(screen.queryByTestId("question-reply")).toBeNull();
  });

  it("a reply to a thread resolved meanwhile is told plainly, keeps the text, and shows it resolved", async () => {
    stubMe(meFixture());
    const questions = fakeQuestionsApi();
    renderApp(
      { authenticated: true, language: "en", services: { questions } },
      { route: "/questions/t-1" },
    );
    await screen.findByTestId("question-reply");
    // The accountant resolves it in the meantime.
    questions.threads.set(
      "t-1",
      fakeThread({ status: "resolved", resolved_at: "2026-10-08T12:00:00+00:00" }),
    );

    fireEvent.change(screen.getByTestId("question-reply"), { target: { value: "Nog één ding" } });
    fireEvent.click(screen.getByTestId("question-send"));

    expect((await screen.findByTestId("question-problem")).textContent).toContain(
      "has just been resolved",
    );
    expect(await screen.findByTestId("question-resolved-note")).toBeTruthy();
  });

  it("another client open in the session is explained, not shown as a bare 409", async () => {
    stubMe(meFixture());
    const questions = fakeQuestionsApi({
      overrides: {
        reply: async () => {
          throw new ApiError(409, "active_client_mismatch", "Conflict");
        },
      },
    });
    renderApp(
      { authenticated: true, language: "en", services: { questions } },
      { route: "/questions/t-1" },
    );
    fireEvent.change(await screen.findByTestId("question-reply"), { target: { value: "Hallo" } });
    fireEvent.click(screen.getByTestId("question-send"));
    expect((await screen.findByTestId("question-problem")).textContent).toContain(
      "Another client is open",
    );
    expect((screen.getByTestId("question-reply") as HTMLTextAreaElement).value).toBe("Hallo");
  });

  it("an unknown thread says so", async () => {
    stubMe(meFixture());
    renderApp(
      { authenticated: true, language: "en", services: { questions: fakeQuestionsApi() } },
      { route: "/questions/nope" },
    );
    expect((await screen.findByTestId("question-error")).textContent).toContain(
      "not in this client's books",
    );
  });

  it("a firm user asks a new question attached to a record from ?resource_type=&resource_id=", async () => {
    stubMe(firmInsideClient);
    const questions = fakeQuestionsApi({ threads: [], side: "firm", userId: "firm-user-2" });
    renderApp(
      { authenticated: true, language: "en", services: { questions } },
      { route: "/questions?resource_type=bank_transaction&resource_id=bt-9" },
    );

    const composer = await screen.findByTestId("questions-composer");
    expect(within(composer).getByTestId("questions-composer-attached").textContent).toBe(
      "About a bank payment",
    );
    // Showing the form posted nothing.
    expect(questions.calls.filter((call) => MUTATIONS.includes(call.method))).toEqual([]);

    fireEvent.click(screen.getByTestId("questions-composer-send"));
    expect((await screen.findByTestId("questions-composer-problem")).textContent).toContain(
      "Fill in a subject",
    );

    fireEvent.change(screen.getByTestId("questions-composer-subject"), {
      target: { value: "Pin Albert Heijn" },
    });
    fireEvent.change(screen.getByTestId("questions-composer-body"), {
      target: { value: "Privé of zakelijk?" },
    });
    fireEvent.click(screen.getByTestId("questions-composer-send"));

    expect(await screen.findByTestId("question-thread")).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "Pin Albert Heijn" })).toBeTruthy();
    const created = questions.calls.find((call) => call.method === "createThread");
    expect(created?.args).toEqual([
      "adm-A",
      {
        subject: "Pin Albert Heijn",
        body: "Privé of zakelijk?",
        resourceType: "bank_transaction",
        resourceId: "bt-9",
      },
    ]);
    expect(screen.getByTestId("question-resource").textContent).toContain("About a bank payment");
    expect(screen.getByTestId("question-awaiting").textContent).toBe("Waiting for the client");
  });

  it("a firm user gets New question from the list; a client's message reads with its name", async () => {
    stubMe(firmInsideClient);
    const questions = fakeQuestionsApi({
      side: "firm",
      userId: "firm-user-2",
      threads: [
        fakeThread({
          awaiting: "firm",
          messages: [
            {
              id: "t-1-m1",
              thread_id: "t-1",
              author_user_id: "user-1",
              author_email: "eigenaar@vandoornbouw.nl",
              author_side: "client",
              body: "Is dit goed zo?",
              created_at: "2026-10-06T09:30:00+00:00",
            },
          ],
        }),
      ],
    });
    renderApp(
      { authenticated: true, language: "en", services: { questions } },
      { route: "/questions" },
    );
    fireEvent.click(await screen.findByTestId("questions-new"));
    expect(screen.getByTestId("questions-composer")).toBeTruthy();
    expect(screen.queryByTestId("questions-composer-attached")).toBeNull();

    fireEvent.click(screen.getByTestId("questions-thread-t-1"));
    expect((await screen.findByTestId("question-message-t-1-m1")).textContent).toContain(
      "Van Doorn (client)",
    );
  });

  it("reads in Dutch", async () => {
    stubMe(meFixture());
    renderApp(
      { authenticated: true, language: "nl", services: { questions: fakeQuestionsApi() } },
      { route: "/questions" },
    );
    expect((await screen.findByTestId("questions-tab-open")).textContent).toBe("Openstaand");
    expect(screen.getByTestId("questions-tab-resolved").textContent).toBe("Afgerond");
    expect((await screen.findByTestId("questions-thread-t-1")).textContent).toContain(
      "Wacht op uw antwoord",
    );
  });

  it("works under StrictMode and mutates nothing on mount", async () => {
    stubMe(meFixture());
    const questions = fakeQuestionsApi();
    render(
      <StrictMode>
        <MemoryRouter initialEntries={["/questions/t-1"]}>
          <App authenticated language="en" services={{ questions }} />
        </MemoryRouter>
      </StrictMode>,
    );
    expect(await screen.findByTestId("question-message-t-1-m1")).toBeTruthy();
    expect(questions.calls.filter((call) => MUTATIONS.includes(call.method))).toEqual([]);
  });

  it("the rail lists Questions and Receipts needed inside a client", async () => {
    stubMe(meFixture());
    renderApp(
      { authenticated: true, language: "en", services: { questions: fakeQuestionsApi() } },
      { route: "/questions" },
    );
    const rail = await screen.findByTestId("shell-rail");
    const nav = within(rail).getByTestId("nav-questions");
    expect(nav.getAttribute("href")).toBe("/questions");
    expect(nav.textContent).toContain("Questions");
    expect(nav.getAttribute("aria-current")).toBe("page");
    expect(within(rail).getByTestId("nav-receipts-needed").getAttribute("href")).toBe(
      "/receipts-needed",
    );
  });
});

describe("the firm inbox opens the thread", () => {
  it("switches to the item's client, then lands on /questions/{thread_id}", async () => {
    const otherClient = {
      ...testAdministration,
      id: "adm-2",
      legal_name: "Eva Mulder Design",
      trade_name: null,
    };
    const firmAtPortfolio = meFixture({
      organization: { id: "org-f", name: "Bakker & Co", kind: "firm", kvk_number: "11223344" },
      administrations: [testAdministration, otherClient],
      active_administration_id: null,
    });
    const calls = stubMe(firmAtPortfolio, (id) => ({
      ...firmAtPortfolio,
      active_administration_id: id,
    }));
    const questions = fakeQuestionsApi({ side: "firm" });
    renderApp(
      { authenticated: true, language: "en", services: { firm: fakeFirmApi(), questions } },
      { route: "/inbox" },
    );

    fireEvent.click(await screen.findByTestId("firm-inbox-open-t-1"));

    expect(await screen.findByTestId("question-thread")).toBeTruthy();
    expect(calls).toContain("PUT /v1/switcher/adm-2");
    const opened = questions.calls.find((call) => call.method === "openThread");
    expect(opened?.args).toEqual(["adm-2", "t-1"]);
  });
});
