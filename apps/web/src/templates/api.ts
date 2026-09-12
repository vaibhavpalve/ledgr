/**
 * The invoice template designer's calls to the API — FR-TPL-001..009,
 * FR-TPL-012, FR-TPL-013.
 *
 * Follows `capture/api.ts`'s conventions exactly (`ApiOptions`, `ApiError`,
 * `OfflineError`, an idempotency key on every mutating call, headers from
 * `languageHeaders`), because this is the same kind of client talking to the
 * same kind of server — there is no reason for the two to disagree about the
 * shape of a fetch call.
 *
 * --- One extra thing this client needs that capture's doesn't ---
 *
 * FR-TPL-009's 422 carries a `violations` array — one entry per statutory
 * field the save would have hidden or dropped — and `TemplateDesigner` needs
 * that array structurally, not just the flattened sentence `ApiError.message`
 * gives capture's simpler refusals. `TemplateNotCompliantError` carries it
 * alongside the same `status`/`reason`/`message` every other `ApiError` has,
 * so a caller that only wants the sentence still gets it, and a caller that
 * wants to place each violation next to the row it is about can.
 */

import type {
  ContentBlockKey,
  InvoiceTemplatePreviewView,
  InvoiceTemplateView,
  LineColumnKey,
  TemplateAssetUploadView,
  TemplateViolationView,
} from "@ledgr/shared-types";
import type { Language } from "@ledgr/i18n";

import { languageHeaders } from "../i18n";

/** The call could not be made because there is no connection. */
export class OfflineError extends Error {}

/**
 * The API refused, with the machine-readable `reason` from
 * `api.i18n.http.problem` and the already-translated sentence beside it —
 * see `capture/api.ts`'s identical class for the full rationale.
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly reason: string | null,
    message: string,
  ) {
    super(message);
  }
}

/**
 * FR-TPL-009's 422 (`reason: "invoice_template_incomplete"`), with the
 * per-field violations a form needs to place each message inline rather than
 * show one flattened sentence.
 */
export class TemplateNotCompliantError extends ApiError {
  constructor(
    status: number,
    message: string,
    readonly violations: readonly TemplateViolationView[],
  ) {
    super(status, "invoice_template_incomplete", message);
  }
}

export interface ApiOptions {
  language: () => Language;
  fetchImpl?: typeof fetch;
}

/** One column setting on the wire — `api.templates.routes.ColumnBody`. */
export interface TemplateColumnPatch {
  column: LineColumnKey;
  visible: boolean;
  position: number;
}

/** One content block on the wire — `api.templates.routes.BlockBody`. */
export interface TemplateBlockPatch {
  block: ContentBlockKey;
  text_nl: string;
  text_en: string;
}

/**
 * The designed object as sent to create/update — `api.templates.routes.
 * TemplateBody`. Every field but `name` is optional on the wire because the
 * server fills in an already-compliant scaffold when one is omitted
 * (`_default_columns`/`_default_blocks`); `TemplateDesigner` always sends the
 * full shape it is editing, but a bootstrap "create with just a name" caller
 * can rely on the same default this type allows.
 */
export interface TemplateWriteBody {
  name: string;
  is_default?: boolean;
  layout?: string;
  /** FR-TPL-005, independent of `layout`. */
  header_arrangement?: string;
  /** FR-TPL-005. */
  totals_position?: string;
  /** FR-TPL-010. */
  page_size?: string;
  /** FR-TPL-010. */
  margins?: string;
  logo?: { asset_id: string | null; position: string; size: string };
  typography?: {
    heading_font: string;
    body_font: string;
    figures_font: string;
    type_scale: string;
    font_weight: string;
    line_height: string;
    letter_spacing: string;
  };
  colors?: { accent: string; text: string; background: string };
  columns?: TemplateColumnPatch[];
  blocks?: TemplateBlockPatch[];
}

/** `api.templates.routes.TemplateUpdateBody`: the write body plus the version read back from a prior GET. */
export interface TemplateUpdateBody extends TemplateWriteBody {
  version: number;
}

