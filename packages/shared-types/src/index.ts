// Shared types consumed by the web app and (via generated clients) the API.

// ISO 3166-1 alpha-2 country codes - kept in their own file (249 entries)
// rather than inlined here, the same way DELIVERY_CHANNELS below is small
// enough to just be a line in this file and this isn't.
export { COUNTRY_CODES, type CountryCode } from "./countries";

/**
 * FR-FRM-000a: the identity of a client, as every surface renders it.
 *
 * One type for web and mobile so the two cannot disagree about what
 * identifies a client. Produced by a single place on the API side
 * (`api.main._entry_json`), which is why the field names here match that
 * payload exactly rather than being a hand-written mirror of it.
 *
 * `colour` is a TOKEN, not a hex value — the clients own what amber looks
 * like, so contrast and palette decisions stay in the design layer and a
 * palette change is not a data migration.
 */
export interface ClientBadge {
  administrationId: string;
  /** Trade name where there is one, legal name otherwise. */
  displayName: string;
  legalName: string;
  tradeName: string | null;
  kvkNumber: string | null;
  colour: ClientColour;
  /**
   * Two characters that are not a colour. Every surface showing the marker
   * also shows this, so a client is still identified where colour is lost —
   * to a colour-vision deficiency, a monochrome rendering, or a header too
   * narrow for a full name.
   */
  initials: string;
  /**
   * True when another client in the same switcher carries this colour. The
   * palette has ten entries and a firm may have hundreds of clients, so the
   * UI is expected to lean on name and initials when this is set rather than
   * treating the colour as distinguishing.
   */
  colourIsAmbiguous: boolean;
}

export type ClientColour =
  | "indigo"
  | "amber"
  | "teal"
  | "rose"
  | "lime"
  | "violet"
  | "cyan"
  | "orange"
  | "emerald"
  | "fuchsia";

/** Mirrors client_colour (migration 0018) and api.firm.switcher.PALETTE. */
export const CLIENT_COLOURS: readonly ClientColour[] = [
  "indigo",
  "amber",
  "teal",
  "rose",
  "lime",
  "violet",
  "cyan",
  "orange",
  "emerald",
  "fuchsia",
];

/**
 * FR-EXP-001b / FR-EXP-001e: the expense form, as every surface renders it.
 *
 * The field names mirror `api.expenses.routes._expense_json` exactly, which is
 * deliberate and is the same rule `ClientBadge` follows: that function's own
 * docstring says "one shape, produced once, so web and mobile render the same
 * form", and a hand-written mirror here would be the second shape.
 *
 * **Every amount is a STRING.** JSON has one number type and it is a double, so
 * `JSON.parse` on an unquoted `1234.56` has already lost the value before any
 * code here sees it. NFR-031 covers the whole calculation path, and the client
 * is on it — these strings go to `money()` and `number()` from `@ledgr/i18n`
 * and are never converted with `Number()`.
 */
export interface ExpenseView {
  readonly id: string;
  readonly status: ExpenseStatus;
  readonly capture_item_id: string;
  readonly expense_date: string | null;
  readonly supplier: string | null;
  readonly gross_amount: string | null;
  readonly vat_treatment: VatTreatment | null;
  /** Shown, never sent: the rate comes from the treatment and the date (CMP-014). */
  readonly vat_rate: string | null;
  readonly vat_amount: string | null;
  readonly net_amount: string | null;
  readonly category: string | null;
  readonly payment_method: PaymentMethod | null;
  /** FR-EXP-001b's "defaulting from the user's history". Null once a category has been chosen. */
  readonly suggested_category: string | null;
  /** What marking ready would still refuse for, so a form can show all of it at once. */
  readonly missing_fields: readonly string[];
  readonly can_be_marked_ready: boolean;
  /** FR-EXP-001g. These WARN and never block — see `DuplicateWarning`. */
  readonly duplicate_warnings: readonly DuplicateWarning[];
}

export type ExpenseStatus = "draft" | "ready" | "posted";

/**
 * Mirrors `api.expenses.model.VatTreatment` — WHICH VAT applies, not the rate.
 *
 * FR-EXP-001b calls the field "VAT rate" and a rate is what the form SHOWS,
 * but a rate is a fact about a treatment on a DATE (CMP-014): `btw_21` was 19%
 * before 2012-10-01. Storing the rate would freeze a number that the receipt's
 * own date is supposed to decide.
 *
 * The names stay Dutch, like `KvK` and `BTW` in the catalogue's glossary
 * (FR-LOC-001c): these are the statutory treatments, and they are the
 * identifiers the API matches on.
 *
 * `btw_marge` is deliberately absent — the margin scheme charges VAT on the
 * margin, so extracting it from a gross amount would overstate the tax by
 * whatever the goods cost. Migration 0033 refuses it with a CHECK.
 */
export const VAT_TREATMENTS = [
  "btw_21",
  "btw_9",
  "btw_0",
  "btw_vrijgesteld",
  "btw_verlegd",
  "btw_icp",
  "btw_export",
] as const;

export type VatTreatment = (typeof VAT_TREATMENTS)[number];

/**
 * FR-EXP-001e. Captured at entry because it determines the posting and cannot
 * be reliably inferred later — `personal_reimbursable` is the one that means
 * the business owes the submitter money (FR-EXP-003).
 */
export const PAYMENT_METHODS = [
  "business_account",
  "business_card",
  "personal_reimbursable",
] as const;

export type PaymentMethod = (typeof PAYMENT_METHODS)[number];

/**
 * FR-EXP-001b's category, as the capture screen offers it BEFORE a file is sent.
 *
 * Mirrors `api.expenses.categories.EXPENSE_CATEGORIES` — the server owns the
 * list and refuses a key that is not on it, and `tests/expenses/test_categories.py`
 * fails if the two drift. One entry per line, in that file's order, so the
 * check can read this list without a TypeScript parser.
 *
 * `rgsCode` and `vatTreatment` are what the picker DISPLAYS. The server stores
 * the category only: a VAT treatment cannot be set without the rate, and the
 * rate follows the receipt's date, which capture does not have yet (CMP-014).
 * Which ledger account an administration posts to is its own mapping.
 */
export const EXPENSE_CATEGORIES = [
  { key: "inventory_stock", rgsCode: "7000", vatTreatment: "btw_21" },
  { key: "car_transport", rgsCode: "4300", vatTreatment: "btw_21" },
  { key: "travel_lodging", rgsCode: "4310", vatTreatment: "btw_9" },
  { key: "lunch", rgsCode: "4350", vatTreatment: "btw_9" },
  { key: "dining_out", rgsCode: "4350", vatTreatment: "btw_9" },
  { key: "entertainment_gifts", rgsCode: "4350", vatTreatment: "btw_21" },
  { key: "office_supplies", rgsCode: "4400", vatTreatment: "btw_21" },
  { key: "rent_premises", rgsCode: "4200", vatTreatment: "btw_21" },
  { key: "utilities", rgsCode: "4600", vatTreatment: "btw_21" },
  { key: "phone_internet", rgsCode: "4510", vatTreatment: "btw_21" },
  { key: "marketing_ads", rgsCode: "4700", vatTreatment: "btw_21" },
  { key: "software_subscriptions", rgsCode: "4720", vatTreatment: "btw_21" },
  { key: "insurance", rgsCode: "4730", vatTreatment: "btw_vrijgesteld" },
  { key: "professional_services", rgsCode: "4740", vatTreatment: "btw_21" },
  { key: "training_education", rgsCode: "4750", vatTreatment: "btw_21" },
  { key: "staff_costs", rgsCode: "4000", vatTreatment: "btw_21" },
  { key: "bank_interest", rgsCode: "4760", vatTreatment: "btw_vrijgesteld" },
  { key: "capital_asset", rgsCode: "0200", vatTreatment: "btw_21" },
  { key: "other", rgsCode: "4400", vatTreatment: "btw_21" },
] as const satisfies readonly {
  readonly key: string;
  readonly rgsCode: string;
  readonly vatTreatment: VatTreatment;
}[];

