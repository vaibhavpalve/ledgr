# The message catalogue (FR-LOC-001)

> Dutch and English are both first-class from **P0** — not an English product with a Dutch
> translation added later. Every screen, error message, email, push notification, PDF template,
> help article and validation message exists in both. A missing translation is a release blocker,
> not a fallback to English.

This directory is the single source of every user-facing string in LEDGR, for the web app, the
mobile apps and the API. One catalogue, not one per client: an error message rendered into a PDF
by the API and the same message shown on a screen have to be the same sentence, and two catalogues
would let them drift.

## The shape, and why it is this shape

A message is **one record holding both languages**:

```json
"client.header.none_selected": {
  "note": "Header state when the session is not inside any client.",
  "nl": "Geen klant geselecteerd",
  "en": "No client selected"
}
```

Not `nl.json` and `en.json` side by side. That is the whole design, and everything else here
follows from it:

- **A missing translation is a malformed record, not a diff between two files.** You cannot add an
  English string without adding a Dutch one, because there is no English file to add it to. The
  CI check (`make check-i18n`, FR-LOC-001d) is then a schema check rather than a set comparison
  that someone has to remember to run in both directions.
- **The two languages are edited together**, so a rewording lands in both or in neither. The
  failure mode of parallel files is that the English gets edited on Tuesday and the Dutch catches
  up in the next sprint — which is precisely "English with translations bolted on".
- **Neither language is the key.** Keys are opaque identifiers (`client.switcher.no_matches`),
  not English sentences. A catalogue keyed by its English text makes English structurally
  primary: the Dutch becomes a lookup from it, English changes silently orphan Dutch strings, and
  there is nowhere to put a Dutch phrase that has no English equivalent.

## Fields

| Field       | Required              | Meaning                                                                       |
| ----------- | --------------------- | ----------------------------------------------------------------------------- |
| `nl`        | yes                   | The Dutch text, or `{one, other}` for a counted message.                      |
| `en`        | yes                   | The English text, in the same form as `nl`.                                   |
| `note`      | no                    | Context for whoever writes or reviews the translation. Never shown to a user. |
| `identical` | when `nl` equals `en` | An explicit acknowledgement — see below.                                      |

### `identical`

Some strings genuinely are the same in both languages: language endonyms (`Nederlands` is
`Nederlands` in English prose), proper nouns, and statutory terms that keep their Dutch form
(`BTW`). Those are legitimate, and they are indistinguishable — to a checker — from someone
pasting the English into the Dutch slot to make the build pass.

So they are legal but never silent: a record whose two languages match must say `"identical":
true`, and the check fails on one that does not. Copying English into Dutch stays possible and
stops being invisible, which is the property worth having.

## Placeholders

`{name}`, interpolated at render time. The set of placeholders must match between the two
languages — the check enforces it — because a `{query}` that exists only in the English is a
Dutch sentence with a hole in it, and one that exists only in the Dutch is a crash or a stray
brace on screen.

Word order around a placeholder is free. That is the point of interpolating rather than
concatenating: `"{count} regels geboekt"` and `"Posted {count} lines"` are the same message.

## Counted messages

```json
"ledger.entry.lines_posted": {
  "nl": { "one": "{count} regel geboekt", "other": "{count} regels geboekt" },
  "en": { "one": "Posted {count} line", "other": "Posted {count} lines" }
}
```

Dutch and English share CLDR's plural categories exactly — `one` and `other`, with the same rule
(`n = 1`) — so two forms is right for both, and neither language is being bent to fit the other's
grammar. A third language would not necessarily share that (Polish has four categories, Irish
five), so `pluralCategory()` in `../src/format.ts` is written as a per-language function rather
than an `n === 1` conditional: adding a jurisdiction (FR-LOC-005) then adds a case there instead
of rewriting every counted message.

## What does NOT belong here

- **Number, date and currency formats.** Those follow the _administration's_ locale, not the
  reader's language (FR-LOC-002), so they live in `../src/locale.ts` as locale specs — including
  month names, which are locale data even though they look like text.
- **Statutory terms.** `glossary.json`, because they carry a definition and a rule about how they
  survive translation, not just a string. See FR-LOC-001c.
- **Log lines, exception text and audit `detail` payloads.** Those are read by operators and by
  machines, are not user-facing, and must stay in one language (English) so a search for an error
  finds every occurrence of it. `ledger.money_text` in migration 0023 makes the same argument
  about locale-independent numbers in machine-readable output.

## Adding a string

1. Add the record to the file whose namespace it belongs to. Every key in `client.json` starts
   with `client.`; the check enforces it, so a key is greppable back to its file.
2. Write **both** languages. If you cannot write the Dutch, the string is not ready — ask, do not
   leave a placeholder. `TODO`, `FIXME`, `???` and an empty string are rejected by the check
   specifically so that "I'll do the Dutch later" cannot ship.
3. Dutch accounting terminology is authoritative and reviewed by a practising Dutch accountant
   (FR-LOC-001c). Machine translation of accounting terms is prohibited. When in doubt about a
   term, check `glossary.json` first — it may be a term that stays Dutch in both.
