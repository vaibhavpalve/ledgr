# ADR-028: Fiscal year definition and period derivation

- **Status**: Accepted
- **Date**: 2026-09-02
- **Implements**: FR-ONB-006 (PRD §6.1)
- **Constrained by**: FR-GL-002, FR-GL-004 (every posting names exactly one period),
  CMP-014 (a period is the unit a VAT return is filed for)
- **Related**: [ADR-023](ADR-023-period-locking.md) — the lifecycle these periods enter;
  [ADR-012](ADR-012-appendix-a-as-source-of-truth.md) — why no permission was invented

## Context

> **FR-ONB-006.** Fiscal year definition, including non-calendar and short first years.

`0001` created `fiscal_year` and `period` and left both to be filled in by hand. Every fixture in
the test suite inserts them one INSERT at a time, which is fine for a fixture and is not a feature.
An administration being onboarded needs a year defined and its periods derived — for a year that may
start in July and may be nine months long.

## Decision

### One rule: whole calendar blocks, truncated at the year's bounds

Walk from the start date, take whole calendar months (or quarters), and clip the first and last to
the year's actual bounds. That single rule produces every case:

| Year | Scheme | Periods |
|---|---|---|
| 2026-01-01 .. 2026-12-31 | monthly | twelve whole months |
| 2026-07-01 .. 2027-06-30 | monthly | twelve months, July to June |
| 2026-03-15 .. 2026-12-31 | monthly | a 17-day stub, then nine whole months |
| 2026-05-01 .. 2027-04-30 | quarterly | **five** periods, two of them stubs |
| 2026-03-31 .. 2026-12-31 | monthly | a **one-day** first period |

### Blocks are calendar-aligned, even for a non-calendar year

The tempting alternative for quarterly is fiscal quarters counted from the year's own start — a May
year giving May–Jul, Aug–Oct, Nov–Jan, Feb–Apr, and exactly four tidy periods.

It is wrong in this schema. `period.status` includes `vat_filed` (`0021`) and CMP-014 keys a
filing's rule fingerprint on the period end date, so **a period is also the unit a VAT return is
filed for** — and Dutch returns are filed for calendar months and calendar quarters whatever the
fiscal year does. A May–July period straddles two calendar quarters and cannot be filed as either.

So every derived period lies inside exactly one calendar month or quarter. The visible cost is that
a quarterly year starting in May has five periods, not four. That is the year genuinely straddling
calendar quarters, and showing it beats hiding it behind a period nobody can file.

### A one-day period is legal now

`0001` wrote `check (end_date > start_date)` on `period`. A year beginning on the last day of a month
derives a first period of `2026-03-31 .. 2026-03-31`, which that check rejects.

The alternative was to absorb a one-day stub into the following period — and that is worse: the
merged period would run `2026-03-31 .. 2026-04-30`, straddle two calendar months, and lose the
containment the section above depends on. A one-day period is odd to look at; a period that cannot
be filed is a defect. The constraint is relaxed to `>=`.

### Two implementations, compared rather than trusted

`derive_periods` in `api/ledger/fiscal.py` and `ledger.derive_fiscal_periods` in `0029` are the same
rule written twice, and `tests/ledger/fiscal_cases.py` runs one table against both.

That is deliberate. The database has to derive periods *inside* the transaction that creates the
year; an onboarding screen has to show them *before* anything is written, without a round trip and
without a tenant. Neither can borrow the other's answer, so the answers are compared — the same
bargain every fake in this repo makes with its migration.

Beyond the table, five property tests assert what has to hold for spans nobody wrote a case for:
the periods tile the year with no gap and no overlap, each lies in one calendar block, the numbers
run 1..n densely, and only the first and last may be partial.

### The year and its periods are created together

`ledger.open_fiscal_year` writes both in one statement. Separately, an administration could hold a
year with no periods — a state nothing else in this schema can read, because every posting names a
period (FR-GL-004).

### Years and periods may not overlap

`0001` had `unique (administration_id, start_date)`, which stops two years sharing a start date and
happily permits `2026-01-01..2026-12-31` to sit on top of `2026-07-01..2027-06-30`. A posting in the
overlap would belong to two fiscal years, and FR-GL-002's "exactly one period" would be false one
level up. Both tables now carry a gist exclusion constraint over
`daterange(start_date, end_date + 1)` — half-open, so a year ending on the 31st and the next starting
on the 1st are adjacent rather than overlapping.

### The scheme belongs to the year, not the administration

FR-ONB-007's filing frequency will supply the default, but a business that moves from quarterly to
monthly filing does so *from a date* — and the years already closed keep the periods they were kept
in. That is the same argument CMP-014 makes about rates.

### No new permission

Appendix A's only fiscal-year capability is "Year-end close" — `("close", "fiscal_year")`, Full for
Owner and Accountant. Defining a year rides on it. That is a wider grant than opening one deserves
and still the right audience, and inventing a permission the PRD does not name is what ADR-012
forbids. `test_appendix_a_places_the_fiscal_year_capability` pins it so a PRD change has one place
to land.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Fiscal quarters counted from the year's start | Four tidy periods that straddle calendar quarters and cannot be filed as VAT periods |
| Merge a one-day stub into the next period | The merged period straddles two calendar months — the same defect, reached differently |
| Derive periods only in SQL | An onboarding screen must show a year's periods before it exists; a round trip per keystroke, and no preview for a year with no administration yet |
| Derive periods only in Python | The year and its periods have to be created in one transaction, and the derivation would sit outside it |
| Derive lazily — no `period` rows until something is posted | Every posting names a period (FR-GL-004), and locking (FR-GL-007) needs rows to lock |
| Store the scheme on `administration` | A change of filing frequency would restate the periods of closed years |
| Keep `end_date > start_date` and refuse such years | Refuses a legitimate incorporation date because of a constraint written before the case was considered |
| A trigger enforcing coverage instead of a report | Coverage is a property of a SET of rows; a row trigger cannot see the set mid-insert, and a deferred constraint trigger would block the hand-built fixtures the suite still relies on |

## Consequences

**Easier.** Onboarding is one call, and `preview()` shows the periods before committing. Reporting
can ask a year whether it is short or non-calendar rather than counting.

**A sharp edge.** A quarterly non-calendar year has five periods. That surprises people, and the
alternative surprises the Belastingdienst.

**Still to do.**

- `ledger.fiscal_year_coverage_deviations()` reports years whose periods do not tile them, because
  `period` has been INSERT-able by the application role since `0001` and hand-built years remain
  reachable. Narrowing that grant so the derivation is the only way in is worth doing and would
  break every fixture in the suite today.
- FR-ONB-007 will supply the scheme from the administration's filing frequency; today it is a
  parameter defaulting to monthly.
- FR-GL-008's year-end close will need the *next* year opened with its opening balances — this
  builds the year, not the close.