export type ExpenseCategoryKey = (typeof EXPENSE_CATEGORIES)[number]["key"];

/**
 * FR-EXP-001g's warning that this receipt may already be claimed.
 *
 * Arrives alongside `can_be_marked_ready: true` on purpose: two identical
 * receipts can be legitimate, so a client showing these must NOT gate its
 * submit button on the list being empty.
 */
export interface DuplicateWarning {
  readonly expense_id: string;
  readonly strength: "exact" | "strong" | "weak";
  readonly supplier: string;
  readonly expense_date: string;
  readonly gross_amount: string;
  readonly status: string;
  /**
   * Their own double entry is a mistake to fix; a colleague's is a
   * conversation to have. Who the other person is stays out of the payload — a
   * duplicate check is a poor place to learn what one's colleagues spend.
   */
  readonly same_submitter: boolean;
  readonly similarity: number;
}

/**
 * MOB-004's Approve/View tabs — `api.expenses.routes._expense_summary_json`
 * exactly. Deliberately lighter than `ExpenseView`: no suggested category, no
 * duplicate warnings — both are per-expense reads the list endpoint does not
 * do (see that function's own docstring), so a client must not expect them
 * on a list row.
 */
export interface ExpenseSummaryView {
  readonly id: string;
  readonly status: ExpenseStatus;
  readonly capture_item_id: string;
  readonly expense_date: string | null;
  readonly supplier: string | null;
  readonly gross_amount: string | null;
  readonly category: string | null;
  readonly missing_fields: readonly string[];
  readonly can_be_marked_ready: boolean;
}

/** FR-EXP-001a's review list, as `api.expenses.routes._session_json` sends it. */
export interface CaptureSessionView {
  readonly id: string;
  readonly open: boolean;
  readonly opened_at: string | null;
  readonly finalised_at: string | null;
  readonly items: readonly CaptureReviewEntry[];
  /** What finalising would produce. One per live item — pages do not count. */
  readonly expenses_to_create: number;
}

export interface CaptureReviewEntry {
  readonly item_id: string;
  readonly position: number;
  readonly expense_id: string | null;
  /** Stored ORIGINALS, not sheets of paper: a four-page PDF reports 1 (ADR-031). */
  readonly page_count: number;
  readonly content_types: readonly string[];
  readonly total_bytes: number;
  readonly discarded: boolean;
  /** A byte-identical earlier capture in this session. Not FR-EXP-001g's matching. */
  readonly duplicate_of: string | null;
}

/**
 * FR-TPL-006's six line-item columns, mirroring `api.templates.model.
 * LineColumn` — six, not seven: there is no per-line VAT amount in the data
 * model (ADR-037 decision #3), so there is no `vat_amount` member to offer a
 * toggle for.
 */
export type LineColumnKey =
  "quantity" | "unit" | "unit_price" | "discount" | "vat_rate" | "line_total";

/**
 * FR-TPL-009's three columns Wet OB art. 35a(1) requires on every line, and
 * therefore the three `TemplateDesigner` never offers a control to hide.
 * Mirrors `api.templates.model.STATUTORY_COLUMNS` — kept as a runtime value,
 * not just a type, for the same reason `PAYMENT_METHODS` is: a designer
 * component needs the actual set to decide which rows lock, not only their
 * shape.
 */
export const STATUTORY_COLUMNS: readonly LineColumnKey[] = ["quantity", "unit_price", "vat_rate"];

/** FR-TPL-007's five editable content blocks, mirroring `api.templates.model.ContentBlock`. */
export type ContentBlockKey = "header" | "intro" | "payment_terms" | "footer" | "legal_identity";

/** One row of `InvoiceTemplateView.columns` — `api.templates.routes._template_json`'s `columns[]` entry. */
export interface TemplateColumnView {
  readonly column: LineColumnKey;
  readonly visible: boolean;
  readonly position: number;
}

/**
 * One row of `InvoiceTemplateView.blocks`. Both languages sit on the one row
 * (FR-TPL-013) — see `api.templates.model.BlockText`'s docstring for why a
 * template with only one language of a mandatory block would make every
 * document to a reader of the other language non-compliant by construction.
 */
export interface TemplateBlockView {
  readonly block: ContentBlockKey;
  readonly text_nl: string;
  readonly text_en: string;
}

/** FR-TPL-005's four starting layouts, mirroring `api.templates.model.Layout`. */
export const LAYOUTS = ["classic", "modern", "compact", "minimal"] as const;
export type LayoutKey = (typeof LAYOUTS)[number];

/**
 * FR-TPL-005's header arrangement, mirroring `api.templates.model.
 * HeaderArrangement` — independent of `LAYOUTS` above, a layout is a
 * starting point and this is one of the things it starts a template at.
 */
export const HEADER_ARRANGEMENTS = ["split", "stacked", "centered"] as const;
export type HeaderArrangementKey = (typeof HEADER_ARRANGEMENTS)[number];

/** FR-TPL-005's totals block position, mirroring `api.templates.model.TotalsPosition`. */
export const TOTALS_POSITIONS = ["right", "left", "full_width"] as const;
export type TotalsPositionKey = (typeof TOTALS_POSITIONS)[number];

/** FR-TPL-010's two page sizes, mirroring `api.templates.model.PageSize`. */
export const PAGE_SIZES = ["a4", "letter"] as const;
export type PageSizeKey = (typeof PAGE_SIZES)[number];

/** FR-TPL-010's margin steps, mirroring `api.templates.model.Margins`. */
export const MARGINS = ["narrow", "normal", "wide"] as const;
export type MarginsKey = (typeof MARGINS)[number];

/** FR-TPL-001's three positions, mirroring `api.templates.model.LogoPosition`. */
export const LOGO_POSITIONS = ["left", "centre", "right"] as const;
export type LogoPositionKey = (typeof LOGO_POSITIONS)[number];

/** FR-TPL-001's three size steps, mirroring `api.templates.model.LogoSize`. */
export const LOGO_SIZES = ["small", "medium", "large"] as const;
export type LogoSizeKey = (typeof LOGO_SIZES)[number];

/** FR-TPL-001. `asset_id` is null for a template with no logo yet — the designer's starting state. */
export interface TemplateLogoView {
  readonly asset_id: string | null;
  readonly position: string;
  readonly size: string;
}

/**
 * `POST .../template-assets`'s response — `api.templates.routes._asset_json`.
 * `content_type` is one of the three FR-TPL-001 formats; `sanitized` is
 * always true for a successful upload (an SVG that failed sanitisation never
 * reaches this shape at all — the call rejects instead).
 */
