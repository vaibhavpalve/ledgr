import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { I18nProvider } from "@ledgr/i18n";

import { axeViolations } from "../testing/axe";
import { COPY } from "./copy";
import { Website } from "./Website";

function renderSite() {
  return render(
    <I18nProvider initialLanguage="en">
      <MemoryRouter initialEntries={["/welcome"]}>
        <Website />
      </MemoryRouter>
    </I18nProvider>,
  );
}

describe("the marketing page (ADR-080)", () => {
  it("uses the reference copy and links its calls to action into the app", () => {
    renderSite();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toContain(COPY.hero.mark);
    const ctas = screen.getAllByRole("link", { name: COPY.cta });
    expect(ctas.length).toBe(3);
    for (const link of ctas) expect(link.getAttribute("href")).toBe("/signup");
  });

  it("carries no prices, testimonials or customer logos", () => {
    const { container } = renderSite();
    expect(container.textContent).not.toMatch(/€\s?\d+\s?\/|per month|testimonial|trusted by/i);
  });

  it("has no automated WCAG violations (CMP-012)", async () => {
    const { container } = renderSite();
    expect(await axeViolations(container)).toEqual([]);
  });
});
