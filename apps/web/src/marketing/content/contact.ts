import type { L } from "../l10n";

/** The address the demo form writes to. A placeholder the brief asks to confirm before launch. */
export const CONTACT_EMAIL = "hello@boeklite.nl";

/** `/demo`, also the contact page (design/site/demo.html). */
export const DEMO = {
  seo: {
    title: { en: "Book a demo | Boeklite", nl: "Demo aanvragen | Boeklite" },
    description: {
      en: "A 30-minute call with a product specialist: receipt capture, bank matching and the BTW-aangifte, with your own questions.",
      nl: "Een gesprek van 30 minuten met een productspecialist: bonnen vastleggen, bankkoppeling en de BTW-aangifte, met uw eigen vragen.",
    },
  },
  hero: {
    eyebrow: { en: "Book a demo", nl: "Demo aanvragen" },
    lead: { en: "See Boeklite with", nl: "Bekijk Boeklite met" },
    mark: { en: "your own questions.", nl: "uw eigen vragen." },
    body: {
      en: "A 30-minute call with a product specialist. We will show you capture, bank matching and the BTW-aangifte.",
      nl: "Een gesprek van 30 minuten met een productspecialist. Wij laten u vastleggen, bankkoppeling en de BTW-aangifte zien.",
    },
  },
  form: {
    title: { en: "Tell us about you", nl: "Vertel ons over u" },
    name: { en: "Name", nl: "Naam" },
    company: { en: "Company", nl: "Bedrijf" },
    email: { en: "Work email", nl: "Zakelijk e-mailadres" },
    phone: { en: "Phone (optional)", nl: "Telefoon (optioneel)" },
    role: { en: "I am", nl: "Ik ben" },
    roles: [
      { en: "An entrepreneur (ZZP, eenmanszaak)", nl: "Ondernemer (zzp, eenmanszaak)" },
      {
        en: "A BV owner or finance lead",
        nl: "Eigenaar van een BV of financieel verantwoordelijke",
      },
      { en: "An accountant or bookkeeper", nl: "Accountant of boekhouder" },
    ] as readonly L[],
    message: { en: "What would you like to see?", nl: "Wat wilt u graag zien?" },
    submit: { en: "Request a demo", nl: "Demo aanvragen" },
    privacy: {
      en: "We use your details only to plan the demo.",
      nl: "Wij gebruiken uw gegevens alleen om de demo in te plannen.",
    },
    required: {
      en: "Fill in your name, company and email address.",
      nl: "Vul uw naam, bedrijf en e-mailadres in.",
    },
    invalidEmail: {
      en: "That email address does not look right.",
      nl: "Dat e-mailadres lijkt niet te kloppen.",
    },
    sentTitle: { en: "Your email is ready to send.", nl: "Uw e-mail staat klaar." },
    sentBody: {
      en: "Your mail app opened with your request to {email}. Send it and we will reply within one working day. Did nothing open? Write to us at {email}.",
      nl: "Uw mailprogramma is geopend met uw aanvraag aan {email}. Verstuur hem en wij reageren binnen één werkdag. Ging er niets open? Mail ons op {email}.",
    },
    subject: { en: "Demo request: {company}", nl: "Demo-aanvraag: {company}" },
  },
  side: {
    title: { en: "What to expect", nl: "Wat u kunt verwachten" },
    ticks: [
      { en: "A call at a time that suits you", nl: "Een gesprek op een moment dat u uitkomt" },
      {
        en: "Your questions answered, not a script",
        nl: "Antwoord op uw vragen, geen vast verhaal",
      },
      { en: "A free 30-day account afterwards", nl: "Daarna een gratis account voor 30 dagen" },
    ] as readonly L[],
    tryTitle: { en: "Rather try it yourself?", nl: "Liever zelf proberen?" },
    tryBody: {
      en: "Start a free account and set up your own administration in minutes.",
      nl: "Maak een gratis account aan en richt uw eigen administratie in een paar minuten in.",
    },
    otherTitle: { en: "Other questions", nl: "Andere vragen" },
  },
} as const;