/** `api.templates.routes.ResetTemplateBody`: just the version, exactly like `TemplateUpdateBody`'s own. */
export interface TemplateResetBody {
  version: number;
}

/** `api.templates.routes.PreviewBody`: the write body plus which document type and language to preview. */
export interface TemplatePreviewBody extends TemplateWriteBody {
  document_type?: string;
  language?: string;
}

export class TemplateApi {
  constructor(private readonly options: ApiOptions) {}

  listTemplates(administrationId: string): Promise<{ templates: InvoiceTemplateView[] }> {
    return this.call<{ templates: InvoiceTemplateView[] }>("GET", this.basePath(administrationId));
  }

  getTemplate(administrationId: string, templateId: string): Promise<InvoiceTemplateView> {
    return this.call<InvoiceTemplateView>("GET", this.templatePath(administrationId, templateId));
  }

  createTemplate(administrationId: string, body: TemplateWriteBody): Promise<InvoiceTemplateView> {
    return this.call<InvoiceTemplateView>("POST", this.basePath(administrationId), body);
  }

  updateTemplate(
    administrationId: string,
    templateId: string,
    body: TemplateUpdateBody,
  ): Promise<InvoiceTemplateView> {
    return this.call<InvoiceTemplateView>(
      "PUT",
      this.templatePath(administrationId, templateId),
      body,
    );
  }

