import { act, fireEvent, render as renderBare, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";
import type { InvoiceTemplateView, TemplateAssetUploadView } from "@ledgr/shared-types";

import { ApiError, TemplateNotCompliantError, type TemplateApi } from "./api";
import { TemplateDesigner } from "./TemplateDesigner";

function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

// jsdom does not implement the Blob URL API at all - stubbed once for every
// test in this file, since `TemplateDesigner`'s logo preview calls
// `URL.createObjectURL`/`revokeObjectURL` on any file selection or upload.
// Real browsers always have both; this stub exists purely for the test
// environment.
beforeAll(() => {
  vi.stubGlobal(
    "URL",
    Object.assign(URL, {
      createObjectURL: vi.fn(() => "blob:mock-preview-url"),
      revokeObjectURL: vi.fn(),
    }),
  );
});

/** `api.templates.routes._default_columns()` — a brand-new template's scaffold. */
const defaultColumns: InvoiceTemplateView["columns"] = [
  { column: "quantity", visible: true, position: 1 },
  { column: "unit", visible: true, position: 2 },
  { column: "unit_price", visible: true, position: 3 },
  { column: "discount", visible: true, position: 4 },
  { column: "vat_rate", visible: true, position: 5 },
  { column: "line_total", visible: true, position: 6 },
];

/** `api.templates.routes._default_blocks()` — already compliant from creation. */
const defaultBlocks: InvoiceTemplateView["blocks"] = [
  { block: "header", text_nl: "", text_en: "" },
  { block: "intro", text_nl: "", text_en: "" },
  { block: "payment_terms", text_nl: "", text_en: "" },
  { block: "footer", text_nl: "", text_en: "" },
  {
    block: "legal_identity",
    text_nl: "KvK {{supplier_kvk_number}} — btw-nr. {{supplier_vat_number}}",
    text_en: "KvK {{supplier_kvk_number}} — VAT no. {{supplier_vat_number}}",
  },
];

const template: InvoiceTemplateView = {
  id: "tpl-1",
  administration_id: "adm-A",
  name: "Standaard",
  is_default: true,
  version: 1,
  layout: "classic",
  header_arrangement: "split",
  totals_position: "right",
  page_size: "a4",
  margins: "normal",
  logo: { asset_id: null, position: "left", size: "medium" },
  typography: {
    heading_font: "ibm_plex_sans",
    body_font: "ibm_plex_sans",
    figures_font: "ibm_plex_mono",
    type_scale: "medium",
    font_weight: "regular",
    line_height: "normal",
    letter_spacing: "normal",
  },
  colors: { accent: "#0f172a", text: "#111827", background: "#ffffff", contrast_warnings: [] },
  columns: defaultColumns,
  blocks: defaultBlocks,
  updated_by_user_id: null,
  created_at: null,
  updated_at: null,
};

const uploadedAsset: TemplateAssetUploadView = {
  id: "asset-1",
  content_type: "image/png",
  sanitized: true,
};

function api(overrides: Partial<TemplateApi> = {}): TemplateApi {
  return {
    listTemplates: vi.fn(),
    getTemplate: vi.fn(),
    createTemplate: vi.fn(async () => template),
    updateTemplate: vi.fn(async () => template),
    uploadTemplateAsset: vi.fn(async () => uploadedAsset),
    fetchTemplateAssetBlob: vi.fn(async () => new Blob(["logo"], { type: "image/png" })),
    previewTemplate: vi.fn(async () => ({
      document_type: "invoice",
      language: "nl",
      content_type: "application/pdf",
      filename: "preview.pdf",
      content_base64: "JVBERi0=",
      compliant: true,
      violations: [],
    })),
    duplicateTemplate: vi.fn(async () => template),
    resetTemplate: vi.fn(async () => template),
    ...overrides,
  } as unknown as TemplateApi;
}

describe("statutory columns offer no way to hide them — FR-TPL-009", () => {
  for (const column of ["quantity", "unit_price", "vat_rate"] as const) {
    it(`renders no interactive control at all in the ${column} row`, () => {
      render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

      const row = screen.getByTestId(`template-column-row-${column}`);
      // Not "disabled" — ABSENT. A disabled control is still an affordance a
      // screen reader announces and a test could find; this row must have
      // none at all.
      expect(row.querySelectorAll("input, button, select").length).toBe(0);
    });
  }

  it("shows the quantity column's own locked caption, not a generic message", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    const caption = screen.getByTestId("template-column-caption-quantity").textContent ?? "";
    expect(caption).toContain("Aantal");
    expect(caption).toContain("niet worden verborgen");
  });

  it("shows the unit price column's own locked caption", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    const caption = screen.getByTestId("template-column-caption-unit_price").textContent ?? "";
    expect(caption).toContain("Stukprijs");
  });

  it("shows the VAT rate column's own locked caption", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    const caption = screen.getByTestId("template-column-caption-vat_rate").textContent ?? "";
    expect(caption).toContain("Btw-tarief");
  });

  it("shows the legal_identity block's KvK and VAT pill captions, naming the exact tag to retype", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    const kvkCaption =
      screen.getByTestId("template-legal-identity-nl-kvk-caption").textContent ?? "";
    expect(kvkCaption).toContain("{{supplier_kvk_number}}");

    const vatCaption =
      screen.getByTestId("template-legal-identity-en-vat-caption").textContent ?? "";
    expect(vatCaption).toContain("{{supplier_vat_number}}");
  });

  it("renders the two legal_identity tags as fixed pills, not as editable text", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    const pill = screen.getByTestId("template-legal-identity-nl-kvk-pill");
    expect(pill.tagName).not.toBe("INPUT");
    expect(pill.tagName).not.toBe("TEXTAREA");
    expect(pill.querySelector("input, textarea, button")).toBeNull();
  });
});