export interface TemplateAssetUploadView {
  readonly id: string;
  readonly content_type: "image/png" | "image/jpeg" | "image/svg+xml";
  readonly sanitized: boolean;
}

/**
 * FR-TPL-002's curated typeface catalogue, mirroring migration 0042's
 * `insert into template_font` seed data exactly — nine SIL Open Font
 * License 1.1 typefaces covering serif, sans and mono, chosen for tabular
 * (lining) figures and full Latin-1 coverage. No endpoint currently serves
 * this list (checked: `api.templates.routes` registers only
 * list/create/get/update/preview, none of them a font catalogue), so it is
 * hardcoded here rather than invented independently by the frontend — the
 * same pattern `STATUTORY_COLUMNS` above already uses for a backend-owned
 * set a component needs as a runtime value, not only a type. Checked against
 * 0042's seed rows and `api.templates.model.FONT_CODES` directly by
 * `index.test.ts`, the same device that file already uses for
 * `CLIENT_COLOURS` against migration 0018 and `api.firm.switcher.PALETTE` —
 * a comment claiming three copies agree is worth exactly as much as a check
 * that they do.
 */
export const FONT_CATALOGUE = [
  { code: "inter", family_name: "Inter", category: "sans" },
  { code: "source_sans", family_name: "Source Sans 3", category: "sans" },
  { code: "ibm_plex_sans", family_name: "IBM Plex Sans", category: "sans" },
  { code: "public_sans", family_name: "Public Sans", category: "sans" },
  { code: "source_serif", family_name: "Source Serif 4", category: "serif" },
  { code: "ibm_plex_serif", family_name: "IBM Plex Serif", category: "serif" },
  { code: "pt_serif", family_name: "PT Serif", category: "serif" },
  { code: "ibm_plex_mono", family_name: "IBM Plex Mono", category: "mono" },
  { code: "source_code_pro", family_name: "Source Code Pro", category: "mono" },
] as const;

export type FontCode = (typeof FONT_CATALOGUE)[number]["code"];

/** FR-TPL-003's four type-scale steps, mirroring `api.templates.model.TypeScale`. */
export const TYPE_SCALES = ["small", "medium", "large", "extra_large"] as const;
export type TypeScaleKey = (typeof TYPE_SCALES)[number];

/** FR-TPL-003's weight steps, mirroring `api.templates.model.FontWeight`. */
export const FONT_WEIGHTS = ["regular", "medium", "bold"] as const;
export type FontWeightKey = (typeof FONT_WEIGHTS)[number];

/** FR-TPL-003's line-height steps, mirroring `api.templates.model.LineHeight`. */
export const LINE_HEIGHTS = ["tight", "normal", "relaxed"] as const;
export type LineHeightKey = (typeof LINE_HEIGHTS)[number];

/** FR-TPL-003's letter-spacing steps, mirroring `api.templates.model.LetterSpacing`. */
export const LETTER_SPACINGS = ["tight", "normal", "wide"] as const;
export type LetterSpacingKey = (typeof LETTER_SPACINGS)[number];

/** FR-TPL-002/003's typography controls, all curated steps rather than free values — see `api.templates.model.Typography`. */
export interface TemplateTypographyView {
  readonly heading_font: string;
  readonly body_font: string;
  readonly figures_font: string;
  readonly type_scale: string;
  readonly font_weight: string;
  readonly line_height: string;
  readonly letter_spacing: string;
}

/**
 * FR-TPL-004. `contrast_warnings` is advisory only and never blocks a save —
 * unlike `TemplateViolationView`, which does.
 */
export interface TemplateColorsView {
  readonly accent: string;
  readonly text: string;
  readonly background: string;
  readonly contrast_warnings: readonly string[];
}

/**
 * The whole designed object, exactly as `api.templates.routes._template_json`
 * sends it — field names match the wire on purpose, the same rule
 * `ExpenseView` follows against `_expense_json`.
 */
export interface InvoiceTemplateView {
  readonly id: string;
  readonly administration_id: string;
  readonly name: string;
  readonly is_default: boolean;
  /** Bumped on every save; sent back on update for optimistic concurrency. */
  readonly version: number;
  readonly layout: string;
  /** FR-TPL-005, independent of `layout` — see `HeaderArrangementKey`. */
  readonly header_arrangement: string;
  /** FR-TPL-005. */
  readonly totals_position: string;
  /** FR-TPL-010. */
  readonly page_size: string;
  /** FR-TPL-010. */
  readonly margins: string;
  readonly logo: TemplateLogoView;
  readonly typography: TemplateTypographyView;
  readonly colors: TemplateColorsView;
  readonly columns: readonly TemplateColumnView[];
  readonly blocks: readonly TemplateBlockView[];
  readonly updated_by_user_id: string | null;
  readonly created_at: string | null;
  readonly updated_at: string | null;
}

/**
 * FR-TPL-009's refusal, one entry per thing the server will not let a
 * template be saved without — `api.templates.routes._violations_json`'s
 * per-item shape exactly. `language` is set only for the two `legal_identity`
 * tag fields (which of the block's two languages lost the tag) and null for a
 * column violation, mirroring `api.templates.compliance.TemplateViolation`.
 */
export interface TemplateViolationView {
  readonly field: string;
  readonly language: "nl" | "en" | null;
  readonly message: string;
}

/**
 * FR-TPL-008's live preview response — `api.templates.routes.
 * preview_template`'s body exactly. `content_base64` decodes to
 * `content_type` (always `application/pdf` today) bytes; `violations` is
 * FR-TPL-009's advisory read of the SAME draft, never blocking the render —
 * see that route's own docstring.
 */
export interface InvoiceTemplatePreviewView {
  readonly document_type: string;
  readonly language: string;
  readonly content_type: string;
  readonly filename: string;
  readonly content_base64: string;
  readonly compliant: boolean;
  readonly violations: readonly TemplateViolationView[];
}

/** Mirrors `api.invoicing.model.InvoiceStatus`. */
export type SalesInvoiceStatus = "draft" | "issued";

/**
 * MOB-005's View tab — `api.invoicing.routes._invoice_summary_json` exactly.
 * Deliberately lighter than `SalesInvoiceView`: no line items, no VAT
 * breakdown, no statutory-failure check — all per-invoice reads the list
 * endpoint does not do (see that function's own docstring).
 */
export interface SalesInvoiceSummaryView {
  readonly id: string;
  readonly status: SalesInvoiceStatus;
  readonly invoice_number: number | null;
  readonly invoice_reference: string | null;
  readonly invoice_date: string;
  readonly due_date: string | null;
  readonly customer_name: string;
  readonly customer_id: string | null;
  readonly document_id: string | null;
}

/**
 * FR-AR-003's "what would stop this being issued" — `api.invoicing.routes.
 * _view_json`'s `statutory_failures[]` entry exactly. `message` is already
 * translated server-side (FR-UX-007); a client places it at the control
 * `field` names rather than composing its own sentence.
 */
export interface StatutoryFailureView {
  readonly field: string;
  readonly line_position: number | null;
  readonly message: string;
}

/** One line of `SalesInvoiceView.lines` — `_view_json`'s `lines[]` entry. */
export interface SalesInvoiceLineView {
  readonly id: string;
  readonly position: number;
  readonly description: string;
  readonly quantity: string;
  readonly unit_price: string;
  readonly discount_percent: string;
  readonly vat_treatment: VatTreatment;
  /** A line has a net amount and deliberately no VAT amount — see `_view_json`. */
  readonly line_net: string;
}

