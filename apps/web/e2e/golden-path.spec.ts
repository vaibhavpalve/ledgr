import { createHmac } from "node:crypto";
import { mkdirSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";

/**
 * A brand-new business, from the sign-up screen to its BTW return, through the web app only.
 *
 * Every step is what a person does with a mouse and a keyboard; the one thing read around the UI
 * is the verification e-mail, from the API's development outbox, because there is no inbox. The
 * run fails at the first screen a customer would get stuck on, and leaves a screenshot of every
 * screen it passed through in e2e/screenshots/<project>/ for a person to look at.
 */

const API = process.env.E2E_API_URL ?? "http://localhost:8000";

function totp(secret: string): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const char of secret.replace(/=+$/, "").toUpperCase()) {
    bits += alphabet.indexOf(char).toString(2).padStart(5, "0");
  }
  const bytes: number[] = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30_000)));
  const digest = createHmac("sha1", Buffer.from(bytes)).update(counter).digest();
  const offset = digest[digest.length - 1]! & 0x0f;
  const code = (digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000;
  return code.toString().padStart(6, "0");
}

/**
 * Every screen is photographed, and every screen is checked for sideways scrolling - the layout
 * fault a unit test cannot see and a phone user sees first. A failure names the elements that
 * stick out, so the fix does not start with a hunt.
 */
async function shot(page: Page, name: string): Promise<void> {
  const dir = `e2e/screenshots/${test.info().project.name}`;
  mkdirSync(dir, { recursive: true });
  await page.screenshot({ path: `${dir}/${name}.png`, fullPage: true });
  const overflow = await page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    if (document.documentElement.scrollWidth <= width + 1) return [];
    const sticksOut = (element: Element | null) =>
      element !== null && element.getBoundingClientRect().right > width + 1;
    // The outermost offenders: an element that sticks out inside a parent that already does is
    // a symptom, not the cause.
    return Array.from(document.querySelectorAll("body *"))
      .filter((element) => sticksOut(element) && !sticksOut(element.parentElement))
      .slice(0, 6)
      .map((element) => {
        const box = element.getBoundingClientRect();
        const classes = Array.from(element.classList).join(".");
        const parent = element.parentElement;
        const inside = parent
          ? ` in ${parent.tagName.toLowerCase()}.${Array.from(parent.classList).join(".")}` +
            ` (width=${Math.round(parent.getBoundingClientRect().width)},` +
            ` overflow-x=${getComputedStyle(parent).overflowX})`
          : "";
        return `${element.tagName.toLowerCase()}${classes ? "." + classes : ""} right=${Math.round(box.right)}${inside}`;
      });
  });
  expect(overflow, `${name}: nothing scrolls sideways`).toEqual([]);
}

/** The nav item, from the rail on a wide screen or the avatar menu on a phone. */
async function go(page: Page, path: string): Promise<void> {
  await page.goto(path);
}

