import { useEffect, useId, useMemo, useState } from "react";
import {
  DEFAULT_FORMATTING_LOCALE,
  FORMATTING_LOCALES,
  isFormattingLocale,
  useI18n,
} from "@ledgr/i18n";
import {
  LEGAL_FORMS,
  PERIOD_SCHEMES,
  type CreatedAdministrationView,
  type FiscalYearPreviewPeriodView,
  type LegalForm,
  type PeriodScheme,
} from "@ledgr/shared-types";

import { ApiError, describeError } from "../api/http";
import { vatNumberFormatProblem } from "../customers/api";
import { Icon } from "../shell/icons";
import { LoadingSkeleton } from "../shell/ScreenState";
import type { CreateAdministrationBody, OnboardingApi } from "./api";

/**
 * FR-ONB-004 (company and legal form), FR-ONB-005 (chart seeded from RGS),
 * FR-ONB-006 (fiscal year) — three steps and a review, then ONE call.
 *
 * --- Only what is needed to start (FR-UX-006) ---
 *
 * Legal name, legal form and a fiscal year are the three things the chart
 * seeder and the first posting cannot do without. Trade name, KvK number
 * and BTW-id are asked for because they are on the paper in front of the
 * person and appear on every invoice, but every one of them can be left
 * blank and filled in later under settings. Nothing about VAT settings,
 * branding or account detail is asked here — that is asked when it first
 * matters.
 *
 * --- Legal form as plain-language cards (D2) ---
 *
 * Each of FR-ONB-004's five forms is a card with the accounting consequence
 * in one line — what the choice changes about the books — rather than a
 * `<select>` of abbreviations. The consequence lines are the reason the
 * seeder wants the form at all (`api.ledger.chart` picks the RGS subset by
 * it), said in the owner's words.
 *
 * --- The fiscal year is previewed live (FR-ONB-006) ---
 *
 * Every change to the dates or the scheme asks `GET /v1/fiscal-years/
 * preview` what periods it would produce, and the answer is shown as a
 * list. The client does not derive periods itself: a short first year and
 * a stub period are the server's arithmetic (`derive_periods`), and a
 * preview that disagreed with what `open_year` then created would be worse
 * than none.
 *
 * --- Progress survives a reload ---
 *
 * The draft lives in component state and is mirrored to `sessionStorage`
 * under a per-organization key. It holds no financial data (FR-WEB-006 is
 * about financial data; a legal name and a date range are not), it is
 * cleared the moment the administration is created, and `sessionStorage`
 * dies with the tab.
 */
export interface OnboardingDraft {
  step: 0 | 1 | 2;
  legalName: string;
  tradeName: string;
  legalForm: LegalForm | null;
  kvkNumber: string;
  vatNumber: string;
  formattingLocale: string;
  startDate: string;
  endDate: string;
  periodScheme: PeriodScheme;
}

export function initialDraft(today = new Date()): OnboardingDraft {
  const year = today.getFullYear();
  return {
    step: 0,
    legalName: "",
    tradeName: "",
    legalForm: null,
    kvkNumber: "",
    vatNumber: "",
    formattingLocale: "nl-NL",
    startDate: `${year}-01-01`,
    endDate: `${year}-12-31`,
    periodScheme: "monthly",
  };
}

/** `nl-NL` → `onboarding.company.formatting_locale.nl_nl`: catalogue keys are lower-case words. */
export function localeKey(locale: string): string {
  return `onboarding.company.formatting_locale.${locale.replace("-", "_").toLowerCase()}`;
}

/**
 * FR-LOC-002's choice, offered only where there is one to make.
 *
 * `FORMATTING_LOCALES` comes from `@ledgr/i18n` and holds exactly one entry
 * today. It is NOT restated here: this file used to carry its own
 * `["nl-NL", "en-GB"]`, and the second of those is a locale nothing in the
 * product supports — not `api.i18n.formatting.LOCALES`, not the
 * `administration_formatting_locale` CHECK in migration 0030, not the
 * package this list belongs to. Choosing it got as far as the last step of
 * onboarding and was then refused by the API, which is the worst possible
 * place to discover it.
 *
 * With one supported locale the control is a statement, not a question
 * (FR-UX-006: ask only what is needed). A second entry turns it back into a
 * `<select>` with no further change here.
 */
