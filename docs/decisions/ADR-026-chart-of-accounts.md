# ADR-026: The chart of accounts, and RGS as data

- **Status**: Accepted
- **Date**: 2026-09-01
- **Implements**: FR-GL-005, FR-ONB-004, FR-ONB-005 (PRD §6.1, §6.2), CMP-003 (§11)
- **Related**: [ADR-022](ADR-022-ledger-bounded-context.md) — the tables this extends;
  [ADR-012](ADR-012-appendix-a-as-source-of-truth.md) — why no new permission was invented

## Context

> **FR-GL-005.** Chart of accounts with account type (asset, liability, equity, revenue,
> expense), RGS reference code, VAT default, and blocked/active state.
>
> **FR-ONB-005.** Chart of accounts seeded from the RGS MKB profile matching the legal form;
> user may extend but not break RGS mapping integrity.
>
> **CMP-003.** RGS 3.8 (and successors) reference codes maintained on the chart of accounts,
> with a versioned upgrade path when a new RGS version is released.

Migration 0020 already created `ledger_account` with all four of FR-GL-005's fields, so the
*columns* existed. None of the *meaning* did: `rgs_code` was free text with no referent, which made
`'BLimKas'`, `'BLIMKAS'` and `'not sure yet'` equally valid, and nothing connected an account to the
RGS version its code came from.

That gap is not cosmetic. RGS codes leave this system in two places where somebody else reads them —
the XAF 3.2 audit file an inspector opens (CMP-002) and the SBR filing that goes to the
Belastingdienst (CMP-004).

## Decision

### The RGS version is a row, and a release is a file

`rgs_version`, `rgs_element`, `rgs_profile_account` and `rgs_code_mapping` hold a published RGS
release. `scripts/load_rgs_version.py` loads a JSON document into them, checksummed;
`administration_rgs_version` pins each administration to one.

CMP-003 asks for "a versioned upgrade path when a new RGS version is released", and a hardcoded seed
cannot have one. Upgrading would mean editing a constant, deploying, and hoping every
administration's chart still meant what it said afterwards. As data:

| | |
|---|---|
| load | `scripts/load_rgs_version.py --file rgs-4.0.json` |
| plan | `ledger.plan_rgs_upgrade` — per account, before anything is written |
| apply | `ledger.apply_rgs_upgrade` — refuses while anything needs a human |

Supporting RGS 4.0 is a file. No migration, no deploy, no code change — and the old version stays,
because CMP-014 wants historical periods to keep the rules that applied at the time.

### The reference data is append-only

A published RGS release is a fixed publication. If an element's `account_type` could be edited after
accounts map to it, every one of those accounts would silently change meaning and both the XAF export
and the SBR filing would move with nothing recording that they had.

So `rgs_element`, `rgs_profile_account` and `rgs_code_mapping` take no UPDATE and no DELETE from
anyone — the shape 0019 and 0020 use: unconditional RAISE triggers, withheld privileges, a NOLOGIN
owner. Correcting a version means publishing another one. `rgs_version` takes exactly one UPDATE, its
lifecycle status, through a guarded transition.

This is also what makes it safe to let `ledgr_ops` load a version: the role can *add* a release and
cannot alter one. That is the only write privilege ops holds anywhere near the ledger, and CMP-009's
"including support tooling" is untouched — ops still cannot post and cannot touch a chart.

### "May extend but not break RGS mapping integrity" is four triggers

FR-ONB-005's second clause needs to mean something checkable. `ledger_account_rgs_integrity()`:

1. **The code exists**, in the version the administration is pinned to — a composite foreign key
   `(rgs_version_id, rgs_code)`, not a text column.
2. **The version is derived** from the pin, never supplied. A caller that could name its own could
   map an account to a code withdrawn two releases ago and still satisfy the foreign key.
3. **The element is postable.** An RGS heading holds a subtree; an account mapped to one puts an
   amount where the taxonomy expects the sum of its children.
4. **The types match.** An expense account mapped to a revenue element balances, reports, and files
   wrongly — the failure nothing downstream would notice.

What a user *can* do, and must: add accounts, block them, correct a mapping within its type, and keep
an account with no RGS code at all (0020 made the column nullable deliberately — a customer may carry
an account with no RGS equivalent). An unmapped account is reported by `ledger.rgs_readiness()`
rather than passing silently.

### The legal form is mapped, not constrained

`administration.legal_form` already holds `'BV'` — the KvK's vocabulary, arriving from FR-ONB-002's
company lookup. Adding a CHECK constraint would fail on existing rows (NFR-044) and would be the
wrong fix anyway: the KvK's vocabulary is not ours to define. `legal_form_alias` maps captured
spellings onto FR-ONB-004's five forms, and a spelling nobody has an alias for **refuses to seed**
rather than seeding the wrong chart. A BV's equity accounts in an eenmanszaak's books are wrong in a
way that is invisible until the year-end.

### The upgrade refuses rather than guesses