/** FR-AR-002's per-rate breakdown — `_view_json`'s `vat_groups[]` entry. */
export interface VatGroupView {
  readonly vat_treatment: string;
  readonly role: string;
  /** Null for the margin scheme, never conflated with 0. */
  readonly rate: string | null;
  readonly taxable_amount: string;
  readonly vat_amount: string;
  readonly legal_wording: string | null;
}

/**
 * The whole invoice, exactly as `api.invoicing.routes._view_json` sends it —
 * field names match the wire, the same rule `ExpenseView` follows.
 */
export interface SalesInvoiceView {
  readonly id: string;
  readonly status: SalesInvoiceStatus;
  readonly invoice_number: number | null;
  readonly invoice_reference: string | null;
  readonly invoice_date: string;
  readonly supply_date: string | null;
  readonly due_date: string | null;
  readonly customer_name: string;
  readonly customer_address: string;
  readonly customer_country: string;
  readonly customer_vat_number: string | null;
  readonly customer_id: string | null;
  readonly customer_language: string;
  readonly credits_invoice_id: string | null;
  readonly journal_entry_id: string | null;
  readonly document_id: string | null;
  readonly notes: string | null;
  readonly issued_at: string | null;
  readonly lines: readonly SalesInvoiceLineView[];
  readonly vat_groups: readonly VatGroupView[];
  readonly net_amount: string;
  readonly vat_amount: string;
  readonly gross_amount: string;
  readonly statutory_failures: readonly StatutoryFailureView[];
  readonly can_be_issued: boolean;
  /**
   * SI-12. Recent invoices to the same customer that look like this draft. A
   * warning shown beside it, never a reason `can_be_issued` is false - do not
   * gate the issue button on this being empty. Always empty on an issued
   * invoice or a credit note.
   */
  readonly duplicate_warnings: readonly DuplicateWarningView[];
  /**
   * SI-13. Which OB-aangifte boxes this invoice reports into, recomputed on
   * every save so a draft shows the consequence of its VAT treatments before
   * anything is filed. Informational only; gates nothing.
   */
  readonly rubriek_preview: RubriekPreviewView;
  readonly wording_is_provisional: boolean;
}

/**
 * ADR-070 - `api.invoicing.routes._payment_json`. A payment received against an
 * issued invoice; the ledger entry that recorded it is `journal_entry_id`.
 * A voided payment stays in the list (`voided_at` set) as history.
 */
export interface InvoicePaymentView {
  readonly id: string;
  readonly invoice_id: string;
  readonly amount: string;
  readonly paid_on: string;
  readonly method: "bank_transfer" | "cash" | "card" | "other";
  readonly reference: string | null;
  readonly bank_account_id: string;
  readonly journal_entry_id: string;
  readonly recorded_at: string;
  readonly voided_at: string | null;
  readonly void_journal_entry_id: string | null;
}

/** ADR-070 - what one invoice owes: `gross - credited - paid`. All amounts are decimal strings. */
export interface InvoiceBalanceView {
  readonly gross_amount: string;
  readonly credited_amount: string;
  readonly paid_amount: string;
  /** Written off as uncollectable (SI-10); an unvoided write-off clears the balance. */
  readonly written_off_amount: string;
  readonly outstanding_amount: string;
  /** `credited` = owed nothing because it was credited and never paid. */
  readonly state: "open" | "partially_paid" | "paid" | "credited" | "written_off";
}

/** `POST .../sales-invoices/{id}/payments` and `.../payments/{id}/void` answer this. */
export interface RecordedPaymentView {
  readonly payment: InvoicePaymentView;
  readonly balance: InvoiceBalanceView;
}

/** `GET .../sales-invoices/{id}/payments`. */
export interface InvoicePaymentsView {
  readonly payments: readonly InvoicePaymentView[];
  readonly balance: InvoiceBalanceView;
}

/** Days past the DUE date; `no_due_date` is counted in the totals, never guessed into another bucket. */
export type AgeBucketCode =
  "current" | "days_1_30" | "days_31_60" | "days_61_90" | "over_90" | "no_due_date";

/**
 * SI-06 (ADR-072) - `api.invoicing.routes._ageing_json`. `GET .../receivables/ageing?as_of=`.
 * Reproducible: the same `as_of` gives the same report later. Every amount is a decimal string.
 */
export interface AgeingReportView {
  readonly as_of: string;
  /** In display order, each with its label in the reader's language. */
  readonly buckets: readonly {
    readonly bucket: AgeBucketCode;
    readonly label: string;
    readonly amount: string;
  }[];
  readonly grand_total: string;
  /** Largest debt first. */
  readonly customers: readonly {
    /** Null for a one-off customer, who is grouped by the name typed on the invoice. */
    readonly customer_id: string | null;
    readonly customer_name: string;
    readonly total: string;
    readonly buckets: Readonly<Record<AgeBucketCode, string>>;
    /** The drill-down to the invoices (FR-RPT-002). */
    readonly invoices: readonly {
      readonly invoice_id: string;
      readonly invoice_reference: string | null;
      readonly invoice_date: string;
      readonly due_date: string | null;
      readonly outstanding_amount: string;
      readonly bucket: AgeBucketCode;
      readonly days_late: number;
    }[];
  }[];
}

/**
 * SI-06 - `api.invoicing.routes._statement_json`. `GET .../customers/{id}/statement?from=&to=`.
 * Positive balances mean the customer owes; negative means they are in credit.
 */
export interface CustomerStatementView {
  readonly customer_id: string;
  readonly customer_name: string;
  readonly date_from: string;
  readonly date_to: string;
  /** Everything before `date_from`, brought forward. */
  readonly opening_balance: string;
  readonly lines: readonly {
    readonly date: string;
    readonly kind:
      "invoice" | "credit_note" | "payment" | "payment_void" | "write_off" | "write_off_void";
    readonly label: string;
    readonly reference: string | null;
    readonly invoice_id: string | null;
    readonly debit: string;
    readonly credit: string;
    /** What the customer owed after this line. */
    readonly balance: string;
  }[];
  readonly total_debit: string;
  readonly total_credit: string;
  readonly closing_balance: string;
}

/** SI-04 (ADR-071) - `api.invoicing.routes._step_json`. One rung of the reminder ladder. */
export interface DunningStepView {
  readonly position: number;
  /** Days after the DUE date on which this step becomes due. */
  readonly days_after_due: number;
  readonly kind: "friendly" | "reminder" | "formal_notice";
  readonly charge_interest: boolean;
  /** Only a `formal_notice` may charge collection cost; the API refuses anything else. */
  readonly charge_collection_cost: boolean;
}

/** Why nothing is sent for an invoice right now - each has a sentence in `blocker_message`. */
export type DunningBlocker =
  | "nothing_outstanding"
  | "paused"
  | "no_due_date"
  | "not_overdue"
  | "ladder_complete"
  | "step_not_due"
  | "too_soon"
  | "interest_rate_missing";

/**
 * SI-04 - `api.invoicing.routes._assessment_json`: what should happen to one
 * invoice today. Read-only; looking sends nothing.
 */
