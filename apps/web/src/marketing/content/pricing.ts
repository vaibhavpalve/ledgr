import type { L } from "../l10n";

/**
 * `/pricing` (design/site/pricing.html). The amounts are the prototype's, which the brief marks
 * as placeholders to confirm before launch; the plan contents list only what the product does.
 * "On the way" rows say so in every column rather than promising a plan will have them.
 */
export type Cell = "yes" | "no" | "soon" | L;

export interface Plan {
  readonly id: "start" | "grow" | "accountant";
  readonly name: L;
  readonly for: L;
  /** Whole euros per month, monthly and yearly billing; null is "on request". */
  readonly monthly: number | null;
  readonly yearly: number | null;
  readonly featured: boolean;
  readonly ticks: readonly L[];
}

export const PLANS: readonly Plan[] = [
  {
    id: "start",
    name: { en: "Start", nl: "Start" },
    for: { en: "For ZZP and starters", nl: "Voor zzp'ers en starters" },
    monthly: 12,
    yearly: 10,
    featured: false,
    ticks: [
      { en: "1 administration", nl: "1 administratie" },
      { en: "Receipt capture and bank matching", nl: "Bonnen vastleggen en bankkoppeling" },
      { en: "Grootboek and BTW-aangifte", nl: "Grootboek en BTW-aangifte" },
      { en: "10 invoices per month", nl: "10 facturen per maand" },
    ],
  },
  {
    id: "grow",
    name: { en: "Grow", nl: "Groei" },
    for: { en: "For growing businesses and BVs", nl: "Voor groeiende bedrijven en BV's" },
    monthly: 24,
    yearly: 20,
    featured: true,
    ticks: [
      { en: "Everything in Start", nl: "Alles uit Start" },
      {
        en: "Unlimited invoices in your own template",
        nl: "Onbeperkt facturen in uw eigen sjabloon",
      },
      { en: "Fixed assets and depreciation", nl: "Vaste activa en afschrijvingen" },
      { en: "Access for your accountant", nl: "Toegang voor uw accountant" },
    ],
  },
  {
    id: "accountant",
    name: { en: "Accountant", nl: "Accountant" },
    for: { en: "For accounting firms", nl: "Voor accountantskantoren" },
    monthly: null,
    yearly: null,
    featured: false,
    ticks: [
      { en: "Unlimited client administrations", nl: "Onbeperkt klantadministraties" },
      { en: "Client portfolio and quick switching", nl: "Klantportefeuille en snel wisselen" },
      { en: "Roles per client for your team", nl: "Rollen per klant voor uw team" },
      { en: "Onboarding for your team", nl: "Onboarding voor uw team" },
    ],
  },
];

export const COMPARE: readonly {
  group: L;
  rows: readonly { label: L; cells: readonly [Cell, Cell, Cell] }[];
}[] = [
  {
    group: { en: "Bookkeeping", nl: "Boekhouding" },
    rows: [
      {
        label: { en: "Administrations", nl: "Administraties" },
        cells: [
          { en: "1", nl: "1" },
          { en: "1", nl: "1" },
          { en: "Unlimited", nl: "Onbeperkt" },
        ],
      },
      {
        label: { en: "Receipt capture (photo, upload)", nl: "Bonnen vastleggen (foto, upload)" },
        cells: ["yes", "yes", "yes"],
      },
      {
        label: {
          en: "Bank statement import and matching",
          nl: "Bankafschrift importeren en koppelen",
        },
        cells: ["yes", "yes", "yes"],
      },
      {
        label: { en: "Grootboek, memoriaal and periods", nl: "Grootboek, memoriaal en perioden" },
        cells: ["yes", "yes", "yes"],
      },
      {
        label: { en: "BTW-aangifte prepared", nl: "BTW-aangifte opgesteld" },
        cells: ["yes", "yes", "yes"],
      },
      {
        label: { en: "Fixed assets and depreciation", nl: "Vaste activa en afschrijvingen" },
        cells: ["no", "yes", "yes"],
      },
      {
        label: { en: "Profit and loss, balance sheet", nl: "Winst-en-verlies, balans" },
        cells: ["yes", "yes", "yes"],
      },
    ],
  },
  {
    group: { en: "Getting paid", nl: "Betaald krijgen" },
    rows: [
      {
        label: { en: "Invoices per month", nl: "Facturen per maand" },
        cells: [
          { en: "10", nl: "10" },
          { en: "Unlimited", nl: "Onbeperkt" },
          { en: "Unlimited", nl: "Onbeperkt" },
        ],
      },
      {
        label: { en: "Your own invoice template", nl: "Uw eigen factuursjabloon" },
        cells: ["no", "yes", "yes"],
      },
      { label: { en: "Credit notes", nl: "Creditnota's" }, cells: ["yes", "yes", "yes"] },
      {
        label: { en: "Payments matched to invoices", nl: "Betalingen gekoppeld aan facturen" },
        cells: ["yes", "yes", "yes"],
      },
    ],
  },
  {
    group: { en: "Working together", nl: "Samenwerken" },
    rows: [
      {
        label: { en: "Access for your accountant", nl: "Toegang voor uw accountant" },
        cells: ["no", "yes", "yes"],
      },
      { label: { en: "Client portfolio", nl: "Klantportefeuille" }, cells: ["no", "no", "yes"] },
      { label: { en: "Roles per client", nl: "Rollen per klant" }, cells: ["no", "no", "yes"] },
    ],
  },
  {
    group: { en: "On the way", nl: "Onderweg" },
    rows: [
      {
        label: { en: "Live bank feed (PSD2)", nl: "Live bankkoppeling (PSD2)" },
        cells: ["soon", "soon", "soon"],
      },
      {
        label: { en: "Peppol e-invoicing", nl: "Peppol e-facturen" },
        cells: ["soon", "soon", "soon"],
      },
      {
        label: { en: "Filing through Digipoort", nl: "Indienen via Digipoort" },
        cells: ["soon", "soon", "soon"],
      },
    ],
  },
];

