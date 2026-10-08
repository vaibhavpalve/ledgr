import type { L } from "../l10n";

/** `/product`: every module, each with an anchor the mega menu links to (design/site/product.html). */
export const PRODUCT = {
  seo: {
    title: {
      en: "Product: everything your books need | Boeklite",
      nl: "Product: alles wat uw boekhouding nodig heeft | Boeklite",
    },
    description: {
      en: "Receipt capture, bank matching, grootboek, BTW-aangifte, invoicing and reports in one calm app, built for Dutch rules.",
      nl: "Bonnen vastleggen, bankkoppeling, grootboek, BTW-aangifte, facturen en rapportages in één rustige app, gebouwd voor Nederlandse regels.",
    },
  },
  hero: {
    eyebrow: { en: "Product", nl: "Product" },
    lead: { en: "Everything your books need.", nl: "Alles wat uw boekhouding nodig heeft." },
    mark: { en: "Nothing they don't.", nl: "Niets wat ze niet nodig heeft." },
    body: {
      en: "Capture, bank, grootboek, BTW, invoicing and reports in one calm app, built for Dutch rules.",
      nl: "Vastleggen, bank, grootboek, BTW, facturen en rapportages in één rustige app, gebouwd voor Nederlandse regels.",
    },
  },
  sections: [
    {
      id: "capture",
      eyebrow: { en: "Receipt capture", nl: "Bonnen vastleggen" },
      title: { en: "Snap it. It is booked.", nl: "Foto erop. Geboekt." },
      body: {
        en: "Photograph a receipt or drop a PDF, one at a time or a whole stack. Boeklite reads it and proposes the entry; you check it and submit.",
        nl: "Fotografeer een bon of sleep een pdf erin, één tegelijk of een hele stapel. Boeklite leest hem en stelt de boeking voor; u controleert en dient in.",
      },
      ticks: [
        {
          en: "Reads supplier, date, total, BTW rate and amount",
          nl: "Leest leverancier, datum, totaal, BTW-tarief en -bedrag",
        },
        {
          en: "Choose the category first, so it arrives already filed",
          nl: "Kies eerst de categorie, dan komt hij al goed binnen",
        },
        {
          en: "Warns before the same invoice number is booked twice",
          nl: "Waarschuwt voordat hetzelfde factuurnummer twee keer wordt geboekt",
        },
        {
          en: "Reverse-charge BTW recognised and booked both ways",
          nl: "Verlegde BTW herkend en aan beide kanten geboekt",
        },
        {
          en: "The original kept with the entry for the statutory seven years",
          nl: "Het origineel bewaard bij de boeking, de wettelijke zeven jaar",
        },
      ] as readonly L[],
      media: "capture",
    },
    {
      id: "bank",
      eyebrow: { en: "Bank & matching", nl: "Bank & koppelen" },
      title: {
        en: "Reconciled before your coffee is cold.",
        nl: "Verwerkt voordat uw koffie koud is.",
      },
      body: {
        en: "Import the statement your bank exports. Every line arrives with its best match, how sure that match is, and why; certain matches book in one go.",
        nl: "Importeer het afschrift dat uw bank exporteert. Elke regel komt binnen met de beste match, hoe zeker die is en waarom; zekere matches boekt u in één keer.",
      },
      ticks: [
        {
          en: "CAMT.053, MT940 and the CSV export of ING, Rabobank, bunq, Knab and ABN AMRO",
          nl: "CAMT.053, MT940 en de CSV-export van ING, Rabobank, bunq, Knab en ABN AMRO",
        },
        {
          en: "A statement imported twice is never booked twice",
          nl: "Een afschrift dat twee keer wordt ingelezen, wordt nooit dubbel geboekt",
        },
        {
          en: "Money in matched to invoices, money out to receipts",
          nl: "Geld in gekoppeld aan facturen, geld uit aan bonnen",
        },
        {
          en: "Partial payments, and one payment split over several invoices",
          nl: "Deelbetalingen, en één betaling verdeeld over meerdere facturen",
        },
      ] as readonly L[],
      media: "bank",
    },
    {
      id: "grootboek",
      eyebrow: { en: "Grootboek", nl: "Grootboek" },
      title: { en: "Double-entry, done properly.", nl: "Dubbel boekhouden, zoals het hoort." },
      body: {
        en: "A complete Dutch chart of accounts on the reference chart (RGS), ready on day one. Every entry balances to the cent before it can be booked, and stays booked.",
        nl: "Een compleet Nederlands rekeningschema volgens het RGS, klaar op dag één. Elke boeking sluit tot op de cent voordat hij geboekt kan worden, en blijft geboekt.",
      },
      ticks: [
        {
          en: "Chart of accounts per legal form, on RGS",
          nl: "Rekeningschema per rechtsvorm, volgens RGS",
        },
        {
          en: "Debit always equals credit, enforced by the ledger itself",
          nl: "Debet is altijd credit, afgedwongen door het grootboek zelf",
        },
        {
          en: "Booked entries are permanent; corrections are reversing entries",
          nl: "Geboekte posten zijn blijvend; correcties zijn tegenboekingen",
        },
        {
          en: "Memoriaal entries, opening balance and closed periods",
          nl: "Memoriaalboekingen, beginbalans en afgesloten perioden",
        },
        {
          en: "Fixed assets (MVA) with depreciation booked for you",
          nl: "Vaste activa (MVA) met afschrijvingen die voor u worden geboekt",
        },
      ] as readonly L[],
      media: "grootboek",
    },
    {
      id: "btw",
      eyebrow: { en: "BTW-aangifte", nl: "BTW-aangifte" },
      title: { en: "Your quarterly return, prepared.", nl: "Uw kwartaalaangifte, opgesteld." },
      body: {
        en: "Boeklite fills each rubriek from your booked entries, checks for gaps and tells you what to pay or reclaim, and by when.",
        nl: "Boeklite vult elke rubriek uit uw geboekte posten, controleert op gaten en vertelt wat u betaalt of terugkrijgt, en wanneer.",
      },
      ticks: [
        {
          en: "All rubrieken prepared from your entries",
          nl: "Alle rubrieken opgesteld uit uw boekingen",
        },
        {
          en: "Click any amount to see the entries behind it",
          nl: "Klik op een bedrag om de boekingen erachter te zien",
        },
        {
          en: "Checks before filing: unbooked receipts, open periods",
          nl: "Controles vóór het indienen: ongeboekte bonnen, open perioden",
        },
        {
          en: "Filed return stored with its reference; the period is locked",
          nl: "Ingediende aangifte bewaard met kenmerk; de periode gaat op slot",
        },
        { en: "Reminder emails before the deadline", nl: "Herinneringsmails vóór de deadline" },
      ] as readonly L[],
      media: "btw",
    },
    {
      id: "invoicing",
      eyebrow: { en: "Invoicing", nl: "Facturen" },
      title: { en: "Send it. See it paid. It is booked.", nl: "Versturen. Betaald zien. Geboekt." },
      body: {
        en: "Create invoices in your own style and send them as PDF by email. When the payment shows up on your statement, it is matched to the invoice.",
        nl: "Maak facturen in uw eigen huisstijl en verstuur ze als pdf per e-mail. Verschijnt de betaling op uw afschrift, dan wordt hij aan de factuur gekoppeld.",
      },
      ticks: [
        {
          en: "Gapless invoice numbering and the statutory fields checked",
          nl: "Doorlopende factuurnummers en de wettelijke gegevens gecontroleerd",
        },
        {
          en: "Your own template: logo, colours and fonts",
          nl: "Uw eigen sjabloon: logo, kleuren en lettertypen",
        },
        {
          en: "Credit notes for corrections, never an edited invoice",
          nl: "Creditnota's voor correcties, nooit een aangepaste factuur",
        },
        {
          en: "Overdue invoices flagged on your dashboard",
          nl: "Achterstallige facturen gemarkeerd op uw dashboard",
        },
      ] as readonly L[],
      media: "invoicing",
    },
    {
      id: "reports",
      eyebrow: { en: "Reports", nl: "Rapportages" },
      title: { en: "Know where you stand.", nl: "Weet waar u staat." },
      body: {
        en: "Profit and loss, balance sheet and cash position, always up to date because the books are.",
        nl: "Winst-en-verlies, balans en liquiditeit, altijd actueel omdat de boeken dat zijn.",
      },
      ticks: [
        {
          en: "Profit and loss and balance sheet per fiscal year",
          nl: "Winst-en-verliesrekening en balans per boekjaar",
        },
        {
          en: "Trial balance and the full journal",
          nl: "Proef- en saldibalans en het volledige journaal",
        },
        { en: "Exports for your accountant", nl: "Exports voor uw accountant" },
      ] as readonly L[],
      media: "reports",
    },
    {
      id: "mobile",
      eyebrow: { en: "On your phone", nl: "Op uw telefoon" },
      title: { en: "Your administration in your pocket.", nl: "Uw administratie in uw broekzak." },
      body: {
        en: "Install Boeklite on your phone from the browser and capture receipts the moment you pay.",
        nl: "Installeer Boeklite vanuit de browser op uw telefoon en leg bonnen vast op het moment dat u betaalt.",
      },
      ticks: [
        {
          en: "One tap from the home screen to the camera",
          nl: "Eén tik van het startscherm naar de camera",
        },
        {
          en: "Works without a connection; captures are kept encrypted until sent",
          nl: "Werkt zonder verbinding; vastleggingen blijven versleuteld tot ze verstuurd zijn",
        },
        {
          en: "Your dashboard and to-dos wherever you are",
          nl: "Uw dashboard en taken waar u ook bent",
        },
      ] as readonly L[],
      media: "mobile",
    },
  ],
  cta: {
    title: { en: "See your own books in Boeklite.", nl: "Bekijk uw eigen boeken in Boeklite." },
    body: {
      en: "Start free for 30 days, or book a demo and we will show you around.",
      nl: "Probeer 30 dagen gratis, of vraag een demo aan en wij laten u alles zien.",
    },
  },
} as const;
