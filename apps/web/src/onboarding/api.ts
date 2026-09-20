/**
 * Onboarding's calls — docs/founder-review-2026-09-14.md §4.2, FR-ONB-004,
 * FR-ONB-005, FR-ONB-006.
 *
 *   GET   /v1/fiscal-years/preview?start_date&end_date&period_scheme
 *   POST  /v1/administrations                      the one transaction
 *   PATCH /v1/administrations/{id}                 legal/trade name, VAT number, locale
 *   GET   /v1/administrations/{id}/fiscal-years
 *   POST  /v1/administrations/{id}/fiscal-years
 *
 * Built against the contract while the backend lands it; the wire shapes
 * are `@ledgr/shared-types`' `AdministrationView`/`FiscalYearView`/
 * `FiscalYearPreviewPeriodView`. `toPeriods` is the one seam that tolerates
 * the preview arriving as a bare array or under a `periods` key.
 */

import type {
  AdministrationView,
  CreatedAdministrationView,
  FiscalYearPreviewPeriodView,
  FiscalYearView,
  LegalForm,
  PeriodScheme,
} from "@ledgr/shared-types";

import { callJson, pathOf, queryOf, unwrapList, type ApiOptions } from "../api/http";

export { ApiError, OfflineError } from "../api/http";

export interface FiscalYearBody {
  start_date: string;
  end_date: string;
  period_scheme: PeriodScheme;
}

/** `POST /v1/administrations`' body, exactly as §4.2 spells it. */
export interface CreateAdministrationBody {
  legal_name: string;
  trade_name: string | null;
  legal_form: LegalForm;
  kvk_number: string | null;
  vat_number: string | null;
  formatting_locale: string;
  fiscal_year: FiscalYearBody;
}

/** `PATCH /v1/administrations/{id}`: only what is passed is sent. */
export interface AdministrationPatch {
  legal_name?: string;
  trade_name?: string | null;
  vat_number?: string | null;
  formatting_locale?: string;
  /** SI-02. Validated server-side (mod-97 checksum); `null` clears it. */
  iban?: string | null;
}

export class OnboardingApi {
  constructor(private readonly options: ApiOptions) {}

  /** FR-ONB-006's live period preview. Arithmetic on two dates; no administration needed. */
  async previewFiscalYear(body: FiscalYearBody): Promise<FiscalYearPreviewPeriodView[]> {
    const raw = await callJson<
      readonly FiscalYearPreviewPeriodView[] | { periods: readonly FiscalYearPreviewPeriodView[] }
    >(this.options, "GET", `/v1/fiscal-years/preview${queryOf({ ...body })}`);
    return unwrapList<FiscalYearPreviewPeriodView>(raw, "periods");
  }

  createAdministration(body: CreateAdministrationBody): Promise<CreatedAdministrationView> {
    return callJson<CreatedAdministrationView>(this.options, "POST", "/v1/administrations", body);
  }

  updateAdministration(
    administrationId: string,
    patch: AdministrationPatch,
  ): Promise<AdministrationView> {
    return callJson<AdministrationView>(
      this.options,
      "PATCH",
      pathOf("v1", "administrations", administrationId),
      patch,
    );
  }

  async listFiscalYears(administrationId: string): Promise<FiscalYearView[]> {
    const raw = await callJson<
      readonly FiscalYearView[] | { fiscal_years: readonly FiscalYearView[] }
    >(this.options, "GET", pathOf("v1", "administrations", administrationId, "fiscal-years"));
    return unwrapList<FiscalYearView>(raw, "fiscal_years");
  }

  createFiscalYear(administrationId: string, body: FiscalYearBody): Promise<FiscalYearView> {
    return callJson<FiscalYearView>(
      this.options,
      "POST",
      pathOf("v1", "administrations", administrationId, "fiscal-years"),
      body,
    );
  }
}
