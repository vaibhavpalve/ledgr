# ADR-027: Effective-dated tax rules, and the filed frontier

- **Status**: Accepted
- **Date**: 2026-09-02
- **Implements**: CMP-014 (PRD §11)
- **Serves**: CMP-013 (regulatory watch), FR-VAT-001 (rubrieken), FR-AR-002 (treatments)
- **Related**: [ADR-026](ADR-026-chart-of-accounts.md) — RGS as data, the same shape;
  [ADR-023](ADR-023-period-locking.md) — the filing this protects

## Context

> **CMP-014.** Rate and rule changes are effective-dated, so historical periods keep the rules that
> applied at the time.

And the sentence that makes it hard: *retroactively changing a rate must not alter a filed period.*

Three kinds of rule are in scope — VAT rates, rubriek definitions, RGS versions. `0024` already gave
`rgs_version` an `effective_from` column and nothing that read it; VAT had no rates at all, only the
eight treatment codes `0024` constrained `ledger_account.default_vat_code` to.

## Decision

### `valid_from`, and deliberately no `valid_to`

The rule in force on a date is the row with the greatest `valid_from` at or before it. Superseding a
rule is an INSERT and nothing else.

A closed `[valid_from, valid_to)` range would require writing an end date onto an existing row when
its successor arrives — making the normal, expected act of publishing a rate change into an UPDATE
of a historical row. A table you routinely edit is one where history can be rewritten by accident.
With `valid_from` alone the rule tables are append-only in the same sense the ledger is, enforced by
the same shape of trigger.

The cost, named plainly: a gap is not representable. There is no way to say "no rate applied
between these dates". For VAT rates that is correct — some rate always applies — and where it would
not be, the answer is a rule row that says so rather than an absence.

**A missing rule resolves to NULL, never to zero.** `vat.rate_on()` returning nothing means the
ruleset has a hole; a return computed at 0% because of one would be wrong in the only way nothing
downstream can catch, since every later check would agree with it.

### The barrier: rules may only be introduced ahead of the filed frontier

Effective dating alone does not satisfy CMP-014's second sentence. A row inserted today with
`valid_from = 2019-01-01` changes what applied in 2019, and every return filed since silently
restates.

So `vat_rule_not_behind_a_filing()` refuses any rule row whose `valid_from` falls on or before the
last day any period has been VAT-filed through. The refusal names the route that *is* open — a
suppletie (FR-VAT-005) — because a dead end is a design failure (PRD D5).

This also gives CMP-013's "defined lead time for product changes" teeth: a rate change must be
loaded *before* the period it applies to is filed. That is enforced, not scheduled.

### The frontier is a watermark, not a scan of `period`

The guard needs the latest date covered by any filed period **across every tenant**, because a VAT
rate is national and one row reaches all of them. `period` is RLS-protected, so a trigger reading it
would see only the caller's tenant and would happily let a rate be backdated over somebody else's
filed return.

`vat_filing_watermark` holds that one date, advanced by `ledger.mark_period_filed` in the same
statement that files the period. It carries no tenant column because it is not tenant data — it is a
single fact about the whole system, and the guard needs it to be exactly that. It only ever moves
forward: letting it retreat would reopen every rule date it had passed.
`vat.watermark_drift()` recomputes it from `period` so the derived value stays checkable.

### Detection beside prevention

A period records `vat_rules_fingerprint` — a hash of every rule in force on its end date — at the
moment it is filed. `vat.filed_period_drift()` recomputes it and reports any period whose rules have
moved underneath it.

Always empty while the barrier stands. It exists for the same reason `0020` built a gap report the
allocator makes impossible: a check that can only ever be empty is the check on the thing that makes
it empty — and it is what notices if a future migration drops the trigger or a superuser writes
around it. `tests/integration/test_effective_dated_rules.py` disarms the trigger deliberately and
asserts the drift is caught, because a detector nobody has watched detect is not a detector.

Like the NFR-033 integrity job, both drift functions are `SECURITY INVOKER`: they answer for what the
caller can see, so the cross-tenant sweep runs as `ledgr_ops` (BYPASSRLS). That required granting
`ledgr_ops` SELECT on `period`, which it never had.

### Treatment codes name a rate they have already outlived

`btw_21` and `btw_9` were introduced in `0024`, and both encode a rate in an identifier that has to
survive it: **btw_9 was 6% until 2019, and btw_21 was 19% until 2012.** The code already lies about
part of its own history, which is precisely the anti-pattern CMP-014 exists to correct.

`vat_treatment.role` (`standard`, `reduced`, …) is the stable semantics, and anything reasoning about
a treatment reads that. The codes were **not** renamed here because they are already stored in
`ledger_account.default_vat_code`, constrained by a CHECK in `0024`, and written into the shipped RGS
profile dataset — renaming touches all three and is its own change with its own migration. Recorded
as owed work rather than done badly in passing.

## Alternatives considered

| Option | Rejected because |
|---|---|
| `[valid_from, valid_to)` ranges | Publishing a rate change becomes an UPDATE of a historical row; the table stops being append-only exactly where it matters most |
| A rate column on the treatment, updated in place | Cannot answer "what applied in 2018" at all — the requirement in one line |
| Effective dating with no barrier | A row backdated over a filed period restates every return since, silently. CMP-014's second sentence exists because dating alone is not enough |
| Scan `period` in the guard instead of a watermark | RLS means the trigger sees one tenant, so a rate could be backdated over another tenant's filing. The guard needs a system-wide fact |
| Let the watermark move backwards when a period is unfiled | `vat_filed` is terminal (FR-GL-007); and a retreating frontier reopens every rule date it had passed |
| Snapshot every rule row onto the period at filing | Reproducible, and enormous — one copy of the whole ruleset per period per tenant. A fingerprint proves the same thing and is 64 bytes |
| Rename `btw_21` → `btw_standard` in this migration | Touches a CHECK constraint, existing account rows and the shipped RGS dataset. A rename deserves its own migration, not a corner of this one |
| Store rates as floats | NFR-031. A rate is the first multiplier in the calculation path; the loader rejects a JSON number for the same reason `ledger.post_entry` does |

## Consequences

**Easier.** A rate change is a file: `make load-vat ARGS="--file data/vat/nl-2027-01.json"`. Asking
what applied on a date is one function call. A filing carries proof of what it was computed under.

**Harder, deliberately.** A rate change must be loaded before the affected period is filed. Miss the
window and the load is refused — correctly, because the alternative is restating filed returns. The
refusal names the suppletie route.

**A sharp edge worth knowing.** The frontier is global and monotonic, so *any* tenant filing a period
constrains what rules can be introduced for *every* tenant. That is right for national tax law and
would be wrong for anything tenant-specific; a per-tenant rule would need its own frontier.

**Still to do.**

- The treatment-to-rubriek mapping is provisional and belongs to FR-VAT-001. The rates are the
  published ones; **which box a treatment reports into is not verified**, and getting it wrong
  misstates a return without misstating the ledger — the kind of error that survives every other
  check here. `vat.load_ruleset` refuses a provisional document without `p_allow_provisional`, and
  `provisional_rulesets()` reports it.
- No VAT return is built. This is the rule layer FR-VAT-001 will read, not the return itself.
- `ledger.rgs_version_on()` resolves RGS by date, and the shipped 3.8 subset declares no
  `effective_from` — so nothing resolves to it, and
  `ledger.rgs_versions_without_effective_from()` reports exactly that. A dataset property, surfaced
  rather than hidden.
- Rename the rate-named treatment codes.