export interface DunningAssessmentView {
  readonly invoice_id: string;
  readonly invoice_reference: string | null;
  readonly customer_id: string | null;
  readonly customer_name: string;
  readonly due_date: string | null;
  readonly days_overdue: number;
  readonly outstanding_amount: string;
  readonly can_send: boolean;
  readonly blocker: DunningBlocker | null;
  /** The blocker's sentence in the reader's language. */
  readonly blocker_message: string | null;
  /** The step in question, including when it is not yet due. Null only when none is left. */
  readonly next_step: DunningStepView | null;
  /** What THIS step will claim. Null where it claims nothing - never "0.00". */
  readonly interest_amount: string | null;
  readonly collection_cost_amount: string | null;
  /** For a formal notice: the last day the customer is given to pay. */
  readonly pay_by: string | null;
  /** Inferred (a VAT or KvK number): there is no consumer flag on the customer. */
  readonly is_business: boolean;
  readonly interest_kind: "commercial" | "consumer";
  readonly is_paused: boolean;
  readonly sent_steps: readonly number[];
}

/** `GET .../dunning`. */
export interface DunningOverviewView {
  readonly ladder: { readonly is_default: boolean; readonly steps: readonly DunningStepView[] };
  readonly overdue: readonly DunningAssessmentView[];
}

/**
 * FR-AR-005 - `api.invoicing.routes._delivery_json`: one dispatch of an invoice
 * (or, for SI-04, of a reminder about it) over one channel.
 */
export interface InvoiceDeliveryView {
  readonly channel: string;
  readonly status: "queued" | "sent" | "delivered" | "bounced" | "failed";
  /** Whether it actually ARRIVED - not the same as having been sent. */
  readonly reached_the_customer: boolean;
  readonly is_settled: boolean;
  readonly recipient: string;
  readonly language: "nl" | "en";
  readonly attempts: number;
  readonly provider: string | null;
  readonly provider_reference: string | null;
  readonly document_id: string | null;
  readonly requested_at: string | null;
  readonly sent_at: string | null;
  readonly settled_at: string | null;
  readonly next_attempt_at: string | null;
}

/**
 * SI-07 (ADR-074) - `api.invoicing.routes._recurring_json`. A schedule that generates sales
 * invoices on a rhythm. A TEMPLATE: generating one goes through the same service a person's
 * invoice does, so nothing here bypasses the statutory gate, numbering or posting.
 */
export interface RecurringInvoiceView {
  readonly id: string;
  readonly customer_id: string;
  readonly name: string;
  /** One of 1, 2, 3, 4, 6, 12. */
  readonly interval_months: number;
  readonly start_date: string;
  /** The last date a run may fall on, inclusive. Whichever of this and `max_runs` comes first ends it. */
  readonly end_date: string | null;
  readonly max_runs: number | null;
  /** Payment term in days; null means the customer's own terms. */
  readonly due_days: number | null;
  /** Annual price indexation, percent, compounding once per COMPLETED year. Decimal string. */
  readonly indexation_percent: string;
  /** Issue (number and post) each invoice, or leave it a draft. SENDING is never automatic. */
  readonly auto_issue: boolean;
  readonly notes: string | null;
  readonly status: "active" | "paused" | "ended";
  readonly runs_generated: number;
  readonly next_run_on: string | null;
  /** Why the last attempt failed, and its sentence in the reader's language. */
  readonly last_error:
    | "no_fiscal_year"
    | "customer_not_found"
    | "customer_archived"
    | "customer_details_incomplete"
    | null;
  readonly last_error_message: string | null;
  readonly lines: readonly {
    readonly description: string;
    readonly quantity: string;
    /** What is AGREED - the base price, before indexation. */
    readonly unit_price: string;
    /** What the NEXT invoice will charge once indexation applies; null when ended. */
    readonly next_unit_price: string | null;
    readonly discount_percent: string;
    readonly vat_treatment: string;
  }[];
}

/** `POST .../recurring-invoices/run`. Always 200 with a per-run report. */
export interface RecurringRunReportView {
  readonly summary: {
    readonly generated: number;
    readonly already_generated: number;
    readonly failed: number;
    readonly issued: number;
    /** Generated but not issued although the schedule asked to be: somebody must finish these. */
    readonly left_as_draft: number;
  };
  readonly results: readonly {
    readonly schedule_id: string;
    readonly schedule_name: string;
    /** Also the invoice date: a caught-up run is dated in its own month, not today. */
    readonly run_date: string;
    readonly status: "generated" | "already_generated" | "failed";
    readonly invoice_id: string | null;
    readonly issued: boolean;
    readonly issue_error: string | null;
    readonly issue_error_message: string | null;
    readonly error: string | null;
    readonly error_message: string | null;
  }[];
}

/** Why an invoice was not attempted by a bulk chase (SI-11). */
export type ChaseSkip =
  "blocked" | "needs_confirmation" | "step_changed" | "not_overdue_or_unknown" | "deferred";

/** Why an attempted reminder did not go (SI-11). */
export type ChaseFailure =
  "unreachable" | "not_rendered" | "not_delivered" | "already_sent" | "unavailable";

/**
 * SI-11 (ADR-073) - `api.invoicing.routes._chase_json`. `POST .../dunning/chase`.
 * Always HTTP 200 with a per-invoice report: one customer's failure does not stop the rest.
 *
 * Body: `{ items?: { invoice_id, step_position }[] | null }`. Omitted or null means "chase
 * everything" - friendly and ordinary reminders only; a formal notice is NEVER sent that way
 * and is reported as `needs_confirmation`. With `items`, exactly those invoices are chased, each
 * with the step the person REVIEWED (`step_changed` if it has since moved on); naming a formal
 * notice there is the explicit confirmation.
 */
export interface ChaseReportView {
  readonly summary: {
    readonly sent: number;
    readonly skipped: number;
    readonly failed: number;
    /** Over the per-call cap. Call again: anyone already reminded is `too_soon`. */
    readonly deferred: number;
  };
  readonly results: readonly {
    readonly invoice_id: string;
    readonly invoice_reference: string | null;
    readonly customer_name: string | null;
    readonly status: "sent" | "skipped" | "failed";
    readonly step: DunningStepView | null;
    readonly skip: ChaseSkip | null;
    /** The reason, in the reader's language - the blocker's sentence for `blocked`. */
    readonly skip_message: string | null;
    readonly blocker: DunningBlocker | null;
    readonly failure: ChaseFailure | null;
    readonly failure_message: string | null;
    readonly delivery: InvoiceDeliveryView | null;
  }[];
}

/** `POST .../sales-invoices/{id}/dunning/send`. */
export interface SentReminderView {
  readonly step: DunningStepView | null;
  readonly delivery: InvoiceDeliveryView;
  readonly outstanding_amount: string;
  readonly interest_amount: string | null;
  readonly collection_cost_amount: string | null;
  readonly pay_by: string | null;
}

/** SI-13 - `api.invoicing.routes._view_json`'s `rubriek_preview`. */
export interface RubriekPreviewView {
  readonly boxes: readonly RubriekBoxView[];
  /**
   * Treatments whose amounts cannot be shown in a box: the ruleset has no box
   * for them on the invoice date, or they are the margin scheme (the return
   * reports the margin, not the invoice's sale price). Say "cannot be placed",
   * never show a zero.
   */
  readonly unplaced_treatments: readonly string[];
}

