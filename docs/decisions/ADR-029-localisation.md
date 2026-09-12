# ADR-029: Dutch and English as equal first-class languages

- **Status**: Accepted
- **Date**: 2026-09-03
- **Implements**: FR-LOC-001, FR-LOC-001a, FR-LOC-001b, FR-LOC-001c, FR-LOC-001d, FR-LOC-002
  (PRD §20)
- **Serves**: IAM-010g / FR-ONB-000 (language selectable before login), FR-UX-007 (no
  developer-facing string reaches a user), MOB-016, FR-LOC-005 (adding a jurisdiction)
- **Constrained by**: NFR-031 / CLAUDE.md rule four (no floating point in the calculation path),
  NFR-032 (idempotency), IAM-005 (tenant-isolation test per endpoint), NFR-044 (migrations)
- **Related**: [ADR-012](ADR-012-appendix-a-as-source-of-truth.md) — why no permission was
  invented for the language endpoint; [ADR-028](ADR-028-fiscal-year-definition.md) — the
  same one-table-two-implementations device, used there for period derivation

## Context

> **FR-LOC-001.** Dutch and English are both first-class from **P0** — not an English product
> with a Dutch translation added later. […] A missing translation is a release blocker, not a
> fallback to English.
>
> **FR-LOC-001b.** Language is a per-user setting, not per-organization.
>
> **FR-LOC-002.** Formatting follows the administration's locale, not the user's UI language, so
> amounts read identically to every user.

The requirement is unusual in what it rules out rather than what it asks for. "Both first-class"
and "not a fallback to English" are statements about failure modes, and the default behaviour of
every mainstream i18n library violates both: parallel per-language files that can drift, English
sentences used as lookup keys, and a fallback chain that renders a missing Dutch string as English
without telling anyone.

The codebase had none of this. Every string in the web app was an English literal, and the API's
error bodies were English sentences with developer-facing identifiers interpolated into them
(`"Not permitted to post journal_entry."`).

## Decision

### 1. Language and formatting locale are two settings, on two tables

The single most consequential decision, and the one FR-LOC-002 forces:

| | Answers | Lives on | Changes when |
|---|---|---|---|
| **Language** | which words a person reads | `users.language` | they click the language control |
| **Formatting locale** | how figures are written | `administration.formatting_locale` | they switch client |

A single `locale` setting cannot express "English words, Dutch numbers", which is the exact case
the requirement is written about. Derived from the reader instead, the same trial balance would
show `1.234,56` to a Dutch bookkeeper and `1,234.56` to an English-speaking owner — and `1.234`
means one thousand two hundred and thirty-four to one of them and one-point-two-three-four to the
other. Two people would read two different numbers off one ledger with no way to notice.

This is enforced by signature, not by discipline: no formatting function takes a `Language`, so a
caller *cannot* make an amount depend on who is reading it.

`users.language` is nullable and `administration.formatting_locale` is not. NULL means "never
chosen", which IAM-010g needs to tell apart from "chose Dutch" when it applies the pre-login device
choice to the account at first login. There is no corresponding unknown state for an
administration: one with no locale cannot render a trial balance.

### 2. One catalogue, one record per message, holding both languages

`packages/i18n/catalogue/*.json`, read by the web app, the mobile apps and the API — not a copy
each.

```json
"client.header.none_selected": { "nl": "Geen klant geselecteerd", "en": "No client selected" }
```

Everything else follows from the shape:

- **A missing translation is a malformed record, not a diff between two files.** You cannot add
  an English string without adding a Dutch one, because there is no English file to add it to.
- **Neither language is the key.** Keys are opaque identifiers. A catalogue keyed by English text
  makes English structurally primary: the Dutch becomes a lookup from it, an English edit silently
  orphans the Dutch, and there is nowhere to put a Dutch phrase with no English equivalent.
- **Identical languages are legal but never silent.** A record whose two languages match must
  declare `"identical": true`. Legitimate for endonyms (`Nederlands`) and statutory terms (`BTW`);
  indistinguishable, without the flag, from pasting the English into the Dutch slot to make a
  build pass.

### 3. Both runtimes raise on a missing string; CI is what makes that safe

`translate` throws rather than falling back, on both sides. This is only defensible because
`scripts/check_translations.py` (FR-LOC-001d) fails the build first — the ordering is the whole
argument. A fallback would hide a missing translation in every environment where somebody might
have noticed it.

