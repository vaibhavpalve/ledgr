import { describe, expect, it, vi } from "vitest";

import { ApiError, ClientApi } from "./api";

function respond(status: number, body?: unknown): typeof fetch {
  return vi.fn(async () =>
    body === undefined
      ? new Response(null, { status })
      : new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
  ) as unknown as typeof fetch;
}

function client(fetchImpl: typeof fetch): ClientApi {
  return new ClientApi({ language: () => "nl", fetchImpl });
}

const wireEntry = {
  administration_id: "adm-A",
  display_name: "Bakker IT",
  legal_name: "Bakker Consultancy B.V.",
  trade_name: "Bakker IT",
  kvk_number: "12345678",
  colour: "indigo",
  initials: "BI",
  colour_is_ambiguous: false,
  // Present on the wire (SwitcherEntry extends ClientBadge server-side) but
  // not part of ClientBadge — proving these are dropped, not just ignored by
  // accident, is what the first test below checks.
  role: "Accountant",
  role_is_system: true,
  expires_at: null,
};

describe("ClientApi — FR-FRM-000a", () => {
  it("fetches GET /v1/switcher/active and maps the wire's snake_case entry to ClientBadge", async () => {
    const fetchImpl = respond(200, wireEntry);

    const badge = await client(fetchImpl).fetchActiveBadge();

    expect(badge).toEqual({
      administrationId: "adm-A",
      displayName: "Bakker IT",
      legalName: "Bakker Consultancy B.V.",
      tradeName: "Bakker IT",
      kvkNumber: "12345678",
      colour: "indigo",
      initials: "BI",
      colourIsAmbiguous: false,
    });
    expect(fetchImpl).toHaveBeenCalledWith(
      "/v1/switcher/active",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("resolves to null for the API's confirmed 'no active client' response, not an error", async () => {
    const fetchImpl = respond(200, null);

    const badge = await client(fetchImpl).fetchActiveBadge();

    expect(badge).toBeNull();
  });

  it("throws ApiError on a refusal, carrying the server's reason", async () => {
    const fetchImpl = respond(403, {
      detail: { reason: "no_authenticated_user", message: "Not signed in" },
    });

    const failure = client(fetchImpl).fetchActiveBadge();

    await expect(failure).rejects.toThrow(ApiError);
    await failure.catch((error: ApiError) => {
      expect(error.status).toBe(403);
      expect(error.reason).toBe("no_authenticated_user");
    });
  });
});
