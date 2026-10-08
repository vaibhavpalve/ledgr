import type { L } from "../l10n";

/**
 * The articles at `/articles`. Written for Dutch small businesses, in both languages, about the
 * rules Boeklite is built around. Each states rules as the Belastingdienst publishes them in
 * general terms; none is tax advice for a particular case, and each says so at the end.
 */
export type Block =
  | { readonly type: "p"; readonly text: L }
  | { readonly type: "h2"; readonly text: L }
  | { readonly type: "ul"; readonly items: readonly L[] }
  | { readonly type: "tip"; readonly text: L }
  | {
      readonly type: "table";
      readonly head: readonly L[];
      readonly rows: readonly (readonly L[])[];
    };

export type Category = "btw" | "basics" | "bank" | "invoicing";

export interface Article {
  readonly slug: string;
  readonly category: Category;
  /** ISO date. */
  readonly published: string;
  readonly title: L;
  readonly summary: L;
  readonly body: readonly Block[];
}

export const CATEGORIES: Record<Category, L> = {
  btw: { en: "BTW", nl: "BTW" },
  basics: { en: "Bookkeeping basics", nl: "Basis van boekhouden" },
  bank: { en: "Bank", nl: "Bank" },
  invoicing: { en: "Invoicing", nl: "Facturen" },
};

const same = (text: string): L => ({ en: text, nl: text });

