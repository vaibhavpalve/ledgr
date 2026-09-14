import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { CLIENT } from "./primitives";
import { CSS_PREFIX, cssVar, darkTheme, lightTheme, scaleTokens, themeValue } from "./tokens";

/**
 * `tokens.css` and `tokens.ts` are two representations of the same facts, so
 * this suite parses the stylesheet and asserts they agree — token for token,
 * in both themes.
 *
 * The same shape as `shared-types`' `index.test.ts`, which parses migration
 * 0018 rather than trusting a comment claiming the TypeScript matches the
 * database. A design system whose typed mirror has drifted from the CSS is
 * worse than no mirror at all, because callers believe it.
 */

/**
 * Comments are stripped before anything is located, because tokens.css
 * DOCUMENTS its own selectors in prose — the header explains the three theme
 * states by naming `:root[data-theme="dark"]`. Searching the raw text finds
 * that sentence before the rule it describes, and every assertion then reads
 * the light block while believing it is the dark one. (It did, at first: this
 * suite failed 98 times against a stylesheet that was entirely correct.)
 */
const CSS = readFileSync(fileURLToPath(new URL("../tokens.css", import.meta.url)), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

/**
 * Values are compared after collapsing whitespace, unifying quote style and
 * lowercasing. Prettier owns the stylesheet's formatting — it wraps the long
 * shadow declarations and rewrites `'Source Sans 3'` to double quotes — and
 * `#FBF8F4` and `#fbf8f4` are the same colour. None of that is worth failing
 * on. A different colour, or a different font, is.
 */
function normalise(value: string): string {
  return value.trim().replace(/\s+/g, " ").replace(/'/g, '"').toLowerCase();
}

/**
 * Every `--ledgr-*: value;` declaration inside one balanced `{ … }` block,
 * found by scanning from `selector` and counting braces — so the nested block
 * inside `@media` is handled without a CSS parser.
 */
function declarationsAfter(selector: string): Map<string, string> {
  const start = CSS.indexOf(selector);
  if (start === -1) throw new Error(`selector not found in tokens.css: ${selector}`);

  // Scanned from `start`, NOT from the end of the selector: callers pass
  // `":root {"` (brace included) to tell the bare root apart from
  // `:root[data-theme=…]`, and skipping the selector's own length would step
  // over that very brace and lock onto the next rule's block instead.
  const open = CSS.indexOf("{", start);
  if (open === -1) throw new Error(`no block after: ${selector}`);

  let depth = 0;
  let end = -1;
  for (let i = open; i < CSS.length; i += 1) {
    if (CSS[i] === "{") depth += 1;
    if (CSS[i] === "}") {
      depth -= 1;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }
  if (end === -1) throw new Error(`unbalanced block after: ${selector}`);

  const body = CSS.slice(open + 1, end);
  const found = new Map<string, string>();
  for (const match of body.matchAll(/--([\w-]+)\s*:\s*([^;]+);/g)) {
    found.set(match[1]!, normalise(match[2]!));
  }
  return found;
}

/** The bare `:root {` — light, and where every token must be declared. */
const rootBlock = declarationsAfter(":root {");
/** The explicit opt-in. */
const stampedDarkBlock = declarationsAfter(':root[data-theme="dark"]');
/** The OS-preference branch, nested inside its @media. */
const mediaDarkBlock = declarationsAfter(':root:not([data-theme="light"])');

describe("the light theme", () => {
  it.each(Object.entries(lightTheme))("declares --ledgr-%s on the bare :root", (name, value) => {
    expect(rootBlock.get(`${CSS_PREFIX}-${name}`)).toBe(normalise(value));
  });
});

describe("the dark theme", () => {
  // Both dark blocks, because a token redefined in only one of them means a
  // user who chose dark on a light OS sees a different product from one whose
  // OS chose it — the bug the two blocks exist to prevent.
  it.each(Object.entries(darkTheme))(
    "redefines --ledgr-%s under prefers-color-scheme: dark",
    (name, value) => {
      expect(mediaDarkBlock.get(`${CSS_PREFIX}-${name}`)).toBe(normalise(value));
    },
  );

  it.each(Object.entries(darkTheme))(
    "redefines --ledgr-%s under an explicit [data-theme=dark]",
    (name, value) => {
      expect(stampedDarkBlock.get(`${CSS_PREFIX}-${name}`)).toBe(normalise(value));
    },
  );

  it("defines exactly the same tokens in both dark blocks", () => {
    expect([...stampedDarkBlock.keys()].sort()).toEqual([...mediaDarkBlock.keys()].sort());
  });

  it("gives both dark blocks identical values", () => {
    for (const [name, value] of stampedDarkBlock) {
      expect(mediaDarkBlock.get(name), `--${name} differs between the two dark blocks`).toBe(value);
    }
  });

  it("sets color-scheme so the browser's own widgets follow", () => {
    expect(CSS).toMatch(/:root\[data-theme="dark"\][\s\S]*?color-scheme:\s*dark/);
  });
});

describe("the theme contract", () => {
  // This is the assertion that catches the classic unreadable-artifact bug: a
  // colour whose ONLY definition sits inside a media query or a [data-theme]
  // block is undefined for the majority of viewers, who have made no explicit
  // choice and are not on a dark OS.
  it("declares every dark-theme token on the bare :root first", () => {
    for (const name of mediaDarkBlock.keys()) {
      expect(rootBlock.has(name), `--${name} is only defined inside a dark block`).toBe(true);
    }
  });

  it("gives both themes the same set of token names", () => {
    expect(Object.keys(lightTheme).sort()).toEqual(Object.keys(darkTheme).sort());
  });

  it("changes something between the themes", () => {
    // Guards against a copy-paste that leaves the dark theme a clone of the
    // light one — every assertion above would still pass.
    const changed = Object.keys(lightTheme).filter(
      (name) =>
        lightTheme[name as keyof typeof lightTheme] !== darkTheme[name as keyof typeof darkTheme],
    );
    expect(changed.length).toBeGreaterThan(10);
  });
});

describe("scale tokens", () => {
  it.each(Object.entries(scaleTokens))("declares --ledgr-%s once, on :root", (name, value) => {
    expect(rootBlock.get(`${CSS_PREFIX}-${name}`)).toBe(normalise(value));
  });

  it("never redefines a scale token in a theme block", () => {
    // Type and spacing do not change with the theme. If one did, a dark-mode
    // user would get a different layout, not a different palette.
    for (const name of Object.keys(scaleTokens)) {
      expect(stampedDarkBlock.has(`${CSS_PREFIX}-${name}`), `--${name} is theme-dependent`).toBe(
        false,
      );
    }
  });

  it("keeps the type scale to seven steps", () => {
    const steps = Object.keys(scaleTokens).filter((name) => name.startsWith("type-"));
    expect(steps).toHaveLength(7);
  });

  it("keeps spacing on a four-pixel grid", () => {
    const steps = Object.entries(scaleTokens).filter(([name]) => name.startsWith("space-"));
    expect(steps).toHaveLength(7);
    for (const [name, value] of steps) {
      const px = Number.parseFloat(value) * 16;
      expect(px % 4, `--ledgr-${name} (${value} = ${px}px) is off the 4px grid`).toBe(0);
    }
  });

  it("keeps radii to four roles", () => {
    const radii = Object.keys(scaleTokens).filter((name) => name.startsWith("radius-"));
    expect(radii.sort()).toEqual(["radius-chip", "radius-control", "radius-panel", "radius-pill"]);
  });
});

describe("client marker colours", () => {
  it.each(Object.entries(CLIENT))("declares --ledgr-client-%s", (name, value) => {
    expect(rootBlock.get(`${CSS_PREFIX}-client-${name}`)).toBe(normalise(value));
  });

  // Theme-independent by design (primitives.ts): a client's colour is that
  // client's identity, and an identity that changed at night would defeat the
  // signal FR-FRM-000a exists to carry.
  it("never redefines a client colour in either dark block", () => {
    for (const name of Object.keys(CLIENT)) {
      const token = `${CSS_PREFIX}-client-${name}`;
      expect(stampedDarkBlock.has(token), `--${token} changes with the theme`).toBe(false);
      expect(mediaDarkBlock.has(token), `--${token} changes with the theme`).toBe(false);
    }
  });
});

describe("helpers", () => {
  it("builds a var() reference", () => {
    expect(cssVar("accent")).toBe("var(--ledgr-accent)");
    expect(cssVar("space-4")).toBe("var(--ledgr-space-4)");
  });

  it("resolves a token to a real value per theme", () => {
    expect(themeValue("light", "surface-ground")).toBe(lightTheme["surface-ground"]);
    expect(themeValue("dark", "surface-ground")).toBe(darkTheme["surface-ground"]);
    expect(themeValue("light", "surface-ground")).not.toBe(themeValue("dark", "surface-ground"));
  });
});
