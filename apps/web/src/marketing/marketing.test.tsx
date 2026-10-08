import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { axeViolations } from "../testing/axe";
import { renderApp } from "../testing/renderApp";
import { ACCOUNTANTS, SECURITY } from "./content/accountants";
import { ARTICLES, ARTICLES_PAGE } from "./content/articles";
import { COMMON, MODULES } from "./content/common";
import { DEMO, LEGAL } from "./content/contact";
import { HOME } from "./content/home";
import { MOCK } from "./content/mockups";
import { COMPARE, PLANS, PRICING } from "./content/pricing";
import { PRODUCT } from "./content/product";

/**
 * The public site (ADR-107): reachable without signing in, both languages complete, every page
 * and article rendering, and the sitemap naming all of them.
 */

// The site is a lazy chunk (ADR-107): the first render waits for it to load, which under a busy
// machine takes longer than the default 1s. Generous, and still instant when nothing is wrong.
configure({ asyncUtilTimeout: 5000 });

// jsdom implements no scrolling; the layout scrolls on every navigation.
beforeEach(() => {
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

const PUBLIC_ROUTES = [
  "/",
  "/product",
  "/accountants",
  "/pricing",
  "/security",
  "/demo",
  "/articles",
  "/privacy",
  "/cookies",
  "/responsible-disclosure",
];

describe("the public site's door", () => {
  it("shows the landing page at / to a visitor who has never signed in", async () => {
    renderApp({ language: "en" }, { route: "/" });
    expect((await screen.findByTestId("mk-home-title")).textContent).toBe(
      "From receipt to return, automatically.",
    );
    expect(screen.queryByTestId("login-form")).toBeNull();
  });

  it("still sends an anonymous visitor from an app URL to the sign-in screen", async () => {
    renderApp({ language: "en" }, { route: "/ledger" });
    await waitFor(() => expect(screen.getByTestId("login-form")).toBeDefined());
    expect(screen.queryByTestId("marketing-site")).toBeNull();
  });

  it("wires Start free to sign-up and Log in to sign-in", async () => {
    renderApp({ language: "en" }, { route: "/pricing" });
    const header = await screen.findByTestId("marketing-site");
    expect(within(header).getByTestId("mk-start").getAttribute("href")).toBe("/signup");
    expect(
      within(header)
        .getAllByText("Log in")
        .some((link) => link.getAttribute("href") === "/login"),
    ).toBe(true);
  });

  it.each(PUBLIC_ROUTES)("renders %s inside the site frame", async (route) => {
    renderApp({ language: "en" }, { route });
    expect(await screen.findByTestId("marketing-site")).toBeDefined();
    expect(document.title).toContain("Boeklite");
  });
});

describe("the pages", () => {
  it("switches every page's copy to Dutch from the header", async () => {
    renderApp({ language: "en" }, { route: "/" });
    await screen.findByTestId("mk-home-title");
    fireEvent.click(screen.getByTestId("mk-language-nl"));
    expect(screen.getByTestId("mk-home-title").textContent).toBe(
      "Van bon tot aangifte, automatisch.",
    );
    expect(document.title).toBe(HOME.seo.title.nl);
  });

  it("switches the audience cards with the tabs", async () => {
    renderApp({ language: "en" }, { route: "/" });
    await screen.findByTestId("mk-home-title");
    expect(screen.getByText("Capture in seconds")).toBeDefined();
    fireEvent.click(screen.getByTestId("mk-audience-accountants"));
    expect(screen.getByText("All clients, one portfolio")).toBeDefined();
    expect(screen.queryByText("Capture in seconds")).toBeNull();
  });

  it("shows the yearly price when billing is switched to yearly", async () => {
    renderApp({ language: "en" }, { route: "/pricing" });
    expect((await screen.findByTestId("price-start")).textContent).toBe("€12");
    fireEvent.click(screen.getByTestId("mk-billing"));
    expect(screen.getByTestId("price-start").textContent).toBe("€10");
    expect(screen.getByText("Billed yearly at €120")).toBeDefined();
  });

  it("refuses an incomplete demo request and opens a filled-in email otherwise", async () => {
    const assigned: string[] = [];
    const original = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...original,
        set href(value: string) {
          assigned.push(value);
        },
      },
    });
    try {
      renderApp({ language: "en" }, { route: "/demo" });
      fireEvent.click(await screen.findByTestId("mk-demo-submit"));
      expect(screen.getByTestId("mk-demo-error").textContent).toBe(DEMO.form.required.en);

      fireEvent.change(screen.getByTestId("mk-demo-name"), { target: { value: "Anna" } });
      fireEvent.change(screen.getByTestId("mk-demo-company"), {
        target: { value: "Bakkerij Vos" },
      });
      fireEvent.change(screen.getByTestId("mk-demo-email"), { target: { value: "anna@vos.nl" } });
      fireEvent.click(screen.getByTestId("mk-demo-submit"));

      expect(assigned).toHaveLength(1);
      expect(assigned[0]).toMatch(
        /^mailto:hello@boeklite\.nl\?subject=Demo%20request%3A%20Bakkerij%20Vos/,
      );
      expect(screen.getByTestId("mk-demo-sent")).toBeDefined();
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: original });
    }
  });

  it.each(ARTICLES.map((article) => article.slug))(
    "renders the article %s in both languages",
    async (slug) => {
      const article = ARTICLES.find((candidate) => candidate.slug === slug)!;
      renderApp({ language: "nl" }, { route: `/articles/${slug}` });
      expect((await screen.findByTestId("mk-article-title")).textContent).toBe(article.title.nl);
      fireEvent.click(screen.getByTestId("mk-language-en"));
      expect(screen.getByTestId("mk-article-title").textContent).toBe(article.title.en);
      expect(screen.getByTestId("mk-article-body").textContent).toContain(
        ARTICLES_PAGE.disclaimer.en,
      );
    },
  );

  it("filters the article list by topic", async () => {
    renderApp({ language: "en" }, { route: "/articles" });
    const list = await screen.findByTestId("mk-article-list");
    expect(within(list).getAllByRole("link")).toHaveLength(ARTICLES.length);
    fireEvent.click(screen.getByTestId("mk-filter-btw"));
    const btw = ARTICLES.filter((article) => article.category === "btw").length;
    expect(within(screen.getByTestId("mk-article-list")).getAllByRole("link")).toHaveLength(btw);
  });

  it("shows a not-found page for an article that does not exist", async () => {
    renderApp({ language: "en" }, { route: "/articles/no-such-article" });
    expect(await screen.findByText(COMMON.notFoundTitle.en)).toBeDefined();
  });

  it("has no automated WCAG violations on the home page (CMP-012)", async () => {
    const { container } = renderApp({ language: "en" }, { route: "/" });
    await screen.findByTestId("mk-home-title");
    expect(await axeViolations(container)).toEqual([]);
  });
});