  /**
   * FR-TPL-001's logo upload - `api.templates.routes.upload_template_asset`.
   * The body is the raw file bytes, `Content-Type` describing them - the
   * SAME shape `api.documents.routes.upload_document` takes, mirrored here
   * rather than a multipart form (see that route's own docstring for why: no
   * parser running over untrusted bytes before anything has decided to
   * accept them, and it costs a caller nothing since a `File` is already a
   * `Blob`). Returns the server's `{id, content_type, sanitized}` - `id` is
   * what `TemplateDesigner` writes into `logo.asset_id`.
   *
   * A 422/415 refusal (wrong type, or FR-TPL-018's sanitiser rejecting an
   * unsafe SVG) surfaces as an ordinary `ApiError` - the caller shows its
   * `message` inline at the logo control, the same posture every other
   * server-validated field in this component takes.
   */
  async uploadTemplateAsset(
    administrationId: string,
    file: Blob,
    contentType: string,
  ): Promise<TemplateAssetUploadView> {
    const fetchImpl = this.options.fetchImpl ?? fetch;
    let response: Response;
    try {
      response = await fetchImpl(this.assetsPath(administrationId), {
        method: "POST",
        headers: {
          ...languageHeaders(this.options.language()),
          "Content-Type": contentType,
          // NFR-032. A fresh key per upload: each one is a new intention.
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: file,
      });
    } catch (cause) {
      throw new OfflineError(`POST ${this.assetsPath(administrationId)} could not reach the API`, {
        cause,
      });
    }

    if (!response.ok) throw await problemFrom(response);
    return (await response.json()) as TemplateAssetUploadView;
  }

  /**
   * The stored logo bytes, as a `Blob` - for `TemplateDesigner`'s own
   * preview, built with `URL.createObjectURL` on the RESULT of this call
   * rather than a direct `<img src>` at the endpoint (see that component's
   * docstring on why - SEC-005's attachment disposition). Used when a
   * template being reopened already has a saved `logo.asset_id` but no
   * in-browser `File` left over from the upload that created it.
   */
  async fetchTemplateAssetBlob(administrationId: string, assetId: string): Promise<Blob> {
    const fetchImpl = this.options.fetchImpl ?? fetch;
    const path = `${this.assetsPath(administrationId)}/${encodeURIComponent(assetId)}`;
    let response: Response;
    try {
      response = await fetchImpl(path, {
        headers: languageHeaders(this.options.language()),
      });
    } catch (cause) {
      throw new OfflineError(`GET ${path} could not reach the API`, { cause });
    }

    if (!response.ok) throw await problemFrom(response);
    return await response.blob();
  }

  /**
   * FR-TPL-008's live preview - `api.templates.routes.preview_template`. Not
   * gated by FR-TPL-009: a momentarily non-compliant draft still renders,
   * with `violations` alongside it rather than a 422 - see that route's own
   * docstring. `templateId` only anchors the request to a real template in
   * this administration (tenancy/authorization), its STORED content is never
   * read - the full `body` is what gets rendered.
   */
  previewTemplate(
    administrationId: string,
    templateId: string,
    body: TemplatePreviewBody,
  ): Promise<InvoiceTemplatePreviewView> {
    return this.call<InvoiceTemplatePreviewView>(
      "POST",
      `${this.templatePath(administrationId, templateId)}/preview`,
      body,
    );
  }

  /**
   * FR-TPL-019's one-click duplicate - `api.templates.routes.
   * duplicate_template`. No request body: the server copies every field of
   * the source template into a brand-new row and names it itself ("Copy of
   * ..."), retrying on a name collision - see that route's own docstring.
   */
  duplicateTemplate(administrationId: string, templateId: string): Promise<InvoiceTemplateView> {
    return this.call<InvoiceTemplateView>(
      "POST",
      `${this.templatePath(administrationId, templateId)}/duplicate`,
    );
  }

  /**
   * FR-TPL-019's one-click reset - `api.templates.routes.reset_template`.
   * `id`/`name`/`is_default` are untouched server-side; `version` is bumped
   * exactly like an ordinary update, so a stale `version` refuses with the
   * same `invoice_template_stale_version` reason `updateTemplate` uses.
   */
  resetTemplate(
    administrationId: string,
    templateId: string,
    body: TemplateResetBody,
  ): Promise<InvoiceTemplateView> {
    return this.call<InvoiceTemplateView>(
      "POST",
      `${this.templatePath(administrationId, templateId)}/reset`,
      body,
    );
  }

  private basePath(administrationId: string): string {
    return `/v1/administrations/${encodeURIComponent(administrationId)}/invoice-templates`;
  }

  private templatePath(administrationId: string, templateId: string): string {
    return `${this.basePath(administrationId)}/${encodeURIComponent(templateId)}`;
  }

  private assetsPath(administrationId: string): string {
    return `/v1/administrations/${encodeURIComponent(administrationId)}/template-assets`;
  }

  private async call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const mutating = method !== "GET";
    const fetchImpl = this.options.fetchImpl ?? fetch;

    let response: Response;
    try {
      response = await fetchImpl(path, {
        method,
        headers: {
          ...languageHeaders(this.options.language()),
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
          // NFR-032. A fresh key per call: each save is a new intention.
          ...(mutating ? { "Idempotency-Key": crypto.randomUUID() } : {}),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (cause) {
      throw new OfflineError(`${method} ${path} could not reach the API`, { cause });
    }

    if (!response.ok) throw await problemFrom(response);
    return (await response.json()) as T;
  }
}

async function problemFrom(response: Response): Promise<ApiError> {
  let reason: string | null = null;
  let message = "";
  let violations: TemplateViolationView[] | null = null;
  try {
    const body = (await response.json()) as { detail?: unknown };
    const detail = body.detail;
    if (typeof detail === "string") {
      message = detail;
    } else if (detail !== null && typeof detail === "object") {
      const fields = detail as { reason?: unknown; message?: unknown; violations?: unknown };
      if (typeof fields.reason === "string") reason = fields.reason;
      if (typeof fields.message === "string") message = fields.message;
      if (Array.isArray(fields.violations)) {
        violations = fields.violations as TemplateViolationView[];
      }
    }
  } catch {
    // A gateway's HTML or an empty body. The status is still the answer.
  }

  if (reason === "invoice_template_incomplete" && violations !== null) {
    return new TemplateNotCompliantError(
      response.status,
      message || `HTTP ${response.status}`,
      violations,
    );
  }
  return new ApiError(response.status, reason, message || `HTTP ${response.status}`);
}
