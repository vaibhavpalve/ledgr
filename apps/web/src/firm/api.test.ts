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