describe("non-statutory columns can be hidden and reordered", () => {
  it("toggles discount off via a checkbox", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    const checkbox = screen.getByTestId("template-column-visible-discount") as HTMLInputElement;
    expect(checkbox.checked).toBe(true);

    fireEvent.click(checkbox);

    expect(checkbox.checked).toBe(false);
  });

  it("moving discount up swaps it past the next movable (non-statutory, visible) neighbour", async () => {
    const calls = api();
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

    // discount starts at position 4, between unit_price (3, locked) and
    // vat_rate (5, locked) — "up" must skip unit_price and land on unit (2).
    fireEvent.click(screen.getByTestId("template-column-up-discount"));
    fireEvent.click(screen.getByTestId("template-save"));

    await waitFor(() => expect(calls.updateTemplate).toHaveBeenCalled());
    const body = vi.mocked(calls.updateTemplate).mock.calls[0]?.[2];
    const positionOf = (column: string): number => {
      const position = body?.columns?.find((c) => c.column === column)?.position;
      expect(position).toBeDefined();
      return position as number;
    };
    expect(positionOf("discount")).toBeLessThan(positionOf("unit_price"));
    expect(positionOf("unit")).toBeGreaterThan(positionOf("discount"));
    // The three locked columns never move relative to each other.
    expect(positionOf("quantity")).toBeLessThan(positionOf("unit_price"));
    expect(positionOf("unit_price")).toBeLessThan(positionOf("vat_rate"));
  });

  it("disables the up button for a column already at the movable top", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    // unit is the first movable (non-statutory) column — nothing above it to
    // trade places with once quantity is skipped.
    expect(screen.getByTestId("template-column-up-unit").hasAttribute("disabled")).toBe(true);
  });
});