export const PRICING = {
  seo: {
    title: { en: "Pricing | Boeklite", nl: "Prijzen | Boeklite" },
    description: {
      en: "Plans for ZZP, growing businesses and accounting firms. Try every feature free for 30 days, no credit card needed.",
      nl: "Abonnementen voor zzp'ers, groeiende bedrijven en accountantskantoren. Probeer alles 30 dagen gratis, zonder creditcard.",
    },
  },
  hero: {
    eyebrow: { en: "Pricing", nl: "Prijzen" },
    lead: { en: "Plans that grow", nl: "Abonnementen die meegroeien" },
    mark: { en: "with your business.", nl: "met uw bedrijf." },
    body: {
      en: "Try every feature free for 30 days. No credit card needed.",
      nl: "Probeer alles 30 dagen gratis. Geen creditcard nodig.",
    },
  },
  billing: {
    label: { en: "Bill yearly", nl: "Jaarlijks betalen" },
    monthly: { en: "Monthly", nl: "Maandelijks" },
    yearly: { en: "Yearly", nl: "Jaarlijks" },
    save: { en: "2 months free", nl: "2 maanden gratis" },
    perMonth: { en: "/ month, excl. BTW", nl: "/ maand, excl. BTW" },
    billedYearly: { en: "Billed yearly at €{amount}", nl: "Jaarlijks gefactureerd: €{amount}" },
    onRequest: { en: "On request", nl: "Op aanvraag" },
    perClient: { en: "Priced per active client", nl: "Prijs per actieve klant" },
  },
  mostChosen: { en: "Most chosen", nl: "Meest gekozen" },
  compareTitle: { en: "Compare plans", nl: "Abonnementen vergelijken" },
  feature: { en: "Feature", nl: "Functie" },
  included: { en: "Included", nl: "Inbegrepen" },
  notIncluded: { en: "Not included", nl: "Niet inbegrepen" },
  soon: { en: "Coming", nl: "Komt eraan" },
  faqTitle: { en: "Pricing questions", nl: "Vragen over prijzen" },
  faq: [
    {
      q: { en: "Can I change plans later?", nl: "Kan ik later van abonnement wisselen?" },
      a: {
        en: "Yes, at any time. Upgrades apply immediately; downgrades from the next billing period.",
        nl: "Ja, altijd. Een upgrade geldt direct; een downgrade vanaf de volgende periode.",
      },
    },
    {
      q: { en: "Are prices including BTW?", nl: "Zijn de prijzen inclusief BTW?" },
      a: { en: "Prices are shown excluding 21% BTW.", nl: "Prijzen zijn exclusief 21% BTW." },
    },
    {
      q: { en: "What happens when my trial ends?", nl: "Wat gebeurt er na de proefperiode?" },
      a: {
        en: "Choose a plan to keep going. If you do not, your administration becomes read-only and you can still export everything.",
        nl: "Kies een abonnement om door te gaan. Doet u dat niet, dan wordt uw administratie alleen-lezen en kunt u nog steeds alles exporteren.",
      },
    },
    {
      q: { en: "Do you offer a discount for starters?", nl: "Is er korting voor starters?" },
      a: {
        en: "Ask us. We like helping new businesses get their books right from day one.",
        nl: "Vraag het ons. Wij helpen nieuwe bedrijven graag om vanaf dag één hun boeken op orde te hebben.",
      },
    },
  ],
} as const;