export const ARTICLES: readonly Article[] = [
  {
    slug: "btw-aangifte-deadlines",
    category: "btw",
    published: "2026-09-29",
    title: {
      en: "BTW-aangifte deadlines: the quarterly calendar",
      nl: "Deadlines voor de BTW-aangifte: de kwartaalkalender",
    },
    summary: {
      en: "When each quarterly return is due, what 'due' really means, and how to correct a return you already filed.",
      nl: "Wanneer elke kwartaalaangifte uiterlijk binnen moet zijn, wat 'uiterlijk' echt betekent, en hoe u een ingediende aangifte corrigeert.",
    },
    body: [
      {
        type: "p",
        text: {
          en: "Most Dutch businesses file their BTW-aangifte every quarter. The rule is the same for every quarter: the return must be filed, and the BTW you owe must be received by the Belastingdienst, no later than the last day of the month after the quarter ends.",
          nl: "De meeste Nederlandse ondernemers doen elk kwartaal BTW-aangifte. De regel is voor elk kwartaal gelijk: de aangifte moet zijn ingediend, en de verschuldigde BTW moet door de Belastingdienst zijn ontvangen, uiterlijk op de laatste dag van de maand na afloop van het kwartaal.",
        },
      },
      { type: "h2", text: { en: "The four dates", nl: "De vier data" } },
      {
        type: "table",
        head: [
          { en: "Quarter", nl: "Kwartaal" },
          { en: "Covers", nl: "Periode" },
          { en: "File and pay by", nl: "Indienen en betalen vóór" },
        ],
        rows: [
          [
            same("Q1"),
            { en: "January to March", nl: "januari tot en met maart" },
            { en: "30 April", nl: "30 april" },
          ],
          [
            same("Q2"),
            { en: "April to June", nl: "april tot en met juni" },
            { en: "31 July", nl: "31 juli" },
          ],
          [
            same("Q3"),
            { en: "July to September", nl: "juli tot en met september" },
            { en: "31 October", nl: "31 oktober" },
          ],
          [
            same("Q4"),
            { en: "October to December", nl: "oktober tot en met december" },
            { en: "31 January (next year)", nl: "31 januari (volgend jaar)" },
          ],
        ],
      },
      {
        type: "p",
        text: {
          en: "If you file monthly, the same logic applies one month at a time: the return for March is due by 30 April. A small number of businesses file once a year, but only when the Belastingdienst has agreed to it.",
          nl: "Doet u maandaangifte, dan geldt dezelfde logica per maand: de aangifte over maart moet uiterlijk 30 april binnen zijn. Een klein aantal ondernemers doet één keer per jaar aangifte, maar alleen als de Belastingdienst daarmee heeft ingestemd.",
        },
      },
      { type: "h2", text: { en: "'Received', not 'sent'", nl: "'Ontvangen', niet 'verstuurd'" } },
      {
        type: "p",
        text: {
          en: "The deadline is about the money arriving, not about you pressing 'pay'. A transfer started on the last day may land a day or two later. Late filing and late payment can each lead to a fine, so treat the last week of the month as the real deadline and pay with the payment reference the Belastingdienst gives you.",
          nl: "De deadline gaat over het moment dat het geld binnenkomt, niet over het moment dat u op 'betalen' drukt. Een overboeking op de laatste dag kan een dag of twee later binnenkomen. Te laat indienen en te laat betalen kunnen elk tot een boete leiden, dus beschouw de laatste week van de maand als de echte deadline, en betaal met het betalingskenmerk dat de Belastingdienst u geeft.",
        },
      },
      {
        type: "h2",
        text: { en: "Nothing to pay? Still file.", nl: "Niets te betalen? Toch aangifte doen." },
      },
      {
        type: "p",
        text: {
          en: "If you had no sales and no costs in a quarter, you still file a return, with zeros. Not filing is treated as filing late, whatever the amount would have been.",
          nl: "Had u in een kwartaal geen omzet en geen kosten, dan doet u toch aangifte, met nullen. Geen aangifte doen wordt behandeld als te laat aangifte doen, hoe hoog het bedrag ook geweest zou zijn.",
        },
      },
      {
        type: "h2",
        text: {
          en: "Found a mistake in an earlier return?",
          nl: "Een fout in een eerdere aangifte?",
        },
      },
      {
        type: "ul",
        items: [
          {
            en: "Is the difference €1,000 or less? You may correct it in your next regular return.",
            nl: "Is het verschil € 1.000 of minder? Dan mag u het in uw volgende gewone aangifte corrigeren.",
          },
          {
            en: "Is it more than €1,000? File a correction (suppletie) for the period concerned.",
            nl: "Is het meer dan € 1.000? Dien dan een suppletie in over het betreffende tijdvak.",
          },
        ],
      },
      {
        type: "tip",
        text: {
          en: "In Boeklite the return is prepared from your booked entries, and every reminder email goes out before the deadline, not on it. Once you file, the period is locked so the figures you reported cannot drift.",
          nl: "In Boeklite wordt de aangifte opgesteld uit uw geboekte posten, en elke herinneringsmail gaat vóór de deadline de deur uit, niet erop. Zodra u indient, gaat de periode op slot, zodat de cijfers die u opgaf niet meer kunnen verschuiven.",
        },
      },
    ],
  },
  {
    slug: "bewaarplicht-bonnen-bewaren",
    category: "basics",
    published: "2026-09-15",
    title: {
      en: "How long to keep receipts: the seven-year rule",
      nl: "Hoe lang bewaart u bonnen? De zevenjaarsregel",
    },
    summary: {
      en: "The bewaarplicht in plain words: what you keep, for how long, and whether a scan is enough.",
      nl: "De bewaarplicht in gewone woorden: wat u bewaart, hoe lang, en of een scan genoeg is.",
    },
    body: [
      {
        type: "p",
        text: {
          en: "Every Dutch business has a bewaarplicht: the legal duty to keep its administration so that the Belastingdienst can check it. For most records, that duty lasts seven years.",
          nl: "Elke Nederlandse ondernemer heeft een bewaarplicht: de wettelijke plicht om de administratie te bewaren, zodat de Belastingdienst die kan controleren. Voor de meeste stukken duurt die plicht zeven jaar.",
        },
      },
      { type: "h2", text: { en: "What you keep", nl: "Wat u bewaart" } },
      {
        type: "ul",
        items: [
          { en: "Purchase invoices and receipts", nl: "Inkoopfacturen en bonnen" },
          {
            en: "Copies of the sales invoices you sent",
            nl: "Kopieën van de verkoopfacturen die u verstuurde",
          },
          { en: "Bank statements", nl: "Bankafschriften" },
          {
            en: "Your grootboek, journal and the returns you filed",
            nl: "Uw grootboek, journaal en de aangiften die u deed",
          },
          {
            en: "Contracts and agreements that affect your tax",
            nl: "Contracten en afspraken die invloed hebben op uw belasting",
          },
        ],
      },
      { type: "h2", text: { en: "Seven years, sometimes ten", nl: "Zeven jaar, soms tien" } },
      {
        type: "p",
        text: {
          en: "Seven years is the rule. Records about real estate (onroerende zaken) are the main exception: because BTW on a building can be revised for years after you start using it, those records must be kept for ten years.",
          nl: "Zeven jaar is de regel. Stukken over onroerende zaken zijn de belangrijkste uitzondering: omdat de BTW op een gebouw nog jaren na ingebruikname kan worden herzien, moet u die stukken tien jaar bewaren.",
        },
      },
      { type: "h2", text: { en: "Is a scan enough?", nl: "Is een scan genoeg?" } },
      {
        type: "p",
        text: {
          en: "In general, yes. You may keep your administration digitally and convert paper receipts to digital copies, as long as the copy is complete and legible, and you can show it within a reasonable time when asked. What matters is that nothing is lost in the conversion: the supplier, the date, the amounts and the BTW must all still be readable.",
          nl: "Over het algemeen wel. U mag uw administratie digitaal bewaren en papieren bonnen omzetten naar digitale kopieën, zolang de kopie volledig en leesbaar is en u hem op verzoek binnen redelijke tijd kunt laten zien. Waar het om gaat, is dat er bij het omzetten niets verloren gaat: de leverancier, de datum, de bedragen en de BTW moeten allemaal nog leesbaar zijn.",
        },
      },
      {
        type: "p",
        text: {
          en: "Some records, the so-called basisgegevens such as the grootboek, debtor and creditor records and your purchases and sales, must be kept in a form that cannot be quietly altered. Bookkeeping software that never edits a booked entry, but corrects it with a new one, does exactly that.",
          nl: "Sommige stukken, de zogeheten basisgegevens zoals het grootboek, de debiteuren- en crediteurenadministratie en uw in- en verkopen, moeten zo bewaard worden dat ze niet ongemerkt kunnen worden aangepast. Boekhoudsoftware die een geboekte post nooit wijzigt, maar met een nieuwe boeking corrigeert, doet precies dat.",
        },
      },
      {
        type: "tip",
        text: {
          en: "Boeklite keeps the original receipt with its entry, encrypted, for the statutory seven years, and a booked entry can never be edited or deleted.",
          nl: "Boeklite bewaart de originele bon bij de boeking, versleuteld, de wettelijke zeven jaar, en een geboekte post kan nooit worden gewijzigd of verwijderd.",
        },
      },
    ],
  },
  {
    slug: "dubbel-boekhouden-uitgelegd",
    category: "basics",
    published: "2026-08-27",
    title: {
      en: "Double-entry bookkeeping, explained with one receipt",
      nl: "Dubbel boekhouden, uitgelegd met één bon",
    },
    summary: {
      en: "Debit, credit and why they must balance, worked through with a single € 52,80 receipt.",
      nl: "Debet, credit en waarom ze moeten sluiten, uitgewerkt met één bon van € 52,80.",
    },
    body: [
      {
        type: "p",
        text: {
          en: "Double-entry bookkeeping sounds old-fashioned and complicated. It is old, about five hundred years, but the idea fits in one sentence: every amount is recorded twice, once where the money came from and once where it went, and the two sides always add up to the same total.",
          nl: "Dubbel boekhouden klinkt ouderwets en ingewikkeld. Het is inderdaad oud, zo'n vijfhonderd jaar, maar het idee past in één zin: elk bedrag wordt twee keer vastgelegd, één keer waar het geld vandaan kwam en één keer waar het naartoe ging, en de twee kanten tellen altijd op tot hetzelfde totaal.",
        },
      },
      { type: "h2", text: { en: "One receipt", nl: "Eén bon" } },
      {
        type: "p",
        text: {
          en: "You buy printer paper at Papierhuis Amsterdam and pay € 52,80 from your business account. The receipt says € 43,64 for the paper and € 9,16 BTW at 21%.",
          nl: "U koopt printerpapier bij Papierhuis Amsterdam en betaalt € 52,80 vanaf uw zakelijke rekening. Op de bon staat € 43,64 voor het papier en € 9,16 BTW tegen 21%.",
        },
      },
      {
        type: "table",
        head: [
          { en: "Account", nl: "Rekening" },
          { en: "Debit", nl: "Debet" },
          { en: "Credit", nl: "Credit" },
        ],
        rows: [
          [same("4300 Kantoorkosten"), same("43,64"), same("")],
          [same("1520 BTW te vorderen"), same("9,16"), same("")],
          [same("1100 Bank"), same(""), same("52,80")],
          [{ en: "Total", nl: "Totaal" }, same("52,80"), same("52,80")],
        ],
      },
      { type: "h2", text: { en: "Reading the entry", nl: "De boeking lezen" } },
      {
        type: "ul",
        items: [
          {
            en: "Kantoorkosten (office costs) goes up by € 43,64: that is what the paper cost your business.",
            nl: "Kantoorkosten stijgen met € 43,64: dat is wat het papier uw bedrijf kostte.",
          },
          {
            en: "BTW te vorderen (BTW to reclaim) goes up by € 9,16: you get this back through your BTW-aangifte, so it is not a cost.",
            nl: "BTW te vorderen stijgt met € 9,16: die krijgt u terug via uw BTW-aangifte, dus het is geen kostenpost.",
          },
          {
            en: "Bank goes down by € 52,80: the money left your account.",
            nl: "De bank daalt met € 52,80: het geld ging van uw rekening af.",
          },
        ],
      },
      {
        type: "p",
        text: {
          en: "Debit is simply the left column and credit the right. Costs and things you own increase on the debit side; money leaving the bank is a credit to the bank account. The words matter less than the rule: both columns must total the same. If they do not, something is missing, and the entry cannot be booked.",
          nl: "Debet is gewoon de linkerkolom en credit de rechter. Kosten en bezittingen nemen toe aan de debetkant; geld dat van de bank afgaat, is een credit op de bankrekening. De woorden zijn minder belangrijk dan de regel: beide kolommen moeten hetzelfde totaal hebben. Is dat niet zo, dan ontbreekt er iets, en kan de boeking niet worden geboekt.",
        },
      },
      { type: "h2", text: { en: "Why it is worth it", nl: "Waarom het de moeite waard is" } },
      {
        type: "p",
        text: {
          en: "Because every euro is recorded twice, mistakes show up instead of hiding. And because the same entry feeds your costs, your BTW and your bank balance, one receipt updates your profit and loss, your BTW-aangifte and your cash position at once.",
          nl: "Omdat elke euro twee keer wordt vastgelegd, vallen fouten op in plaats van zich te verstoppen. En omdat dezelfde boeking uw kosten, uw BTW en uw banksaldo voedt, werkt één bon tegelijk uw winst-en-verliesrekening, uw BTW-aangifte en uw liquiditeit bij.",
        },
      },
      { type: "h2", text: { en: "And when it is wrong?", nl: "En als het fout is?" } },
      {
        type: "p",
        text: {
          en: "You do not erase it. You book a reversing entry, the same lines with debit and credit swapped, and then the correct entry. The history shows what happened, which is exactly what your accountant and the Belastingdienst want to see.",
          nl: "U gumt het niet uit. U boekt een tegenboeking, dezelfde regels met debet en credit omgedraaid, en daarna de juiste boeking. De geschiedenis laat zien wat er gebeurde, en dat is precies wat uw accountant en de Belastingdienst willen zien.",
        },
      },
      {
        type: "tip",
        text: {
          en: "Boeklite proposes this entry from a photo of the receipt and refuses any entry that does not balance to the cent.",
          nl: "Boeklite stelt deze boeking voor op basis van een foto van de bon, en weigert elke boeking die niet tot op de cent sluit.",
        },
      },
    ],
  },
  {
    slug: "btw-verlegd-reverse-charge",
    category: "btw",
    published: "2026-08-12",
    title: {
      en: "BTW verlegd: when your supplier charges no BTW",
      nl: "BTW verlegd: als uw leverancier geen BTW rekent",
    },
    summary: {
      en: "An invoice with 'reverse charge' on it is not BTW-free. Here is what to declare, in which rubriek, and why it usually nets to zero.",
      nl: "Een factuur met 'btw verlegd' erop is niet BTW-vrij. Dit geeft u aan, in welke rubriek, en waarom het meestal op nul uitkomt.",
    },
    body: [
      {
        type: "p",
        text: {
          en: "Sometimes an invoice arrives without BTW, with 'BTW verlegd' or 'reverse charge' printed on it. That does not mean no BTW applies. It means the BTW is shifted to you: you declare it in your own return instead of paying it to the supplier.",
          nl: "Soms komt er een factuur binnen zonder BTW, met 'btw verlegd' of 'reverse charge' erop. Dat betekent niet dat er geen BTW geldt. Het betekent dat de BTW naar u is verlegd: u geeft hem zelf aan in uw aangifte in plaats van hem aan de leverancier te betalen.",
        },
      },
      { type: "h2", text: { en: "When it happens", nl: "Wanneer het voorkomt" } },
      {
        type: "ul",
        items: [
          {
            en: "Services bought from a business in another EU country, such as a software subscription from Ireland.",
            nl: "Diensten die u inkoopt bij een bedrijf in een ander EU-land, zoals een software-abonnement uit Ierland.",
          },
          {
            en: "Goods bought from a business in another EU country.",
            nl: "Goederen die u inkoopt bij een bedrijf in een ander EU-land.",
          },
          {
            en: "Services from a business outside the EU.",
            nl: "Diensten van een bedrijf buiten de EU.",
          },
          {
            en: "Certain situations within the Netherlands, for example subcontracting in construction.",
            nl: "Bepaalde situaties binnen Nederland, bijvoorbeeld onderaanneming in de bouw.",
          },
        ],
      },
      {
        type: "h2",
        text: { en: "Where it goes in your return", nl: "Waar het in uw aangifte komt" },
      },
      {
        type: "table",
        head: [
          { en: "Situation", nl: "Situatie" },
          { en: "Rubriek", nl: "Rubriek" },
        ],
        rows: [
          [
            { en: "Reverse charge within the Netherlands", nl: "Verlegging binnen Nederland" },
            same("2a"),
          ],
          [{ en: "From countries outside the EU", nl: "Uit landen buiten de EU" }, same("4a")],
          [{ en: "From countries within the EU", nl: "Uit landen binnen de EU" }, same("4b")],
        ],
      },
      {
        type: "h2",
        text: { en: "Why it usually costs nothing", nl: "Waarom het meestal niets kost" },
      },
      {
        type: "p",
        text: {
          en: "Say you pay € 100 for a subscription from an Irish company. You declare € 100 in rubriek 4b and the € 21 BTW that goes with it. If you may deduct BTW on that purchase, which is normal for a business cost, you also include the € 21 as voorbelasting in rubriek 5b. The two cancel out. The point of the system is not to make you pay more, but to make the purchase visible in the right country.",
          nl: "Stel, u betaalt € 100 voor een abonnement bij een Iers bedrijf. U geeft € 100 aan in rubriek 4b, met de € 21 BTW die daarbij hoort. Mag u de BTW op die aankoop aftrekken, wat bij een zakelijke kostenpost normaal is, dan neemt u de € 21 ook op als voorbelasting in rubriek 5b. De twee heffen elkaar op. Het systeem is er niet om u meer te laten betalen, maar om de aankoop zichtbaar te maken in het juiste land.",
        },
      },
      {
        type: "h2",
        text: { en: "What the invoice must show", nl: "Wat er op de factuur moet staan" },
      },
      {
        type: "p",
        text: {
          en: "A reverse-charge invoice should state 'BTW verlegd' or 'reverse charge' and carry your BTW-identificatienummer as well as the supplier's. If yours is missing, ask the supplier for a corrected invoice.",
          nl: "Een factuur met verlegde BTW vermeldt 'btw verlegd' of 'reverse charge' en bevat uw btw-identificatienummer en dat van de leverancier. Ontbreekt het uwe, vraag dan om een gecorrigeerde factuur.",
        },
      },
      {
        type: "tip",
        text: {
          en: "When Boeklite reads 'BTW verlegd' on a receipt, it books both sides for you, the BTW you owe and the BTW you may deduct, so the return comes out right without extra work.",
          nl: "Leest Boeklite 'btw verlegd' op een bon, dan boekt het beide kanten voor u, de BTW die u verschuldigd bent en de BTW die u mag aftrekken, zodat de aangifte zonder extra werk klopt.",
        },
      },
    ],
  },
  {
    slug: "bankafschrift-camt053-mt940-csv",
    category: "bank",
    published: "2026-07-22",
    title: {
      en: "CAMT.053, MT940 or CSV: which bank file to import",
      nl: "CAMT.053, MT940 of CSV: welk bankbestand importeert u?",
    },
    summary: {
      en: "Three formats, one statement. Which to choose, where to find it in your internet banking, and how to check the import is complete.",
      nl: "Drie formaten, één afschrift. Welke u kiest, waar u het vindt in uw internetbankieren, en hoe u controleert of de import compleet is.",
    },
    body: [
      {
        type: "p",
        text: {
          en: "Your bank can give you the same transactions in several file formats. Bookkeeping software reads them all, but they are not equally good. Here is the difference, in order of preference.",
          nl: "Uw bank kan dezelfde transacties in verschillende bestandsformaten leveren. Boekhoudsoftware leest ze allemaal, maar ze zijn niet even goed. Dit is het verschil, in volgorde van voorkeur.",
        },
      },
      { type: "h2", text: same("CAMT.053") },
      {
        type: "p",
        text: {
          en: "The modern standard, based on ISO 20022 and written in XML. It carries the most structured detail: the counterparty's name and IBAN, the payment reference and the description in separate fields. That makes automatic matching to invoices much more reliable. Choose this one when your bank offers it.",
          nl: "De moderne standaard, gebaseerd op ISO 20022 en geschreven in XML. Het bevat de meeste gestructureerde details: naam en IBAN van de tegenpartij, het betalingskenmerk en de omschrijving in aparte velden. Daardoor is het automatisch koppelen aan facturen veel betrouwbaarder. Kies dit formaat als uw bank het aanbiedt.",
        },
      },
      { type: "h2", text: same("MT940") },
      {
        type: "p",
        text: {
          en: "The older SWIFT format, plain text, used by banks for decades. It works, but some details arrive squashed together in one description line. Several banks are moving away from it in favour of CAMT.053.",
          nl: "Het oudere SWIFT-formaat, platte tekst, al tientallen jaren in gebruik bij banken. Het werkt, maar sommige details komen samengeperst in één omschrijvingsregel binnen. Meerdere banken stappen ervan af, ten gunste van CAMT.053.",
        },
      },
      { type: "h2", text: same("CSV") },
      {
        type: "p",
        text: {
          en: "A spreadsheet export. Every bank uses its own columns, and some use a different character set, which is how 'Privé' turns into nonsense in the wrong software. Fine as a fallback, but the structured formats above are better.",
          nl: "Een spreadsheet-export. Elke bank gebruikt eigen kolommen, en sommige een andere tekenset, en zo verandert 'Privé' in onzin in de verkeerde software. Prima als terugvaloptie, maar de gestructureerde formaten hierboven zijn beter.",
        },
      },
      { type: "h2", text: { en: "Where to find it", nl: "Waar u het vindt" } },
      {
        type: "p",
        text: {
          en: "In your business internet banking, look for 'download transactions' or 'export', choose the account and the period, and pick the format. The exact menu differs per bank, but every Dutch business bank has one.",
          nl: "Zoek in uw zakelijke internetbankieren naar 'transacties downloaden' of 'exporteren', kies de rekening en de periode, en kies het formaat. Het precieze menu verschilt per bank, maar elke Nederlandse zakelijke bank heeft het.",
        },
      },
      { type: "h2", text: { en: "Check the import", nl: "Controleer de import" } },
      {
        type: "ul",
        items: [
          {
            en: "Import whole periods, a month or a quarter at a time, without gaps.",
            nl: "Importeer hele perioden, een maand of kwartaal tegelijk, zonder gaten.",
          },
          {
            en: "Compare the balance in your bookkeeping with the closing balance on the statement.",
            nl: "Vergelijk het saldo in uw boekhouding met het eindsaldo op het afschrift.",
          },
          {
            en: "Overlap is not a problem in good software: lines already imported should be recognised and skipped.",
            nl: "Overlap is in goede software geen probleem: regels die al zijn ingelezen, worden herkend en overgeslagen.",
          },
        ],
      },
      {
        type: "tip",
        text: {
          en: "Boeklite reads CAMT.053, MT940 and the CSV exports of ING, Rabobank, bunq, Knab and ABN AMRO. A line imported twice is never booked twice.",
          nl: "Boeklite leest CAMT.053, MT940 en de CSV-exports van ING, Rabobank, bunq, Knab en ABN AMRO. Een regel die twee keer wordt ingelezen, wordt nooit dubbel geboekt.",
        },
      },
    ],
  },
  {
    slug: "e-facturen-peppol-vida",
    category: "invoicing",
    published: "2026-07-08",
    title: {
      en: "E-invoicing is coming: what ViDA means for your business",
      nl: "E-facturen komen eraan: wat ViDA betekent voor uw bedrijf",
    },
    summary: {
      en: "A PDF by email is not an e-invoice. What the EU's 'VAT in the Digital Age' package changes, and what you can do now.",
      nl: "Een pdf per e-mail is geen e-factuur. Wat het Europese pakket 'VAT in the Digital Age' verandert, en wat u nu al kunt doen.",
    },
    body: [
      {
        type: "p",
        text: {
          en: "Most invoices today are PDFs sent by email. They look digital, but a person still has to read them. An e-invoice is different: structured data, readable by software, sent from one system to another without anyone typing anything over.",
          nl: "De meeste facturen zijn vandaag pdf's die per e-mail worden verstuurd. Ze lijken digitaal, maar iemand moet ze nog steeds lezen. Een e-factuur is anders: gestructureerde gegevens, leesbaar voor software, van het ene systeem naar het andere verstuurd zonder dat iemand iets overtypt.",
        },
      },
      { type: "h2", text: { en: "What ViDA is", nl: "Wat ViDA is" } },
      {
        type: "p",
        text: {
          en: "'VAT in the Digital Age' (ViDA) is a package of EU rules adopted in 2025. Its best-known part makes structured e-invoicing and digital reporting the norm for business-to-business transactions between EU countries, from July 2030. It also allows member states to require e-invoices for domestic transactions.",
          nl: "'VAT in the Digital Age' (ViDA) is een pakket Europese regels dat in 2025 is aangenomen. Het bekendste onderdeel maakt gestructureerde e-facturen en digitale rapportage de norm voor zakelijke transacties tussen EU-landen, vanaf juli 2030. Het staat lidstaten ook toe e-facturen te verplichten voor binnenlandse transacties.",
        },
      },
      { type: "h2", text: { en: "Where Peppol fits in", nl: "Waar Peppol in past" } },
      {
        type: "p",
        text: {
          en: "Peppol is a network for exchanging e-invoices, a bit like email but for structured business documents. You connect once, through your software, and can then send to anyone else on the network. Dutch government bodies already receive e-invoices this way, and the invoice itself follows the European standard EN 16931.",
          nl: "Peppol is een netwerk voor het uitwisselen van e-facturen, een beetje zoals e-mail, maar voor gestructureerde zakelijke documenten. U sluit één keer aan, via uw software, en kunt daarna versturen aan iedereen op het netwerk. Nederlandse overheidsorganisaties ontvangen e-facturen al op deze manier, en de factuur zelf volgt de Europese norm EN 16931.",
        },
      },
      { type: "h2", text: { en: "What you can do now", nl: "Wat u nu al kunt doen" } },
      {
        type: "ul",
        items: [
          {
            en: "Keep your customer data clean: legal name, address, KvK number and BTW-identificatienummer.",
            nl: "Houd uw klantgegevens op orde: statutaire naam, adres, KvK-nummer en btw-identificatienummer.",
          },
          {
            en: "Ask your larger customers whether they already receive invoices over Peppol, and note their Peppol ID.",
            nl: "Vraag uw grotere klanten of zij al facturen via Peppol ontvangen, en noteer hun Peppol-ID.",
          },
          {
            en: "Use invoicing software that numbers invoices without gaps and checks the statutory fields; an e-invoice cannot be fixed by hand afterwards.",
            nl: "Gebruik factuursoftware die doorlopend nummert en de wettelijke gegevens controleert; een e-factuur kunt u achteraf niet met de hand repareren.",
          },
        ],
      },
      {
        type: "p",
        text: {
          en: "There is no need to panic about 2030. But businesses that get their invoice data structured now will find the switch to e-invoicing a setting, not a project.",
          nl: "Er is geen reden voor paniek over 2030. Maar wie zijn factuurgegevens nu al gestructureerd vastlegt, merkt straks dat de overstap op e-facturen een instelling is, geen project.",
        },
      },
      {
        type: "tip",
        text: {
          en: "Boeklite already stores a Peppol ID per customer and sends invoices as PDF by email today. Sending over Peppol is on the way, and will use the same invoices you create now.",
          nl: "Boeklite slaat nu al per klant een Peppol-ID op en verstuurt facturen vandaag als pdf per e-mail. Versturen via Peppol is onderweg, en gebruikt dezelfde facturen die u nu al maakt.",
        },
      },
    ],
  },
];