export interface RubriekBoxView {
  /** The OB-aangifte box, e.g. `1a`. */
  readonly code: string;
  /** The box's title in the reader's language. */
  readonly description: string;
  readonly turnover_amount: string;
  /** Null where the box has no VAT column, which is not a VAT column of zero. */
  readonly vat_amount: string | null;
  /** The VAT treatment codes feeding the box. */
  readonly treatments: readonly string[];
}

/** SI-12 - `api.invoicing.routes._view_json`'s `duplicate_warnings[]` entry. `message` is already translated. */
export interface DuplicateWarningView {
  readonly invoice_id: string;
  /** `identical`: same lines. `same_total`: different lines, same net amount. */
  readonly strength: "identical" | "same_total";
  readonly status: "draft" | "issued";
  /** Null for a draft, which has no number yet. */
  readonly invoice_reference: string | null;
  readonly invoice_date: string;
  readonly net_amount: string;
  readonly message: string;
}

/**
 * FR-UX-005 / MOB-006's home-screen dashboard - `api.dashboard.routes.
 * _dashboard_json` exactly. Every amount is a STRING (NFR-031), the same rule
 * `ExpenseView` and `SalesInvoiceView` follow.
 */
export interface DashboardView {
  readonly cash_position: string;
  readonly receivables: string;
  readonly vat_estimate: string;
  /** The date range `vat_estimate` covers, so the UI can show it plainly. */
  readonly vat_period_start: string;
  readonly vat_period_end: string;
  /** FR-UX-005: already prioritised by the server - a client must not re-sort. */
  readonly items_needing_action: readonly DashboardActionItemView[];
  /** Draft receipts awaiting review, for the Review nav item. Absent from older servers. */
  readonly receipts_to_review?: number;
  /** Month-end cash, oldest first, ending with today's figure. Fewer than two points early in a fiscal year. */
  readonly cash_history?: readonly { readonly month: string; readonly balance: string }[];
  /** This month's cash minus last month's, or null without a previous month in the year. */
  readonly cash_change?: string | null;
  /** The filing period still open today, its due date and the VAT estimate for it. */
  readonly vat_return?: {
    readonly period_start: string;
    readonly period_end: string;
    readonly due_date: string;
    readonly estimate: string;
  } | null;
}

/** Mirrors `api.dashboard.model.ActionItemKind`. */
export type DashboardActionItemKind = "overdue_invoice" | "draft_expense" | "draft_invoice";

/**
 * One row of `DashboardView.items_needing_action` - `_action_item_json`
 * exactly. `description` is already translated server-side (FR-UX-007), the
 * same rule `StatutoryFailureView.message` follows; a client places it as-is
 * rather than composing its own sentence from `kind`.
 */
export interface DashboardActionItemView {
  readonly kind: DashboardActionItemKind;
  readonly id: string;
  readonly description: string;
  /** Invoice items: who it is for. Also inside description. */
  readonly customer_name?: string | null;
  /** Draft receipts only: the gross on the row. An invoice item carries none. */
  readonly amount?: string | null;
}

/* ==========================================================================
 * SESSION BOOTSTRAP — `GET /v1/me` (docs/founder-review-2026-09-14.md §4.1)
 * ==========================================================================
 *
 * Field names match the wire exactly, the same rule `ExpenseView` follows:
 * this shape is placed on screen more or less as received (the header, the
 * settings screens, the firm portfolio), and a hand-written camelCase mirror
 * would be the second shape. `ClientBadge` above is the deliberate exception
 * and says why.
 */

/** FR-ONB-004's list, mirroring `api.ledger.chart.LegalForm` and migration 0024's CHECK. */
export const LEGAL_FORMS = ["eenmanszaak", "vof", "bv", "stichting", "vereniging"] as const;
export type LegalForm = (typeof LEGAL_FORMS)[number];

/** FR-ONB-006, mirroring `api.ledger.fiscal.PeriodScheme`. */
export const PERIOD_SCHEMES = ["monthly", "quarterly"] as const;
export type PeriodScheme = (typeof PERIOD_SCHEMES)[number];

export interface FiscalYearView {
  readonly id: string;
  /** ISO calendar dates, never instants — see `formatDate` in @ledgr/i18n. */
  readonly start_date: string;
  readonly end_date: string;
  readonly period_scheme: PeriodScheme;
  readonly is_current: boolean;
}

/** One period of `GET /v1/fiscal-years/preview` — `api.ledger.fiscal.DerivedPeriod`. */
export interface FiscalYearPreviewPeriodView {
  readonly period_number: number;
  readonly start_date: string;
  readonly end_date: string;
}

/**
 * One administration the caller holds a grant on — the `administrations[]`
 * entry of `GET /v1/me`, and the response of `POST /v1/administrations`.
 * `role`/`role_is_system` follow `SwitcherEntry`'s reasoning: the NAME is the
 * identifier and is rendered through `roleLabel`.
 */
export interface AdministrationView {
  readonly id: string;
  readonly legal_name: string;
  readonly trade_name: string | null;
  /** Whatever the administration recorded (FR-ONB-002) — canonicalised server-side to `LegalForm`. */
  readonly legal_form: string;
  readonly kvk_number: string | null;
  readonly vat_number: string | null;
  /** FR-LOC-002: how figures in THESE books are written, for every reader. */
  readonly formatting_locale: string;
  /** SI-02: the administration's own bank account, for the "pay by bank" QR code on an invoice PDF. `null` means no QR code is rendered. */
  readonly iban: string | null;
  readonly colour: ClientColour;
  readonly initials: string;
  readonly role: string;
  readonly role_is_system: boolean;
  readonly fiscal_years: readonly FiscalYearView[];
}

/** `POST /v1/administrations`' answer: the administration plus what seeding it produced (FR-ONB-005). */
export interface CreatedAdministrationView extends AdministrationView {
  readonly chart: { readonly seeded: number; readonly rgs_version: string };
}

/** `kind` is `business` (Model B) or `firm` (Model A) — FR-MDL-001. */
export type OrganizationKind = "business" | "firm";

export interface MeView {
  readonly user: {
    readonly id: string;
    readonly email: string;
    readonly language: string | null;
    /** IAM-010b. False shows the persistent verification banner. */
    readonly email_verified: boolean;
  };
  readonly organization: {
    readonly id: string;
    readonly name: string;
    readonly kind: OrganizationKind;
    readonly kvk_number: string | null;
  };
  readonly administrations: readonly AdministrationView[];
  readonly active_administration_id: string | null;
  readonly mfa: { readonly has_totp: boolean; readonly has_passkey: boolean };
  readonly onboarding: { readonly needs_administration: boolean };
}

/* ==========================================================================
 * ACCOUNT SECURITY — §4.4
 * ==========================================================================
 */

/** One row of `GET /v1/me/sessions` — `api.auth.models.Session`, minus the secret. */
export interface SessionView {
  readonly id: string;
  readonly created_at: string;
  readonly last_active_at: string;
  readonly expires_at: string;
  /** The session this request itself rides on — the one that cannot be revoked from here without signing out. */
  readonly is_current: boolean;
}

/** One row of `GET /v1/me/passkeys` — `api.auth.passkeys.Passkey`, minus the key material. */
export interface PasskeyView {
  readonly id: string;
  readonly name: string;
  readonly created_at: string;
  readonly last_used_at: string | null;
}

