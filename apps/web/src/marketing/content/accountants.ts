import type { L } from "../l10n";

/** `/accountants` (design/site/accountants.html), held to the firm features that exist. */
export const ACCOUNTANTS = {
  seo: {
    title: {
      en: "For accountants: review, don't re-enter | Boeklite",
      nl: "Voor accountants: controleren, niet overtypen | Boeklite",
    },
    description: {
      en: "Your clients capture and approve. You review, correct and file, in the same administration. Boeklite for accounting firms.",
      nl: "Uw klanten leggen vast en keuren goed. U controleert, corrigeert en doet aangifte, in dezelfde administratie. Boeklite voor accountantskantoren.",
    },
  },
  hero: {
    eyebrow: { en: "For accountants", nl: "Voor accountants" },
    lead: { en: "Review your clients' books.", nl: "Controleer de boeken van uw klanten." },
    mark: { en: "Stop re-entering them.", nl: "Stop met overtypen." },
    body: {
      en: "Your clients capture and approve. You review, correct and file, all in the same administration.",
      nl: "Uw klanten leggen vast en keuren goed. U controleert, corrigeert en doet aangifte, allemaal in dezelfde administratie.",
    },
    secondary: { en: "Firm pricing", nl: "Prijzen voor kantoren" },
  },
  pillars: [
    {
      icon: "users",
      title: { en: "Client portfolio", nl: "Klantportefeuille" },
      body: {
        en: "Every client administration you work in, in one list.",
        nl: "Elke klantadministratie waarin u werkt, in één lijst.",
      },
    },
    {
      icon: "eye",
      title: { en: "Review in place", nl: "Ter plekke controleren" },
      body: {
        en: "Pre-booked purchases with the source attached.",
        nl: "Voorgeboekte inkopen met de bron erbij.",
      },
    },
    {
      icon: "percent",
      title: { en: "BTW per client", nl: "BTW per klant" },
      body: {
        en: "Each client's return prepared from their entries.",
        nl: "De aangifte van elke klant opgesteld uit diens boekingen.",
      },
    },
    {
      icon: "shield",
      title: { en: "Never the wrong client", nl: "Nooit de verkeerde klant" },
      body: {
        en: "The open client is named and coloured on every screen.",
        nl: "De geopende klant staat met naam en kleur op elk scherm.",
      },
    },
  ],
  sections: [
    {
      id: "portfolio",
      eyebrow: { en: "Client portfolio", nl: "Klantportefeuille" },
      title: { en: "Every client, one list.", nl: "Elke klant, één lijst." },
      body: {
        en: "Open the day with your portfolio: each client administration with its KvK number and your role in it. Filter as you type and open a client in one click, or jump between clients from the keyboard.",
        nl: "Begin de dag met uw portefeuille: elke klantadministratie met KvK-nummer en uw rol erin. Filter terwijl u typt en open een klant met één klik, of spring tussen klanten met het toetsenbord.",
      },
      ticks: [
        {
          en: "Add a new client with the same onboarding a business uses",
          nl: "Voeg een nieuwe klant toe met dezelfde onboarding als een ondernemer",
        },
        {
          en: "Ctrl+K to switch client from anywhere",
          nl: "Ctrl+K om vanaf elk scherm van klant te wisselen",
        },
        {
          en: "Your role per client decides what you can do there",
          nl: "Uw rol per klant bepaalt wat u daar kunt doen",
        },
      ] as readonly L[],
      media: "clients",
    },
    {
      id: "review",
      eyebrow: { en: "Review workflow", nl: "Controleren" },
      title: {
        en: "Approve in seconds, correct in place.",
        nl: "Goedkeuren in seconden, ter plekke corrigeren.",
      },
      body: {
        en: "Purchases arrive pre-booked with the receipt or invoice beside the proposed entry. Approve it, change the account, or correct a booked entry with a reversing entry that stays linked to the original.",
        nl: "Inkopen komen voorgeboekt binnen, met de bon of factuur naast de voorgestelde boeking. Keur goed, wijzig de rekening, of corrigeer een geboekte post met een tegenboeking die aan het origineel gekoppeld blijft.",
      },
      ticks: [
        {
          en: "Source document next to every proposed entry",
          nl: "Brondocument naast elke voorgestelde boeking",
        },
        {
          en: "Duplicate invoice numbers caught before booking",
          nl: "Dubbele factuurnummers opgemerkt vóór het boeken",
        },
        {
          en: "Corrections are new entries, never edits",
          nl: "Correcties zijn nieuwe boekingen, nooit wijzigingen",
        },
      ] as readonly L[],
      media: "review",
    },
    {
      id: "btw",
      eyebrow: { en: "BTW per client", nl: "BTW per klant" },
      title: {
        en: "Prepare the return for every client.",
        nl: "Stel voor elke klant de aangifte op.",
      },
      body: {
        en: "Each client's return is prepared from their own entries, with every amount traceable. File it, record the Belastingdienst's reference, and the period is locked with the return as filed.",
        nl: "De aangifte van elke klant wordt opgesteld uit diens eigen boekingen, met elk bedrag herleidbaar. Dien in, leg het kenmerk van de Belastingdienst vast, en de periode gaat op slot met de aangifte zoals ingediend.",
      },
      ticks: [
        { en: "Rubrieken prepared and traceable", nl: "Rubrieken opgesteld en herleidbaar" },
        {
          en: "Checks for unbooked receipts before filing",
          nl: "Controle op ongeboekte bonnen vóór het indienen",
        },
        { en: "Filed returns kept per client", nl: "Ingediende aangiften bewaard per klant" },
      ] as readonly L[],
      media: "btw",
    },
  ],
  faqTitle: { en: "Questions from firms.", nl: "Vragen van kantoren." },
  faq: [
    {
      q: { en: "How do I start as a firm?", nl: "Hoe begin ik als kantoor?" },
      a: {
        en: "Sign up and choose 'an accounting firm'. Your portfolio starts empty; add your first client and set up their administration in a few minutes.",
        nl: "Meld u aan en kies 'een accountantskantoor'. Uw portefeuille begint leeg; voeg uw eerste klant toe en richt de administratie in een paar minuten in.",
      },
    },
    {
      q: {
        en: "Can clients keep using their own package?",
        nl: "Kunnen klanten hun eigen pakket blijven gebruiken?",
      },
      a: {
        en: "Boeklite works best when the client books in Boeklite. To start, enter the opening balance from their previous package and import this year's bank statements.",
        nl: "Boeklite werkt het best als de klant in Boeklite boekt. Om te beginnen voert u de beginbalans uit het vorige pakket in en importeert u de bankafschriften van dit jaar.",
      },
    },
    {
      q: {
        en: "Who pays for the client's subscription?",
        nl: "Wie betaalt het abonnement van de klant?",
      },
      a: {
        en: "Either. The firm can pay for clients on a firm plan, or clients pay their own plan. Ask us about firm pricing.",
        nl: "Allebei kan. Het kantoor kan klanten op een kantoorabonnement nemen, of klanten betalen hun eigen abonnement. Vraag ons naar de kantoorprijzen.",
      },
    },
    {
      q: {
        en: "Can I limit what a team member can do?",
        nl: "Kan ik beperken wat een medewerker kan doen?",
      },
      a: {
        en: "Yes. Access is granted per client with a role, and every action is checked against that role on the server, not just hidden in the screen.",
        nl: "Ja. Toegang wordt per klant verleend met een rol, en elke handeling wordt op de server tegen die rol gecontroleerd, niet alleen in het scherm verborgen.",
      },
    },
  ],
  cta: {
    title: {
      en: "Bring your first five clients across.",
      nl: "Breng uw eerste vijf klanten over.",
    },
    body: {
      en: "Book a demo and we will help you set them up.",
      nl: "Vraag een demo aan en wij helpen u ze in te richten.",
    },
  },
} as const;