describe("saving sends a legal_identity block that always carries both tags", () => {
  it("embeds both tags in the saved payload even with free text that would otherwise say nothing about them", async () => {
    const calls = api();
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

    fireEvent.change(screen.getByTestId("template-legal-identity-nl-freetext"), {
      target: { value: "IBAN NL00 BANK 0123 4567 89" },
    });
    fireEvent.change(screen.getByTestId("template-legal-identity-en-freetext"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByTestId("template-save"));

    await waitFor(() => expect(calls.updateTemplate).toHaveBeenCalled());
    const body = vi.mocked(calls.updateTemplate).mock.calls[0]?.[2];
    const legalIdentity = body?.blocks?.find((b) => b.block === "legal_identity");
    expect(legalIdentity?.text_nl).toContain("{{supplier_kvk_number}}");
    expect(legalIdentity?.text_nl).toContain("{{supplier_vat_number}}");
    expect(legalIdentity?.text_nl).toContain("IBAN NL00 BANK 0123 4567 89");
    expect(legalIdentity?.text_en).toContain("{{supplier_kvk_number}}");
    expect(legalIdentity?.text_en).toContain("{{supplier_vat_number}}");
  });

  it("calls createTemplate instead when isNew is set, with no version", async () => {
    const calls = api();
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} isNew />);

    fireEvent.click(screen.getByTestId("template-save"));

    await waitFor(() => expect(calls.createTemplate).toHaveBeenCalled());
    expect(calls.updateTemplate).not.toHaveBeenCalled();
  });

  it("reports the saved state and passes the server's answer to onChanged", async () => {
    const saved = { ...template, version: 2 };
    const calls = api({ updateTemplate: vi.fn(async () => saved) });
    const onChanged = vi.fn();
    render(
      <TemplateDesigner
        administrationId="adm-A"
        template={template}
        api={calls}
        onChanged={onChanged}
      />,
    );

    fireEvent.click(screen.getByTestId("template-save"));

    await waitFor(() => expect(screen.getByTestId("template-saved")).toBeDefined());
    expect(onChanged).toHaveBeenCalledWith(saved);
  });
});

describe("a 422 shows each violation inline at its own location — FR-TPL-009", () => {
  it("places a column violation next to that column's row, and a language-scoped tag violation next to that language's pill", async () => {
    const violations = [
      { field: "quantity_column", language: null, message: "QUANTITY_VIOLATION_TEXT" },
      {
        field: "legal_identity_vat_tag",
        language: "en" as const,
        message: "VAT_TAG_EN_VIOLATION_TEXT",
      },
    ];
    const calls = api({
      updateTemplate: vi.fn(async () => {
        throw new TemplateNotCompliantError(
          422,
          "Dit sjabloon kan niet worden opgeslagen.",
          violations,
        );
      }),
    });
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

    fireEvent.click(screen.getByTestId("template-save"));

    await waitFor(() =>
      expect(screen.getByTestId("template-column-violation-quantity").textContent).toBe(
        "QUANTITY_VIOLATION_TEXT",
      ),
    );
    expect(screen.getByTestId("template-legal-identity-en-vat-violation").textContent).toBe(
      "VAT_TAG_EN_VIOLATION_TEXT",
    );

    // Not the Dutch pill, and not a summary list, and not a generic banner.
    expect(screen.queryByTestId("template-legal-identity-nl-vat-violation")).toBeNull();
    expect(screen.queryByTestId("template-unmapped-violations")).toBeNull();
    expect(screen.queryByText(/validation failed/i)).toBeNull();
    expect(screen.queryByText(/^Overige problemen$/)).toBeNull();
  });

  it("falls back to a clearly-worded general area for a violation with no matching row, rather than swallowing it", async () => {
    const violations = [
      { field: "something_new", language: null, message: "UNEXPECTED_SERVER_MESSAGE" },
    ];
    const calls = api({
      updateTemplate: vi.fn(async () => {
        throw new TemplateNotCompliantError(422, "message", violations);
      }),
    });
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

    fireEvent.click(screen.getByTestId("template-save"));

    await waitFor(() => expect(screen.getByTestId("template-unmapped-violations")).toBeDefined());
    expect(screen.getByTestId("template-unmapped-violations").textContent).toContain(
      "UNEXPECTED_SERVER_MESSAGE",
    );
  });
});