/** One row of `GET /v1/me/trusted-devices` (ADR-061) — `api.auth.trusted_devices.TrustedDevice`, minus the token hash. */
export interface TrustedDeviceView {
  readonly id: string;
  readonly name: string | null;
  readonly created_at: string;
  readonly last_used_at: string;
  readonly expires_at: string;
}

/* ==========================================================================
 * CUSTOMERS — `api.customers.routes._customer_json`, field for field
 * ==========================================================================
 */

/** Mirrors `api.customers.model.VatNumberStatus`: the VIES verdict travels WITH the number (FR-ONB-003). */
export type VatNumberStatus = "unchecked" | "valid" | "invalid" | "unavailable";

/** Mirrors `api.customers.model.DeliveryChannel` (FR-AR-005). */
export const DELIVERY_CHANNELS = ["email", "peppol"] as const;
export type DeliveryChannel = (typeof DELIVERY_CHANNELS)[number];

export interface CustomerView {
  readonly id: string;
  readonly name: string;
  readonly trade_name: string | null;
  readonly address_line1: string | null;
  readonly address_line2: string | null;
  readonly postal_code: string | null;
  readonly city: string | null;
  readonly country: string;
  /** FR-AR-003: street, postcode and city — returned rather than inferred from four nullable fields. */
  readonly address_is_complete: boolean;
  readonly kvk_number: string | null;
  readonly vat_number: string | null;
  readonly vat_number_status: VatNumberStatus;
  readonly vat_number_checked_at: string | null;
  readonly vat_number_checked_name: string | null;
  readonly vat_number_consultation_number: string | null;
  readonly peppol_participant_id: string | null;
  readonly peppol_checked_at: string | null;
  readonly is_deliverable_over_peppol: boolean;
  readonly payment_terms_days: number;
  /** NFR-031: a decimal STRING; null is "no limit set", which is not "0". */
  readonly credit_limit: string | null;
  readonly delivery_channel: DeliveryChannel;
  readonly invoice_email: string | null;
  readonly language: string;
  readonly notes: string | null;
  readonly archived_at: string | null;
  readonly erased_at: string | null;
}

/* ==========================================================================
 * LEDGER READS — §4.3 (the Grootboek screen)
 * ==========================================================================
 *
 * Every amount is a STRING (NFR-031).
 */

/** Mirrors `api.ledger.model.AccountType`. */
export const ACCOUNT_TYPES = ["asset", "liability", "equity", "revenue", "expense"] as const;
export type AccountType = (typeof ACCOUNT_TYPES)[number];

/** One row of `GET .../chart-of-accounts` — `api.ledger.chart.ChartAccount`. */
export interface ChartAccountView {
  readonly id: string;
  readonly code: string;
  readonly name: string;
  readonly account_type: AccountType;
  readonly status: "active" | "blocked";
  readonly rgs_code: string | null;
  readonly control_kind: string | null;
}

/** One row of `GET .../trial-balance` — `api.ledger.model.TrialBalanceRow`. */
export interface TrialBalanceRowView {
  readonly account_id: string;
  readonly account_code: string;
  readonly account_name: string;
  readonly account_type: AccountType;
  readonly total_debit: string;
  readonly total_credit: string;
  readonly balance: string;
}

export interface TrialBalanceView {
  readonly fiscal_year_id: string;
  readonly rows: readonly TrialBalanceRowView[];
  readonly total_debit: string;
  readonly total_credit: string;
}

/** One line of a journal entry — `api.ledger.model.PostedLine`, with the account named for display. */
export interface JournalLineView {
  readonly id: string;
  readonly line_number: number;
  readonly account_id: string;
  readonly account_code: string;
  readonly account_name: string;
  readonly debit: string;
  readonly credit: string;
  readonly description: string | null;
}

/** One row of `GET .../journal-entries` — `api.ledger.model.PostedEntry` without its lines. */
export interface JournalEntrySummaryView {
  readonly id: string;
  readonly entry_number: number;
  readonly entry_date: string;
  readonly description: string;
  readonly journal_code: string;
  readonly document_reference: string | null;
  /** FR-GL-003: set when this entry reverses another — the only kind of row that earns a marker. */
  readonly reverses_entry_id: string | null;
  readonly total: string;
}

/** Cursor pagination, the shape §4.3 names: `cursor` is opaque and `null` when the list is exhausted. */
export interface JournalEntryPageView {
  readonly entries: readonly JournalEntrySummaryView[];
  readonly next_cursor: string | null;
}

export interface JournalEntryView extends JournalEntrySummaryView {
  readonly posted_at: string;
  readonly source_system: string;
  readonly lines: readonly JournalLineView[];
}

export interface SwitcherEntry extends ClientBadge {
  /**
   * The role's NAME, which is its identifier — authorization matches on it and
   * audit entries record it, so it crosses the wire untranslated whoever asks.
   * Render it through `roleLabel` from `@ledgr/i18n` rather than directly.
   */
  role: string;
  /**
   * FR-LOC-001: true for one of PRD §8.4's twelve system roles, whose name is
   * LEDGR's own vocabulary and is translated; false for a custom role
   * (ADR-013), whose name the organization chose and which is shown verbatim,
   * exactly as a client's legal name is.
   *
   * The API answers this because only the database can: an organization may
   * name a custom role "Bookkeeper", and deciding from whether the name
   * happens to be in the catalogue would render it as "Boekhouder" — a role
   * that organization does not have.
   */
  roleIsSystem: boolean;
  expiresAt: string | null;
}

/**
 * SI-08 (ADR-075) - `api.invoicing.routes._quote_json`. An offer or order confirmation that
 * converts into a DRAFT invoice. Totals are NET only: the VAT rate is the one in force on the
 * invoice date, so a VAT figure here would promise a rate that can change.
 */
export interface QuoteView {
  readonly id: string;
  readonly kind: "quote" | "order_confirmation";
  /** OF-0001 for quotes, OB-0001 for order confirmations. */
  readonly reference: string;
  readonly customer_id: string;
  readonly subject: string | null;
  readonly valid_until: string | null;
  readonly notes: string | null;
  readonly status: "draft" | "sent" | "accepted" | "declined" | "cancelled" | "converted";
  /** Derived, never stored: only a quote still out can expire. */
  readonly is_expired: boolean;
  readonly net_amount: string;
  readonly sent_at: string | null;
  readonly accepted_at: string | null;
  readonly accepted_by_name: string | null;
  readonly acceptance_reference: string | null;
  readonly declined_at: string | null;
  readonly decline_reason: string | null;
  readonly cancelled_at: string | null;
  readonly converted_at: string | null;
  readonly converted_invoice_id: string | null;
  readonly lines: readonly {
    readonly description: string;
    readonly quantity: string;
    readonly unit_price: string;
    readonly discount_percent: string;
    readonly vat_treatment: string;
    readonly line_net: string;
  }[];
}

/** `POST .../quotes/{id}/convert`. `already_converted` is true when a retry hit an existing conversion. */
export interface QuoteConversionView {
  readonly invoice_id: string;
  readonly already_converted: boolean;
  readonly quote: QuoteView;
}