export const ARTICLES_PAGE = {
  seo: {
    title: {
      en: "Articles about Dutch bookkeeping and BTW | Boeklite",
      nl: "Artikelen over boekhouden en BTW | Boeklite",
    },
    description: {
      en: "Plain-language articles on BTW deadlines, keeping receipts, double-entry bookkeeping, reverse charge, bank files and e-invoicing.",
      nl: "Heldere artikelen over BTW-deadlines, bonnen bewaren, dubbel boekhouden, verlegde BTW, bankbestanden en e-facturen.",
    },
  },
  hero: {
    eyebrow: { en: "Articles", nl: "Artikelen" },
    lead: { en: "Bookkeeping, explained", nl: "Boekhouden, uitgelegd" },
    mark: { en: "in plain words.", nl: "in gewone woorden." },
    body: {
      en: "Short, practical articles on the rules every Dutch business runs into, from BTW deadlines to e-invoicing.",
      nl: "Korte, praktische artikelen over de regels waar elke Nederlandse ondernemer mee te maken krijgt, van BTW-deadlines tot e-facturen.",
    },
  },
  all: { en: "All", nl: "Alles" },
  filter: { en: "Filter by topic", nl: "Filter op onderwerp" },
  minutes: { en: "{count} min read", nl: "{count} min lezen" },
  back: { en: "All articles", nl: "Alle artikelen" },
  related: { en: "Keep reading", nl: "Verder lezen" },
  inBoeklite: { en: "In Boeklite", nl: "In Boeklite" },
  disclaimer: {
    en: "This article describes the general rules. It is not tax advice for your situation; when in doubt, ask your accountant or the Belastingdienst.",
    nl: "Dit artikel beschrijft de algemene regels. Het is geen belastingadvies voor uw situatie; twijfelt u, vraag het dan aan uw accountant of de Belastingdienst.",
  },
  ctaTitle: { en: "Let the rules work for you.", nl: "Laat de regels voor u werken." },
  ctaBody: {
    en: "Boeklite applies them as you book. Start free for 30 days.",
    nl: "Boeklite past ze toe terwijl u boekt. Probeer 30 dagen gratis.",
  },
} as const;

/** Reading time from the words in the body, at 200 words a minute, rounded up. */
export function readingMinutes(article: Article, language: "en" | "nl"): number {
  const words = article.body
    .flatMap((block) =>
      block.type === "ul"
        ? block.items.map((item) => item[language])
        : block.type === "table"
          ? block.rows.flat().map((cell) => cell[language])
          : [block.text[language]],
    )
    .join(" ")
    .split(/\s+/)
    .filter(Boolean).length;
  return Math.max(1, Math.ceil(words / 200));
}
