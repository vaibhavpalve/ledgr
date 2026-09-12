import { useCallback, useEffect, useMemo, useState } from "react";
import { useI18n } from "@ledgr/i18n";
import {
  FONT_CATALOGUE,
  FONT_WEIGHTS,
  HEADER_ARRANGEMENTS,
  LAYOUTS,
  LETTER_SPACINGS,
  LINE_HEIGHTS,
  LOGO_POSITIONS,
  LOGO_SIZES,
  MARGINS,
  PAGE_SIZES,
  STATUTORY_COLUMNS,
  TOTALS_POSITIONS,
  TYPE_SCALES,
} from "@ledgr/shared-types";
import type {
  ContentBlockKey,
  HeaderArrangementKey,
  InvoiceTemplateView,
  LayoutKey,
  LineColumnKey,
  LogoPositionKey,
  LogoSizeKey,
  MarginsKey,
  PageSizeKey,
  TemplateLogoView,
  TemplateTypographyView,
  TemplateViolationView,
  TotalsPositionKey,
} from "@ledgr/shared-types";

import {
  ApiError,
  TemplateNotCompliantError,
  type TemplateApi,
  type TemplateBlockPatch,
  type TemplateColumnPatch,
  type TemplatePreviewBody,
  type TemplateWriteBody,
} from "./api";
import { composeLegalIdentityText, decomposeLegalIdentityText } from "./legalIdentity";

/**
 * FR-TPL-009's frontend half: the backend already refuses to SAVE a
 * non-compliant template (`api.templates.compliance.check()`, the 422 and
 * 0042's CHECK constraints); this component's job is to never OFFER the
 * option in the first place.
 *
 * --- Structural prevention, not a second implementation of `check()` ---
 *
 * The three statutory columns (`STATUTORY_COLUMNS`) render with no checkbox,
 * toggle or reorder control at all — absent from the DOM, not disabled —
 * because a disabled control is still an affordance a screen reader
 * announces and a test can find. The `legal_identity` block's two required
 * merge tags render as fixed pills a user cannot edit or delete, with free
 * text collected separately and concatenated with the tags at save time
 * (`legalIdentity.ts`). Neither of these reimplements
 * `api.templates.compliance.check()` — they make its two refusal conditions
 * impossible to construct through this UI's own controls. The server
 * remains the actual gate: CLAUDE.md rule 3 is explicit that client-side
 * hiding is presentation only, so a 422 is still handled properly below
 * rather than assumed unreachable.
 *
 * --- What this component deliberately does not edit ---
 *
 * `name`, `is_default` and `colors` are echoed back unchanged on save.
 * FR-TPL-009 is about columns and content blocks; FR-TPL-004's colour
 * pickers belong to a different pass. `typography` (FR-TPL-002/003), `logo`
 * (FR-TPL-001, FR-TPL-018) and `layout`/`header_arrangement`/
 * `totals_position`/`page_size`/`margins` (FR-TPL-005, FR-TPL-010) are the
 * exceptions — see the sections below.
 *
 * --- Logo: the one legitimate file input in this whole designer (FR-TPL-001) ---
 *
 * FR-TPL-020 forbids a file input for FONT upload; it says nothing about
 * logos, and FR-TPL-001 requires exactly this control. Selecting a file
 * uploads it immediately via `TemplateApi.uploadTemplateAsset` (multipart is
 * not used — the raw file bytes with its own `Content-Type`, mirroring
 * `api.templates.routes.upload_template_asset`'s own request shape); the
 * server's `asset_id` is written into `logo.asset_id` in draft state, and a
 * 422/415 refusal (wrong type, or FR-TPL-018's sanitiser rejecting an unsafe
 * SVG) renders inline at the logo section via `logoProblem` — never the
 * generic `problem` banner, the same "server-validated field, shown at its
 * own control" posture every other field in this component takes.
 *
 * The preview renders the picked/uploaded file directly with
 * `URL.createObjectURL` — never an `<img src="/v1/...">` pointed at the
 * download endpoint, which would ask the browser to navigate to and render
 * the raw response body (exactly what SEC-005's attachment disposition
 * exists to discourage). A `fetch()` + `Blob` + client-rendered `<img>`
 * sidesteps that question entirely. The preview container's background is
 * `template.colors.background` — FR-TPL-001's "paper colour" — so a
 * transparent PNG or SVG logo is checked against the actual chosen paper,
 * not a default white or checkerboard.
 *
 * --- Typography: curated steps, never free entry (FR-TPL-002/003) ---
 *
 * All seven controls (three font selections, type scale, weight, line
 * height, letter spacing) are `<select>` elements populated from
 * `@ledgr/shared-types`' `FONT_CATALOGUE`/`TYPE_SCALES`/`FONT_WEIGHTS`/
 * `LINE_HEIGHTS`/`LETTER_SPACINGS` — never a numeric `<input>`, so a user
 * cannot type their way to an unreadable invoice (FR-TPL-003's whole point).
 * FR-TPL-020 forbids a font-UPLOAD affordance outright; there is no file
 * input anywhere in this component, checked directly by this file's own
 * test suite. A font code or enum value the server would reject is
 * structurally unreachable through these selects, but `api.templates.
 * routes._typography` still validates every field server-side (CLAUDE.md
 * rule 3) — its 422 arrives as a plain `ApiError` (reason
 * `customer_field_invalid`, not the `TemplateViolationView` shape FR-TPL-009
 * uses), so it surfaces through the SAME generic `problem` banner near Save
 * that a stale-version or conflict refusal already uses below, rather than
 * needing its own per-field inline slot.
 *
 * --- Bootstrap flow scoped out ---
 *
 * Fetching the template to edit, and creating the very first one for an
 * administration that has none, is a separate concern left to a thin wrapper
 * (or a documented follow-up — see this feature's report). `isNew` is the
 * seam that wrapper would use: when true, Save calls `createTemplate`
 * instead of `updateTemplate` and sends no `version`.
 */