/**
 * SI-10 (ADR-076) - `api.invoicing.routes._write_off_json`. An issued invoice written off as
 * uncollectable. The WHOLE outstanding balance is written off, never part of it. The VAT
 * reclaim is a second, later step (`vat_reclaimed_on`), allowed once the waiting period from the
 * due date has passed or when the customer is insolvent.
 */
export interface InvoiceWriteOffView {
  readonly id: string;
  readonly invoice_id: string;
  readonly amount: string;
  /** The VAT-bearing part of `amount`, reclaimable in due course. */
  readonly vat_amount: string;
  readonly vat_split: readonly {
    readonly vat_treatment: string;
    readonly vat_amount: string;
  }[];
  readonly written_off_on: string;
  readonly reason: string;
  readonly customer_insolvent: boolean;
  readonly expense_account_id: string;
  readonly journal_entry_id: string;
  readonly recorded_at: string;
  readonly vat_reclaimed_on: string | null;
  readonly vat_reclaim_journal_entry_id: string | null;
  /** A voided write-off stays in the list as history. */
  readonly voided_at: string | null;
  readonly void_journal_entry_id: string | null;
}

/** `POST .../write-offs` and `.../write-offs/{id}/void` answer this. */
export interface WriteOffResultView {
  readonly write_off: InvoiceWriteOffView;
  readonly balance: InvoiceBalanceView;
}

/** `GET .../write-offs`. */
export interface WriteOffListView {
  readonly write_offs: readonly InvoiceWriteOffView[];
  readonly balance: InvoiceBalanceView;
}

/**
 * SI-09 (ADR-077) - `api.invoicing.routes._mandate_json`. A customer's signed SEPA direct debit
 * authorisation. Evidence of consent: never edited, only revoked. A customer who changes bank
 * signs a new mandate.
 */
export interface SepaMandateView {
  readonly id: string;
  readonly customer_id: string;
  readonly mandate_reference: string;
  readonly scheme: "core" | "b2b";
  readonly kind: "recurring" | "one_off";
  readonly signed_on: string;
  readonly debtor_name: string;
  readonly debtor_iban: string;
  readonly debtor_bic: string | null;
  readonly status: "active" | "revoked";
  /** Derived: unused for 36 months, so it may no longer be collected on. */
  readonly is_lapsed: boolean;
  /** The first / recurring flag the NEXT collection on this mandate will carry. */
  readonly next_sequence: "FRST" | "RCUR" | "OOFF";
  readonly last_collected_on: string | null;
  readonly revoked_at: string | null;
  readonly revoked_reason: string | null;
  readonly created_at: string;
}

/** One generated pain.008 file. Download it from `.../sepa-collections/{id}/file`. */
export interface SepaCollectionBatchView {
  readonly id: string;
  readonly message_id: string;
  readonly collection_date: string;
  readonly item_count: number;
  readonly total_amount: string;
  /** SHA-256 of the file exactly as generated and served. */
  readonly file_sha256: string;
  readonly created_at: string;
  readonly cancelled_at: string | null;
}

/** One invoice inside a batch, and how it turned out. */
export interface SepaCollectionView {
  readonly id: string;
  readonly batch_id: string;
  readonly invoice_id: string;
  readonly mandate_id: string;
  readonly amount: string;
  readonly sequence_type: "FRST" | "RCUR" | "OOFF";
  readonly end_to_end_id: string;
  readonly status: "submitted" | "collected" | "failed" | "cancelled";
  readonly decided_at: string | null;
  readonly failure_reason: string | null;
  /** The payment recorded when the collection was confirmed. */
  readonly payment_id: string | null;
}

/** Why an invoice was left out of a batch. Reported, never silently dropped. */
export type SepaSkipReason =
  | "not_found"
  | "not_collectable"
  | "nothing_outstanding"
  | "already_in_collection"
  | "not_yet_due"
  | "no_mandate"
  | "mandate_lapsed";

/** `POST .../sepa-collections`. */
export interface SepaBatchResultView {
  readonly batch: SepaCollectionBatchView;
  readonly items: readonly SepaCollectionView[];
  readonly skipped: readonly { readonly invoice_id: string; readonly reason: SepaSkipReason }[];
}

/**
 * SI-16 (ADR-078) - `api.invoicing.routes._approval_json`. One request for the owner to approve
 * one draft, bound to a hash of the draft's contents: edit the draft afterwards and the approval
 * stops counting (`state: "stale"`).
 */
export interface InvoiceApprovalView {
  readonly id: string;
  readonly invoice_id: string;
  readonly status: "pending" | "approved" | "rejected" | "superseded";
  readonly content_hash: string;
  readonly requested_by_user_id: string;
  readonly requested_at: string;
  readonly request_note: string | null;
  readonly decided_by_user_id: string | null;
  readonly decided_at: string | null;
  readonly decision_reason: string | null;
}

/** `GET .../sales-invoices/{id}/approval`. */
export interface InvoiceApprovalStatusView {
  /** The administration has switched invoice approval on. */
  readonly required: boolean;
  /** `stale` = approved, but the draft was changed afterwards. */
  readonly state: "none" | "pending" | "approved" | "rejected" | "stale";
  /** Whether the CALLER could issue this draft right now (an approver, or a current approval). */
  readonly can_issue: boolean;
  readonly approval: InvoiceApprovalView | null;
}

/** One row of the owner's queue, `GET .../sales-invoice-approvals`. */
export interface InvoiceApprovalQueueEntryView extends InvoiceApprovalView {
  readonly customer_name: string;
  readonly invoice_date: string;
  readonly invoice_reference: string | null;
}

/**
 * SI-17 (ADR-079) - `api.invoicing.routes._batch_result_json`. One pass that raised an invoice per
 * entry. Every entry is a real draft (or, with `issue`, an issued invoice); an entry that could
 * not be raised is reported here and never spoils the others. A repeat with the same `batch_key`
 * returns the first batch with `already_exists: true` and creates nothing.
 */
export interface InvoiceBatchView {
  readonly id: string;
  readonly name: string;
  readonly batch_key: string | null;
  readonly invoice_date: string;
  readonly issue_requested: boolean;
  readonly summary: {
    readonly entries: number;
    readonly drafted: number;
    readonly issued: number;
    readonly failed: number;
  };
  readonly created_at: string;
}

export interface InvoiceBatchItemView {
  /** 1-based position of the entry in the request. */
  readonly position: number;
  readonly customer_id: string;
  /** `drafted` = a draft exists (see `issue_error` if issuing was asked for). */
  readonly status: "drafted" | "issued" | "failed";
  readonly invoice_id: string | null;
  readonly error:
    | "no_fiscal_year"
    | "customer_not_found"
    | "customer_archived"
    | "customer_details_incomplete"
    | "invoice_invalid"
    | null;
  readonly error_message: string | null;
  /** Why an entry stayed a draft although issuing was asked for. */
  readonly issue_error:
    | "not_authorized_to_issue"
    | "approval_required"
    | "not_statutory_compliant"
    | "no_open_period"
    | "no_sales_journal"
    | "posting_unconfigured"
    | "template_not_renderable"
    | null;
  readonly issue_error_message: string | null;
}

/** `POST .../sales-invoice-batches` and `GET .../sales-invoice-batches/{id}`. */
export interface InvoiceBatchResultView {
  readonly batch: InvoiceBatchView;
  readonly already_exists: boolean;
  readonly items: readonly InvoiceBatchItemView[];
}
