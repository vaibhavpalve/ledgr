import { createHmac } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { expect, test, type Locator, type Page } from "@playwright/test";

/**
 * The accountant's firm home (`/todo`, docs/firm-home/contract.md) in a real browser, on a desktop
 * and a phone project: sign in, the count strip, the chips, J/K/Enter, snooze, assign, the review
 * sheet with one group approved, the right column, and the inbox. Every screen is photographed and
 * checked for sideways scrolling, as golden-path.spec.ts does.
 *
 * The firm is seeded over HTTP beforehand (a firm, five Model A clients, bank lines imported from
 * CSV, receipts, question threads, one snoozed client). The seed writes the accountant's e-mail,
 * password and TOTP secret to the JSON file `E2E_FIRM_SEED` names; without it this spec is skipped.
 * Each run works the list (snoozes, assigns, approves), so re-seed before each project:
 *     (seed) ; playwright test e2e/firm-home.spec.ts --project desktop
 *     (seed) ; playwright test e2e/firm-home.spec.ts --project phone
 */

interface Seed {
  email: string;
  password: string;
  secret: string;
  clients: Record<string, string>;
}

const SEED_PATH = process.env.E2E_FIRM_SEED;
const seed: Seed | null =
  SEED_PATH !== undefined && existsSync(SEED_PATH)
    ? (JSON.parse(readFileSync(SEED_PATH, "utf8")) as Seed)
    : null;

/** RFC 6238, as golden-path.spec.ts computes it. */
function totp(secret: string, step: number): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const char of secret.replace(/=+$/, "").toUpperCase()) {
    bits += alphabet.indexOf(char).toString(2).padStart(5, "0");
  }
  const bytes: number[] = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(step));
  const digest = createHmac("sha1", Buffer.from(bytes)).update(counter).digest();
  const offset = digest[digest.length - 1]! & 0x0f;
  const code = (digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000;
  return code.toString().padStart(6, "0");
}

/**
 * A code from a time step no earlier sign-in used: the server refuses a replayed TOTP code, and
 * the seed and the two projects can all land in the same 30-second window.
 */
async function freshCode(page: Page, secret: string): Promise<string> {
  const marker = "e2e/.results/firm-home-totp-step";
  mkdirSync("e2e/.results", { recursive: true });
  const used = existsSync(marker) ? Number(readFileSync(marker, "utf8")) : 0;
  // Always a step that starts after this call: the seed (or a previous sign-in this test cannot
  // see) may have spent the current one.
  const floor = Math.max(used, Math.floor(Date.now() / 30_000));
  let step = floor;
  while (step <= floor) {
    await page.waitForTimeout(1_000);
    step = Math.floor(Date.now() / 30_000);
  }
  writeFileSync(marker, String(step));
  return totp(secret, step);
}

async function shot(page: Page, name: string): Promise<void> {
  const dir = `e2e/screenshots/${test.info().project.name}`;
  mkdirSync(dir, { recursive: true });
  await page.screenshot({ path: `${dir}/firm-${name}.png`, fullPage: true });
  const overflow = await page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    if (document.documentElement.scrollWidth <= width + 1) return [];
    const sticksOut = (element: Element | null) =>
      element !== null && element.getBoundingClientRect().right > width + 1;
    return Array.from(document.querySelectorAll("body *"))
      .filter((element) => sticksOut(element) && !sticksOut(element.parentElement))
      .slice(0, 6)
      .map((element) => {
        const box = element.getBoundingClientRect();
        const classes = Array.from(element.classList).join(".");
        return `${element.tagName.toLowerCase()}${classes ? "." + classes : ""} right=${Math.round(box.right)}`;
      });
  });
  expect(overflow, `${name}: nothing scrolls sideways`).toEqual([]);
}

/** Every clickable control inside `scope` lies within the viewport horizontally (no clipped buttons). */
async function noClippedButtons(page: Page, scope: Locator, name: string): Promise<void> {
  const clipped = await scope.evaluate((root) => {
    const width = document.documentElement.clientWidth;
    return Array.from(root.querySelectorAll("button, a, input, select"))
      .filter((element) => {
        const box = element.getBoundingClientRect();
        return box.width > 0 && (box.left < -1 || box.right > width + 1);
      })
      .map((element) => element.getAttribute("data-testid") ?? element.textContent?.trim() ?? "?");
  });
  expect(clipped, `${name}: no control is clipped`).toEqual([]);
}

async function signIn(page: Page, s: Seed): Promise<void> {
  await page.goto("/login");
  await expect(page.getByTestId("login-form")).toBeVisible();
  await page.getByTestId("login-email").fill(s.email);
  await page.getByTestId("login-password").fill(s.password);
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("mfa-totp-code").first()).toBeVisible();
  await page
    .getByTestId("mfa-totp-code")
    .first()
    .fill(await freshCode(page, s.secret));
  await page.getByTestId("mfa-totp-verify").click();
}

