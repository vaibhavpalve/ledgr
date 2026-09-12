# ADR-034: Duplicate detection for expenses

- **Status**: Accepted
- **Date**: 2026-09-04
- **Implements**: FR-EXP-001g (PRD §6.7)
- **Constrained by**: FR-EXP-001c (never blocks on extraction), IAM-093, §8.4's Expense Submitter
  scoping, ADR-012
- **Related**: [ADR-032](ADR-032-expense-form.md) — where the triple is entered;
  [ADR-031](ADR-031-receipt-capture.md) — the byte-identical signal this complements

## Context

> **FR-EXP-001g.** Duplicate detection **warns** when a receipt matching an existing expense on
> supplier, date and amount is captured.

## Decision

### 1. Warn, never block — asserted at every layer that could take it away

There is **no constraint**. No unique index on `(supplier, date, amount)`, and there is not going to
be one. Legitimate duplicates are ordinary: two identical coffees on one morning, two colleagues
each buying the same train ticket, a subscription billed twice because the first attempt failed. A
system that refused any of those would be wrong about the money *and* would teach people to work
around it, which is worse than the duplicates it stopped.

Every function returns findings; nothing raises. `can_be_marked_ready` deliberately does **not**
consult the warnings, and a test says so — because "warn, don't block" is exactly the property that
quietly becomes "block" the first time somebody adds `and not self.duplicate_warnings` to that line.

What a warning *does* change is the record: the submission audit entry says how many were
outstanding, so an approver reading it later can see what the submitter saw.

### 2. The check is a function of the triple, not of the capture event

The requirement says the check happens at capture. Matching on supplier, date and amount needs
those three, and **a photograph has none of them** — extraction is P1 and not built (FR-EXP-001c),
so a freshly captured receipt is an empty draft.

So the check takes a `ExpenseTriple` and runs wherever the triple becomes known. Today that is the
expense form, as the person types. When extraction lands and fills those fields at capture, it runs
there instead **with no change to any of it** — which is the whole reason it takes three values
rather than a receipt.

There is already a capture-time signal needing no extraction: the review list flags a byte-identical
*file* captured twice in one session (ADR-031). That is a different question — the same image rather
than the same claim — and the two are complementary. A re-photographed receipt is a different image
of the same purchase, and only this check sees it.

### 3. Two strengths, and only one of them exists in Python

| | Rule | Decided by |
|---|---|---|
| `exact` | normalised suppliers equal, dates equal, amounts equal | Python **and** SQL, compared against one table |
| `probable` | same date and amount, *similar* supplier | pg_trgm alone |

Reimplementing trigram similarity in Python to the same answer is a promise that breaks the first
time Postgres tunes it. The split is stated rather than hidden, and the shared case table covers
only the half both sides genuinely share.

Date and amount are matched **exactly** on both strengths. The requirement names all three fields,
and loosening two at once turns a warning into noise — which is how a warning gets dismissed
without being read.

`SIMILARITY_THRESHOLD` is 0.45, generous on purpose: the costs are asymmetric. A false positive is
a line somebody glances at and dismisses; a false negative is a claim paid twice.

### 4. Administration-wide, but it does not name the other person

The case worth catching most is **two people claiming one receipt** — a shared lunch, one bill, two
photographs. Scoping to the submitter's own claims would miss exactly that.

What comes back is deliberately narrow: the matching claim's id, its own triple (which tells the
reader nothing they did not just type), its status, and **whether the submitter was the same
person** — not who the other person is. Their own double entry is a mistake to fix; somebody else's
is a conversation to have, and that distinction is all a warning needs. A duplicate check is a poor
place to learn what one's colleagues have been spending.

The audit entry records only counts and strengths, never the matching ids or suppliers: the audit
log has its own retention (IAM-093) and is not the place to accumulate a second index of who spent
what.

### 5. A supplier that normalises to nothing matches nothing

Found by a property test, not by reasoning. `":"` and `"..."` both normalise to the empty string,
so under plain equality **every claim with a punctuation-only supplier would warn about every other
one** on the same date and amount. Migration 0033's CHECK does not stop one being entered —
`length(btrim(':')) > 0` is true.

The rule is not a special case: a name carrying no letters or digits says nothing about which shop
it was, so it cannot establish that two claims are the same shop. It lives in both implementations.

## Alternatives considered

| Option | Rejected because |
|---|---|
| A unique constraint on the triple | Refuses legitimate duplicates and teaches people to work around it. |
| Require acknowledgement before submitting | That is blocking with extra steps. The requirement says warn. |
| Run the check at capture regardless | There is nothing to match on until extraction exists. |
| Match on date ± 1 day | Loosens two of three fields at once; a duplicate photographed twice has the same date anyway, and mistyped dates are a different problem. |
| Reimplement pg_trgm similarity in Python | A promise that breaks the first time Postgres tunes it. |
| Scope the search to the submitter's own claims | Misses the shared-receipt case, which is the one worth catching most. |
| Return the other submitter's identity | More than a warning needs, and turns a duplicate check into a window on colleagues' spending. |
| Put the matching claims' ids in the audit detail | A second index of who spent what, under a different retention rule. |

## Consequences

**Easier.** When extraction lands, the capture-time check is a call to `find_exact_matches` with no
new logic. FR-EXP-002's approver sees, in the audit trail, whether the submitter had a warning in
front of them.

**Harder.** Two lookups per completed form (the category suggestion and this one). Both are indexed
and neither runs until the triple is complete.

**Known gaps, each with a trigger.**

- **No capture-time warning yet**, because there is nothing to match on. The trigger is extraction
  (FR-AP-002, P1).
- **No UI.** The warnings are on the form response with `can_be_marked_ready: true` beside them, on
  purpose — a client showing them must not gate the submit button on them being empty.
- **`same_submitter` is currently the only thing an Expense Submitter cannot already see.**
  `api.authz.matrix` notes that §8.4's row-level "own submissions" scoping is not modelled, so today
  they can read any expense in the administration anyway. When that scoping lands, this response is
  worth re-reading — the flag stays right, but the matching claim's id may need to go.
- **Similarity is tuned by one constant** with no per-administration override. A firm with many
  same-chain suppliers may want it higher; that is a setting, and settings are configuration work
  nobody has asked for yet.
