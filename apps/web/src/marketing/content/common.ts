import type { L } from "../l10n";

/**
 * Copy shared by every page of the public site: header, mega menu, footer and the closing CTA.
 * Accounting terms (grootboek, BTW-aangifte, KvK) stay Dutch in both languages, as in the app.
 */

export type ModuleId = "capture" | "bank" | "grootboek" | "btw" | "invoicing" | "reports";

export const MODULES: readonly { id: ModuleId; title: L; blurb: L }[] = [
  {
    id: "capture",
    title: { en: "Receipt capture", nl: "Bonnen vastleggen" },
    blurb: { en: "Photo or upload. Read in seconds.", nl: "Foto of upload. In seconden gelezen." },
  },
  {
    id: "bank",
    title: { en: "Bank & matching", nl: "Bank & koppelen" },
    blurb: {
      en: "Statement lines matched to receipts and invoices.",
      nl: "Afschriftregels gekoppeld aan bonnen en facturen.",
    },
  },
  {
    id: "grootboek",
    title: { en: "Grootboek", nl: "Grootboek" },
    blurb: {
      en: "Double-entry, permanent, exact to the cent.",
      nl: "Dubbel boekhouden, blijvend, tot op de cent.",
    },
  },
  {
    id: "btw",
    title: { en: "BTW-aangifte", nl: "BTW-aangifte" },
    blurb: {
      en: "Quarterly return prepared from your entries.",
      nl: "Kwartaalaangifte opgesteld uit uw boekingen.",
    },
  },
  {
    id: "invoicing",
    title: { en: "Invoicing", nl: "Facturen" },
    blurb: {
      en: "Send invoices and see what is paid.",
      nl: "Facturen sturen en zien wat betaald is.",
    },
  },
  {
    id: "reports",
    title: { en: "Reports", nl: "Rapportages" },
    blurb: {
      en: "Profit and loss, balance sheet, cash.",
      nl: "Winst-en-verlies, balans, liquiditeit.",
    },
  },
];

export const COMMON = {
  brandHome: { en: "Boeklite home", nl: "Boeklite home" },
  nav: {
    label: { en: "Main", nl: "Hoofdmenu" },
    product: { en: "Product", nl: "Product" },
    accountants: { en: "For accountants", nl: "Voor accountants" },
    pricing: { en: "Pricing", nl: "Prijzen" },
    security: { en: "Security", nl: "Veiligheid" },
    articles: { en: "Articles", nl: "Artikelen" },
    allFeatures: { en: "All features", nl: "Alle functies" },
    allFeaturesHint: { en: "See every feature in one place", nl: "Alle functies op één plek" },
    openMenu: { en: "Open menu", nl: "Menu openen" },
    closeMenu: { en: "Close menu", nl: "Menu sluiten" },
  },
  logIn: { en: "Log in", nl: "Inloggen" },
  openApp: { en: "Open Boeklite", nl: "Boeklite openen" },
  startFree: { en: "Start free", nl: "Gratis starten" },
  startFree30: { en: "Start free for 30 days", nl: "30 dagen gratis proberen" },
  bookDemo: { en: "Book a demo", nl: "Demo aanvragen" },
  cta: {
    title: {
      en: "Close the quarter without the stress.",
      nl: "Sluit het kwartaal af zonder stress.",
    },
    body: {
      en: "Start free for 30 days. No credit card. Your accountant can join any time.",
      nl: "30 dagen gratis. Geen creditcard. Uw accountant kan altijd aansluiten.",
    },
  },
  footer: {
    tagline: {
      en: "Dutch bookkeeping for small businesses and their accountants. From receipt to return, automatically.",
      nl: "Nederlandse boekhouding voor kleine bedrijven en hun accountants. Van bon tot aangifte, automatisch.",
    },
    product: { en: "Product", nl: "Product" },
    forWhom: { en: "For whom", nl: "Voor wie" },
    company: { en: "Company", nl: "Bedrijf" },
    learn: { en: "Learn", nl: "Leren" },
    entrepreneurs: { en: "Entrepreneurs", nl: "Ondernemers" },
    zzp: { en: "ZZP & eenmanszaak", nl: "ZZP & eenmanszaak" },
    bv: { en: "BV owners", nl: "Eigenaren van een BV" },
    accountants: { en: "Accountants", nl: "Accountants" },
    contact: { en: "Contact", nl: "Contact" },
    allArticles: { en: "All articles", nl: "Alle artikelen" },
    btwDeadlines: { en: "BTW deadlines", nl: "BTW-deadlines" },
    bewaarplicht: { en: "Keeping receipts", nl: "Bonnen bewaren" },
    privacy: { en: "Privacy", nl: "Privacy" },
    cookies: { en: "Cookies", nl: "Cookies" },
    disclosure: { en: "Responsible disclosure", nl: "Responsible disclosure" },
    rights: { en: "© {year} Boeklite", nl: "© {year} Boeklite" },
  },
  language: { en: "Language", nl: "Taal" },
  readMore: { en: "Read more", nl: "Lees meer" },
  notFoundTitle: { en: "This page does not exist.", nl: "Deze pagina bestaat niet." },
  notFoundBody: {
    en: "The link may be old, or the address mistyped.",
    nl: "De link is misschien oud, of het adres bevat een typfout.",
  },
  backHome: { en: "Back to the home page", nl: "Terug naar de homepage" },
} as const;
