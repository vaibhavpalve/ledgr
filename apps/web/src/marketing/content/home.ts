import type { L } from "../l10n";

/**
 * The landing page (design/site/index.html), held to what the product does today. Where the
 * prototype promised a live PSD2 feed, iDEAL links, Peppol or Digipoort filing, the copy says
 * what works now (statement import, PDF invoices, a return you file yourself) and the rest is
 * listed honestly under "On the way".
 */
export const HOME = {
  seo: {
    title: {
      en: "Boeklite: Dutch bookkeeping, from receipt to return",
      nl: "Boeklite: Nederlandse boekhouding, van bon tot aangifte",
    },
    description: {
      en: "Boeklite reads your receipts, matches your bank statement and prepares your BTW-aangifte. For small businesses and their accountants.",
      nl: "Boeklite leest uw bonnen, koppelt uw bankafschrift en stelt uw BTW-aangifte op. Voor kleine bedrijven en hun accountants.",
    },
  },
  hero: {
    eyebrow: {
      en: "Bookkeeping for Dutch businesses",
      nl: "Boekhouden voor Nederlandse bedrijven",
    },
    lead: { en: "From receipt to return,", nl: "Van bon tot aangifte," },
    mark: { en: "automatically.", nl: "automatisch." },
    body: {
      en: "Snap a receipt, import your bank statement, and Boeklite books every line, matches every payment and prepares your BTW-aangifte. Your accountant sees the same books.",
      nl: "Maak een foto van een bon, importeer uw bankafschrift, en Boeklite boekt elke regel, koppelt elke betaling en stelt uw BTW-aangifte op. Uw accountant ziet dezelfde boeken.",
    },
    micro: [
      { en: "30 days free", nl: "30 dagen gratis" },
      { en: "No credit card", nl: "Geen creditcard" },
      { en: "Data stored in the EU", nl: "Gegevens in de EU" },
      { en: "Cancel any time", nl: "Altijd opzegbaar" },
    ] as readonly L[],
  },
  proof: [
    {
      title: { en: "Exact to the cent", nl: "Tot op de cent" },
      body: {
        en: "Decimal maths across the whole grootboek",
        nl: "Decimaal rekenen door het hele grootboek",
      },
    },
    {
      title: { en: "Permanent entries", nl: "Blijvende boekingen" },
      body: {
        en: "A correction is a new entry, never a deletion",
        nl: "Een correctie is een nieuwe boeking, nooit een verwijdering",
      },
    },
    {
      title: { en: "BTW-ready", nl: "BTW-klaar" },
      body: {
        en: "Return prepared from your own entries",
        nl: "Aangifte opgesteld uit uw eigen boekingen",
      },
    },
    {
      title: { en: "Passkey sign-in", nl: "Inloggen met passkey" },
      body: {
        en: "Nothing to remember, nothing to phish",
        nl: "Niets te onthouden, niets te phishen",
      },
    },
  ],
  audience: {
    eyebrow: { en: "Built for two people", nl: "Gemaakt voor twee mensen" },
    title: {
      en: "One set of books. Two ways to work.",
      nl: "Eén administratie. Twee manieren van werken.",
    },
    body: {
      en: "The entrepreneur captures and approves. The accountant reviews and files. Nobody emails spreadsheets.",
      nl: "De ondernemer legt vast en keurt goed. De accountant controleert en doet aangifte. Niemand mailt nog spreadsheets.",
    },
    label: { en: "Audience", nl: "Doelgroep" },
    owners: { en: "For entrepreneurs", nl: "Voor ondernemers" },
    accountants: { en: "For accountants", nl: "Voor accountants" },
    ownerCards: [
      {
        icon: "receipt",
        title: { en: "Capture in seconds", nl: "In seconden vastgelegd" },
        body: {
          en: "Photograph a receipt or drop the PDF. Supplier, date, amount and BTW are read for you.",
          nl: "Fotografeer een bon of sleep de pdf erin. Leverancier, datum, bedrag en BTW worden voor u gelezen.",
        },
        link: { en: "Receipt capture", nl: "Bonnen vastleggen" },
        to: "/product#capture",
      },
      {
        icon: "bank",
        title: { en: "Your bank, already matched", nl: "Uw bank, al gekoppeld" },
        body: {
          en: "Import the statement your bank exports. Each line comes with its likely invoice or receipt; you only decide what is unsure.",
          nl: "Importeer het afschrift dat uw bank exporteert. Elke regel krijgt de waarschijnlijke factuur of bon erbij; u beslist alleen wat onzeker is.",
        },
        link: { en: "Bank & matching", nl: "Bank & koppelen" },
        to: "/product#bank",
      },
      {
        icon: "percent",
        title: { en: "BTW without the evening", nl: "BTW zonder avondwerk" },
        body: {
          en: "Every quarter the return is filled from your entries. Check it, file it, and the period is locked.",
          nl: "Elk kwartaal wordt de aangifte gevuld uit uw boekingen. Controleren, indienen, en de periode staat vast.",
        },
        link: { en: "BTW-aangifte", nl: "BTW-aangifte" },
        to: "/product#btw",
      },
    ],
    accountantCards: [
      {
        icon: "users",
        title: { en: "All clients, one portfolio", nl: "Alle klanten, één portefeuille" },
        body: {
          en: "Every client administration you work in, with its KvK number and your role. Switch with one shortcut.",
          nl: "Elke klantadministratie waarin u werkt, met KvK-nummer en uw rol. Wisselen met één sneltoets.",
        },
        link: { en: "For accountants", nl: "Voor accountants" },
        to: "/accountants",
      },
      {
        icon: "eye",
        title: { en: "Review, don't re-enter", nl: "Controleren, niet overtypen" },
        body: {
          en: "Purchases arrive pre-booked with the source document beside them. Approve or correct in one place.",
          nl: "Inkopen komen voorgeboekt binnen, met het brondocument ernaast. Goedkeuren of corrigeren op één plek.",
        },
        link: { en: "Review workflow", nl: "Controleren" },
        to: "/accountants#review",
      },
      {
        icon: "history",
        title: { en: "A trail you can trust", nl: "Een spoor dat klopt" },
        body: {
          en: "Booked entries are permanent. Corrections are reversing entries, linked to the original.",
          nl: "Geboekte posten zijn blijvend. Correcties zijn tegenboekingen, gekoppeld aan het origineel.",
        },
        link: { en: "Audit trail", nl: "Audittrail" },
        to: "/security#audit",
      },
    ],
  },
  features: {
    capture: {
      eyebrow: { en: "Receipt capture", nl: "Bonnen vastleggen" },
      title: { en: "Snap it. It is booked.", nl: "Foto erop. Geboekt." },
      body: {
        en: "Take a photo or drop a PDF. Boeklite reads the receipt and proposes the journal entry with the right grootboek account and BTW code.",
        nl: "Maak een foto of sleep een pdf erin. Boeklite leest de bon en stelt de boeking voor, met de juiste grootboekrekening en BTW-code.",
      },
      ticks: [
        {
          en: "Supplier, date, total and BTW read automatically",
          nl: "Leverancier, datum, totaal en BTW automatisch gelezen",
        },
        {
          en: "A warning before the same invoice is booked twice",
          nl: "Een waarschuwing voordat dezelfde factuur twee keer wordt geboekt",
        },
        {
          en: "Source document stays attached to the entry",
          nl: "Het brondocument blijft bij de boeking",
        },
      ] as readonly L[],
      link: { en: "More about capture", nl: "Meer over vastleggen" },
    },
    bank: {
      eyebrow: { en: "Bank & matching", nl: "Bank & koppelen" },
      title: {
        en: "Your statement, reconciled in minutes.",
        nl: "Uw afschrift, in minuten verwerkt.",
      },
      body: {
        en: "Import the CAMT.053, MT940 or CSV file from your bank. Each line arrives with its best match and how sure Boeklite is; the certain ones book in one click.",
        nl: "Importeer het CAMT.053-, MT940- of CSV-bestand van uw bank. Elke regel komt binnen met de beste match en hoe zeker Boeklite is; de zekere boekt u in één klik.",
      },
      ticks: [
        {
          en: "ING, Rabobank, ABN AMRO, bunq and Knab exports",
          nl: "Exports van ING, Rabobank, ABN AMRO, bunq en Knab",
        },
        {
          en: "Matches on amount, reference and name",
          nl: "Koppelt op bedrag, kenmerk en naam",
        },
        {
          en: "Partial payments and one payment for several invoices",
          nl: "Deelbetalingen en één betaling voor meerdere facturen",
        },
      ] as readonly L[],
      link: { en: "More about bank", nl: "Meer over bank" },
    },
    btw: {
      eyebrow: { en: "BTW-aangifte", nl: "BTW-aangifte" },
      title: { en: "The quarter closes itself.", nl: "Het kwartaal sluit zichzelf." },
      body: {
        en: "Your return is filled from your own entries, box by box. Click any amount to see the entries behind it.",
        nl: "Uw aangifte wordt gevuld uit uw eigen boekingen, rubriek voor rubriek. Klik op een bedrag om de boekingen erachter te zien.",
      },
      ticks: [
        {
          en: "Rubrieken 1a to 5g prepared for you",
          nl: "Rubrieken 1a tot en met 5g voor u ingevuld",
        },
        { en: "Every amount traceable to its source", nl: "Elk bedrag herleidbaar tot de bron" },
        { en: "Reminder emails before each deadline", nl: "Herinneringsmails vóór elke deadline" },
      ] as readonly L[],
      link: { en: "More about BTW", nl: "Meer over BTW" },
    },
  },
  steps: {
    eyebrow: { en: "How it works", nl: "Zo werkt het" },
    title: { en: "Up and running in an afternoon.", nl: "In één middag aan de slag." },
    items: [
      {
        title: { en: "Create your administration", nl: "Maak uw administratie aan" },
        body: {
          en: "Choose your legal form and fiscal year. The grootboek is set up for you on the Dutch reference chart (RGS).",
          nl: "Kies uw rechtsvorm en boekjaar. Het grootboek wordt voor u ingericht volgens het Referentie Grootboekschema (RGS).",
        },
      },
      {
        title: { en: "Bring in your bank", nl: "Haal uw bank binnen" },
        body: {
          en: "Add your business account and import its statement. Enter your opening balance if you are switching mid-year.",
          nl: "Voeg uw zakelijke rekening toe en importeer het afschrift. Stapt u halverwege het jaar over, voer dan uw beginbalans in.",
        },
      },
      {
        title: { en: "Capture and approve", nl: "Vastleggen en goedkeuren" },
        body: {
          en: "Snap receipts as you go. Approve what Boeklite proposes. Bring your accountant in when you like.",
          nl: "Leg bonnen direct vast. Keur goed wat Boeklite voorstelt. Haal uw accountant erbij wanneer u wilt.",
        },
      },
    ],
  },
  integrations: {
    eyebrow: { en: "Works with your bank", nl: "Werkt met uw bank" },
    title: { en: "Works with the way you already work.", nl: "Past bij hoe u al werkt." },
    body: {
      en: "Statement files from every major Dutch bank, in the formats they already export.",
      nl: "Afschriftbestanden van alle grote Nederlandse banken, in de formaten die zij al exporteren.",
    },
    now: ["ING", "Rabobank", "ABN AMRO", "bunq", "Knab", "CAMT.053", "MT940", "CSV"],
    soonLabel: { en: "On the way", nl: "Onderweg" },
    soon: [
      { en: "Live bank feed (PSD2)", nl: "Live bankkoppeling (PSD2)" },
      { en: "Peppol e-invoicing", nl: "Peppol e-facturen" },
      { en: "Filing through Digipoort", nl: "Indienen via Digipoort" },
    ] as readonly L[],
  },
  pricing: {
    eyebrow: { en: "Pricing", nl: "Prijzen" },
    title: { en: "Simple pricing. No surprises.", nl: "Eenvoudige prijzen. Geen verrassingen." },
    body: {
      en: "Every plan includes receipt capture, bank matching and the BTW-aangifte.",
      nl: "Elk abonnement bevat bonnen vastleggen, bankkoppeling en de BTW-aangifte.",
    },
    compare: { en: "Compare all plans", nl: "Alle abonnementen vergelijken" },
  },
  faqTitle: { en: "Questions, answered.", nl: "Vragen, beantwoord." },
  faq: [
    {
      q: {
        en: "Is Boeklite suitable for a BV as well as a ZZP?",
        nl: "Is Boeklite geschikt voor een BV én voor een zzp'er?",
      },
      a: {
        en: "Yes. Boeklite supports eenmanszaak, VOF, BV, stichting and vereniging administrations, each with a chart of accounts on the Dutch reference chart (RGS) for its legal form.",
        nl: "Ja. Boeklite ondersteunt administraties voor eenmanszaak, VOF, BV, stichting en vereniging, elk met een rekeningschema volgens het RGS voor die rechtsvorm.",
      },
    },
    {
      q: {
        en: "Can my accountant work in my administration?",
        nl: "Kan mijn accountant in mijn administratie werken?",
      },
      a: {
        en: "Yes. Accounting firms have their own Boeklite account and open each client's administration from one portfolio. They see the same books you do.",
        nl: "Ja. Accountantskantoren hebben een eigen Boeklite-account en openen de administratie van elke klant vanuit één portefeuille. Zij zien dezelfde boeken als u.",
      },
    },
    {
      q: { en: "How does the BTW-aangifte work?", nl: "Hoe werkt de BTW-aangifte?" },
      a: {
        en: "Boeklite fills each rubriek from your booked entries and shows what to pay or reclaim. You file those figures in Mijn Belastingdienst Zakelijk and record the reference; Boeklite stores the return as filed and locks the period. Direct filing through Digipoort is on the way.",
        nl: "Boeklite vult elke rubriek uit uw geboekte posten en toont wat u moet betalen of terugkrijgt. U dient die cijfers in via Mijn Belastingdienst Zakelijk en legt het kenmerk vast; Boeklite bewaart de aangifte zoals ingediend en sluit de periode af. Rechtstreeks indienen via Digipoort is onderweg.",
      },
    },
    {
      q: {
        en: "Can I switch from another bookkeeping package?",
        nl: "Kan ik overstappen van een ander boekhoudpakket?",
      },
      a: {
        en: "Yes. Enter your opening balance from your previous package and import this year's bank statements, and you are ready to go.",
        nl: "Ja. Voer de beginbalans uit uw vorige pakket in en importeer de bankafschriften van dit jaar, en u kunt beginnen.",
      },
    },
    {
      q: { en: "Where is my data stored?", nl: "Waar worden mijn gegevens opgeslagen?" },
      a: {
        en: "In the European Union. Documents are encrypted with a key per administration. See the Security page for the details.",
        nl: "In de Europese Unie. Documenten worden versleuteld met een sleutel per administratie. Zie de pagina Veiligheid voor de details.",
      },
    },
    {
      q: { en: "Is there a mobile app?", nl: "Is er een app?" },
      a: {
        en: "Boeklite installs on your phone from the browser, with the camera for receipts. Captures made without a connection are kept encrypted on the phone and sent when you are back online.",
        nl: "Boeklite installeert u vanuit de browser op uw telefoon, met de camera voor bonnen. Wat u zonder verbinding vastlegt, blijft versleuteld op de telefoon en wordt verstuurd zodra u weer online bent.",
      },
    },
  ],
} as const;