describe("typography — closed-set controls only (FR-TPL-002/003), never upload (FR-TPL-020)", () => {
  it("has no numeric or free-text input bound to any of the seven typography fields", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    const section = screen.getByTestId("template-typography");
    expect(section.querySelectorAll('input[type="number"]').length).toBe(0);
    expect(section.querySelectorAll("input").length).toBe(0);
    expect(section.querySelectorAll("textarea").length).toBe(0);
  });

  it("has no file input in the typography section — FR-TPL-020 forbids a font-upload affordance", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);
    const section = screen.getByTestId("template-typography");
    expect(section.querySelectorAll('input[type="file"]').length).toBe(0);
  });

  it("has exactly ONE file input in the whole designer, and it is the logo control (FR-TPL-001)", () => {
    // FR-TPL-020 forbids a file input for FONTS specifically, not for logos -
    // FR-TPL-001 requires exactly one, at the logo section. This is the
    // structural proof the two requirements do not conflict.
    const { container } = render(
      <TemplateDesigner administrationId="adm-A" template={template} api={api()} />,
    );
    const fileInputs = container.querySelectorAll('input[type="file"]');
    expect(fileInputs).toHaveLength(1);
    const fileInput = fileInputs[0];
    expect(fileInput).toBeDefined();
    expect(screen.getByTestId("template-logo").contains(fileInput ?? null)).toBe(true);
  });

  it("exposes exactly the seven typography fields, each as a <select>", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    for (const field of [
      "heading-font",
      "body-font",
      "figures-font",
      "type-scale",
      "font-weight",
      "line-height",
      "letter-spacing",
    ]) {
      const control = screen.getByTestId(`template-typography-${field}`);
      expect(control.tagName).toBe("SELECT");
    }
  });

  it("populates the font selects from the nine curated FONT_CATALOGUE entries, not an invented list", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    const headingFont = screen.getByTestId("template-typography-heading-font") as HTMLSelectElement;
    const values = Array.from(headingFont.options).map((option) => option.value);
    expect(values.sort()).toEqual(
      [
        "inter",
        "source_sans",
        "ibm_plex_sans",
        "public_sans",
        "source_serif",
        "ibm_plex_serif",
        "pt_serif",
        "ibm_plex_mono",
        "source_code_pro",
      ].sort(),
    );
  });

  it("offers exactly the four type-scale steps, no more and no fewer", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    const select = screen.getByTestId("template-typography-type-scale") as HTMLSelectElement;
    const values = Array.from(select.options).map((option) => option.value);
    expect(values).toEqual(["small", "medium", "large", "extra_large"]);
  });

  it("offers exactly the three font-weight steps", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    const select = screen.getByTestId("template-typography-font-weight") as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "regular",
      "medium",
      "bold",
    ]);
  });

  it("offers exactly the three line-height steps", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    const select = screen.getByTestId("template-typography-line-height") as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "tight",
      "normal",
      "relaxed",
    ]);
  });

  it("offers exactly the three letter-spacing steps", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    const select = screen.getByTestId("template-typography-letter-spacing") as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "tight",
      "normal",
      "wide",
    ]);
  });

  it("starts each select at the template's current typography values", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    expect(
      (screen.getByTestId("template-typography-heading-font") as HTMLSelectElement).value,
    ).toBe("ibm_plex_sans");
    expect(
      (screen.getByTestId("template-typography-figures-font") as HTMLSelectElement).value,
    ).toBe("ibm_plex_mono");
    expect((screen.getByTestId("template-typography-type-scale") as HTMLSelectElement).value).toBe(
      "medium",
    );
  });

  it("sends a chosen typography combination to the API in the expected shape", async () => {
    const calls = api();
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

    fireEvent.change(screen.getByTestId("template-typography-heading-font"), {
      target: { value: "source_serif" },
    });
    fireEvent.change(screen.getByTestId("template-typography-type-scale"), {
      target: { value: "large" },
    });
    fireEvent.change(screen.getByTestId("template-typography-font-weight"), {
      target: { value: "bold" },
    });
    fireEvent.change(screen.getByTestId("template-typography-line-height"), {
      target: { value: "relaxed" },
    });
    fireEvent.change(screen.getByTestId("template-typography-letter-spacing"), {
      target: { value: "wide" },
    });
    fireEvent.click(screen.getByTestId("template-save"));

    await waitFor(() => expect(calls.updateTemplate).toHaveBeenCalled());
    const body = vi.mocked(calls.updateTemplate).mock.calls[0]?.[2];
    expect(body?.typography).toEqual({
      heading_font: "source_serif",
      body_font: "ibm_plex_sans",
      figures_font: "ibm_plex_mono",
      type_scale: "large",
      font_weight: "bold",
      line_height: "relaxed",
      letter_spacing: "wide",
    });
  });

  it("surfaces a rejected typography value through the same generic banner a stale-version refusal uses", async () => {
    // api.templates.routes._typography's 422 is `customer_field_invalid`, a
    // plain ApiError rather than TemplateNotCompliantError's violations
    // array - this is structurally unreachable through the closed selects,
    // but the server is still the authority (CLAUDE.md rule 3) and this
    // proves the honest fallback path still displays something.
    const calls = api({
      updateTemplate: vi.fn(async () => {
        throw new ApiError(422, "customer_field_invalid", "Onbekend lettertype.");
      }),
    });
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

    fireEvent.click(screen.getByTestId("template-save"));

    await waitFor(() =>
      expect(screen.getByTestId("template-problem").textContent).toContain("Onbekend lettertype."),
    );
  });
});

