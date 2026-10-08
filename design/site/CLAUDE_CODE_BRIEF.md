# Brief: build the Boeklite marketing site

This folder is a working static prototype of the public website. Open `index.html` in a browser to click through it. Rebuild it in the project's own frontend stack, keeping the look and structure.

## Pages
| File | Route | Purpose |
|---|---|---|
| `index.html` | `/` | Landing page |
| `product.html` | `/product` | All features, with anchors `#capture #bank #grootboek #btw #invoicing #reports #mobile` |
| `accountants.html` | `/accountants` | Accounting firms |
| `pricing.html` | `/pricing` | Plans, monthly/yearly toggle, comparison table, FAQ |
| `security.html` | `/security` | Security and privacy |
| `demo.html` | `/demo` | Demo request form (also used as Contact) |

## Landing page sections, in order
1. Sticky header: logo, Product mega menu (6 modules), For accountants, Pricing, Security, EN/NL switch, Log in, "Start free".
2. Forest hero: eyebrow, headline with "automatically." in lemon, lead, "Start free for 30 days" (lemon) + "Book a demo" (outline), reassurance row, product mockup in a browser frame.
3. Proof strip: four product principles.
4. Audience switch (tabs): entrepreneurs / accountants, three cards each.
5. Three alternating feature rows: capture, bank, BTW. Each: eyebrow, H2, lead, three ticks, link, visual.
6. How it works: three numbered steps on forest.
7. Integrations: bank and payment names as text pills.
8. Pricing teaser: three plans, the middle one featured.
9. FAQ accordion (`<details>`).
10. CTA band, then footer with four link columns and a legal row.

## Build rules
- Styling comes from `assets/tokens.css` (design tokens, same names as the app design system) and `assets/site.css`. Move the tokens into the project's theme (CSS variables or Tailwind theme). Never hard-code hex values in components.
- One font: Inter via Google Fonts, already imported in `site.css`. All numbers use `tabular-nums`.
- Turn repeated markup into components: Header + MegaMenu, Hero, ProofStrip, AudienceTabs, FeatureRow, Steps, LogoPills, PlanCard, CompareTable, Faq, CtaBand, Footer, and the product mockups (AppMockup, ReceiptCard, JournalEntryCard, BankLines, BtwCard, ClientsTable, PhoneMock).
- The product mockups are HTML, not images. Keep them that way so they stay sharp and translatable.
- The marketing site is light theme only (`data-theme="light"` on `<html>`).
- Responsive breakpoints in `site.css`: 1080px (hamburger menu), 860px (single column), 560px. Match `screenshots/index-mobile.png` on phones.
- Accessibility: keep the tab roles and `aria-*` attributes, the visible focus ring, label every form field, and keep text contrast at 4.5:1 or more.
- Add EN and NL copy through the project's i18n. English is written; Dutch translations are still needed. Accounting terms (grootboek, BTW-aangifte, KvK) stay in Dutch in both languages.
- Wire "Start free" to registration, "Log in" to the app sign-in, and the demo form to the backend or a form service.
- Add per-page `<title>`, meta description, Open Graph tags, `sitemap.xml` and `robots.txt`.

## Placeholders to confirm before launch
Everything below is example content. Check it against the real product and business before going live.
- Prices (€12 / €24, yearly €10 / €20) and plan contents.
- Integration names (ING, Rabobank, ABN AMRO, bunq, Knab, Triodos, Mollie, Stripe, Peppol): list only what actually works.
- Security claims (EU hosting, encryption at rest, daily backups, data processing agreement) and features such as Peppol, iDEAL links, KOR support and the mobile app.
- Company name "Boeklite B.V.", email hello@boeklite.nl and the footer link targets.
- There are no testimonials, customer logos or usage numbers on purpose. Add them only once they are real.

## Reference
`screenshots/` holds full-page renders of every page with the real font. Work page by page and compare against them.
