import { render as renderBare, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { I18nProvider, type Language } from "@ledgr/i18n";
import type { ClientBadge } from "@ledgr/shared-types";

import { ClientHeader } from "./ClientHeader";

/**
 * Every string here comes from the catalogue (FR-LOC-001), so it needs a
 * language to render in. `render` is shadowed rather than each call site
 * being changed — see `ClientSwitcher.test.tsx`'s identical helper, which
 * this file's fixtures were originally alongside before being split into
 * their own file specifically for `ClientHeader`.
 */
function render(ui: ReactElement, language: Language = "nl") {
  return renderBare(<I18nProvider initialLanguage={language}>{ui}</I18nProvider>);
}

const badge = (over: Partial<ClientBadge> & { administrationId: string }): ClientBadge => ({
  displayName: "Bakker IT",
  legalName: "Bakker Consultancy B.V.",
  tradeName: "Bakker IT",
  kvkNumber: "12345678",
  colour: "indigo",
  initials: "BI",
  colourIsAmbiguous: false,
  ...over,
});

const BAKKER = badge({ administrationId: "a" });
const DE_VRIES = badge({
  administrationId: "b",
  displayName: "De Vries Holding",
  initials: "DV",
  colour: "amber",
});

describe("FR-FRM-000a: the active client is unmistakable", () => {
  it("shows the name, not only a colour", () => {
    render(<ClientHeader badge={BAKKER} />);

    expect(screen.getByTestId("client-name").textContent).toBe("Bakker IT");
  });

  it("shows initials in the marker, so colour is never the only signal", () => {
    // Survives a colour-vision deficiency, a monochrome rendering, and a
    // viewport too narrow for the name.
    render(<ClientHeader badge={BAKKER} />);

    expect(screen.getByTestId("client-marker").textContent).toBe("BI");
  });

  it("carries the colour as data rather than a hard-coded style", () => {
    render(<ClientHeader badge={DE_VRIES} />);

    expect(screen.getByTestId("client-header").dataset.colour).toBe("amber");
  });

  it("shows the legal name alongside the trade name when they differ", () => {
    render(<ClientHeader badge={BAKKER} />);

    expect(screen.getByTestId("client-legal-name").textContent).toBe("Bakker Consultancy B.V.");
  });

  it("does not repeat the name when there is no separate trade name", () => {
    render(
      <ClientHeader
        badge={badge({
          administrationId: "d",
          displayName: "De Vries Holding B.V.",
          legalName: "De Vries Holding B.V.",
          tradeName: null,
        })}
      />,
    );

    expect(screen.queryByTestId("client-legal-name")).toBeNull();
  });

  it("warns when another client shares the colour", () => {
    // Ten colours, potentially hundreds of clients. Saying so is better than
    // letting someone rely on a signal that is not distinguishing for them.
    render(<ClientHeader badge={badge({ administrationId: "a", colourIsAmbiguous: true })} />);

    expect(screen.getByTestId("client-colour-ambiguous")).toBeTruthy();
  });

  it("stays quiet when the colour is distinguishing", () => {
    render(<ClientHeader badge={BAKKER} />);

    expect(screen.queryByTestId("client-colour-ambiguous")).toBeNull();
  });

  it("says no client is selected rather than rendering an empty header", () => {
    // A blank space is indistinguishable from a header that failed to load,
    // and the one thing this must never do is let someone assume a client is
    // open when none is.
    render(<ClientHeader badge={null} />, "en");

    expect(screen.getByTestId("client-header").textContent).toContain("No client selected");
    expect(screen.getByTestId("client-header").dataset.state).toBe("none");
    expect(screen.queryByTestId("client-marker")).toBeNull();
  });

  it("hides the marker from screen readers, since the name is right beside it", () => {
    render(<ClientHeader badge={BAKKER} />);

    expect(screen.getByTestId("client-marker").getAttribute("aria-hidden")).toBe("true");
  });
});

describe("FR-FRM-000a: loading is a third state, never rendered as 'no client selected'", () => {
  it("shows a neutral loading state for `undefined`, not the empty-header text", () => {
    render(<ClientHeader badge={undefined} />, "en");

    expect(screen.getByTestId("client-header").dataset.state).toBe("loading");
    expect(screen.getByTestId("client-header").textContent).not.toContain("No client selected");
    expect(screen.getByTestId("client-header").textContent).toContain("Loading client");
  });

  it("only a confirmed `null` renders 'no client selected'", () => {
    render(<ClientHeader badge={null} />, "en");

    expect(screen.getByTestId("client-header").dataset.state).toBe("none");
    expect(screen.getByTestId("client-header").textContent).toContain("No client selected");
  });

  it("a resolved badge renders 'active', not 'loading' or 'none'", () => {
    render(<ClientHeader badge={BAKKER} />);

    expect(screen.getByTestId("client-header").dataset.state).toBe("active");
  });
});