describe("logo — upload, position/size, preview against the paper colour (FR-TPL-001)", () => {
  function pngFile(name = "logo.png"): File {
    return new File(["fake-png-bytes"], name, { type: "image/png" });
  }

  it("uploads the selected file via TemplateApi.uploadTemplateAsset, with its own content type", async () => {
    const calls = api();
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

    const input = screen.getByTestId("template-logo-file") as HTMLInputElement;
    const file = pngFile();
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => expect(calls.uploadTemplateAsset).toHaveBeenCalled());
    const args = vi.mocked(calls.uploadTemplateAsset).mock.calls[0];
    expect(args?.[0]).toBe("adm-A");
    expect(args?.[1]).toBe(file);
    expect(args?.[2]).toBe("image/png");
  });

  it("writes the server's returned asset id into logo.asset_id and sends it on save", async () => {
    const calls = api();
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

    fireEvent.change(screen.getByTestId("template-logo-file"), { target: { files: [pngFile()] } });
    await waitFor(() => expect(calls.uploadTemplateAsset).toHaveBeenCalled());

    fireEvent.click(screen.getByTestId("template-save"));
    await waitFor(() => expect(calls.updateTemplate).toHaveBeenCalled());

    const body = vi.mocked(calls.updateTemplate).mock.calls[0]?.[2];
    expect(body?.logo?.asset_id).toBe(uploadedAsset.id);
  });

  it("shows a 422 refusal inline at the logo section, not as a generic banner", async () => {
    const calls = api({
      uploadTemplateAsset: vi.fn(async () => {
        throw new ApiError(
          422,
          "template_asset_svg_rejected",
          "Dit SVG-bestand is geweigerd omdat het onveilige inhoud bevat.",
        );
      }),
    });
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

    fireEvent.change(screen.getByTestId("template-logo-file"), {
      target: { files: [pngFile("logo.svg")] },
    });

    await waitFor(() =>
      expect(screen.getByTestId("template-logo-problem").textContent).toContain(
        "Dit SVG-bestand is geweigerd",
      ),
    );
    // Never the generic Save-time banner.
    expect(screen.queryByTestId("template-problem")).toBeNull();
  });

  it("offers position as a closed-set select with exactly the three FR-TPL-001 steps", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);
    const select = screen.getByTestId("template-logo-position") as HTMLSelectElement;
    expect(select.tagName).toBe("SELECT");
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "left",
      "centre",
      "right",
    ]);
  });

  it("offers size as a closed-set select with exactly the three FR-TPL-001 steps", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);
    const select = screen.getByTestId("template-logo-size") as HTMLSelectElement;
    expect(select.tagName).toBe("SELECT");
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "small",
      "medium",
      "large",
    ]);
  });

  it("has no numeric input for position or size — curated steps only", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);
    const section = screen.getByTestId("template-logo");
    expect(section.querySelectorAll('input[type="number"]').length).toBe(0);
  });

  it("sends the chosen position and size on save", async () => {
    const calls = api();
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

    fireEvent.change(screen.getByTestId("template-logo-position"), {
      target: { value: "right" },
    });
    fireEvent.change(screen.getByTestId("template-logo-size"), { target: { value: "large" } });
    fireEvent.click(screen.getByTestId("template-save"));

    await waitFor(() => expect(calls.updateTemplate).toHaveBeenCalled());
    const body = vi.mocked(calls.updateTemplate).mock.calls[0]?.[2];
    expect(body?.logo?.position).toBe("right");
    expect(body?.logo?.size).toBe("large");
  });

  it("the preview container's background reflects template.colors.background", () => {
    const paperColoured = { ...template, colors: { ...template.colors, background: "#123456" } };
    render(<TemplateDesigner administrationId="adm-A" template={paperColoured} api={api()} />);

    const preview = screen.getByTestId("template-logo-preview");
    expect(preview.style.backgroundColor).toBe("rgb(18, 52, 86)");
  });

  it("shows a placeholder, not a broken image, before any logo has been uploaded", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);
    expect(screen.getByTestId("template-logo-none")).toBeDefined();
    expect(screen.getByTestId("template-logo-preview").querySelector("img")).toBeNull();
  });

  it("renders an <img> preview, built from the uploaded blob, once a logo is uploaded", async () => {
    const calls = api();
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

    fireEvent.change(screen.getByTestId("template-logo-file"), { target: { files: [pngFile()] } });

    await waitFor(() => {
      const img = screen.getByTestId("template-logo-preview").querySelector("img");
      expect(img).not.toBeNull();
    });
    // Never a direct reference to the download endpoint - see this
    // component's own docstring on why (SEC-005's attachment disposition).
    const img = screen.getByTestId("template-logo-preview").querySelector("img");
    expect(img?.getAttribute("src")).not.toMatch(/\/v1\//);
  });

  it("fetches the saved asset's bytes to preview a template reopened with an existing logo", async () => {
    const withLogo = {
      ...template,
      logo: { asset_id: "asset-9", position: "left", size: "medium" },
    };
    const calls = api();
    render(<TemplateDesigner administrationId="adm-A" template={withLogo} api={calls} />);

    await waitFor(() =>
      expect(calls.fetchTemplateAssetBlob).toHaveBeenCalledWith("adm-A", "asset-9"),
    );
    await waitFor(() => {
      expect(screen.getByTestId("template-logo-preview").querySelector("img")).not.toBeNull();
    });
  });
});