`apply_rgs_upgrade` will not run while any account's `resolution` is `needs_a_decision` — a split, a
withdrawal, a missing mapping, or a target whose type or postability does not fit. A half-upgraded
chart files some accounts under the new taxonomy and some under the old, with nothing on the screen
saying which. The plan is a query, not a dry run, so an administration can be shown exactly what
would happen and decide.

### The shipped dataset is provisional, and says so

`apps/api/data/rgs/rgs-3.8-mkb.json` is a starter subset in RGS's shape, not the official
publication: the level-1 and level-2 codes are the published ones, the deeper codes are not verified,
and it holds ~60 elements rather than the MKB profile's several thousand.

That is enough to build and test the seeding, the integrity rules and the upgrade path against —
none of which depend on the dataset's contents — and it is **not** enough for CMP-002 or CMP-004.
Three things keep that from being forgotten:

- `rgs_version.source` records it, and `ledger.rgs_readiness()` reports every administration pinned
  to a provisional version with `ready_to_file = false`.
- `ledger.load_rgs_version` refuses a provisional document unless the caller passes
  `p_allow_provisional`.
- `tests/ledger/test_rgs_dataset.py` fails if the file stops declaring itself provisional, so
  replacing it is deliberate enough to break a test.

**Replacing it with the official publication from referentiegrootboekschema.nl is a GA blocker.** It
is a data change: load the official file, and every administration on the provisional one moves
through the same upgrade path a real RGS release would use.

### No new permission

Appendix A has one capability, "View chart of accounts", whose R column is
`("view", "chart_of_accounts")` and whose F column is `("manage", "chart_of_accounts")`. Seeding,
extending, remapping, blocking and **upgrading the RGS version** all ride on `manage`.

An RGS upgrade rewrites every account's mapping and is a wider thing than adding an account, so
sharing the grant with a Bookkeeper is arguably too generous. It is recorded here rather than fixed,
because ADR-012 settled that Appendix A is the source of truth: a gap in it is a question for the
PRD, not a decision for a service module. If the PRD grows a "Manage reporting taxonomy" capability,
`MANAGE_CHART` in `api/ledger/chart.py` is the one line that changes.

## Alternatives considered

| Option | Rejected because |
|---|---|
| RGS codes as free text with a CHECK or regex | Validates the shape of a code, not that it exists, is postable, or means what the account claims — which is the whole of FR-ONB-005's second clause |
| Seed the chart from a constant in Python or a migration | CMP-003 asks for an upgrade path, and a constant has none: supporting RGS 4.0 becomes a deploy, and existing administrations have nothing to migrate through |
| Generate a migration from the dataset, like `generate_role_catalogue.py` | Works, and still ties an RGS release to a deploy. CMP-013 sets a regulatory-watch lead time that a release train should not be inside |
| Let `ledgr_app` write the reference tables | Application code could invent an RGS code and map an account to it — satisfying every foreign key and breaking the requirement completely |
| Mutable reference data, corrected in place | An element retyped after accounts map to it silently changes what every one of them files as |
| Constrain `administration.legal_form` to the five forms | Fails on existing `'BV'` rows (NFR-044), and defines the KvK's vocabulary for it |
| Upgrade automatically, skipping accounts that cannot be mapped | Produces a chart that is half on each taxonomy, with nothing recording which accounts are which |
| Fold RGS mapping into the NFR-033 integrity job | That job verifies three named things; a fourth kind of finding makes its contract "whatever we thought of". `ledger.rgs_mapping_deviations()` is its own report |
| Ship no dataset until the official one is available | Leaves seeding, the profiles and the upgrade path untestable and unbuilt, which is the larger risk |

## Consequences

**Easier.** Onboarding a new administration is one call. A future RGS release is a file and a
`make load-rgs`. The XAF export (CMP-002) has a guaranteed-resolvable code on every mapped account,
and `ledger.rgs_readiness()` answers "can this administration file yet" before the export is built.

**Harder, deliberately.** An account's type is immutable (0020) and its RGS element must match, so
"this should have been an expense account" is a new account and a reversal rather than an edit. That
is the same trade the ledger already makes everywhere else.

**Still to do.**

- Replace the provisional dataset with the official publication (GA blocker, above).
- No HTTP endpoint. When the chart becomes a screen, it should construct `ChartOfAccountsService`
  over the request's `ledgr_app` session — the service takes a repository protocol for exactly that.
- `default_vat_code` is constrained to FR-AR-002's *treatments*, not to a VAT code table with rates
  and rubriek mapping. That table is FR-VAT's, and CMP-014 will want it effective-dated.
- The MKB profile is one profile. RGS publishes others (ZZP, extended); `profile_code` is a column
  rather than an assumption, so adding one is data.

**Not built.** Nothing reads `rgs_code` yet — the XAF export and the SBR mapping are the consumers,
and both are separate requirements. The mapping is maintained now so that when they arrive there is
seven years of history behind it, which is the part that cannot be backfilled.
