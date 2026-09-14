import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FORMATTING_LOCALES, I18nProvider } from "@ledgr/i18n";

import type { OnboardingApi } from "./api";
import { OnboardingWizard } from "./OnboardingWizard";

/**
 * FR-LOC-002's control, and the reason it has a test of its own.
 *
 * The wizard used to carry its own `["nl-NL", "en-GB"]`. The second is a
 * locale nothing in the product supports — not `api.i18n.formatting.LOCALES`,
 * not `administration_formatting_locale`'s CHECK in migration 0030, not
 * `@ledgr/i18n`'s own list — so picking it got a person all the way to the
 * last step of onboarding and was then refused by the API, with their company
 * details filled in and nothing to do but go back. Nothing caught it, because
 * a hard-coded list in a component agrees with itself.
 */

const STORAGE_KEY = "test.onboarding.draft";

function api(): OnboardingApi {
  return {
    createAdministration: vi.fn(),
    previewFiscalYear: vi.fn(async () => []),
  } as unknown as OnboardingApi;
}

function renderWizard() {
  return render(
    <I18nProvider initialLanguage="nl">
      <OnboardingWizard
        api={api()}
        storageKey={STORAGE_KEY}
        firmClient={false}
        onCreated={vi.fn()}
      />
    </I18nProvider>,
  );
}

beforeEach(() => {
  sessionStorage.clear();
});

describe("the number and date format (FR-LOC-002)", () => {
  it("offers nothing the product does not support", () => {
    renderWizard();
    const field = screen.getByTestId("onboarding-locale");

    const offered =
      field.tagName === "SELECT"
        ? [...field.querySelectorAll("option")].map((option) => option.value)
        : [field.dataset.locale];

    expect(offered).toEqual([...FORMATTING_LOCALES]);
  });

  it("states the one supported format rather than asking a question with one answer", () => {
    // FR-UX-006. A `<select>` holding a single option is a control that
    // cannot do anything; it becomes one again the moment there are two.
    renderWizard();

    const field = screen.getByTestId("onboarding-locale");
    expect(FORMATTING_LOCALES).toHaveLength(1);
    expect(field.tagName).not.toBe("SELECT");
    expect(field.dataset.locale).toBe("nl-NL");
  });

  it("recovers a draft saved with a locale that is no longer offered", () => {
    // A draft outlives a release: this one was saved while the screen still
    // offered `en-GB`. Restoring it as-is renders a catalogue key that no
    // longer exists, and both i18n runtimes RAISE on a missing key rather
    // than falling back (FR-LOC-001) — so the wizard would not render at all.
    sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ step: 0, legalName: "Datapal BV", formattingLocale: "en-GB" }),
    );

    renderWizard();

    expect(screen.getByTestId("onboarding-locale").dataset.locale).toBe("nl-NL");
    // …and the rest of the draft survives: the fallback replaces one field,
    // not the work someone had already done.
    expect(screen.getByTestId("onboarding-legal-name")).toHaveProperty("value", "Datapal BV");
  });
});