The check also scans for hard-coded user-facing text — in the web app's JSX, and in the API's
`HTTPException`/`JSONResponse` bodies. That check, not the catalogue checks, is what keeps the
product from drifting back to "English with translations bolted on": a literal typed into a
component or a response is not a *missing* translation, it is a string that is permanently English
and invisible to every completeness check, because it never reached the catalogue at all. Each
escape hatch is an explicit allowlist entry, and a test asserts both stay short enough to be read.

**Both apps need that scan.** Writing the JSX half first and stopping there let four untranslated
strings survive the original pass — two "no authenticated user" 403s, IAM-019's rate-limit message,
and IAM-010f's "add a passkey or password" prompt, which is an instruction, and an instruction a
person cannot read is not one.

The API rule is scoped to the two response constructors rather than to any `detail=`, because
`AuthorizationDecision.detail` and several domain errors use that name for internal diagnostics
that are correctly English. Three of the first four reports were exactly those, plus docstrings
quoting an example body; a gate that cries wolf gets disabled, and then it protects nothing.

**The gate is itself tested.** `apps/api/tests/i18n/test_check_translations.py` asserts every rule
in both directions — a catalogue that violates it fails, one that does not passes — following the
convention `test_audit_coverage.py` and `test_idempotency_coverage.py` already set for their own
checks. One-directional tests would be satisfied by a checker that rejected everything. Verified by
mutation: neutering `Problems.add` fails 28 of the 51.

### 4. Formatting is a hand-written table checked against one shared case file

`Intl.NumberFormat` and Python's `locale`/`babel` were all rejected. There are two
implementations of the same rules (`packages/i18n/src/format.ts`, `api.i18n.formatting`), because
the API renders the same amounts into PDFs and filings, and "reads identically to every user" is a
claim about both. `packages/i18n/formatting-cases.json` is what makes it checkable: both test
suites run every case in it.

That is the device [ADR-028](ADR-028-fiscal-year-definition.md) uses for period derivation, which
also exists twice (SQL and Python) and is compared against a shared table rather than trusted.

Money crosses this boundary as a `Decimal` or an exact decimal **string**, never a number, and is
taken apart with string operations. `Intl.NumberFormat.format` takes a `number`, and
`numeric(19,2)` reaches 17 integer digits — four beyond what a double represents exactly. Formatting
is the last place NFR-031 can still be lost, and it is the place where losing it looks fine.

### 5. Role names: system roles are translated, custom roles are not

A role's **name is its identifier** — authorization matches on it, audit entries record it, grants
name it — so it crosses the wire untranslated whoever asks. A translated identifier would mean a
grant that reads differently depending on who fetched it, which is a data defect rather than a
localisation feature. Only the **label** is translated.

Which labels get translated is not a judgement the client can make:

| | Name is | Rendered |
|---|---|---|
| The twelve **system** roles of §8.4 | LEDGR's own vocabulary | translated (`roles.json`) |
| A **custom** role (ADR-013, migration 0011) | the organization's own words — tenant data | verbatim |

The API answers this with `role_is_system`, read from `role.is_system`, because only the database
knows. Deciding from whether the name happens to be in the catalogue is wrong in the case that
matters most: an organization is free to name a custom role `Bookkeeper`, and rendering that as
`Boekhouder` would show a Dutch reader a role their organization does not have. A custom role's
name is not ours to reword, for the same reason a client's legal name is not.

`roleLabel` falls back to the raw name for a system role with no catalogue entry. That is the one
fallback in the package, and it is deliberately not the forbidden kind: falling back *between
languages* hides a missing translation from everyone who might notice, while falling back to the
identifier is conspicuous. It exists because a role row can appear in the database without a
deploy. Completeness is enforced where it can be — `tests/i18n/test_role_names.py` walks the real
`matrix.ROLES` and fails the build on a missing, stale or untranslated label.

### 6. Before authentication: the device answers; at first login, the account takes over

IAM-010g's resolution order, and every step of it is load-bearing:

| | Source | Why it outranks the next |
|---|---|---|
| 1 | the signed-in account | FR-LOC-001b makes language a property of the *person*; the same person on a borrowed laptop must not switch language |
| 2 | an explicit choice on this device | this **is** the person overriding their browser, on this very screen — letting the browser win would make the control do nothing next visit |
| 3 | `navigator.languages` | matched on the primary subtag, so `nl-BE` resolves |
| 4 | a `.nl` hostname | deliberately *below* the browser: someone whose browser says English asked for English, and the domain does not know better |
| 5 | `DEFAULT_LANGUAGE` | see Q12 below |

The control lives in `PreAuthScreen`, the frame **both** screens share, so "on the login and signup
screens" is a consequence of the structure rather than something to remember when the second screen
ships. It is a labelled footer rather than an icon in a corner, because this is the one screen in
the product whose reader may not be able to read it — a globe glyph is discoverable only to someone
already looking for one.