/** Privacy, cookies and responsible disclosure: short, factual, and marked for legal review. */
export const LEGAL = {
  privacy: {
    seo: {
      title: { en: "Privacy | Boeklite", nl: "Privacy | Boeklite" },
      description: {
        en: "How Boeklite handles your personal data.",
        nl: "Hoe Boeklite met uw persoonsgegevens omgaat.",
      },
    },
    title: { en: "Privacy", nl: "Privacy" },
    intro: {
      en: "This page explains, in plain words, which personal data Boeklite processes and why.",
      nl: "Deze pagina legt in gewone woorden uit welke persoonsgegevens Boeklite verwerkt en waarom.",
    },
    sections: [
      {
        title: { en: "What we process", nl: "Wat wij verwerken" },
        body: {
          en: "Your account details (name, email address, sign-in methods), the administration you keep in Boeklite (entries, receipts, invoices, bank statements) and the technical data needed to keep the service secure, such as sign-in times.",
          nl: "Uw accountgegevens (naam, e-mailadres, inlogmethoden), de administratie die u in Boeklite bijhoudt (boekingen, bonnen, facturen, bankafschriften) en de technische gegevens die nodig zijn om de dienst veilig te houden, zoals inlogtijden.",
        },
      },
      {
        title: { en: "Why", nl: "Waarom" },
        body: {
          en: "To provide the bookkeeping service you signed up for, to meet legal obligations such as the seven-year retention of financial records, and to keep your account secure. We do not sell data and we do not use it for advertising.",
          nl: "Om de boekhouddienst te leveren waarvoor u zich aanmeldde, om aan wettelijke plichten te voldoen zoals de bewaarplicht van zeven jaar voor financiële gegevens, en om uw account te beveiligen. Wij verkopen geen gegevens en gebruiken ze niet voor reclame.",
        },
      },
      {
        title: { en: "Where", nl: "Waar" },
        body: {
          en: "In data centres in the European Union. Documents are encrypted with a key per administration.",
          nl: "In datacenters in de Europese Unie. Documenten worden versleuteld met een sleutel per administratie.",
        },
      },
      {
        title: { en: "Your rights", nl: "Uw rechten" },
        body: {
          en: "You can ask to see, correct or export your data, and to remove it where the law does not require us to keep it. Write to {email}.",
          nl: "U kunt vragen uw gegevens in te zien, te corrigeren of te exporteren, en ze te verwijderen waar de wet ons niet verplicht ze te bewaren. Mail naar {email}.",
        },
      },
    ],
  },
  cookies: {
    seo: {
      title: { en: "Cookies | Boeklite", nl: "Cookies | Boeklite" },
      description: {
        en: "What Boeklite stores in your browser.",
        nl: "Wat Boeklite in uw browser opslaat.",
      },
    },
    title: { en: "Cookies and browser storage", nl: "Cookies en browseropslag" },
    intro: {
      en: "Boeklite uses no advertising or tracking cookies, and this site loads nothing from third parties.",
      nl: "Boeklite gebruikt geen advertentie- of trackingcookies, en deze site laadt niets van derden.",
    },
    sections: [
      {
        title: { en: "What is stored", nl: "Wat er wordt opgeslagen" },
        body: {
          en: "Only what the app needs to work: your sign-in session, your choice of language and theme, and on a phone, receipts captured offline (encrypted) until they are sent.",
          nl: "Alleen wat de app nodig heeft om te werken: uw inlogsessie, uw keuze voor taal en thema, en op een telefoon de offline vastgelegde bonnen (versleuteld) tot ze verstuurd zijn.",
        },
      },
      {
        title: { en: "Why there is no banner", nl: "Waarom er geen banner is" },
        body: {
          en: "Storage that is strictly necessary for a service you asked for needs no consent. We keep it that way: nothing on this site follows you around.",
          nl: "Opslag die strikt noodzakelijk is voor een dienst waar u om vroeg, heeft geen toestemming nodig. Dat houden wij zo: niets op deze site volgt u.",
        },
      },
    ],
  },
  disclosure: {
    seo: {
      title: { en: "Responsible disclosure | Boeklite", nl: "Responsible disclosure | Boeklite" },
      description: {
        en: "How to report a security issue in Boeklite.",
        nl: "Zo meldt u een beveiligingsprobleem in Boeklite.",
      },
    },
    title: { en: "Responsible disclosure", nl: "Responsible disclosure" },
    intro: {
      en: "Found a weakness in Boeklite? Thank you for telling us. We take every report seriously.",
      nl: "Een zwakke plek in Boeklite gevonden? Dank dat u het ons laat weten. Wij nemen elke melding serieus.",
    },
    sections: [
      {
        title: { en: "How to report", nl: "Zo meldt u het" },
        body: {
          en: "Email {email} with a description of the issue and the steps to reproduce it. Do not include other people's data.",
          nl: "Mail {email} met een beschrijving van het probleem en de stappen om het na te bootsen. Neem geen gegevens van anderen op.",
        },
      },
      {
        title: { en: "What we ask", nl: "Wat wij vragen" },
        body: {
          en: "Test only with your own account and administration, do not access, change or delete data that is not yours, do not degrade the service, and give us reasonable time to fix the issue before you share it.",
          nl: "Test alleen met uw eigen account en administratie, bekijk, wijzig of verwijder geen gegevens die niet van u zijn, belast de dienst niet, en geef ons redelijk de tijd om het op te lossen voordat u het deelt.",
        },
      },
      {
        title: { en: "What we promise", nl: "Wat wij beloven" },
        body: {
          en: "We reply within three working days, keep you informed while we fix it, and will not take legal action against research done within these rules.",
          nl: "Wij reageren binnen drie werkdagen, houden u op de hoogte terwijl wij het oplossen, en ondernemen geen juridische stappen tegen onderzoek binnen deze regels.",
        },
      },
    ],
  },
} as const;