describe("the content", () => {
  /** Every bilingual string in a content tree: both halves present and non-empty. */
  function collect(node: unknown, path: string, out: string[]) {
    if (node === null || typeof node !== "object") return;
    const record = node as Record<string, unknown>;
    if (typeof record.en === "string" && typeof record.nl === "string") {
      if (!record.en.trim() || !record.nl.trim()) out.push(path);
      return;
    }
    for (const [key, value] of Object.entries(record)) collect(value, `${path}.${key}`, out);
  }

  it("has an English and a Dutch half for every string", () => {
    const empty: string[] = [];
    const trees = {
      HOME,
      PRODUCT,
      ACCOUNTANTS,
      SECURITY,
      PRICING,
      PLANS,
      COMPARE,
      DEMO,
      LEGAL,
      ARTICLES,
      ARTICLES_PAGE,
      COMMON,
      MODULES,
      MOCK,
    };
    for (const [name, tree] of Object.entries(trees)) collect(tree, name, empty);
    // A table's empty cell is intentionally blank in both languages; nothing else may be.
    expect(empty.filter((path) => !path.includes(".rows."))).toEqual([]);
  });

  it("lists every public page and every article in the sitemap", () => {
    const sitemap = readFileSync(resolve(__dirname, "../../public/sitemap.xml"), "utf-8");
    for (const route of PUBLIC_ROUTES) {
      expect(sitemap).toContain(`<loc>https://boeklite.nl${route}</loc>`);
    }
    for (const article of ARTICLES) {
      expect(sitemap).toContain(`<loc>https://boeklite.nl/articles/${article.slug}</loc>`);
    }
  });

  it("gives every article a unique slug and an ISO publication date", () => {
    const slugs = ARTICLES.map((article) => article.slug);
    expect(new Set(slugs).size).toBe(slugs.length);
    for (const article of ARTICLES) expect(article.published).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });
});
