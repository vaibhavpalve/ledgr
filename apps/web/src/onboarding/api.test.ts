import { describe, expect, it } from "vitest";

import { fakeFetch, jsonResponse } from "../testing/fakeFetch";
import { OnboardingApi } from "./api";

describe("OnboardingApi", () => {
  it("previews a fiscal year as a GET with the three query parameters", async () => {
    const periods = [
      { period_number: 1, start_date: "2026-01-01", end_date: "2026-03-31" },
      { period_number: 2, start_date: "2026-04-01", end_date: "2026-06-30" },
    ];
    const { impl, calls } = fakeFetch({
      "GET /v1/fiscal-years/preview": () => jsonResponse({ periods }),
    });
    const api = new OnboardingApi({ language: () => "nl", fetchImpl: impl });

    const result = await api.previewFiscalYear({
      start_date: "2026-01-01",
      end_date: "2026-06-30",
      period_scheme: "quarterly",
    });

    expect(result).toEqual(periods);
    expect(calls[0]?.url).toBe(
      "/v1/fiscal-years/preview?start_date=2026-01-01&end_date=2026-06-30&period_scheme=quarterly",
    );
    expect(calls[0]?.headers["idempotency-key"]).toBeUndefined();
  });

  it("accepts the preview as a bare array too", async () => {
    const periods = [{ period_number: 1, start_date: "2026-01-01", end_date: "2026-01-31" }];
    const api = new OnboardingApi({
      language: () => "nl",
      fetchImpl: fakeFetch({ "GET /v1/fiscal-years/preview": () => jsonResponse(periods) }).impl,
    });

    expect(
      await api.previewFiscalYear({
        start_date: "2026-01-01",
        end_date: "2026-01-31",
        period_scheme: "monthly",
      }),
    ).toEqual(periods);
  });

  it("creates an administration with the §4.2 body and an idempotency key", async () => {
    const { impl, calls } = fakeFetch({
      "POST /v1/administrations": () =>
        jsonResponse({
          id: "adm-1",
          legal_name: "Van Doorn Bouw B.V.",
          trade_name: null,
          legal_form: "bv",
          kvk_number: "34281907",
          vat_number: "NL001234567B01",
          formatting_locale: "nl-NL",
          colour: "indigo",
          initials: "VD",
          role: "Owner",
          role_is_system: true,
          fiscal_years: [
            {
              id: "fy-1",
              start_date: "2026-01-01",
              end_date: "2026-12-31",
              period_scheme: "monthly",
              is_current: true,
            },
          ],
          chart: { seeded: 62, rgs_version: "3.8-provisional" },
        }),
    });
    const api = new OnboardingApi({ language: () => "nl", fetchImpl: impl });

    const body = {
      legal_name: "Van Doorn Bouw B.V.",
      trade_name: null,
      legal_form: "bv" as const,
      kvk_number: "34281907",
      vat_number: "NL001234567B01",
      formatting_locale: "nl-NL",
      fiscal_year: {
        start_date: "2026-01-01",
        end_date: "2026-12-31",
        period_scheme: "monthly" as const,
      },
    };
    const created = await api.createAdministration(body);

    expect(created.chart.seeded).toBe(62);
    expect(calls[0]?.body).toEqual(body);
    expect(calls[0]?.headers["idempotency-key"]).toMatch(/[0-9a-f-]{36}/);
    expect(calls[0]?.headers["content-type"]).toBe("application/json");
  });

  it("PATCHes only the fields given", async () => {
    const { impl, calls } = fakeFetch({
      "PATCH /v1/administrations/adm-1": (call) =>
        jsonResponse({ id: "adm-1", ...(call.body as object) }),
    });
    const api = new OnboardingApi({ language: () => "nl", fetchImpl: impl });

    await api.updateAdministration("adm-1", { trade_name: "Van Doorn" });

    expect(calls[0]?.body).toEqual({ trade_name: "Van Doorn" });
  });
});
