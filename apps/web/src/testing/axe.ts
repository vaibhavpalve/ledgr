import axe, { type RunOptions } from "axe-core";

/**
 * CMP-012/FR-LOC-004: WCAG 2.2 AA, verified by automated testing in CI.
 *
 * axe-core rather than a Jest-authored wrapper (jest-axe and friends): this
 * project runs on Vitest, and axe-core is the engine either kind of wrapper
 * would call anyway. Calling it directly means one dependency instead of two,
 * and no Jest-typings friction in a project with none.
 *
 * --- Only `violations`, never `incomplete` ---
 *
 * `axe.run()` also returns `incomplete` — checks it could not fully resolve
 * and needs a human to judge. Under jsdom that is not a corner case: jsdom
 * does not lay out or paint, so axe's colour-contrast check (and a few
 * others that read computed geometry) cannot run and report `incomplete`
 * for every element they would otherwise check, `disabled: false` or not.
 * Asserting on `incomplete` here would make every test in this file fail on
 * a check this environment cannot perform, for a reason that has nothing to
 * do with whether the component is accessible — exactly the false-failure
 * this function exists to avoid. `violations` is what axe is CERTAIN about,
 * which is what a CI gate needs: contrast itself is verified by the
 * calculations in docs/decisions/ADR-053-accessibility-audit.md instead,
 * against the actual colour values these stylesheets use.
 *
 * --- What this still catches, reliably, under jsdom ---
 *
 * Everything structural: missing accessible names, invalid ARIA
 * role/attribute combinations, unlabelled form fields, missing alt text,
 * duplicate ids, heading order, list structure — the class of bug this
 * audit actually found (ClientSwitcher's dialog with no `aria-modal`,
 * TemplateDesigner's `.visually-hidden` text that had no CSS to hide it).
 * None of that needs layout; it is present in the DOM/ARIA tree exactly as a
 * browser would build it, and jsdom builds that part correctly.
 */
export async function axeViolations(
  container: Element | Document,
  options?: RunOptions,
): Promise<axe.Result[]> {
  const results = await axe.run(container, {
    // Not disabled by name (a rule turned off silently is a rule nobody
    // notices went missing) — excluded because it cannot produce a
    // trustworthy answer here, per this module's own docstring, and the
    // exclusion is this one line rather than a config buried elsewhere.
    rules: { "color-contrast": { enabled: false } },
    ...options,
  });
  return results.violations;
}

/**
 * One assertion, with a message that names the actual rule and the actual
 * element — `expect(violations).toEqual([])` alone would leave a failing
 * test printing an opaque array of axe's own nested objects.
 */
export function assertNoAxeViolations(violations: readonly axe.Result[]): void {
  if (violations.length === 0) return;
  const detail = violations
    .map((violation) => {
      const targets = violation.nodes.map((node) => node.target.join(" ")).join(", ");
      return `  [${violation.id}] ${violation.help} (${violation.helpUrl})\n    ${targets}`;
    })
    .join("\n");
  throw new Error(`axe-core found ${violations.length} accessibility violation(s):\n${detail}`);
}