export function FormattingLocaleField({
  id,
  value,
  testId,
  onChange,
}: {
  id: string;
  value: string;
  testId: string;
  onChange: (locale: string) => void;
}) {
  const { t } = useI18n();

  if (FORMATTING_LOCALES.length === 1) {
    return (
      <>
        <span className="label" id={`${id}-label`}>
          {t("onboarding.company.formatting_locale")}
        </span>
        <p aria-labelledby={`${id}-label`} data-testid={testId} data-locale={value}>
          {t(localeKey(value))}
        </p>
      </>
    );
  }

  return (
    <>
      <label htmlFor={id}>{t("onboarding.company.formatting_locale")}</label>
      <select id={id} value={value} data-testid={testId} onChange={(event) => onChange(event.target.value)}>
        {FORMATTING_LOCALES.map((locale) => (
          <option key={locale} value={locale}>
            {t(localeKey(locale))}
          </option>
        ))}
      </select>
    </>
  );
}

const STEP_KEYS = ["onboarding.step.company", "onboarding.step.fiscal_year", "onboarding.step.review"] as const;

export function OnboardingWizard({
  api,
  storageKey,
  firmClient,
  onCreated,
  onCancel,
}: {
  api: OnboardingApi;
  /** Per organization, so two accounts on one browser never share a draft. */
  storageKey: string;
  /** FR-ONB-001b: a firm adding a client — same wizard, different words. */
  firmClient: boolean;
  onCreated: (created: CreatedAdministrationView) => void;
  /** Present when there is somewhere to go back to (a firm's portfolio). */
  onCancel?: () => void;
}) {
  const { t, date } = useI18n();
  const [draft, setDraft] = useState<OnboardingDraft>(() => {
    const restored = readDraft(storageKey) ?? initialDraft();
    // A draft outlives a release. One saved while this screen still offered a
    // locale the product does not support would otherwise be restored holding
    // it, and `t(localeKey(...))` raises on a key that is no longer in the
    // catalogue — a blank screen instead of a wizard. Anything unrecognised
    // falls back to the default rather than being trusted.
    return isFormattingLocale(restored.formattingLocale)
      ? restored
      : { ...restored, formattingLocale: DEFAULT_FORMATTING_LOCALE };
  });
  const [submitting, setSubmitting] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [fieldProblem, setFieldProblem] = useState<string | null>(null);
  const headingId = useId();

  useEffect(() => {
    writeDraft(storageKey, draft);
  }, [storageKey, draft]);

  const update = (patch: Partial<OnboardingDraft>) => setDraft((current) => ({ ...current, ...patch }));

  const companyProblems = useMemo(() => validateCompany(draft), [draft]);
  const yearProblems = useMemo(() => validateYear(draft), [draft]);

  const submit = async () => {
    if (draft.legalForm === null) return;
    setSubmitting(true);
    setProblem(null);
    setFieldProblem(null);
    const body: CreateAdministrationBody = {
      legal_name: draft.legalName.trim(),
      trade_name: draft.tradeName.trim() === "" ? null : draft.tradeName.trim(),
      legal_form: draft.legalForm,
      kvk_number: draft.kvkNumber.trim() === "" ? null : draft.kvkNumber.replace(/\s/g, ""),
      vat_number: draft.vatNumber.trim() === "" ? null : draft.vatNumber.replace(/[\s.-]/g, "").toUpperCase(),
      formatting_locale: draft.formattingLocale,
      fiscal_year: {
        start_date: draft.startDate,
        end_date: draft.endDate,
        period_scheme: draft.periodScheme,
      },
    };
    try {
      const created = await api.createAdministration(body);
      clearDraft(storageKey);
      onCreated(created);
    } catch (error) {
      if (error instanceof ApiError && typeof error.fields.field === "string") {
        setFieldProblem(error.fields.field);
      }
      setProblem(describeError(error));
      setSubmitting(false);
    }
  };

  return (
    <section className="onboarding" aria-labelledby={headingId} data-testid="onboarding">
      <header className="onboarding__head">
        <h1 id={headingId}>{firmClient ? t("onboarding.title_firm") : t("onboarding.title")}</h1>
        <p className="onboarding__intro">{firmClient ? t("onboarding.intro_firm") : t("onboarding.intro")}</p>
        <ol className="steps" aria-label={t("onboarding.progress_label")}>
          {STEP_KEYS.map((key, index) => (
            <li
              key={key}
              aria-current={draft.step === index ? "step" : undefined}
              data-done={index < draft.step ? "true" : undefined}
            >
              {t(key)}
            </li>
          ))}
        </ol>
      </header>

      {draft.step === 0 ? (
        <form
          className="form onboarding__form"
          aria-label={t("onboarding.step.company")}
          data-testid="onboarding-company"
          onSubmit={(event) => {
            event.preventDefault();
            if (companyProblems.length === 0) update({ step: 1 });
          }}
        >
          <fieldset className="form__field">
            <legend className="label">{t("onboarding.legal_form.label")}</legend>
            <p className="form__hint">{t("onboarding.legal_form.hint")}</p>
            <div className="card-grid" role="radiogroup" aria-label={t("onboarding.legal_form.label")}>
              {LEGAL_FORMS.map((form) => (
                <label key={form} className="card choice" data-testid={`legal-form-${form}`}>
                  <input
                    type="radio"
                    name="legal-form"
                    value={form}
                    checked={draft.legalForm === form}
                    onChange={() => update({ legalForm: form })}
                  />
                  <span className="choice__title">{t(`onboarding.legal_form.${form}.title`)}</span>
                  <span className="choice__body">{t(`onboarding.legal_form.${form}.consequence`)}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <div className="form__grid">
            <div className="form__field">
              <label htmlFor="onb-legal-name">{t("onboarding.company.legal_name")}</label>
              <input
                id="onb-legal-name"
                type="text"
                required
                autoComplete="organization"
                value={draft.legalName}
                data-testid="onboarding-legal-name"
                onChange={(event) => update({ legalName: event.target.value })}
              />
              <p className="form__hint">{t("onboarding.company.legal_name_hint")}</p>
            </div>
            <div className="form__field">
              <label htmlFor="onb-trade-name">{t("onboarding.company.trade_name")}</label>
              <input
                id="onb-trade-name"
                type="text"
                value={draft.tradeName}
                data-testid="onboarding-trade-name"
                onChange={(event) => update({ tradeName: event.target.value })}
              />
              <p className="form__hint">{t("onboarding.company.trade_name_hint")}</p>
            </div>
            <div className="form__field">
              <label htmlFor="onb-kvk">{t("onboarding.company.kvk_number")}</label>
              <input
                id="onb-kvk"
                type="text"
                inputMode="numeric"
                value={draft.kvkNumber}
                aria-invalid={companyProblems.includes("kvk") ? true : undefined}
                aria-describedby="onb-kvk-hint"
                data-testid="onboarding-kvk"
                onChange={(event) => update({ kvkNumber: event.target.value })}
              />
              <p id="onb-kvk-hint" className={companyProblems.includes("kvk") ? "field-error" : "form__hint"}>
                {companyProblems.includes("kvk") ? t("onboarding.company.kvk_invalid") : t("onboarding.company.kvk_hint")}
              </p>
            </div>
            <div className="form__field">
              <label htmlFor="onb-vat">{t("onboarding.company.vat_number")}</label>
              <input
                id="onb-vat"
                type="text"
                value={draft.vatNumber}
                aria-invalid={companyProblems.includes("vat") ? true : undefined}
                aria-describedby="onb-vat-hint"
                data-testid="onboarding-vat"
                onChange={(event) => update({ vatNumber: event.target.value })}
              />
              <p id="onb-vat-hint" className={companyProblems.includes("vat") ? "field-error" : "form__hint"}>
                {companyProblems.includes("vat") ? t("onboarding.company.vat_invalid") : t("onboarding.company.vat_hint")}
              </p>
            </div>
            <div className="form__field">
              <FormattingLocaleField
                id="onb-locale"
                value={draft.formattingLocale}
                testId="onboarding-locale"
                onChange={(formattingLocale) => update({ formattingLocale })}
              />
              <p className="form__hint">{t("onboarding.company.formatting_locale_hint")}</p>
            </div>
          </div>

          {companyProblems.includes("legal_form") || companyProblems.includes("legal_name") ? (
            <p className="field-error" role="alert" data-testid="onboarding-company-problem">
              {t("onboarding.company.incomplete")}
            </p>
          ) : null}

          <div className="form__actions">
            <button type="submit" data-testid="onboarding-next">
              {t("onboarding.action.next")}
            </button>
            {onCancel !== undefined ? (
              <button type="button" className="button--quiet" onClick={onCancel}>
                {t("common.action.cancel")}
              </button>
            ) : null}
          </div>
        </form>
      ) : null}

      {draft.step === 1 ? (
        <form
          className="form onboarding__form"
          aria-label={t("onboarding.step.fiscal_year")}
          data-testid="onboarding-fiscal-year"
          onSubmit={(event) => {
            event.preventDefault();
            if (yearProblems.length === 0) update({ step: 2 });
          }}
        >
          <p className="form__hint">{t("onboarding.fiscal_year.intro")}</p>
          <div className="form__grid">
            <div className="form__field">
              <label htmlFor="onb-start">{t("onboarding.fiscal_year.start")}</label>
              <input
                id="onb-start"
                type="date"
                required
                value={draft.startDate}
                data-testid="onboarding-start"
                onChange={(event) => update({ startDate: event.target.value })}
              />
            </div>
            <div className="form__field">
              <label htmlFor="onb-end">{t("onboarding.fiscal_year.end")}</label>
              <input
                id="onb-end"
                type="date"
                required
                value={draft.endDate}
                aria-invalid={yearProblems.length > 0 ? true : undefined}
                data-testid="onboarding-end"
                onChange={(event) => update({ endDate: event.target.value })}
              />
            </div>
            <fieldset className="form__field form__span">
              <legend className="label">{t("onboarding.fiscal_year.scheme")}</legend>
              <div className="option-row">
                {PERIOD_SCHEMES.map((scheme) => (
                  <label key={scheme} data-testid={`onboarding-scheme-${scheme}`}>
                    <input
                      type="radio"
                      name="period-scheme"
                      value={scheme}
                      checked={draft.periodScheme === scheme}
                      onChange={() => update({ periodScheme: scheme })}
                    />
                    {t(`onboarding.fiscal_year.scheme.${scheme}`)}
                  </label>
                ))}
              </div>
              <p className="form__hint">{t("onboarding.fiscal_year.scheme_hint")}</p>
            </fieldset>
          </div>

          {yearProblems.length > 0 ? (
            <p className="field-error" role="alert" data-testid="onboarding-year-problem">
              {t("onboarding.fiscal_year.invalid")}
            </p>
          ) : (
            <PeriodPreview api={api} draft={draft} />
          )}

          <div className="form__actions">
            <button type="submit" data-testid="onboarding-next">
              {t("onboarding.action.next")}
            </button>
            <button type="button" className="button--quiet" data-testid="onboarding-back" onClick={() => update({ step: 0 })}>
              {t("common.action.back")}
            </button>
          </div>
        </form>
      ) : null}

      {draft.step === 2 && draft.legalForm !== null ? (
        <div className="form onboarding__form" data-testid="onboarding-review">
          <p className="form__hint">{t("onboarding.review.intro")}</p>
          <dl className="facts panel panel__body">
            <div>
              <dt className="label">{t("onboarding.company.legal_name")}</dt>
              <dd data-testid="review-legal-name">{draft.legalName}</dd>
            </div>
            <div>
              <dt className="label">{t("onboarding.legal_form.label")}</dt>
              <dd>{t(`onboarding.legal_form.${draft.legalForm}.title`)}</dd>
            </div>
            {draft.tradeName.trim() !== "" ? (
              <div>
                <dt className="label">{t("onboarding.company.trade_name")}</dt>
                <dd>{draft.tradeName}</dd>
              </div>
            ) : null}
            {draft.kvkNumber.trim() !== "" ? (
              <div>
                <dt className="label">{t("onboarding.company.kvk_number")}</dt>
                <dd className="ledgr-num">{draft.kvkNumber}</dd>
              </div>
            ) : null}
            {draft.vatNumber.trim() !== "" ? (
              <div>
                <dt className="label">{t("onboarding.company.vat_number")}</dt>
                <dd className="ledgr-num">{draft.vatNumber}</dd>
              </div>
            ) : null}
            <div>
              <dt className="label">{t("onboarding.company.formatting_locale")}</dt>
              <dd>{t(localeKey(draft.formattingLocale))}</dd>
            </div>
            <div>
              <dt className="label">{t("onboarding.step.fiscal_year")}</dt>
              <dd className="ledgr-num">
                {t("onboarding.review.year_range", { start: date(draft.startDate), end: date(draft.endDate) })}
              </dd>
              <dd className="caption">{t(`onboarding.fiscal_year.scheme.${draft.periodScheme}`)}</dd>
            </div>
          </dl>

          <p className="alert" data-testid="onboarding-chart-note">
            <Icon name="ledger" size={18} />
            <span>{t("onboarding.review.chart_note", { form: t(`onboarding.legal_form.${draft.legalForm}.title`) })}</span>
          </p>

          {problem !== null ? (
            <div role="alert" className="alert alert--attention" data-testid="onboarding-problem">
              <p>{problem}</p>
              {fieldProblem !== null ? <p className="caption">{t("onboarding.review.field_problem", { field: fieldProblem })}</p> : null}
            </div>
          ) : null}

          <div className="form__actions">
            <button
              type="button"
              className="button--primary"
              disabled={submitting}
              data-testid="onboarding-submit"
              onClick={() => void submit()}
            >
              {submitting ? t("onboarding.action.creating") : firmClient ? t("onboarding.action.create_firm") : t("onboarding.action.create")}
            </button>
            <button type="button" className="button--quiet" data-testid="onboarding-back" disabled={submitting} onClick={() => update({ step: 1 })}>
              {t("common.action.back")}
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}

function PeriodPreview({ api, draft }: { api: OnboardingApi; draft: OnboardingDraft }) {
  const { t, date } = useI18n();
  const [periods, setPeriods] = useState<readonly FiscalYearPreviewPeriodView[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPeriods(null);
    setProblem(null);
    api
      .previewFiscalYear({ start_date: draft.startDate, end_date: draft.endDate, period_scheme: draft.periodScheme })
      .then((result) => {
        if (!cancelled) setPeriods(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) setProblem(describeError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [api, draft.startDate, draft.endDate, draft.periodScheme]);

  if (problem !== null) {
    return (
      <p role="alert" className="alert alert--attention" data-testid="onboarding-preview-error">
        {problem}
      </p>
    );
  }
  if (periods === null) return <LoadingSkeleton rows={2} testId="onboarding-preview-loading" />;

  return (
    <section className="screen__section" aria-label={t("onboarding.fiscal_year.preview_heading")} data-testid="onboarding-preview">
      <div className="section-head">
        <h2>{t("onboarding.fiscal_year.preview_heading")}</h2>
        <span className="chip chip--accent">{t("onboarding.fiscal_year.period_count", { count: periods.length })}</span>
      </div>
      <ol className="panel list">
        {periods.map((period) => (
          <li key={period.period_number} className="list__row" data-testid="onboarding-period">
            <span className="chip ledgr-num">{period.period_number}</span>
            <span className="list__row-text ledgr-num">
              {t("onboarding.review.year_range", { start: date(period.start_date), end: date(period.end_date) })}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

type CompanyProblem = "legal_form" | "legal_name" | "kvk" | "vat";

export function validateCompany(draft: OnboardingDraft): CompanyProblem[] {
  const problems: CompanyProblem[] = [];
  if (draft.legalForm === null) problems.push("legal_form");
  if (draft.legalName.trim() === "") problems.push("legal_name");
  const kvk = draft.kvkNumber.replace(/\s/g, "");
  if (kvk !== "" && !/^\d{8}$/.test(kvk)) problems.push("kvk");
  if (draft.vatNumber.trim() !== "" && vatNumberFormatProblem(draft.vatNumber) !== null) problems.push("vat");
  return problems;
}

export function validateYear(draft: OnboardingDraft): Array<"order"> {
  const iso = /^\d{4}-\d{2}-\d{2}$/;
  if (!iso.test(draft.startDate) || !iso.test(draft.endDate) || draft.endDate <= draft.startDate) {
    return ["order"];
  }
  return [];
}

function readDraft(key: string): OnboardingDraft | null {
  try {
    const raw = sessionStorage.getItem(key);
    if (raw === null) return null;
    const parsed = JSON.parse(raw) as Partial<OnboardingDraft>;
    return { ...initialDraft(), ...parsed };
  } catch {
    return null;
  }
}

function writeDraft(key: string, draft: OnboardingDraft): void {
  try {
    sessionStorage.setItem(key, JSON.stringify(draft));
  } catch {
    // Storage can be unavailable; the wizard still works for this page's lifetime.
  }
}

function clearDraft(key: string): void {
  try {
    sessionStorage.removeItem(key);
  } catch {
    // See writeDraft.
  }
}