test("a new business goes from sign-up to its BTW return", async ({ page, request }) => {
  const email = `e2e-${Date.now()}@example.nl`;
  const today = new Date();
  const iso = today.toISOString().slice(0, 10);
  const year = today.getFullYear();

  // --- Sign up --------------------------------------------------------------
  await page.goto("/signup");
  await expect(page.getByTestId("signup-form")).toBeVisible();
  await shot(page, "01-signup");
  // The radio is visually hidden inside its segmented label; a person clicks the label.
  await page.getByTestId("signup-account-model-self-managed").check({ force: true });
  await page.getByTestId("signup-organization-name").fill("Bakkerij De Korenaar B.V.");
  await page.getByTestId("signup-email").fill(email);
  await page.getByTestId("signup-password").fill("correct-horse-battery-staple-42");
  await page.getByTestId("signup-submit").click();

  // --- Two-factor enrolment -------------------------------------------------
  await expect(page.getByTestId("mfa-enrollment")).toBeVisible();
  const begun = page.waitForResponse((r) => r.url().includes("/mfa/totp/enroll/begin"));
  await page.getByTestId("mfa-totp-begin").click();
  const { secret } = (await (await begun).json()) as { secret: string };
  await expect(page.getByTestId("mfa-totp-code")).toBeVisible();
  await shot(page, "02-mfa");
  await page.getByTestId("mfa-totp-code").fill(totp(secret));
  await page.getByTestId("mfa-totp-confirm").click();

  // --- Verify the e-mail address, as if the link was clicked ------------------
  await expect(page.getByTestId("onboarding")).toBeVisible();
  const outbox = (await (await request.get(`${API}/v1/dev/outbox`)).json()) as unknown[];
  const mail = JSON.stringify(outbox.filter((m) => JSON.stringify(m).includes(email)));
  const token = /token=([0-9a-f-]{36})/.exec(mail)?.[1];
  expect(token, "a verification link was sent").toBeTruthy();

  // --- Onboarding -------------------------------------------------------------
  await shot(page, "03-onboarding-company");
  await page.getByTestId("legal-form-bv").click();
  await page.getByTestId("onboarding-legal-name").fill("Bakkerij De Korenaar B.V.");
  await page.getByTestId("onboarding-kvk").fill("34281907");
  await page.getByTestId("onboarding-vat").fill("NL001234567B01");
  await page.getByTestId("onboarding-next").click();
  await expect(page.getByTestId("onboarding-fiscal-year")).toBeVisible();
  await page.getByTestId("onboarding-scheme-quarterly").click();
  await shot(page, "04-onboarding-year");
  await page.getByTestId("onboarding-next").click();
  await expect(page.getByTestId("onboarding-review")).toBeVisible();
  await shot(page, "05-onboarding-review");
  await page.getByTestId("onboarding-submit").click();

  // --- The dashboard of an empty company --------------------------------------
  await expect(page.getByTestId("app-shell")).toBeVisible();
  await expect(page.getByTestId("home-figures")).toBeVisible();
  await shot(page, "06-dashboard-empty");

  await page.goto(`/verify-email?token=${token}`);
  await expect(page.getByTestId("verify-email-done")).toBeVisible();

  // --- Settings: the address every invoice must carry --------------------------
  await go(page, "/settings/organization");
  await expect(page.getByTestId("administration-address")).toBeVisible();
  await page.getByTestId("administration-address-line1").fill("Keizersgracht 100");
  await page.getByTestId("administration-postal-code").fill("1015 AB");
  await page.getByTestId("administration-city").fill("Amsterdam");
  await page.getByTestId("administration-iban").fill("NL91ABNA0417164300");
  await page.getByTestId("administration-save").click();
  await expect(page.getByTestId("administration-save")).toBeDisabled();
  await shot(page, "07-settings-organization");

  // --- A customer -----------------------------------------------------------------
  await go(page, "/customers/new");
  await expect(page.getByTestId("customer-form")).toBeVisible();
  await page.getByTestId("customer-name").fill("Hotel De Gouden Leeuw B.V.");
  await page.getByTestId("customer-address1").fill("Damrak 1");
  await page.getByTestId("customer-postal-code").fill("1012 LG");
  await page.getByTestId("customer-city").fill("Amsterdam");
  await page.getByTestId("customer-email").fill("inkoop@goudenleeuw.example");
  await shot(page, "08-customer-form");
  await page.getByTestId("customer-save").click();
  await expect(page).toHaveURL(/\/customers\/[0-9a-f-]{36}$/);
  await shot(page, "09-customer-detail");

  // --- An invoice, issued and sent -------------------------------------------------
  await go(page, "/invoices/new");
  await expect(page.getByTestId("invoice-customer-pick")).toBeVisible();
  await page
    .getByTestId("invoice-customer-pick")
    .selectOption({ label: "Hotel De Gouden Leeuw B.V." });
  await page.getByTestId("invoice-date").fill(iso);
  await page.getByTestId("invoice-line-1-description").fill("Broodlevering september");
  await page.getByTestId("invoice-line-1-quantity").fill("10");
  await page.getByTestId("invoice-line-1-unit-price").fill("95.00");
  await page.getByTestId("invoice-line-1-vat-treatment").selectOption("btw_21");
  await shot(page, "10-invoice-new");
  await page.getByTestId("invoice-submit").click();
  await expect(page.getByTestId("invoice-sent")).toBeVisible();
  await shot(page, "11-invoice-sent");

  await go(page, "/invoices");
  await expect(page.getByTestId("invoice-table")).toBeVisible();
  await shot(page, "12-invoice-list");

  // --- A receipt, uploaded, filled in and booked -----------------------------------
  await go(page, "/purchases");
  await expect(page.getByTestId("purchases")).toBeVisible();
  await page.getByTestId("capture-file-input").setInputFiles({
    name: "bon-staples.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from(
      "%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n",
    ),
  });
  // The category is asked first, so the receipt arrives already filed.
  await expect(page.getByTestId("capture-category-picker")).toBeVisible();
  await shot(page, "13a-category-picker");
  await page.getByTestId("capture-category-office_supplies").click();
  const row = page.getByTestId("purchase-row").first();
  await expect(row).toBeVisible({ timeout: 30_000 });
  await shot(page, "13-purchases-uploaded");
  await row.locator("a").first().click();
  await expect(page.getByTestId("purchase-detail")).toBeVisible();
  await page.getByTestId("expense-date").fill(iso);
  await page.getByTestId("expense-supplier").fill("Staples");
  await page.getByTestId("expense-gross-amount").fill("121.00");
  await page.getByTestId("expense-vat-treatment").selectOption("btw_21");
  await page.getByTestId("expense-payment-method").selectOption("business_account");
  await page.getByTestId("expense-save").click();
  await expect(page.getByTestId("expense-saved")).toBeVisible();
  await shot(page, "14-purchase-filled");
  await page.getByTestId("expense-submit").click();
  await expect(page.getByTestId("purchases")).toBeVisible();
  await expect(page.getByTestId("purchase-row").first()).toContainText(/Geboekt|Booked/);
  await shot(page, "15-purchases-booked");

  // --- The dashboard now has figures -------------------------------------------------
  await go(page, "/");
  await expect(page.getByTestId("home-figures")).toBeVisible();
  await shot(page, "16-dashboard");

  // --- The books ----------------------------------------------------------------------
  await go(page, "/ledger");
  await expect(page.locator("table").first()).toBeVisible();
  await shot(page, "17-ledger");
  await go(page, "/reports?view=income-statement");
  await expect(page.locator("table").first()).toBeVisible();
  await shot(page, "18-income-statement");

  // --- The BTW return -------------------------------------------------------------------
  await go(page, "/vat");
  await expect(page.getByTestId("vat-periods")).toBeVisible();
  await shot(page, "19-vat-overview");
  const quarter = Math.floor(today.getMonth() / 3) + 1;
  await page.getByText(`${quarter}e kwartaal ${year}`).click();
  await expect(page.getByTestId("vat-boxes")).toBeVisible();
  await expect(page.getByTestId("vat-box-1a")).toContainText("950");
  await expect(page.getByTestId("vat-box-1a")).toContainText("199");
  await expect(page.getByTestId("vat-box-5b")).toContainText("21");
  await page.getByTestId("vat-box-toggle-1a").click();
  await expect(page.getByTestId("vat-drill-1a")).toBeVisible();
  await shot(page, "20-vat-return");
});