/** The worklist's rows, as table rows (desktop) or cards (phone). */
function rows(page: Page, narrow: boolean): Locator {
  return narrow
    ? page.locator('[data-testid^="firm-card-"]')
    : page.locator('[data-testid^="firm-row-"]').filter({ has: page.locator("td") });
}

/** Click a chip and wait until the list shows that chip's answer. */
async function chip(page: Page, value: string): Promise<void> {
  await page.getByTestId(`firm-chip-${value}`).click();
  await expect(page.getByTestId(`firm-chip-${value}`)).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".firm-worklist__body")).toHaveAttribute("aria-busy", "false");
}

/** Switch "Mine"/"Everyone" and wait for the answer. */
async function assigned(page: Page, value: "me" | "any"): Promise<void> {
  await page.getByTestId(`firm-assigned-${value}`).click();
  await expect(page.getByTestId(`firm-assigned-${value}`)).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".firm-worklist__body")).toHaveAttribute("aria-busy", "false");
}

async function idOf(row: Locator): Promise<string> {
  const testId = (await row.getAttribute("data-testid")) ?? "";
  return testId.replace(/^firm-(row|card)-/, "");
}

test("an accountant works the week from the firm home", async ({ page }) => {
  test.skip(seed === null, "E2E_FIRM_SEED names no seed file (see this spec's header)");
  const s = seed!;
  const narrow = test.info().project.name === "phone";

  // --- Sign in: no client open, so the firm home is where an accountant lands -------------
  await signIn(page, s);
  await expect(page).toHaveURL(/\/todo$/);
  await expect(page.getByTestId("firm-home")).toBeVisible();

  // --- The count strip -------------------------------------------------------------------
  const counts = page.getByTestId("firm-counts");
  await expect(counts).toBeVisible();
  for (const key of [
    "auto_bookings",
    "receipts_to_book",
    "missing_receipts",
    "bank_to_match",
    "open_questions",
    "broken_feeds",
  ]) {
    await expect(page.getByTestId(`firm-count-${key}`)).toContainText(/\d/);
  }
  await expect(page.getByTestId("firm-count-missing_receipts")).not.toContainText(/^\s*0/);
  await shot(page, "01-landing");

  // Nothing is assigned to anyone yet, so "Mine" is empty: look at everyone's clients.
  await assigned(page, "any");
  await expect(rows(page, narrow).first()).toBeVisible();
  if (narrow) {
    await expect(page.getByTestId("firm-worklist-cards")).toBeVisible();
    await expect(page.getByTestId("firm-worklist-table")).toHaveCount(0);
  } else {
    await expect(page.getByTestId("firm-worklist-table")).toBeVisible();
  }
  await shot(page, "02-worklist-any");

  // --- Chips -----------------------------------------------------------------------------
  await chip(page, "snoozed");
  const groen = rows(page, narrow).filter({ hasText: "Studio Groen" });
  await expect(groen).toHaveCount(1);
  await expect(groen).toContainText(/22-10-2026/);
  await expect(groen).toContainText(/vakantie/i);
  await shot(page, "03-chip-snoozed");
  await chip(page, "waiting_on_client");
  await shot(page, "04-chip-waiting");
  await chip(page, "my_move");
  await expect(rows(page, narrow).first()).toBeVisible();
  const firstRows = rows(page, narrow);

  // Words, not colour alone: every due cell and "behind" note carries text.
  for (const cell of await page.getByTestId("firm-row-due").all()) {
    await expect(cell).toHaveText(/\S/);
  }
  for (const cell of await page.getByTestId("firm-row-behind").all()) {
    await expect(cell).toHaveText(/\d/);
  }
  await shot(page, "05-chip-my-move");

  // --- J / K / Enter (a keyboard is a desktop affair) -------------------------------------
  if (!narrow) {
    expect(await firstRows.count()).toBeGreaterThan(1);
    const first = await idOf(firstRows.nth(0));
    const second = await idOf(firstRows.nth(1));
    await page.locator("body").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press("j");
    await expect(page.getByTestId(`firm-open-${first}`)).toBeFocused();
    await page.keyboard.press("j");
    await expect(page.getByTestId(`firm-open-${second}`)).toBeFocused();
    await page.keyboard.press("k");
    await expect(page.getByTestId(`firm-open-${first}`)).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByTestId("app-shell")).toBeVisible();
    await page.goto("/todo");
    await expect(page.getByTestId("firm-home")).toBeVisible();
    await assigned(page, "any");
  }

  // --- Snooze one client ------------------------------------------------------------------
  await chip(page, "my_move");
  const target = rows(page, narrow).first();
  const snoozedId = await idOf(target);
  await page.getByTestId(`firm-row-menu-${snoozedId}`).click();
  await page.getByTestId(`firm-row-snooze-${snoozedId}`).click();
  await expect(page.getByTestId("firm-snooze-dialog")).toBeVisible();
  const until = new Date(Date.now() + 10 * 86_400_000).toISOString().slice(0, 10);
  await page.getByTestId("firm-snooze-until").fill(until);
  await page.getByTestId("firm-snooze-reason").fill("Wacht op jaarstukken");
  await shot(page, "07-snooze-dialog");
  await noClippedButtons(page, page.getByTestId("firm-snooze-dialog"), "snooze dialog");
  await page.getByTestId("firm-snooze-submit").click();
  await expect(page.getByTestId("firm-snooze-dialog")).toHaveCount(0);
  await chip(page, "snoozed");
  await expect(
    page.getByTestId(narrow ? `firm-card-${snoozedId}` : `firm-row-${snoozedId}`),
  ).toContainText(/jaarstukken/);
  await shot(page, "08-snoozed");

  // --- Assign one client to me -------------------------------------------------------------
  await chip(page, "my_move");
  const toAssign = await idOf(rows(page, narrow).first());
  await page.getByTestId(`firm-row-menu-${toAssign}`).click();
  await page.getByTestId(`firm-row-assign-${toAssign}`).click();
  await expect(page.getByTestId("firm-assign-dialog")).toBeVisible();
  const picker = page.getByTestId("firm-assign-user");
  await expect(picker).toBeEnabled();
  const me = await picker
    .locator("option", { hasText: s.email.split("@")[0]! })
    .getAttribute("value");
  await picker.selectOption(me ?? "");
  await shot(page, "09-assign-dialog");
  await noClippedButtons(page, page.getByTestId("firm-assign-dialog"), "assign dialog");
  await page.getByTestId("firm-assign-submit").click();
  // A clean assignment closes the dialog itself; one with failures stays open to list them.
  await expect(page.getByTestId("firm-assign-dialog")).toHaveCount(0);
  await assigned(page, "me");
  await expect(
    page.getByTestId(narrow ? `firm-card-${toAssign}` : `firm-row-${toAssign}`),
  ).toBeVisible();
  await shot(page, "10-assigned-to-me");
  await assigned(page, "any");

  // --- The review sheet: approve one whole group --------------------------------------------
  const reviewButton = page.getByTestId("firm-review-open");
  if ((await reviewButton.count()) > 0) {
    await reviewButton.click();
    const sheet = page.getByTestId("firm-review-sheet");
    await expect(sheet).toBeVisible();
    const group = sheet.locator('[data-testid^="firm-review-group-"]').first();
    await expect(group).toBeVisible();
    const key = ((await group.getAttribute("data-testid")) ?? "").replace("firm-review-group-", "");
    await page.getByTestId(`firm-review-toggle-${key}`).click();
    await expect(group.locator('[data-testid^="firm-review-item-"]').first()).toBeVisible();
    await shot(page, "11-review-sheet");
    await noClippedButtons(page, sheet, "review sheet");
    await page.getByTestId(`firm-review-approve-all-${key}`).click();
    await expect(page.getByTestId("firm-review-result")).toBeVisible();
    await expect(page.getByTestId("firm-review-failures")).toHaveCount(0);
    await expect(page.getByTestId(`firm-review-group-${key}`)).toHaveCount(0);
    await shot(page, "12-review-approved");
    await page.getByTestId("firm-dialog-close").click();
  }

  // --- The right column ----------------------------------------------------------------------
  await expect(page.locator('[data-testid^="firm-deadline-"]').first()).toBeVisible();
  await expect(page.getByTestId("firm-replies-inbox")).toBeVisible();
  const side = page.locator(".firm-home__side");
  const main = page.locator(".firm-home__main");
  const sideBox = (await side.boundingBox())!;
  const mainBox = (await main.boundingBox())!;
  if (narrow) {
    expect(sideBox.y, "phone: the right column sits under the list").toBeGreaterThanOrEqual(
      mainBox.y + mainBox.height - 1,
    );
  } else {
    expect(sideBox.x, "1440px: the right column sits beside the list").toBeGreaterThan(
      mainBox.x + mainBox.width - 1,
    );
  }
  await side.scrollIntoViewIfNeeded();
  await shot(page, "13-right-column");

  if (!narrow) {
    // Under ~1100px of content the right column drops below the table.
    for (const width of [1280, 1100, 1024]) {
      await page.setViewportSize({ width, height: 900 });
      await page.waitForTimeout(200);
      const s2 = (await side.boundingBox())!;
      const m2 = (await main.boundingBox())!;
      const below = s2.y >= m2.y + m2.height - 1;
      test.info().annotations.push({
        type: "layout",
        description: `${width}px: right column ${below ? "below" : "beside"} the list`,
      });
      if (width <= 1100) expect(below, `${width}px: right column drops below`).toBe(true);
      await shot(page, `14-layout-${width}`);
    }
    await page.setViewportSize({ width: 1440, height: 900 });
  }

  // --- The inbox -------------------------------------------------------------------------------
  await page.getByTestId("firm-replies-inbox").click();
  await expect(page).toHaveURL(/\/inbox$/);
  await expect(page.getByTestId("firm-inbox")).toBeVisible();
  await expect(page.locator('[data-testid^="firm-inbox-open-"]').first()).toBeVisible();
  await shot(page, "15-inbox");
  await noClippedButtons(page, page.getByTestId("firm-inbox"), "inbox");
});