**Adopting is not choosing.** At first login `applyAccountLanguage` reads `GET /v1/me/language`;
`null` means never chosen, so the device's choice seeds the account (`PUT`). Anything else means
the account already knows, and the value is written only to the *device* and applied through
`adoptLanguage` rather than `setLanguage`. Routing it through the normal choice path instead —
which is what the first implementation did — writes the value straight back to the place it was
just read from: one pointless round trip per sign-in on every machine that remembered something
else.

The account read is keyed on the *transition* into an authenticated session, not on a "have we done
this yet" flag. A flag would also suppress the **second** sign-in in one page session, leaving a
colleague with the previous person's language — the exact failure FR-LOC-001b exists to prevent.

**`<html lang>` moves with the language.** WCAG 2.2 SC 3.1.1, which FR-LOC-004 commits to, and part
of "immediately" rather than a detail beside it: a switch that repaints the words while the page
still declares itself English has changed the product for sighted users only — a screen reader goes
on pronouncing Dutch with an English synthesiser. `index.html` also ships `lang="nl"` rather than
`en`, which was wrong in both directions.

### 7. Request language comes from `Accept-Language`, not the stored column

The API produces user-facing text in two situations, and they need different answers:

- **request-scoped** (errors, validation) — `Accept-Language`, which the clients set to the
  language the UI is *currently* in. This cannot go stale, so FR-LOC-001a's "immediately, without
  reload or re-authentication" holds on the server too, and no token needs reissuing.
- **server-initiated** (e-mail, push, a scheduled PDF) — `users.language`, because there is no
  request to read a header from.

Every error body is now `{"message": <localised>, "reason": <stable English token>}`. The action
and resource type stay *out* of the sentence: `"Not permitted to post journal_entry"` puts two
developer-facing identifiers inside a user-facing string, which FR-UX-007 forbids, and a client
wanting to branch on the failure would have to parse a translated sentence.

## Alternatives considered

| Option | Rejected because |
|---|---|
| `nl.json` and `en.json` side by side | The standard layout, and the one that makes FR-LOC-001 unachievable: the two drift, and "missing" is a set difference somebody has to remember to compute in both directions rather than a malformed record. |
| English source strings as keys (`t("No client selected")`) | Makes English structurally primary — exactly what "not an English product with a Dutch translation added later" rules out. English edits orphan Dutch strings silently. |
| Fall back to English for a missing Dutch string | Forbidden by FR-LOC-001 in as many words, and the reason is practical: a fallback hides the gap everywhere it could have been noticed. |
| One `locale` setting per user | Cannot express FR-LOC-002 at all. An amount would render differently for two people reading the same ledger. |
| Formatting locale on the organization | An accounting firm has one organization and many clients' books; the locale belongs to the books being read, not to the firm reading them. |
| `Intl` / `babel` / `locale.setlocale` | ICU output changes between versions (the currency space changed character in ICU 72); `Intl.format`'s obvious overload takes a `number`; `setlocale` is process-global and not thread-safe. None of them survives "identically to every user" across two runtimes. |
| A `lang` claim in the session token | Stale until the token is reissued, so switching language would need re-authentication — which FR-LOC-001a explicitly rules out. |
| A language control on each pre-auth screen | Two controls to keep in step, and the forgotten one would be on whichever screen shipped second. It belongs to the frame both share. |
| `GET /v1/me/language` returning the default instead of `null` | Makes "never chosen" and "chose Dutch" indistinguishable, which is the one distinction first-login seeding turns on: it would then either never seed or overwrite a deliberate choice on every sign-in. |
| Reusing `setLanguage` to apply the account's own language | Notifies the account of a value it just supplied. Hence `adoptLanguage`. |
| A permission for `PUT /v1/me/language` | ADR-012: inventing a permission the PRD does not name. It writes only the caller's own row, and requiring a grant would stop an invited user with no role yet from reading the product in their own language — the moment IAM-010g exists for. Exempted in `AUTHORIZATION_EXEMPT_PATHS` with that reason. |
| Auditing the language change | IAM-090's list is about access and change to *data*. Which of two languages someone reads the interface in changes no figure, no permission and no row. Contrast the client switcher, which is **not** exempt: which client someone was working in is context an auditor needs. |
| An endpoint to set `administration.formatting_locale` | The PRD names no capability for it and Appendix A has no row that fits, so choosing one would be inventing a permission (ADR-012). The column is seeded at creation and read everywhere; setting it belongs to FR-ONB when administration settings ship. |

