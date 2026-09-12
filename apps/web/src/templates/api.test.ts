import { describe, expect, it, vi } from "vitest";

import { ApiError, OfflineError, TemplateApi, TemplateNotCompliantError } from "./api";

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

function client(fetchImpl: typeof fetch): TemplateApi {
  return new TemplateApi({ language: () => "nl", fetchImpl });
}

const templateBody = { id: "tpl-1", name: "Standaard" };

describe("TemplateApi's requests", () => {
  it("lists templates with a plain GET, no idempotency key", async () => {
    const fetchImpl = respond(200, { templates: [templateBody] });

    const result = await client(fetchImpl).listTemplates("adm-A");

    expect(result).toEqual({ templates: [templateBody] });
    expect(fetchImpl).toHaveBeenCalledWith(
      "/v1/administrations/adm-A/invoice-templates",
      expect.objectContaining({ method: "GET" }),
    );
    const [, init] = vi.mocked(fetchImpl).mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBeUndefined();
  });

  it("gets one template by id", async () => {
    const fetchImpl = respond(200, templateBody);

    const result = await client(fetchImpl).getTemplate("adm-A", "tpl-1");

    expect(result).toEqual(templateBody);
    expect(fetchImpl).toHaveBeenCalledWith(
      "/v1/administrations/adm-A/invoice-templates/tpl-1",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("creates a template with POST, a fresh idempotency key, and the body as JSON", async () => {
    const fetchImpl = respond(200, templateBody);

    await client(fetchImpl).createTemplate("adm-A", { name: "Standaard" });

    expect(fetchImpl).toHaveBeenCalledWith(
      "/v1/administrations/adm-A/invoice-templates",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ name: "Standaard" }),
        headers: expect.objectContaining({
          "Content-Type": "application/json",
          "Accept-Language": "nl",
        }),
      }),
    );
    const [, init] = vi.mocked(fetchImpl).mock.calls[0] as [string, RequestInit];
    // NFR-032: present and non-empty, not asserting the exact uuid.
    expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBeTruthy();
  });

  it("updates a template with PUT and its own idempotency key", async () => {
    const fetchImpl = respond(200, templateBody);

    await client(fetchImpl).updateTemplate("adm-A", "tpl-1", { name: "Standaard", version: 3 });

    expect(fetchImpl).toHaveBeenCalledWith(
      "/v1/administrations/adm-A/invoice-templates/tpl-1",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ name: "Standaard", version: 3 }),
      }),
    );
  });

  it("issues a different idempotency key on every mutating call", async () => {
    const fetchImpl = respond(200, templateBody);
    const api = client(fetchImpl);

    await api.createTemplate("adm-A", { name: "A" });
    await api.createTemplate("adm-A", { name: "B" });

    const keys = vi
      .mocked(fetchImpl)
      .mock.calls.map(([, init]) => (init as RequestInit).headers as Record<string, string>)
      .map((headers) => headers["Idempotency-Key"]);
    expect(keys[0]).not.toBe(keys[1]);
  });
});

