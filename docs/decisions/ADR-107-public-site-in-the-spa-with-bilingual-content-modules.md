# ADR-107: The public site lives in the SPA, with its copy in bilingual content modules

- **Status**: Accepted
- **Date**: 2026-10-08
- **Supersedes**: the `/welcome` page of ADR-080

## Context

`design/site/` is a static prototype of the public website: a landing page, product, accountants,
pricing, security and demo pages, with a brief to rebuild it in the product's own stack, in EN and
NL, with per-page titles and meta, a sitemap and robots.txt. Boeklite also wanted articles. ADR-103
makes boeklite.nl the one origin, and ADR-063 has the API serve the built SPA, so there is no
second web server for a separate marketing build.

Three things had to be decided: where the pages live, how their copy is translated, and what they
may claim.

## Decision

- **The site is part of the web app, lazy-loaded.** Public routes (`/product`, `/accountants`,
  `/pricing`, `/security`, `/demo` and `/contact`, `/articles`, `/articles/:slug`, `/privacy`,
  `/cookies`, `/responsible-disclosure`) sit outside the auth guards. One module
  (`marketing/routes.tsx`) is loaded on first visit to any of them, so a signed-in bookkeeper never
  downloads the site and a reader never downloads the ledger.
- **`/` is the landing page for a visitor who has never signed in in this tab**, and the dashboard
  for everyone else. `RequireAuth` takes a `publicHome`, shown only at `/` and only while
  `AuthProvider.wasSignedIn` is false, so signing out and an expired session still land on the
  sign-in screen with their notice, and every other protected URL still redirects to `/login`.
  `/welcome` redirects to `/`.
- **Copy lives in typed bilingual content modules** (`marketing/content/*.ts`): every string is
  `{ en, nl }`, so TypeScript refuses a page with a missing Dutch half, and a test refuses an empty
  one. The product's interface strings stay in the catalogue; long-form pages and articles, where a
  paragraph is the unit and reading the Dutch beside the English is how a translation is checked,
  do not fit one-key-per-string. The language is the app's own (`useI18n`), shared with the sign-in
  screen. No literal text is written in JSX, so `check_translations.py` still holds.
- **Claims are held to what is built.** The prototype promised a PSD2 feed, iDEAL links, Peppol,
  Digipoort filing, KvK auto-fill and email-forwarded receipts. None is live, so the copy says what
  works (statement import, PDF invoices, a return prepared in Boeklite and filed in Mijn
  Belastingdienst) and lists PSD2, Peppol and Digipoort under "On the way". The accountant page
  shows the real portfolio, not a deadline table that does not exist.
- **Light theme only, isolated styles.** `marketing.css` prefixes every class `mk-` and pins the
  light palette on its root above the OS dark-mode block. Its resets are `:where()`, so they never
  outweigh a component class.
- **SEO**: `useSeo` sets title, description, canonical and Open Graph per page; `public/sitemap.xml`
  lists every public page and article (a test keeps it in step) and `public/robots.txt` keeps
  crawlers out of the app and the API.

## Alternatives considered

| Option                                            | Rejected because                                                                                                                                     |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| A separate static or SSR marketing build          | A second build, deploy and server for one origin (ADR-103); the SPA already serves every path. Revisit if search ranking needs server-rendered HTML. |
| All marketing copy in the i18n catalogue          | Hundreds of keys for paragraphs and articles, and translations read out of context.                                                                  |
| English-only copy, as the old `/welcome` page had | The product is bilingual by requirement (FR-LOC-001); the site would be the one place it is not.                                                     |
| Keep the prototype's feature claims               | They describe features that are not built; a customer would buy something that is not there.                                                         |

## Consequences

- The site is client-rendered. Crawlers that run JavaScript see each page's own title and text;
  ones that do not see `index.html`. Server rendering is the follow-up if that matters.
- The demo form has no backend: it opens the visitor's mail app with the request filled in,
  addressed to `hello@boeklite.nl`, and says so. A form endpoint replaces `submit` when one exists.
- Placeholders the brief lists still need confirming before launch: the prices and plan limits
  (no billing is built), `hello@boeklite.nl`, and the privacy, cookies and disclosure texts, which
  are short factual drafts for legal review.
- When PSD2, Peppol or Digipoort ship, their "On the way" entries move into the feature copy.