## Consequences

**Easier.** Adding a string is one record in one file, and forgetting the other language fails the
build rather than shipping. Adding a jurisdiction (FR-LOC-005) is a `LocaleSpec` on each side, rows
in the shared case table, and a value in the `formatting_locale` CHECK — no branch changes. The
mobile apps (MOB-016) consume the same package and the same catalogue.

**Harder.** The catalogue lives outside `apps/api`, so a built wheel needs the `force-include` in
`pyproject.toml` to carry it; without it the API starts and then raises on the first user-facing
string. The formatting table is maintained by hand rather than inherited from CLDR — one locale
today, and every value pinned by a test.

**Open, and recorded rather than left to be rediscovered.**

- **PRD Q12 ("Is Dutch or English the default UI language for a firm's staff users, given many
  Dutch firms work bilingually?") is unresolved**, and is a product decision rather than an
  engineering one. The shipped answer is Dutch, in exactly two constants — `DEFAULT_LANGUAGE` in
  `packages/i18n/src/language.ts` and in `api.i18n.language` — so answering it the other way is a
  two-line change rather than an archaeology exercise. `tests/i18n/test_language.py` pins the
  current answer, so changing it is a deliberate edit with a failing test attached.
- **FR-LOC-001c's review has not happened**, and the glossary now records that in the data rather
  than in prose. Every term carries a `review` block — status, confidence, whether the PRD's own
  appendix glossary defines it, `reviewedBy: null`, and the specific questions it still needs
  answered. `make review-glossary` renders it as a sheet for an accountant, least settled first;
  it is generated from `glossary.json` rather than kept as a second document, so what gets signed
  off is what ships. The build does **not** fail on an unreviewed term — a gate blocking on an
  unscheduled human process gets disabled, and the completeness checks would go with it — but it
  does fail on a term with no review block, on a flagged term that asks no question, and on a
  claimed review naming no reviewer.

  Two terms are flagged, and it is not a coincidence that they are the two the PRD's appendix
  glossary has no entry for:

  - **`grootboek`** — FR-LOC-001c keeps a Dutch term "where no accurate equivalent exists", and
    *general ledger* is an accurate equivalent; FR-RPT-001 already names its report "general ledger
    detail" in English. The requirement lists `grootboek` as a keep-Dutch example and its own
    criterion argues the other way. The product has to choose.
  - **`kolommenbalans`** — appears nowhere in the PRD, so its definition is written from ordinary
    Dutch practice rather than from a requirement. It is also a working paper rather than a
    statutory filing, which is what FR-LOC-001c's rule is about, and "extended trial balance" may
    be an accurate enough equivalent to translate it. Whether it is in scope at all is the first
    question on the sheet.

  The twelve role names in `roles.json` are in the same position and say so in their own header.
- **Only `nl-NL` is a permitted formatting locale.** Not an oversight — a second value would be a
  guess about a jurisdiction with no chart of accounts, VAT ruleset or filing channel specified.
- **Setting `administration.formatting_locale` has no endpoint.** Appendix A has no capability that
  fits, so choosing one would be inventing a permission (ADR-012). The column is seeded at creation
  and read everywhere; setting it belongs to FR-ONB when administration settings ship.
- **The pre-authentication screens carry no credential form.** IAM-010's sign-in methods (password,
  Google, passkey) exist as a service layer in `apps/api/src/api/auth/` and are not exposed over
  HTTP — there is no `POST /v1/auth/...` in `api.main`. `PreAuthScreen` takes the form as
  `children` and the app passes `SignInPending`, which says so. A dead form is indistinguishable
  from a broken one, and somebody would eventually try to sign in with it. When IAM-010 lands,
  delete `SignInPending` and its catalogue record and pass the real form; nothing about the
  language behaviour on that screen changes.
- **`App`'s `authenticated` prop is a seam, not a session check**, for the same reason — there is
  no sign-in flow to derive it from. It defaults to `false`, which is the honest answer while
  nobody can sign in, and is what let IAM-010g's behaviour be built and tested now.

**Closed while implementing, and worth naming because each was nearly left open.**

- Role names were initially rendered untranslated. Fixed in §5 above; the fix needed a new API
  field (`role_is_system`), because the client cannot safely tell a system role from a custom one.
- The 401 from `TenantContextMiddleware` was initially left in English on the argument that a
  client redirects rather than rendering it. That argument is wrong: it is the refusal a person
  meets when their session runs out mid-task. It now goes through the catalogue, and needed no
  token to do so — knowing which of two languages to answer in never required authentication,
  which is the same reason IAM-010g puts a language control on the login screen.