describe("other refusals — stale version, conflict", () => {
  it("shows the server's own sentence near Save for a non-compliance refusal", async () => {
    const calls = api({
      updateTemplate: vi.fn(async () => {
        throw new ApiError(
          409,
          "invoice_template_stale_version",
          "Iemand anders heeft dit al opgeslagen.",
        );
      }),
    });
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

    fireEvent.click(screen.getByTestId("template-save"));

    await waitFor(() =>
      expect(screen.getByTestId("template-problem").textContent).toContain(
        "Iemand anders heeft dit al opgeslagen.",
      ),
    );
  });
});

describe("layout and page setup — closed-set controls only (FR-TPL-005/FR-TPL-010)", () => {
  it("has no numeric or free-text input bound to any of the five layout/page-setup fields", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);

    const section = screen.getByTestId("template-layout");
    expect(section.querySelectorAll("input").length).toBe(0);
    expect(section.querySelectorAll("textarea").length).toBe(0);
    expect(section.querySelectorAll("select").length).toBe(5);
  });

  it("offers exactly the four starting layouts", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);
    const select = screen.getByTestId("template-layout-select") as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "classic",
      "modern",
      "compact",
      "minimal",
    ]);
  });

  it("offers exactly the three header arrangements", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);
    const select = screen.getByTestId("template-header-arrangement") as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "split",
      "stacked",
      "centered",
    ]);
  });

  it("offers exactly the three totals positions", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);
    const select = screen.getByTestId("template-totals-position") as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "right",
      "left",
      "full_width",
    ]);
  });

  it("offers exactly the two page sizes", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);
    const select = screen.getByTestId("template-page-size") as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toEqual(["a4", "letter"]);
  });

  it("offers exactly the three margin steps", () => {
    render(<TemplateDesigner administrationId="adm-A" template={template} api={api()} />);
    const select = screen.getByTestId("template-margins") as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value)).toEqual([
      "narrow",
      "normal",
      "wide",
    ]);
  });

  it("starts each select at the template's current values", () => {
    const custom = {
      ...template,
      layout: "modern",
      header_arrangement: "stacked",
      totals_position: "full_width",
      page_size: "letter",
      margins: "wide",
    };
    render(<TemplateDesigner administrationId="adm-A" template={custom} api={api()} />);

    expect((screen.getByTestId("template-layout-select") as HTMLSelectElement).value).toBe(
      "modern",
    );
    expect((screen.getByTestId("template-header-arrangement") as HTMLSelectElement).value).toBe(
      "stacked",
    );
    expect((screen.getByTestId("template-totals-position") as HTMLSelectElement).value).toBe(
      "full_width",
    );
    expect((screen.getByTestId("template-page-size") as HTMLSelectElement).value).toBe("letter");
    expect((screen.getByTestId("template-margins") as HTMLSelectElement).value).toBe("wide");
  });

  it("sends the chosen layout/page-setup combination on save", async () => {
    const calls = api();
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

    fireEvent.change(screen.getByTestId("template-layout-select"), {
      target: { value: "compact" },
    });
    fireEvent.change(screen.getByTestId("template-header-arrangement"), {
      target: { value: "centered" },
    });
    fireEvent.change(screen.getByTestId("template-totals-position"), {
      target: { value: "left" },
    });
    fireEvent.change(screen.getByTestId("template-page-size"), { target: { value: "letter" } });
    fireEvent.change(screen.getByTestId("template-margins"), { target: { value: "narrow" } });
    fireEvent.click(screen.getByTestId("template-save"));

    await waitFor(() => expect(calls.updateTemplate).toHaveBeenCalled());
    const body = vi.mocked(calls.updateTemplate).mock.calls[0]?.[2];
    expect(body?.layout).toBe("compact");
    expect(body?.header_arrangement).toBe("centered");
    expect(body?.totals_position).toBe("left");
    expect(body?.page_size).toBe("letter");
    expect(body?.margins).toBe("narrow");
  });
});

