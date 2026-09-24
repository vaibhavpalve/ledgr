# ADR-087: The BTW return is computed from the ledger and filed manually first

- **Status**: Accepted
- **Date**: 2026-09-24
- **Implements**: FR-VAT-001 (return preparation, rubrieken 1a–5b), FR-VAT-002 (pre-filing
  validation), FR-VAT-011 (drill-down to postings), FR-VAT-003 in part (a stored, evidenced
  filing; not yet electronic)
- **Builds on**: [ADR-027](ADR-027-effective-dated-tax-rules.md) (the effective-dated rules and
  the filed-period barrier), [ADR-069](ADR-069-live-rubriek-preview.md) (the same mapping, read
  for one invoice), migration 0021 (`vat_filed` as a terminal period status, the suppletie
  table), migration 0035 (the VAT treatment on every journal line)

## Context

The product said "From receipt to return", and had no return. The dashboard showed an estimate and
a due date, while the ledger already held everything a return needs:

- every journal line carries its VAT treatment (0035), written by every posting path;
- the effective-dated rules map each treatment to its rubrieken (0028, ADR-027);
- a period can be hard-locked as `vat_filed`, with no way back except a suppletie (0021);
- Appendix A defines who may prepare, file and view a return (`prepare`, `file` and `view
  vat_return`).

What was missing was the computation, the checks, a record of what was filed, and a screen.

## Decision

### 1. The return is read from the ledger, per period

For each period of the fiscal year (the period scheme doubles as the filing frequency, the same
assumption `api.vat.deadlines` records), the return sums the period's VAT-tagged journal lines by
treatment and **account role**:

| Account role | Resolved from | Contributes |
|---|---|---|
| revenue | `account_type = 'revenue'` | the treatment's turnover rubriek (credit − debit) |
| output VAT | `sales_posting_account.vat_output`, or RGS `BSchObe` | the treatment's VAT rubriek |
| input VAT | `expense_posting_account.vat_input`, or RGS `BVorObe` | 5b (debit − credit) |
| anything else | – | nothing, unless the purchase was reverse-charged |

Credit notes and reversals are therefore netted by construction. The treatment's rubrieken come
from `vat.rules_on(period end)`, the same reader ADR-069 uses, so the preview and the return cannot
disagree.

### 2. Three things the shipped mapping cannot say, said in code

- **Reverse-charged purchases** (btw_verlegd → 2a, btw_icp → 4b, btw_export → 4a, keyed by role).
  The base goes in the box and the VAT is self-assessed at the standard rate. The same VAT is
  deducted in 5b. The shipped mapping only describes sales. Without this, every SMB with a Google,
  Meta or Adobe invoice from Ireland would file a wrong return.
- **Exempt supplies are not declared.** The mapping sends btw_vrijgesteld to 1e, but exempt
  supplies are not entered on the aangifte at all. They are shown as their own figure instead.
- **The margin scheme blocks filing.** The return reports the margin, which is not in the ledger.

### 3. Rounding in the filer's favour, with the exact figure kept

Whole euros: turnover and VAT due round down, deductible VAT rounds up, and 5a is the sum of the
rounded VAT boxes. Every box keeps its exact figure beside the rounded one, and the drill-down
reconciles to the exact figure.

### 4. FR-VAT-002 checks, blocking or warning

| Check | Severity |
|---|---|
| period has not ended | blocking |
| a treatment fits no box (margin scheme, unmapped) | blocking |
| the ruleset is provisional | warning |
| unposted purchases / receipts dated in the period (or undated) | warning |
| draft sales invoices dated in the period | warning |
| unreconciled bank transactions in the period | warning |
| postings on a VAT account without a treatment | warning |
| an earlier period with VAT postings is not filed | warning |

A warning must be acknowledged by the filer, and the acknowledged codes are stored with the
return. Each warning in the UI links to the screen where it is fixed.

### 5. Filing is manual, and is a real record

`POST .../vat-returns/{period}/file` (permission `file vat_return`, audit `FILING`):

1. refuses on a blocking check, an unacknowledged warning, or a total that differs from the one
   the filer reviewed (`expected_total`);
2. records the Belastingdienst reference through the filing channel (`ManualFiling`);
3. hard-locks the period through `PeriodService.mark_filed`, which carries its own period-scoped
   authorization check and audit entry;
4. recomputes the return now that nothing can post into the period, and refuses (rolling the lock
   back) if it moved;
5. stores the return **as filed** in `vat_return` (0068): boxes, totals, rules fingerprint,
   whether the ruleset was provisional, the acknowledged warnings, who and when. The table is
   insert-only, with a trigger refusing UPDATE and DELETE. Unfiled returns are never stored; they
   are the ledger's current answer.

`VatFilingChannel` is the adapter seam (CLAUDE.md #4) for Digipoort.

### 6. The screen

`/vat` lists the year's periods with status, amount due and deadline. `/vat/:period` shows the
return in the form's order, with a copy button per figure for entering it in Mijn Belastingdienst
Zakelijk. Each box drills down to its postings. The screen also shows the checks with links to
fix them, and the filing steps with the irreversible lock behind a confirmation dialog.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Build from invoices and expenses (as the dashboard estimate does) | Misses manual journals, bank reconciliations and write-offs, and needs a new source join for every new posting path. The ledger is where every source has already agreed (0035's argument). |
| Store drafts and update them | Two answers to "what does this period's return say" as soon as a posting lands. |
| Direct Digipoort filing now | Needs an NT OB XBRL instance and a PKIoverheid services certificate that only the business (or LEDGR as an intermediary) can obtain. An unverified submission to the tax authority is not something to ship on a guess. |
| Change the shipped rubriek mapping for exempt supplies | `turnover_rubriek` is NOT NULL, so "not declared" cannot be expressed, and the rule tables are append-only behind the filed-period barrier. |
| Treat every warning as blocking | A business with one unreconciled bank line or a provisional ruleset (every business today) could never file. The PRD asks for "blocking or warning". |

## Consequences

**Easier.** The promise on the homepage is now true. A filed period is permanently evidenced and
drillable. The suppletie machinery in 0021 now has a filed return to correct.

**Harder / known gaps.**

- **Digipoort is not built** (FR-VAT-003/004). Filing is: enter the figures, record the reference.
- **Suppletie UI is not built** (FR-VAT-005). The ledger side exists (0021). Today a correction
  goes into the next period's return, which the Belastingdienst accepts for corrections up to
  €1,000 of VAT.
- **Filing frequency is the fiscal year's period scheme.** A business that books monthly but files
  quarterly needs a separate setting (as `api.vat.deadlines` already notes). Annual filers are not
  supported.
- **Reverse-charge purchases assume full deduction.** A business with exempt or mixed activity has
  partial deduction (pro rata). Its accountant must adjust 5b.
- **Race window owned by the ledger.** A posting whose `journal_entry_validate()` read the period
  as open before the filing's status UPDATE, and that commits after it, lands in a filed period.
  The recompute in step 4 catches postings committed before it, not after. Closing this needs the
  ledger's validate trigger to read the period row with a lock that conflicts with the status
  update (`FOR SHARE`). That is a change to 0020/0021's function and belongs to the ledger owner.
- **KOR, private-use corrections (1d), 5d–5f** are not computed.
- **The ruleset is provisional** (ADR-027). Every return carries that warning until the official
  publication is loaded.