describe("FR-TPL-009's 422 carries structured violations", () => {
  it("throws TemplateNotCompliantError with the violations array, not just the flattened message", async () => {
    const violations = [
      {
        field: "quantity_column",
        language: null,
        message: "De kolom Aantal kan niet worden verborgen.",
      },
      {
        field: "legal_identity_vat_tag",
        language: "en",
        message: "The compulsory content block is missing the {{supplier_vat_number}} placeholder.",
      },
    ];
    const fetchImpl = respond(422, {
      detail: {
        message: "Dit sjabloon verbergt verplichte inhoud en kan niet worden opgeslagen.",
        reason: "invoice_template_incomplete",
        count: 2,
        violations,
      },
    });

    let caught: unknown;
    try {
      await client(fetchImpl).updateTemplate("adm-A", "tpl-1", { name: "Standaard", version: 1 });
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(TemplateNotCompliantError);
    const notCompliant = caught as TemplateNotCompliantError;
    expect(notCompliant.status).toBe(422);
    expect(notCompliant.reason).toBe("invoice_template_incomplete");
    expect(notCompliant.message).toBe(
      "Dit sjabloon verbergt verplichte inhoud en kan niet worden opgeslagen.",
    );
    expect(notCompliant.violations).toEqual(violations);
  });
});

describe("other refusals stay plain ApiErrors", () => {
  it("carries the reason and the server's own sentence for a stale version", async () => {
    const fetchImpl = respond(409, {
      detail: {
        message: "Dit sjabloon is inmiddels door iemand anders opgeslagen.",
        reason: "invoice_template_stale_version",
      },
    });

    await expect(
      client(fetchImpl).updateTemplate("adm-A", "tpl-1", { name: "Standaard", version: 1 }),
    ).rejects.toMatchObject({
      status: 409,
      reason: "invoice_template_stale_version",
      message: "Dit sjabloon is inmiddels door iemand anders opgeslagen.",
    });
  });

  it("is a plain ApiError, not a TemplateNotCompliantError, for a non-compliance refusal", async () => {
    const fetchImpl = respond(409, {
      detail: { message: "conflict", reason: "invoice_template_conflict" },
    });

    let caught: unknown;
    try {
      await client(fetchImpl).updateTemplate("adm-A", "tpl-1", { name: "Standaard", version: 1 });
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(ApiError);
    expect(caught).not.toBeInstanceOf(TemplateNotCompliantError);
  });

  it("survives a gateway body that is not JSON at all", async () => {
    const fetchImpl = vi.fn(
      async () => new Response("<html>gateway</html>", { status: 502 }),
    ) as unknown as typeof fetch;

    await expect(client(fetchImpl).getTemplate("adm-A", "tpl-1")).rejects.toMatchObject({
      status: 502,
      reason: null,
    });
  });
});

describe("TemplateApi.uploadTemplateAsset — FR-TPL-001, FR-TPL-018", () => {
  it("posts the raw file as the body, with its own Content-Type and a fresh idempotency key", async () => {
    const fetchImpl = respond(200, { id: "asset-1", content_type: "image/png", sanitized: true });
    const file = new File(["bytes"], "logo.png", { type: "image/png" });

    const result = await client(fetchImpl).uploadTemplateAsset("adm-A", file, "image/png");

    expect(result).toEqual({ id: "asset-1", content_type: "image/png", sanitized: true });
    expect(fetchImpl).toHaveBeenCalledWith(
      "/v1/administrations/adm-A/template-assets",
      expect.objectContaining({
        method: "POST",
        body: file,
        headers: expect.objectContaining({ "Content-Type": "image/png" }),
      }),
    );
    const [, init] = vi.mocked(fetchImpl).mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBeTruthy();
    // Never JSON-encoded - the raw file is the body itself, exactly as
    // api.templates.routes.upload_template_asset expects (asserted above via
    // `body: file`).
    expect(init.body).toBe(file);
  });

  it("surfaces a sanitiser refusal as an ordinary ApiError", async () => {
    const fetchImpl = respond(422, {
      detail: {
        message: "Dit SVG-bestand is geweigerd omdat het onveilige inhoud bevat.",
        reason: "template_asset_svg_rejected",
      },
    });
    const file = new File(["<svg/>"], "logo.svg", { type: "image/svg+xml" });

    await expect(
      client(fetchImpl).uploadTemplateAsset("adm-A", file, "image/svg+xml"),
    ).rejects.toMatchObject({
      status: 422,
      reason: "template_asset_svg_rejected",
      message: "Dit SVG-bestand is geweigerd omdat het onveilige inhoud bevat.",
    });
  });

  it("is told apart from a refusal when offline", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }) as unknown as typeof fetch;
    const file = new File(["bytes"], "logo.png", { type: "image/png" });

    await expect(
      client(fetchImpl).uploadTemplateAsset("adm-A", file, "image/png"),
    ).rejects.toBeInstanceOf(OfflineError);
  });
});

describe("TemplateApi.fetchTemplateAssetBlob", () => {
  it("GETs the asset and returns its bytes as a Blob, with no idempotency key", async () => {
    const fetchImpl = vi.fn(
      async () => new Response(new Blob(["fake-logo"]), { status: 200 }),
    ) as unknown as typeof fetch;

    const blob = await client(fetchImpl).fetchTemplateAssetBlob("adm-A", "asset-1");

    expect(blob).toBeInstanceOf(Blob);
    expect(fetchImpl).toHaveBeenCalledWith(
      "/v1/administrations/adm-A/template-assets/asset-1",
      expect.objectContaining({ headers: expect.objectContaining({ "Accept-Language": "nl" }) }),
    );
    const [, init] = vi.mocked(fetchImpl).mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBeUndefined();
  });

  it("throws ApiError for a 404", async () => {
    const fetchImpl = respond(404, {
      detail: { message: "Dit logo bestaat niet.", reason: "template_asset_not_found" },
    });

    await expect(
      client(fetchImpl).fetchTemplateAssetBlob("adm-A", "missing"),
    ).rejects.toMatchObject({ status: 404, reason: "template_asset_not_found" });
  });
});

describe("being offline", () => {
  it("is told apart from a refusal", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }) as unknown as typeof fetch;

    await expect(client(fetchImpl).getTemplate("adm-A", "tpl-1")).rejects.toBeInstanceOf(
      OfflineError,
    );
  });
});
