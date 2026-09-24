# ADR-089: The books export as Dutch-Excel CSV now; the XAF auditfile waits for the 4.0 schema

- **Status**: Accepted
- **Date**: 2026-09-24
- **Implements**: IAM-090 (exports are audited), Appendix A "Export data"; groundwork for the
  accountant's XAF auditfile

## Context

An accountant's first question to a client's bookkeeping software is "send me the mutations and a
trial balance" (grootboekmutaties, saldibalans). Beyond that, "send me the auditfile" (XAF).
LEDGR had no export at all: the only way to get the books out was one screenshot at a time. That
is a reason an accountant refuses to take on a client, and a reason a business hesitates to join
(no way out).

For XAF there is a hard fact. **XAF 4.0 has been the required format since 1 January 2026**, and
XAF 3.2 is superseded. The 4.0 schema package is published on the Belastingdienst's ODB site
behind a (free) account login. The 3.2 XSD is public, and a 3.2 file can be validated today, but
it would be a new export in a format the Belastingdienst stopped using this year.

## Decision

- `GET .../exports/journal.csv?fiscal_year_id=` (every posted line of the year, including
  reversals marked as such) and `GET .../exports/trial-balance.csv?fiscal_year_id=`.
- Appendix A's `export report_data`, audited as `EXPORT`. A Bookkeeper's grant is conditional on
  assigned periods and journals. A whole-year export cannot satisfy that condition, so it fails
  closed, as the library is designed to.
- **The Dutch Excel dialect by default**: `;`-separated, decimal comma, UTF-8 BOM, no thousands
  separator. Without all four, Excel in a Dutch locale shows one column of text or mangles
  "Privé". `?dialect=international` gives plain CSV for a tool. Amounts come from Decimal,
  never float (NFR-031).
- The Grootboek screen has the two download buttons. The file is fetched with the authenticated
  fetch, because the bearer token is not a cookie and a plain link would be refused.
- **XAF is not built yet.** It is next once the XAF 4.0 package (XSD and specification) is
  downloaded from odb.belastingdienst.nl with an ODB account. The export must be validated against
  that XSD in the test suite, the way the 3.2 schema was validated in exploring this.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Build XAF 3.2 now | The format has been superseded since 1 January 2026. It would ship an obsolete export. |
| Build XAF 4.0 from secondary descriptions | An unvalidated auditfile that an accountant's software rejects is worse than none, and there is no schema to test against. |
| Comma-separated with decimal points only | Opens as one text column in Dutch Excel, the tool every recipient uses. |
| Client-side CSV from screen data | Bypasses the EXPORT audit entry IAM-090 requires, and the journal is paged on screen. |

## Consequences

An accountant can take the mutations and trial balance into their own tools today. XAF 4.0 needs
the founder (or whoever holds the ODB account) to fetch the schema package. After that it is a
bounded task: generate, validate against the XSD in CI, and add a third download button.
