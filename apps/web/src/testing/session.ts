import type { AdministrationView, MeView } from "@ledgr/shared-types";

/**
 * A `GET /v1/me` answer, for the suites that mount the app past sign-in.
 *
 * Since ADR-058 "signed in" is two facts, not one: `AuthProvider` decides
 * whether anyone is signed in, and `SessionProvider` then loads this before
 * any screen behind `RequireAdministration` can render. A test that sets
 * `authenticated` and stubs no `/v1/me` gets the session error state — which
 * is correct behaviour, and not what most tests mean to assert.
 *
 * Kept here rather than in one suite so the shape is stated once: it mirrors
 * the founder review's §4.1 contract, and when the real backend answers
 * differently the reconciliation belongs in `account/api.ts`, never in a
 * per-test literal that would quietly disagree with it.
 */
export const testFiscalYear = {
  id: "fy-2026",
  start_date: "2026-01-01",
  end_date: "2026-12-31",
  period_scheme: "monthly",
  is_current: true,
} as const;

export const testAdministration: AdministrationView = {
  id: "adm-A",
  legal_name: "Van Doorn Bouw B.V.",
  trade_name: "Van Doorn",
  legal_form: "BV",
  kvk_number: "34281907",
  vat_number: "NL001234567B01",
  formatting_locale: "nl-NL",
  iban: null,
  address_line1: null,
  address_line2: null,
  postal_code: null,
  city: null,
  country: "NL",
  colour: "indigo",
  initials: "VD",
  role: "Owner",
  role_is_system: true,
  fiscal_years: [testFiscalYear],
};

export function meFixture(overrides: Partial<MeView> = {}): MeView {
  return {
    user: {
      id: "user-1",
      email: "eigenaar@vandoornbouw.nl",
      language: null,
      email_verified: true,
    },
    organization: {
      id: "org-1",
      name: "Van Doorn Bouw B.V.",
      kind: "business",
      kvk_number: "34281907",
    },
    administrations: [testAdministration],
    active_administration_id: testAdministration.id,
    mfa: { has_totp: true, has_passkey: false },
    onboarding: { needs_administration: false },
    ...overrides,
  };
}

/** A firm with no clients yet — FR-ONB-001b's empty portfolio. */
export function firmMeFixture(): MeView {
  return meFixture({
    organization: { id: "org-f", name: "Bakker & Co", kind: "firm", kvk_number: "11223344" },
    administrations: [],
    active_administration_id: null,
    onboarding: { needs_administration: true },
  });
}