export function TemplateDesigner({
  administrationId,
  template,
  api,
  isNew = false,
  onChanged,
}: {
  administrationId: string;
  template: InvoiceTemplateView;
  api: TemplateApi;
  /** True when `template` is an unsaved scaffold — see this component's docstring. */
  isNew?: boolean;
  /** Called with the server's answer after every successful save. */
  onChanged?: (next: InvoiceTemplateView) => void;
}) {
  const { t } = useI18n();

  const [columns, setColumns] = useState<TemplateColumnPatch[]>(() =>
    template.columns.map((setting) => ({ ...setting })),
  );
  const [typography, setTypography] = useState<TemplateTypographyView>(() => ({
    ...template.typography,
  }));
  const [logo, setLogo] = useState<TemplateLogoView>(() => ({ ...template.logo }));
  // FR-TPL-005/FR-TPL-010: layout, page setup and the two independently-
  // adjustable header/totals axes - draft state exactly like every other
  // section of this component, `<select>`-driven, no free entry anywhere.
  const [layout, setLayout] = useState<LayoutKey>(() => template.layout as LayoutKey);
  const [headerArrangement, setHeaderArrangement] = useState<HeaderArrangementKey>(
    () => template.header_arrangement as HeaderArrangementKey,
  );
  const [totalsPosition, setTotalsPosition] = useState<TotalsPositionKey>(
    () => template.totals_position as TotalsPositionKey,
  );
  const [pageSize, setPageSize] = useState<PageSizeKey>(() => template.page_size as PageSizeKey);
  const [margins, setMargins] = useState<MarginsKey>(() => template.margins as MarginsKey);
  // The `File` from an upload made THIS session - present immediately, no
  // network round trip needed to preview it. `fetchedLogoBlob` is the
  // fallback for reopening a template that already has a saved logo: no
  // local `File` survives a page reload, so the bytes are fetched once via
  // `TemplateApi.fetchTemplateAssetBlob` (see this component's docstring on
  // why that call, and never a direct `<img src>`, is what builds the
  // preview).
  const [logoFile, setLogoFile] = useState<Blob | null>(null);
  const [fetchedLogoBlob, setFetchedLogoBlob] = useState<Blob | null>(null);
  const [logoUploading, setLogoUploading] = useState(false);
  const [logoProblem, setLogoProblem] = useState<string | null>(null);

  useEffect(() => {
    if (logoFile || !logo.asset_id) {
      setFetchedLogoBlob(null);
      return;
    }
    let cancelled = false;
    api
      .fetchTemplateAssetBlob(administrationId, logo.asset_id)
      .then((blob) => {
        if (!cancelled) setFetchedLogoBlob(blob);
      })
      .catch(() => {
        // The preview is a convenience; a fetch failure here does not block
        // editing the rest of the template.
      });
    return () => {
      cancelled = true;
    };
  }, [administrationId, api, logo.asset_id, logoFile]);

  const logoPreviewBlob = logoFile ?? fetchedLogoBlob;
  const [logoPreviewUrl, setLogoPreviewUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!logoPreviewBlob) {
      setLogoPreviewUrl(null);
      return;
    }
    const url = URL.createObjectURL(logoPreviewBlob);
    setLogoPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [logoPreviewBlob]);
  const [plainBlocks, setPlainBlocks] = useState<Record<PlainBlockKey, BlockDraft>>(() =>
    initialPlainBlocks(template),
  );
  const [legalFreeText, setLegalFreeText] = useState<Record<Language2, string>>(() =>
    initialLegalFreeText(template),
  );
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [duplicating, setDuplicating] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [violations, setViolations] = useState<readonly TemplateViolationView[]>([]);

  const unmatchedViolations = useMemo(
    () => violations.filter((violation) => !isRenderableViolation(violation)),
    [violations],
  );

  const uploadLogo = useCallback(
    async (file: File) => {
      setLogoUploading(true);
      setLogoProblem(null);
      try {
        const result = await api.uploadTemplateAsset(administrationId, file, file.type);
        setLogo((current) => ({ ...current, asset_id: result.id }));
        setLogoFile(file);
        setSaved(false);
      } catch (error) {
        // Server-validated (type rejected, or FR-TPL-018's sanitiser
        // refusing an unsafe SVG) - shown right here, at the logo control,
        // never as the generic `problem` banner near Save.
        setLogoProblem(error instanceof Error ? error.message : String(error));
      } finally {
        setLogoUploading(false);
      }
    },
    [administrationId, api],
  );

  // The FULL current draft, in the exact wire shape - the single source both
  // `write()` (save) and the live preview effect below build their request
  // from, so the two can never silently disagree about what "the current
  // draft" is.
  const draftBody: TemplateWriteBody = useMemo(
    () => ({
      name: template.name,
      is_default: template.is_default,
      layout,
      header_arrangement: headerArrangement,
      totals_position: totalsPosition,
      page_size: pageSize,
      margins,
      logo: { ...logo },
      typography: { ...typography },
      colors: {
        accent: template.colors.accent,
        text: template.colors.text,
        background: template.colors.background,
      },
      columns,
      blocks: buildBlocksPatch(plainBlocks, legalFreeText),
    }),
    [
      columns,
      headerArrangement,
      layout,
      legalFreeText,
      logo,
      margins,
      pageSize,
      plainBlocks,
      template.colors,
      template.is_default,
      template.name,
      totalsPosition,
      typography,
    ],
  );

  const write = useCallback(async () => {
    setSaving(true);
    setProblem(null);
    setViolations([]);
    try {
      const next = isNew
        ? await api.createTemplate(administrationId, draftBody)
        : await api.updateTemplate(administrationId, template.id, {
            ...draftBody,
            version: template.version,
          });

      setSaved(true);
      onChanged?.(next);
    } catch (error) {
      if (error instanceof TemplateNotCompliantError) {
        // FR-TPL-009: shown inline at each row/block below, never as a
        // summary list or a generic banner — see the render below.
        setViolations(error.violations);
      } else if (error instanceof ApiError) {
        // The server's own sentence, already translated (FR-UX-007) — e.g.
        // invoice_template_stale_version or invoice_template_conflict.
        setProblem(error.message);
      } else {
        setProblem(error instanceof Error ? error.message : "");
      }
    } finally {
      setSaving(false);
    }
  }, [administrationId, api, draftBody, isNew, onChanged, template.id, template.version]);

  const duplicate = useCallback(async () => {
    setDuplicating(true);
    setProblem(null);
    try {
      const next = await api.duplicateTemplate(administrationId, template.id);
      onChanged?.(next);
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    } finally {
      setDuplicating(false);
    }
  }, [administrationId, api, onChanged, template.id]);

  const reset = useCallback(async () => {
    setResetting(true);
    setProblem(null);
    try {
      const next = await api.resetTemplate(administrationId, template.id, {
        version: template.version,
      });
      onChanged?.(next);
    } catch (error) {
      // A stale-version conflict here is the same class of error a stale
      // save is - the existing generic banner near Save is the right place
      // for it, not a new one.
      setProblem(error instanceof Error ? error.message : String(error));
    } finally {
      setResetting(false);
    }
  }, [administrationId, api, onChanged, template.id, template.version]);

  // FR-TPL-008's live preview: debounced 500ms after the last change to ANY
  // draft field (`draftBody` already covers all of them), rendered through
  // the SAME engine `write()`'s save eventually reaches - never a second
  // preview implementation. Violations travel into the SAME `violations`
  // state save-time 422s already populate, so they render inline via the
  // SAME per-field logic below rather than a duplicated list.
  const [previewBlob, setPreviewBlob] = useState<Blob | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const previewBody: TemplatePreviewBody = {
            ...draftBody,
            document_type: "invoice",
            language: "nl",
          };
          const result = await api.previewTemplate(administrationId, template.id, previewBody);
          if (cancelled) return;
          const bytes = Uint8Array.from(atob(result.content_base64), (char) => char.charCodeAt(0));
          setPreviewBlob(new Blob([bytes], { type: result.content_type }));
          setViolations(result.violations);
        } catch {
          // A live preview is a convenience; a failure here (offline, a
          // transient 5xx) does not block editing and is not shown as a
          // save-time problem - there is nothing the author needs to act on.
        }
      })();
    }, 500);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [administrationId, api, draftBody, template.id]);

  // Object-URL hygiene: revoke the PREVIOUS url whenever a new preview blob
  // arrives, and on unmount - the same pattern the logo preview above uses.
  useEffect(() => {
    if (!previewBlob) {
      setPreviewUrl(null);
      return;
    }
    const url = URL.createObjectURL(previewBlob);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [previewBlob]);

  return (
    <form
      className="template-designer"
      aria-label={t("invoice.template.designer.title")}
      onSubmit={(event) => {
        event.preventDefault();
        void write();
      }}
    >
      <h2>{t("invoice.template.designer.title")}</h2>

      <section aria-label={t("invoice.template.designer.columns_heading")}>
        <h3>{t("invoice.template.designer.columns_heading")}</h3>
        <ColumnRows
          columns={columns}
          setColumns={(updater) => {
            setColumns(updater);
            setSaved(false);
          }}
          violations={violations}
        />
      </section>

      <section aria-label={t("invoice.template.designer.blocks_heading")}>
        <h3>{t("invoice.template.designer.blocks_heading")}</h3>
        {PLAIN_BLOCKS.map((key) => (
          <PlainBlockFields
            key={key}
            blockKey={key}
            draft={plainBlocks[key]}
            onChange={(next) => {
              setPlainBlocks((current) => ({ ...current, [key]: next }));
              setSaved(false);
            }}
          />
        ))}

        <LegalIdentityFields
          freeText={legalFreeText}
          onChange={(language, value) => {
            setLegalFreeText((current) => ({ ...current, [language]: value }));
            setSaved(false);
          }}
          violations={violations}
        />
      </section>

      <section aria-label={t("invoice.template.designer.logo_heading")}>
        <h3>{t("invoice.template.designer.logo_heading")}</h3>
        <LogoFields
          logo={logo}
          previewUrl={logoPreviewUrl}
          uploading={logoUploading}
          problem={logoProblem}
          paperColor={template.colors.background}
          onSelectFile={(file) => void uploadLogo(file)}
          onChange={(next) => {
            setLogo(next);
            setSaved(false);
          }}
        />
      </section>

      <section aria-label={t("invoice.template.designer.typography_heading")}>
        <h3>{t("invoice.template.designer.typography_heading")}</h3>
        <TypographyFields
          typography={typography}
          onChange={(next) => {
            setTypography(next);
            setSaved(false);
          }}
        />
      </section>

      <section aria-label={t("invoice.template.designer.layout_heading")}>
        <h3>{t("invoice.template.designer.layout_heading")}</h3>
        <LayoutFields
          layout={layout}
          headerArrangement={headerArrangement}
          totalsPosition={totalsPosition}
          pageSize={pageSize}
          margins={margins}
          onChangeLayout={(next) => {
            setLayout(next);
            setSaved(false);
          }}
          onChangeHeaderArrangement={(next) => {
            setHeaderArrangement(next);
            setSaved(false);
          }}
          onChangeTotalsPosition={(next) => {
            setTotalsPosition(next);
            setSaved(false);
          }}
          onChangePageSize={(next) => {
            setPageSize(next);
            setSaved(false);
          }}
          onChangeMargins={(next) => {
            setMargins(next);
            setSaved(false);
          }}
        />
      </section>

      <section aria-label={t("invoice.template.designer.preview_heading")}>
        <h3>{t("invoice.template.designer.preview_heading")}</h3>
        {previewUrl ? (
          <iframe
            title={t("invoice.template.designer.preview_heading")}
            data-testid="template-preview-frame"
            src={previewUrl}
          />
        ) : (
          <p data-testid="template-preview-loading">
            {t("invoice.template.designer.preview_loading")}
          </p>
        )}
      </section>

      {unmatchedViolations.length > 0 ? (
        <section
          aria-label={t("invoice.template.designer.other_problems")}
          data-testid="template-unmapped-violations"
        >
          <h3>{t("invoice.template.designer.other_problems")}</h3>
          <ul>
            {unmatchedViolations.map((violation, index) => (
              <li key={`${violation.field}-${violation.language ?? "none"}-${index}`} role="alert">
                {violation.message}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {problem !== null ? (
        <p role="alert" data-testid="template-problem">
          {problem}
        </p>
      ) : null}

      {saved ? (
        <p role="status" data-testid="template-saved">
          {t("invoice.template.designer.saved")}
        </p>
      ) : null}

      <button type="submit" disabled={saving} data-testid="template-save">
        {saving ? t("invoice.template.designer.saving") : t("invoice.template.designer.save")}
      </button>

      {/*
        FR-TPL-019: one click each, the way back from experimenting made
        obvious. Neither button submits the form (`type="button"`) - both
        act on the SERVER's stored row directly, independently of whatever is
        currently unsaved in this draft.
      */}
      <button
        type="button"
        disabled={duplicating}
        data-testid="template-duplicate"
        onClick={() => void duplicate()}
      >
        {duplicating
          ? t("invoice.template.designer.duplicating")
          : t("invoice.template.designer.duplicate")}
      </button>
      <button
        type="button"
        disabled={resetting}
        data-testid="template-reset"
        onClick={() => void reset()}
      >
        {resetting
          ? t("invoice.template.designer.resetting")
          : t("invoice.template.designer.reset")}
      </button>
    </form>
  );
}

// --- shared shapes -----------------------------------------------------------

type PlainBlockKey = Exclude<ContentBlockKey, "legal_identity">;
type Language2 = "nl" | "en";
interface BlockDraft {
  text_nl: string;
  text_en: string;
}
// Named rather than written inline in `buildBlocksPatch`'s signature: two
// `Record<...>` parameters in a row otherwise reads, to
// `scripts/check_translations.py`'s JSX-text heuristic, as a `>` followed by
// plain words followed by a `<` with nothing code-like between them - the
// same shape as real JSX text between two tags. Aliasing them here removes
// the ambiguity rather than fighting the checker with an allowlist entry,
// which `LITERAL_ALLOWLIST`'s own docstring reserves for genuine untranslatable
// names, not for code the heuristic misread.
type PlainBlockDraftMap = Record<PlainBlockKey, BlockDraft>;
type LegalFreeTextMap = Record<Language2, string>;

const PLAIN_BLOCKS: readonly PlainBlockKey[] = ["header", "intro", "payment_terms", "footer"];

/** The three columns `STATUTORY_COLUMNS` names, narrowed to a literal union so a lookup keyed by it needs no `noUncheckedIndexedAccess` undefined-check — every locked column has an entry, by construction. */
type StatutoryColumnKey = "quantity" | "unit_price" | "vat_rate";

/** `api.templates.compliance`'s three statutory-column fields, derived rather than listed again — see model.ts's own note on why a set duplicated by hand is worse than none. */
const STATUTORY_FIELD: Record<StatutoryColumnKey, string> = Object.fromEntries(
  // Concatenation rather than a template literal: `=> [column, \`...\`]` reads
  // to the same JSX-text heuristic as an arrow immediately followed by JSX
  // text, for want of any code-like character between the `>` and the next
  // `` ` ``/`{`. See the note above `PlainBlockDraftMap` for the same class
  // of false positive.
  STATUTORY_COLUMNS.map((column) => [column, column + "_column"]),
) as Record<StatutoryColumnKey, string>;

const COLUMN_LABEL_KEYS: Record<LineColumnKey, string> = {
  quantity: "invoice.pdf.column_quantity",
  unit: "invoice.pdf.column_unit",
  unit_price: "invoice.pdf.column_unit_price",
  discount: "invoice.pdf.column_discount",
  vat_rate: "invoice.pdf.column_vat_rate",
  line_total: "invoice.pdf.column_amount",
};

const LOCKED_CAPTION_KEYS: Record<StatutoryColumnKey, string> = {
  quantity: "invoice.template.locked.quantity_column",
  unit_price: "invoice.template.locked.unit_price_column",
  vat_rate: "invoice.template.locked.vat_rate_column",
};

/** A type predicate, not just a boolean check — narrows `column` so `STATUTORY_FIELD[column]` and `LOCKED_CAPTION_KEYS[column]` type-check without a runtime-impossible `undefined` case. */
function isStatutory(column: LineColumnKey): column is StatutoryColumnKey {
  return (STATUTORY_COLUMNS as readonly string[]).includes(column);
}

function findViolation(
  violations: readonly TemplateViolationView[],
  field: string,
  language: "nl" | "en" | null = null,
): TemplateViolationView | undefined {
  return violations.find(
    (violation) => violation.field === field && violation.language === language,
  );
}

const KNOWN_COLUMN_FIELDS = new Set(Object.values(STATUTORY_FIELD));
const KNOWN_TAG_FIELDS = new Set(["legal_identity_vat_tag", "legal_identity_kvk_tag"]);

/**
 * Whether `TemplateDesigner`'s own rows have somewhere to put this
 * violation. Defensive: the UI's construction should make every OTHER
 * violation impossible, but the server is the authority (CLAUDE.md rule 3),
 * so anything this returns false for still has to be shown — see the
 * "other problems" fallback section above.
 */
function isRenderableViolation(violation: TemplateViolationView): boolean {
  if (KNOWN_COLUMN_FIELDS.has(violation.field)) return violation.language === null;
  if (KNOWN_TAG_FIELDS.has(violation.field)) {
    return violation.language === "nl" || violation.language === "en";
  }
  return false;
}

function initialPlainBlocks(template: InvoiceTemplateView): PlainBlockDraftMap {
  const result = {} as PlainBlockDraftMap;
  for (const key of PLAIN_BLOCKS) {
    const found = template.blocks.find((block) => block.block === key);
    result[key] = { text_nl: found?.text_nl ?? "", text_en: found?.text_en ?? "" };
  }
  return result;
}

function initialLegalFreeText(template: InvoiceTemplateView): LegalFreeTextMap {
  const found = template.blocks.find((block) => block.block === "legal_identity");
  return {
    nl: decomposeLegalIdentityText("nl", found?.text_nl ?? ""),
    en: decomposeLegalIdentityText("en", found?.text_en ?? ""),
  };
}

function buildBlocksPatch(
  plainBlocks: PlainBlockDraftMap,
  legalFreeText: LegalFreeTextMap,
): TemplateBlockPatch[] {
  return [
    ...PLAIN_BLOCKS.map((key) => ({
      block: key,
      text_nl: plainBlocks[key].text_nl,
      text_en: plainBlocks[key].text_en,
    })),
    {
      block: "legal_identity" as const,
      text_nl: composeLegalIdentityText("nl", legalFreeText.nl),
      text_en: composeLegalIdentityText("en", legalFreeText.en),
    },
  ];
}

/**
 * Swap `column`'s position with the next MOVABLE neighbour in `direction`
 * (-1 up, +1 down) — "movable" meaning visible and non-statutory. Statutory
 * columns hold their position (the backend already orders them; this never
 * touches one), and a hidden column has nothing on screen to trade places
 * with, so both are skipped when looking for a neighbour rather than being a
 * valid target. Returns `current` unchanged (same reference) when there is
 * no movable neighbour in that direction, which callers use to disable the
 * button rather than emit a no-op state update.
 */
function moveColumn(
  current: readonly TemplateColumnPatch[],
  column: LineColumnKey,
  direction: -1 | 1,
): TemplateColumnPatch[] {
  const sorted = [...current].sort((a, b) => a.position - b.position);
  const index = sorted.findIndex((setting) => setting.column === column);
  if (index === -1) return current as TemplateColumnPatch[];

  let target = index + direction;
  // `noUncheckedIndexedAccess` means `sorted[target]` types as possibly
  // `undefined` regardless of the bounds the `while` condition already
  // checks, so it is read once into `candidate` and checked explicitly
  // rather than indexed again inside the condition.
  while (target >= 0 && target < sorted.length) {
    const candidate = sorted[target];
    if (candidate !== undefined && !isStatutory(candidate.column) && candidate.visible) break;
    target += direction;
  }
  if (target < 0 || target >= sorted.length) return current as TemplateColumnPatch[];

  const from = sorted[index];
  const to = sorted[target];
  if (from === undefined || to === undefined) return current as TemplateColumnPatch[];

  return current.map((setting) => {
    if (setting.column === from.column) return { ...setting, position: to.position };
    if (setting.column === to.column) return { ...setting, position: from.position };
    return setting;
  });
}

// --- column rows -----------------------------------------------------------

function ColumnRows({
  columns,
  setColumns,
  violations,
}: {
  columns: TemplateColumnPatch[];
  setColumns: (updater: (current: TemplateColumnPatch[]) => TemplateColumnPatch[]) => void;
  violations: readonly TemplateViolationView[];
}) {
  const { t } = useI18n();
  const sorted = [...columns].sort((a, b) => a.position - b.position);

  return (
    <div data-testid="template-columns">
      {sorted.map((setting) => {
        const { column } = setting;
        if (isStatutory(column)) {
          const field = STATUTORY_FIELD[column];
          const violation = findViolation(violations, field);
          return (
            <div key={column} data-testid={`template-column-row-${column}`}>
              {/*
                No checkbox, toggle or reorder control here — not disabled,
                absent. This is the literal enforcement of FR-TPL-009's "not
                offered as an option" for the three columns Wet OB art. 35a(1)
                requires on every line.
              */}
              <span aria-hidden="true">🔒</span>
              <span className="visually-hidden">
                {t("invoice.template.designer.locked_column_icon")}
              </span>
              <span>{t(COLUMN_LABEL_KEYS[column])}</span>
              <p data-testid={`template-column-caption-${column}`}>
                {t(LOCKED_CAPTION_KEYS[column])}
              </p>
              {violation ? (
                <p role="alert" data-testid={`template-column-violation-${column}`}>
                  {violation.message}
                </p>
              ) : null}
            </div>
          );
        }

        const canMoveUp = moveColumn(columns, column, -1) !== columns;
        const canMoveDown = moveColumn(columns, column, 1) !== columns;
        return (
          <div key={column} data-testid={`template-column-row-${column}`}>
            <label>
              <input
                type="checkbox"
                data-testid={`template-column-visible-${column}`}
                checked={setting.visible}
                onChange={(event) => {
                  const nextVisible = event.target.checked;
                  setColumns((current) =>
                    current.map((entry) =>
                      entry.column === column ? { ...entry, visible: nextVisible } : entry,
                    ),
                  );
                }}
              />
              {t(COLUMN_LABEL_KEYS[column])}
              <span className="visually-hidden">
                {t("invoice.template.designer.column_visible")}
              </span>
            </label>
            <button
              type="button"
              aria-label={t("invoice.template.designer.move_up")}
              data-testid={`template-column-up-${column}`}
              disabled={!canMoveUp}
              onClick={() => setColumns((current) => moveColumn(current, column, -1))}
            >
              ↑
            </button>
            <button
              type="button"
              aria-label={t("invoice.template.designer.move_down")}
              data-testid={`template-column-down-${column}`}
              disabled={!canMoveDown}
              onClick={() => setColumns((current) => moveColumn(current, column, 1))}
            >
              ↓
            </button>
          </div>
        );
      })}
    </div>
  );
}

// --- content blocks -----------------------------------------------------------

function PlainBlockFields({
  blockKey,
  draft,
  onChange,
}: {
  blockKey: PlainBlockKey;
  draft: BlockDraft;
  onChange: (next: BlockDraft) => void;
}) {
  const { t } = useI18n();
  const blockLabel = t(`invoice.template.designer.block.${blockKey}`);

  return (
    <div data-testid={`template-block-${blockKey}`}>
      <label>
        {t("invoice.template.designer.field_nl", { block: blockLabel })}
        <textarea
          data-testid={`template-block-${blockKey}-nl`}
          value={draft.text_nl}
          onChange={(event) => onChange({ ...draft, text_nl: event.target.value })}
        />
      </label>
      <label>
        {t("invoice.template.designer.field_en", { block: blockLabel })}
        <textarea
          data-testid={`template-block-${blockKey}-en`}
          value={draft.text_en}
          onChange={(event) => onChange({ ...draft, text_en: event.target.value })}
        />
      </label>
    </div>
  );
}

/**
 * FR-TPL-007's free block for KvK number, VAT number, IBAN and general terms
 * — the harder half of FR-TPL-009. The two required tags render as fixed
 * pills with no delete control, one language subsection at a time, because
 * the compliance check itself is per-language (a block can lose the tag from
 * its English text while its Dutch text still has it). Free text is
 * collected separately per language and concatenated with the fixed tags at
 * save time by `composeLegalIdentityText` — see legalIdentity.ts for why
 * that concatenation is unconditional.
 */
function LegalIdentityFields({
  freeText,
  onChange,
  violations,
}: {
  freeText: Record<Language2, string>;
  onChange: (language: Language2, value: string) => void;
  violations: readonly TemplateViolationView[];
}) {
  const { t } = useI18n();
  const blockLabel = t("invoice.template.designer.block.legal_identity");

  return (
    <div data-testid="template-legal-identity">
      {(["nl", "en"] as const).map((language) => {
        const languageName = t(`common.language.name.${language}`);
        const kvkViolation = findViolation(violations, "legal_identity_kvk_tag", language);
        const vatViolation = findViolation(violations, "legal_identity_vat_tag", language);

        return (
          <div key={language} data-testid={`template-legal-identity-${language}`}>
            {/*
              Fixed, non-removable pills — no delete control, no text input
              that could drop the tag literal. This is what makes the
              legal_identity block's two mandatory merge tags impossible to
              remove through this UI, the FR-TPL-009 counterpart to the
              statutory columns having no toggle at all.
            */}
            <span data-testid={`template-legal-identity-${language}-kvk-pill`}>
              {t("invoice.template.designer.kvk_pill")}
            </span>
            <p data-testid={`template-legal-identity-${language}-kvk-caption`}>
              {t("invoice.template.locked.legal_identity_kvk_tag", {
                language: languageName,
                tag: "{{supplier_kvk_number}}",
              })}
            </p>
            {kvkViolation ? (
              <p role="alert" data-testid={`template-legal-identity-${language}-kvk-violation`}>
                {kvkViolation.message}
              </p>
            ) : null}

            <span data-testid={`template-legal-identity-${language}-vat-pill`}>
              {t("invoice.template.designer.vat_pill")}
            </span>
            <p data-testid={`template-legal-identity-${language}-vat-caption`}>
              {t("invoice.template.locked.legal_identity_vat_tag", {
                language: languageName,
                tag: "{{supplier_vat_number}}",
              })}
            </p>
            {vatViolation ? (
              <p role="alert" data-testid={`template-legal-identity-${language}-vat-violation`}>
                {vatViolation.message}
              </p>
            ) : null}

            <label>
              {t(
                language === "nl"
                  ? "invoice.template.designer.field_nl"
                  : "invoice.template.designer.field_en",
                {
                  block: blockLabel,
                },
              )}
              <textarea
                data-testid={`template-legal-identity-${language}-freetext`}
                value={freeText[language]}
                onChange={(event) => onChange(language, event.target.value)}
              />
            </label>
          </div>
        );
      })}
    </div>
  );
}

// --- typography (FR-TPL-002/003) --------------------------------------------

/**
 * Seven closed-set selects, wired to the same draft/save flow the columns
 * and blocks sections above use. Every option list comes from
 * `@ledgr/shared-types` (itself mirroring the backend's own curated
 * catalogues) — never invented here — and every control is a `<select>`,
 * never a numeric or free-text `<input>`, which is what makes FR-TPL-003's
 * "a small number of sensible steps rather than free numeric entry" true of
 * this screen rather than merely true of the API it talks to. There is no
 * file input anywhere here either — FR-TPL-020 forbids a font-upload
 * affordance outright, and this component's own test suite asserts both
 * absences directly.
 */
function TypographyFields({
  typography,
  onChange,
}: {
  typography: TemplateTypographyView;
  onChange: (next: TemplateTypographyView) => void;
}) {
  const { t } = useI18n();

  function setField<K extends keyof TemplateTypographyView>(
    key: K,
    value: TemplateTypographyView[K],
  ): void {
    onChange({ ...typography, [key]: value });
  }

  return (
    <div data-testid="template-typography">
      <label>
        {t("invoice.template.designer.heading_font")}
        <select
          data-testid="template-typography-heading-font"
          value={typography.heading_font}
          onChange={(event) => setField("heading_font", event.target.value)}
        >
          {FONT_CATALOGUE.map((font) => (
            <option key={font.code} value={font.code}>
              {font.family_name}
            </option>
          ))}
        </select>
      </label>

      <label>
        {t("invoice.template.designer.body_font")}
        <select
          data-testid="template-typography-body-font"
          value={typography.body_font}
          onChange={(event) => setField("body_font", event.target.value)}
        >
          {FONT_CATALOGUE.map((font) => (
            <option key={font.code} value={font.code}>
              {font.family_name}
            </option>
          ))}
        </select>
      </label>

      <label>
        {t("invoice.template.designer.figures_font")}
        <select
          data-testid="template-typography-figures-font"
          value={typography.figures_font}
          onChange={(event) => setField("figures_font", event.target.value)}
        >
          {FONT_CATALOGUE.map((font) => (
            <option key={font.code} value={font.code}>
              {font.family_name}
            </option>
          ))}
        </select>
      </label>

      <label>
        {t("invoice.template.designer.type_scale")}
        <select
          data-testid="template-typography-type-scale"
          value={typography.type_scale}
          onChange={(event) => setField("type_scale", event.target.value)}
        >
          {TYPE_SCALES.map((scale) => {
            const key = `invoice.template.designer.type_scale.${scale}` as const;
            return (
              <option key={scale} value={scale}>
                {t(key)}
              </option>
            );
          })}
        </select>
      </label>

      <label>
        {t("invoice.template.designer.font_weight")}
        <select
          data-testid="template-typography-font-weight"
          value={typography.font_weight}
          onChange={(event) => setField("font_weight", event.target.value)}
        >
          {FONT_WEIGHTS.map((weight) => {
            const key = `invoice.template.designer.font_weight.${weight}` as const;
            return (
              <option key={weight} value={weight}>
                {t(key)}
              </option>
            );
          })}
        </select>
      </label>

      <label>
        {t("invoice.template.designer.line_height")}
        <select
          data-testid="template-typography-line-height"
          value={typography.line_height}
          onChange={(event) => setField("line_height", event.target.value)}
        >
          {LINE_HEIGHTS.map((height) => {
            const key = `invoice.template.designer.line_height.${height}` as const;
            return (
              <option key={height} value={height}>
                {t(key)}
              </option>
            );
          })}
        </select>
      </label>

      <label>
        {t("invoice.template.designer.letter_spacing")}
        <select
          data-testid="template-typography-letter-spacing"
          value={typography.letter_spacing}
          onChange={(event) => setField("letter_spacing", event.target.value)}
        >
          {LETTER_SPACINGS.map((spacing) => {
            const key = `invoice.template.designer.letter_spacing.${spacing}` as const;
            return (
              <option key={spacing} value={spacing}>
                {t(key)}
              </option>
            );
          })}
        </select>
      </label>
    </div>
  );
}

// --- layout and page setup (FR-TPL-005, FR-TPL-010) --------------------------

/**
 * Five closed-set selects, following the exact pattern `TypographyFields`
 * above already establishes: options from `@ledgr/shared-types`, never
 * invented here, every control a `<select>` and nothing else. `layout` picks
 * one of FR-TPL-005's four starting points; the other four remain
 * independently adjustable regardless of which layout was picked - see
 * `api.templates.model.HeaderArrangement`'s docstring for why that
 * independence is deliberate.
 */
function LayoutFields({
  layout,
  headerArrangement,
  totalsPosition,
  pageSize,
  margins,
  onChangeLayout,
  onChangeHeaderArrangement,
  onChangeTotalsPosition,
  onChangePageSize,
  onChangeMargins,
}: {
  layout: LayoutKey;
  headerArrangement: HeaderArrangementKey;
  totalsPosition: TotalsPositionKey;
  pageSize: PageSizeKey;
  margins: MarginsKey;
  onChangeLayout: (next: LayoutKey) => void;
  onChangeHeaderArrangement: (next: HeaderArrangementKey) => void;
  onChangeTotalsPosition: (next: TotalsPositionKey) => void;
  onChangePageSize: (next: PageSizeKey) => void;
  onChangeMargins: (next: MarginsKey) => void;
}) {
  const { t } = useI18n();

  return (
    <div data-testid="template-layout">
      <label>
        {t("invoice.template.designer.layout")}
        <select
          data-testid="template-layout-select"
          value={layout}
          onChange={(event) => onChangeLayout(event.target.value as LayoutKey)}
        >
          {LAYOUTS.map((value) => {
            const key = `invoice.template.designer.layout.${value}` as const;
            return (
              <option key={value} value={value}>
                {t(key)}
              </option>
            );
          })}
        </select>
      </label>

      <label>
        {t("invoice.template.designer.header_arrangement")}
        <select
          data-testid="template-header-arrangement"
          value={headerArrangement}
          onChange={(event) =>
            onChangeHeaderArrangement(event.target.value as HeaderArrangementKey)
          }
        >
          {HEADER_ARRANGEMENTS.map((value) => {
            const key = `invoice.template.designer.header_arrangement.${value}` as const;
            return (
              <option key={value} value={value}>
                {t(key)}
              </option>
            );
          })}
        </select>
      </label>

      <label>
        {t("invoice.template.designer.totals_position")}
        <select
          data-testid="template-totals-position"
          value={totalsPosition}
          onChange={(event) => onChangeTotalsPosition(event.target.value as TotalsPositionKey)}
        >
          {TOTALS_POSITIONS.map((value) => {
            const key = `invoice.template.designer.totals_position.${value}` as const;
            return (
              <option key={value} value={value}>
                {t(key)}
              </option>
            );
          })}
        </select>
      </label>

      <label>
        {t("invoice.template.designer.page_size")}
        <select
          data-testid="template-page-size"
          value={pageSize}
          onChange={(event) => onChangePageSize(event.target.value as PageSizeKey)}
        >
          {PAGE_SIZES.map((value) => {
            const key = `invoice.template.designer.page_size.${value}` as const;
            return (
              <option key={value} value={value}>
                {t(key)}
              </option>
            );
          })}
        </select>
      </label>

      <label>
        {t("invoice.template.designer.margins")}
        <select
          data-testid="template-margins"
          value={margins}
          onChange={(event) => onChangeMargins(event.target.value as MarginsKey)}
        >
          {MARGINS.map((value) => {
            const key = `invoice.template.designer.margins.${value}` as const;
            return (
              <option key={value} value={value}>
                {t(key)}
              </option>
            );
          })}
        </select>
      </label>
    </div>
  );
}

// --- logo (FR-TPL-001, FR-TPL-018) -------------------------------------------

/**
 * The one legitimate file input in this whole designer — see this
 * component's top-level docstring. `position`/`size` are closed-set
 * `<select>`s populated from `@ledgr/shared-types`' `LOGO_POSITIONS`/
 * `LOGO_SIZES`, the same "curated steps, never free entry" posture every
 * other control here takes; there is no numeric x/y or pixel-size input
 * anywhere.
 */
function LogoFields({
  logo,
  previewUrl,
  uploading,
  problem,
  paperColor,
  onSelectFile,
  onChange,
}: {
  logo: TemplateLogoView;
  /** An object URL for the picked/uploaded (or previously saved) logo, or
   * `null` when there is nothing to preview yet. */
  previewUrl: string | null;
  uploading: boolean;
  /** The server's own D5 message for a rejected upload, or `null`. Rendered
   * right here, never as the generic Save-time banner. */
  problem: string | null;
  paperColor: string;
  onSelectFile: (file: File) => void;
  onChange: (next: TemplateLogoView) => void;
}) {
  const { t } = useI18n();

  return (
    <div data-testid="template-logo">
      <label>
        {t("invoice.template.designer.logo_upload")}
        <input
          type="file"
          data-testid="template-logo-file"
          accept="image/png,image/jpeg,image/svg+xml"
          disabled={uploading}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) onSelectFile(file);
            // Cleared so selecting the SAME file again (e.g. after fixing it
            // and re-exporting under the same name) still fires `onChange`.
            event.target.value = "";
          }}
        />
      </label>
      {uploading ? (
        <p role="status" data-testid="template-logo-uploading">
          {t("invoice.template.designer.logo_uploading")}
        </p>
      ) : null}
      {problem !== null ? (
        <p role="alert" data-testid="template-logo-problem">
          {problem}
        </p>
      ) : null}

      <label>
        {t("invoice.template.designer.logo_position")}
        <select
          data-testid="template-logo-position"
          value={logo.position}
          onChange={(event) =>
            onChange({ ...logo, position: event.target.value as LogoPositionKey })
          }
        >
          {LOGO_POSITIONS.map((position) => {
            const key = `invoice.template.designer.logo_position.${position}` as const;
            return (
              <option key={position} value={position}>
                {t(key)}
              </option>
            );
          })}
        </select>
      </label>

      <label>
        {t("invoice.template.designer.logo_size")}
        <select
          data-testid="template-logo-size"
          value={logo.size}
          onChange={(event) => onChange({ ...logo, size: event.target.value as LogoSizeKey })}
        >
          {LOGO_SIZES.map((size) => {
            const key = `invoice.template.designer.logo_size.${size}` as const;
            return (
              <option key={size} value={size}>
                {t(key)}
              </option>
            );
          })}
        </select>
      </label>

      {/*
        FR-TPL-001's "transparent-background preview against the paper
        colour" - the container's own background is the template's chosen
        paper colour, not a default white or checkerboard, so a transparent
        PNG/SVG logo is checked against what it will actually sit on.
      */}
      <div
        data-testid="template-logo-preview"
        aria-label={t("invoice.template.designer.logo_preview")}
        style={{ backgroundColor: paperColor }}
      >
        {previewUrl ? (
          <img src={previewUrl} alt={t("invoice.template.designer.logo_preview_alt")} />
        ) : (
          <p data-testid="template-logo-none">{t("invoice.template.designer.logo_none")}</p>
        )}
      </div>
    </div>
  );
}