/** `/security` (design/site/security.html), held to what is built. */
export const SECURITY = {
  seo: {
    title: { en: "Security and privacy | Boeklite", nl: "Veiligheid en privacy | Boeklite" },
    description: {
      en: "EU hosting, a key per administration, passkey sign-in with mandatory two-step verification, and a ledger that never deletes. How Boeklite protects your books.",
      nl: "Opslag in de EU, een sleutel per administratie, inloggen met passkey en verplichte tweestapsverificatie, en een grootboek dat nooit verwijdert. Zo beschermt Boeklite uw boeken.",
    },
  },
  hero: {
    eyebrow: { en: "Security & privacy", nl: "Veiligheid & privacy" },
    lead: { en: "Your books are", nl: "Uw boeken zijn" },
    mark: { en: "safe here.", nl: "hier veilig." },
    body: {
      en: "Financial data deserves care. This is how we protect yours, in plain words.",
      nl: "Financiële gegevens verdienen zorg. Zo beschermen wij de uwe, in gewone woorden.",
    },
  },
  pillars: [
    {
      icon: "globe",
      title: { en: "Stored in the EU", nl: "Opgeslagen in de EU" },
      body: {
        en: "The application, its database and your documents run in data centres in the European Union.",
        nl: "De applicatie, de database en uw documenten draaien in datacenters in de Europese Unie.",
      },
    },
    {
      icon: "lock",
      title: { en: "A key per administration", nl: "Een sleutel per administratie" },
      body: {
        en: "Receipts and invoices are encrypted with a key of their own administration, itself protected by a managed key service.",
        nl: "Bonnen en facturen worden versleuteld met een sleutel van hun eigen administratie, die zelf door een beheerde sleuteldienst wordt beschermd.",
      },
    },
    {
      icon: "key",
      title: { en: "Passkeys and two steps", nl: "Passkeys en twee stappen" },
      body: {
        en: "Sign in with a passkey, or a password plus an authenticator code. A second factor is required for everyone.",
        nl: "Log in met een passkey, of met wachtwoord plus een code uit uw authenticator-app. Een tweede factor is voor iedereen verplicht.",
      },
    },
    {
      icon: "history",
      title: { en: "Permanent audit trail", nl: "Blijvend audittrail" },
      body: {
        en: "Booked entries cannot be changed or deleted, not even by our own support. A correction is a new entry.",
        nl: "Geboekte posten kunnen niet worden gewijzigd of verwijderd, ook niet door onze eigen support. Een correctie is een nieuwe boeking.",
      },
    },
    {
      icon: "shield",
      title: { en: "Walls between administrations", nl: "Muren tussen administraties" },
      body: {
        en: "Every record carries its administration, and the database itself refuses to return another's, not only the application.",
        nl: "Elk record hoort bij één administratie, en de database zelf weigert die van een ander te tonen, niet alleen de applicatie.",
      },
    },
    {
      icon: "users",
      title: { en: "Roles and access", nl: "Rollen en toegang" },
      body: {
        en: "Access is given per administration with a role, and checked on the server for every request.",
        nl: "Toegang wordt per administratie met een rol gegeven en bij elk verzoek op de server gecontroleerd.",
      },
    },
  ],
  audit: {
    eyebrow: { en: "Audit trail", nl: "Audittrail" },
    title: { en: "Nothing disappears.", nl: "Er verdwijnt niets." },
    body: {
      en: "A booked entry is permanent. A correction is a new entry that reverses the old one, linked so you and your accountant can always see what happened, when, and who did it.",
      nl: "Een geboekte post is blijvend. Een correctie is een nieuwe boeking die de oude terugdraait, gekoppeld zodat u en uw accountant altijd zien wat er gebeurde, wanneer en door wie.",
    },
    ticks: [
      {
        en: "Reversals linked to the original entry",
        nl: "Tegenboekingen gekoppeld aan de oorspronkelijke boeking",
      },
      { en: "Closed periods stay closed", nl: "Afgesloten perioden blijven gesloten" },
      {
        en: "The ledger's integrity can be verified independently",
        nl: "De integriteit van het grootboek is onafhankelijk te controleren",
      },
    ] as readonly L[],
  },
  faqTitle: { en: "Privacy questions", nl: "Vragen over privacy" },
  faq: [
    {
      q: { en: "Is Boeklite AVG / GDPR compliant?", nl: "Voldoet Boeklite aan de AVG?" },
      a: {
        en: "We process personal data under the AVG, store it in the EU and use it only to run your administration. A data processing agreement is available on request.",
        nl: "Wij verwerken persoonsgegevens onder de AVG, slaan ze op in de EU en gebruiken ze alleen om uw administratie te laten werken. Een verwerkersovereenkomst is op aanvraag beschikbaar.",
      },
    },
    {
      q: { en: "Who can see my administration?", nl: "Wie kan mijn administratie zien?" },
      a: {
        en: "Only the people who have been given access to it, such as your accountant. Boeklite support only looks with your permission.",
        nl: "Alleen de mensen die er toegang toe hebben gekregen, zoals uw accountant. Boeklite-support kijkt alleen mee met uw toestemming.",
      },
    },
    {
      q: { en: "Can I export my data?", nl: "Kan ik mijn gegevens exporteren?" },
      a: {
        en: "Yes, at any time, also after your subscription ends. You stay the owner of your books.",
        nl: "Ja, altijd, ook na het einde van uw abonnement. U blijft eigenaar van uw boeken.",
      },
    },
    {
      q: { en: "How long are documents kept?", nl: "Hoe lang worden documenten bewaard?" },
      a: {
        en: "Seven years, the Dutch statutory retention period (bewaarplicht), after which they are removed on schedule.",
        nl: "Zeven jaar, de wettelijke bewaarplicht, waarna ze volgens schema worden verwijderd.",
      },
    },
    {
      q: { en: "How do I report a security issue?", nl: "Hoe meld ik een beveiligingsprobleem?" },
      a: {
        en: "See our responsible disclosure page. We respond to every report.",
        nl: "Zie onze pagina over responsible disclosure. Wij reageren op elke melding.",
      },
    },
  ],
  cta: {
    title: { en: "Questions about security?", nl: "Vragen over veiligheid?" },
    body: {
      en: "Talk to us. We are happy to walk you or your accountant through it.",
      nl: "Neem contact op. Wij lopen het graag met u of uw accountant door.",
    },
  },
} as const;
