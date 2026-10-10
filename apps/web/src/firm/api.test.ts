import { describe, expect, it } from "vitest";

import { ApiError } from "../api/http";
import { fakeFetch, jsonResponse, problemResponse } from "../testing/fakeFetch";
import { FirmApi } from "./api";

/** The wire shapes in docs/firm-home/contract.md, asserted on what is actually sent. */

function api(handlers: Parameters<typeof fakeFetch>[0]) {
  const fetch = fakeFetch(handlers);
  return {
    client: new FirmApi({ language: () => "nl", fetchImpl: fetch.impl }),
    calls: fetch.calls,
  };
}

describe("FirmApi", () => {
  it("sends the worklist query as the contract names it", async () => {
    const { client, calls } = api({
      "GET /v1/firm/worklist": () =>
        jsonResponse({ rows: [], total: 0, page: 2, page_size: 50, chip_counts: {} }),
    });
    await client.getWorklist({
      chip: "waiting_on_client",
      q: "  jansen ",
      assigned: "any",
      sort: "vat_due",
      dir: "desc",
      page: 2,
      pageSize: 50,
    });
    expect(calls[0]?.url).toBe(
      "/v1/firm/worklist?chip=waiting_on_client&q=jansen&assigned=any&sort=vat_due&dir=desc&page=2&page_size=50",
    );
    expect(calls[0]?.headers["accept-language"]).toBe("nl");
    expect(calls[0]?.headers["idempotency-key"]).toBeUndefined();
  });

  it("assigns with snake_case ids and an idempotency key", async () => {
    const { client, calls } = api({
      "POST /v1/firm/clients/assign": () => jsonResponse({ assigned: 2, failed: [] }),
    });
    const result = await client.assign(["a", "b"], null);
    expect(result.assigned).toBe(2);
    expect(calls[0]?.body).toEqual({ administration_ids: ["a", "b"], user_id: null });
    expect(calls[0]?.headers["idempotency-key"]).toBeTruthy();
  });

  it("decides proposals with the wire's field names", async () => {
    const { client, calls } = api({
      "POST /v1/firm/proposals/decide": () =>
        jsonResponse({ approved: 1, rejected: 1, failed: [] }),
    });
    await client.decideProposals([
      { proposalId: "p-1", decision: "approve" },
      { proposalId: "p-2", decision: "reject" },
    ]);
    expect(calls[0]?.body).toEqual({
      decisions: [
        { proposal_id: "p-1", decision: "approve" },
        { proposal_id: "p-2", decision: "reject" },
      ],
    });
    expect(calls[0]?.headers["idempotency-key"]).toBeTruthy();
  });

  it("scopes proposals to administration ids, comma separated", async () => {
    const { client, calls } = api({
      "GET /v1/firm/proposals": () => jsonResponse({ total: 0, groups: [] }),
    });
    await client.listProposals(["a", "b"]);
    await client.listProposals();
    expect(calls[0]?.url).toBe("/v1/firm/proposals?administration_ids=a%2Cb");
    expect(calls[1]?.url).toBe("/v1/firm/proposals");
  });

  it("snoozes, and marks the summary seen, as 204s", async () => {
    const { client, calls } = api({
      "POST /v1/firm/clients/adm-1/snooze": () => new Response(null, { status: 204 }),
      "POST /v1/firm/summary/seen": () => new Response(null, { status: 204 }),
    });
    await client.snooze("adm-1", "2026-10-15", "Wacht op jaarcijfers");
    await client.markSeen();
    expect(calls[0]?.body).toEqual({ until: "2026-10-15", reason: "Wacht op jaarcijfers" });
    expect(calls[1]?.url).toBe("/v1/firm/summary/seen");
    expect(calls.every((call) => call.headers["idempotency-key"])).toBe(true);
  });

  it("reads the inbox with its limit, and refuses with the server's own sentence", async () => {
    const { client, calls } = api({
      "GET /v1/firm/inbox": () => problemResponse(404, "not_found", "Niet gevonden."),
    });
    await expect(client.getInbox({ unread: true, limit: 3 })).rejects.toBeInstanceOf(ApiError);
    expect(calls[0]?.url).toBe("/v1/firm/inbox?unread=true&limit=3");
  });

  it("asks the inbox for one side's threads with awaiting=", async () => {
    const { client, calls } = api({
      "GET /v1/firm/inbox": () => jsonResponse({ unread_count: 0, items: [] }),
    });
    await client.getInbox({ limit: 3, awaiting: "firm" });
    await client.getInbox({ awaiting: "client" });
    expect(calls.map((call) => call.url)).toEqual([
      "/v1/firm/inbox?limit=3&awaiting=firm",
      "/v1/firm/inbox?awaiting=client",
    ]);
  });

  it("wave 2: remember rides on approvals only; the worklist takes vat_frequency", async () => {
    const { client, calls } = api({
      "POST /v1/firm/proposals/decide": () =>
        jsonResponse({ approved: 1, rejected: 1, failed: [], rules_created: 1 }),
      "GET /v1/firm/worklist": () =>
        jsonResponse({ rows: [], total: 0, page: 1, page_size: 50, chip_counts: {} }),
    });
    const result = await client.decideProposals([
      { proposalId: "p-1", decision: "approve", remember: true },
      { proposalId: "p-2", decision: "reject", remember: true },
    ]);
    expect(result.rules_created).toBe(1);
    expect(calls[0]?.body).toEqual({
      decisions: [
        { proposal_id: "p-1", decision: "approve", remember: true },
        { proposal_id: "p-2", decision: "reject" },
      ],
    });
    await client.getWorklist({
      chip: "all",
      q: "",
      assigned: "me",
      sort: "risk",
      dir: "asc",
      page: 1,
      pageSize: 50,
      vatFrequency: "monthly",
    });
    expect(calls[1]?.url).toBe(
      "/v1/firm/worklist?chip=all&assigned=me&sort=risk&dir=asc&page=1&page_size=50&vat_frequency=monthly",
    );
  });

  it("wave 2: next client follows the same query, after the open client", async () => {
    const { client, calls } = api({
      "GET /v1/firm/worklist/next": () =>
        jsonResponse({ administration_id: null, display_name: null, remaining: 0 }),
    });
    const next = await client.nextClient("adm-1", {
      chip: "my_move",
      q: " eva ",
      assigned: "any",
      sort: "vat_due",
      dir: "desc",
      vat_frequency: "quarterly",
    });
    expect(next.administration_id).toBeNull();
    expect(calls[0]?.url).toBe(
      "/v1/firm/worklist/next?after=adm-1&chip=my_move&q=eva&assigned=any&sort=vat_due&dir=desc&vat_frequency=quarterly",
    );
  });

  it("wave 2: chasing, views, rules and the chase setting send the contract's shapes", async () => {
    const { client, calls } = api({
      "POST /v1/firm/chase/preview": () => jsonResponse({ items: [] }),
      "POST /v1/firm/chase/send": () => jsonResponse({ sent: 0, skipped: [] }),
      "GET /v1/firm/views": () => jsonResponse({ views: [] }),
      "POST /v1/firm/views": () => jsonResponse({ id: "v", name: "A", query: {}, count: 0 }, 201),
      "POST /v1/firm/views/v/rename": () => jsonResponse({ id: "v" }),
      "POST /v1/firm/views/v/archive": () => new Response(null, { status: 204 }),
      "GET /v1/administrations/adm-1/rules": () => jsonResponse({ rules: [] }),
      "POST /v1/administrations/adm-1/rules/r-1/max-amount": () => jsonResponse({ id: "r-1" }),
      "POST /v1/administrations/adm-1/rules/r-1/retire": () => jsonResponse({ id: "r-1" }),
      "GET /v1/administrations/adm-1/rule-postings": () => jsonResponse({ items: [] }),
      "POST /v1/administrations/adm-1/rule-postings/p-1/undo": () =>
        new Response(null, { status: 204 }),
      "POST /v1/administrations/adm-1/chase-setting": () =>
        jsonResponse({ enabled: true, cadence: "weekly" }),
    });
    await client.previewChase(["a"]);
    await client.sendChase(["a", "b"]);
    expect(await client.listViews()).toEqual([]);
    await client.createView("A", {
      chip: "all",
      q: "",
      assigned: "me",
      sort: "risk",
      dir: "asc",
      vat_frequency: null,
    });
    await client.renameView("v", "B");
    await client.archiveView("v");
    expect(await client.listRules("adm-1")).toEqual([]);
    await client.setRuleMaxAmount("adm-1", "r-1", "250.00");
    await client.retireRule("adm-1", "r-1");
    expect(await client.listRulePostings("adm-1", 50)).toEqual([]);
    await client.undoRulePosting("adm-1", "p-1");
    await client.setChaseSetting("adm-1", { enabled: true, cadence: "weekly" });

    expect(calls[0]?.body).toEqual({ administration_ids: ["a"] });
    expect(calls[1]?.body).toEqual({ administration_ids: ["a", "b"] });
    expect(calls[3]?.body).toEqual({
      name: "A",
      query: { chip: "all", q: "", assigned: "me", sort: "risk", dir: "asc", vat_frequency: null },
    });
    expect(calls[4]?.body).toEqual({ name: "B" });
    expect(calls[7]?.body).toEqual({ max_amount: "250.00" });
    expect(calls[9]?.url).toBe("/v1/administrations/adm-1/rule-postings?limit=50");
    expect(calls[11]?.body).toEqual({ enabled: true, cadence: "weekly" });
    const mutating = calls.filter((call) => call.method === "POST");
    expect(mutating.every((call) => call.headers["idempotency-key"])).toBe(true);
  });

  it("accepts the staff and deadlines lists bare or wrapped", async () => {
    const { client } = api({
      "GET /v1/firm/staff": () =>
        jsonResponse({ staff: [{ user_id: "u", name: "A", email: "a@b" }] }),
      "GET /v1/firm/deadlines": () => jsonResponse([]),
    });
    expect(await client.listStaff()).toHaveLength(1);
    expect(await client.getDeadlines()).toEqual([]);
  });
});