describe("live preview — FR-TPL-008", () => {
  it("calls previewTemplate with the current draft shape after the debounce settles", async () => {
    vi.useFakeTimers();
    try {
      const calls = api();
      render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(600);
      });

      expect(calls.previewTemplate).toHaveBeenCalledTimes(1);
      const call = vi.mocked(calls.previewTemplate).mock.calls[0];
      expect(call).toBeDefined();
      const [administrationId, templateId, body] = call ?? [];
      expect(administrationId).toBe("adm-A");
      expect(templateId).toBe("tpl-1");
      expect(body?.columns).toEqual(template.columns);
      expect(body?.layout).toBe("classic");
      expect(body?.document_type).toBe("invoice");
    } finally {
      vi.useRealTimers();
    }
  });

  it("coalesces rapid changes into a single request rather than firing on every keystroke", async () => {
    vi.useFakeTimers();
    try {
      const calls = api();
      render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

      fireEvent.change(screen.getByTestId("template-block-header-nl"), {
        target: { value: "H" },
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(100);
      });
      fireEvent.change(screen.getByTestId("template-block-header-nl"), {
        target: { value: "He" },
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(100);
      });
      fireEvent.change(screen.getByTestId("template-block-header-nl"), {
        target: { value: "Hello" },
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(600);
      });

      // One call for the mount-time debounce plus one for the settled edit -
      // never one per keystroke.
      expect(vi.mocked(calls.previewTemplate).mock.calls.length).toBeLessThanOrEqual(2);
      const last = vi.mocked(calls.previewTemplate).mock.calls.at(-1);
      expect(last?.[2].blocks?.find((b) => b.block === "header")?.text_nl).toBe("Hello");
    } finally {
      vi.useRealTimers();
    }
  });

  it("renders an iframe with a blob-based src once a preview response arrives, never a direct endpoint URL", async () => {
    vi.useFakeTimers();
    try {
      const calls = api();
      render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(600);
      });

      const frame = screen.getByTestId("template-preview-frame");
      expect(frame.tagName).toBe("IFRAME");
      expect(frame.getAttribute("src")).toMatch(/^blob:/);
      expect(frame.getAttribute("src")).not.toMatch(/\/v1\//);
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows a live preview's violations inline, the same way save-time violations render", async () => {
    vi.useFakeTimers();
    try {
      const calls = api({
        previewTemplate: vi.fn(async () => ({
          document_type: "invoice",
          language: "nl",
          content_type: "application/pdf",
          filename: "preview.pdf",
          content_base64: "JVBERi0=",
          compliant: false,
          violations: [
            { field: "quantity_column", language: null, message: "LIVE_PREVIEW_VIOLATION" },
          ],
        })),
      });
      render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(600);
      });

      expect(screen.getByTestId("template-column-violation-quantity").textContent).toBe(
        "LIVE_PREVIEW_VIOLATION",
      );
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("duplicate and reset — one action each (FR-TPL-019)", () => {
  it("duplicate calls the endpoint exactly once and propagates the result via onChanged", async () => {
    const duplicated = { ...template, id: "tpl-2", name: "Copy of Standaard", version: 1 };
    const calls = api({ duplicateTemplate: vi.fn(async () => duplicated) });
    const onChanged = vi.fn();
    render(
      <TemplateDesigner
        administrationId="adm-A"
        template={template}
        api={calls}
        onChanged={onChanged}
      />,
    );

    fireEvent.click(screen.getByTestId("template-duplicate"));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(duplicated));
    expect(calls.duplicateTemplate).toHaveBeenCalledTimes(1);
    expect(calls.duplicateTemplate).toHaveBeenCalledWith("adm-A", "tpl-1");
  });

  it("reset calls the endpoint with the current version exactly once and propagates the result", async () => {
    const reset = { ...template, layout: "classic", version: 2 };
    const calls = api({ resetTemplate: vi.fn(async () => reset) });
    const onChanged = vi.fn();
    render(
      <TemplateDesigner
        administrationId="adm-A"
        template={template}
        api={calls}
        onChanged={onChanged}
      />,
    );

    fireEvent.click(screen.getByTestId("template-reset"));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(reset));
    expect(calls.resetTemplate).toHaveBeenCalledTimes(1);
    expect(calls.resetTemplate).toHaveBeenCalledWith("adm-A", "tpl-1", { version: 1 });
  });

  it("shows a stale-version conflict on reset via the same generic banner save uses", async () => {
    const calls = api({
      resetTemplate: vi.fn(async () => {
        throw new ApiError(
          409,
          "invoice_template_stale_version",
          "Iemand anders heeft dit al opgeslagen.",
        );
      }),
    });
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

    fireEvent.click(screen.getByTestId("template-reset"));

    await waitFor(() =>
      expect(screen.getByTestId("template-problem").textContent).toContain(
        "Iemand anders heeft dit al opgeslagen.",
      ),
    );
  });
});

describe("saving disables the button while in flight", () => {
  it("disables Save until the request settles", async () => {
    let resolveSave: (value: InvoiceTemplateView) => void = () => {};
    const pending = new Promise<InvoiceTemplateView>((resolve) => {
      resolveSave = resolve;
    });
    const calls = api({ updateTemplate: vi.fn(() => pending) });
    render(<TemplateDesigner administrationId="adm-A" template={template} api={calls} />);

    fireEvent.click(screen.getByTestId("template-save"));

    expect(screen.getByTestId("template-save").hasAttribute("disabled")).toBe(true);

    resolveSave(template);
    await waitFor(() =>
      expect(screen.getByTestId("template-save").hasAttribute("disabled")).toBe(false),
    );
  });
});
