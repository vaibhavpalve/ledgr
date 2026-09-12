# ADR-032: The expense entry form

- **Status**: Accepted
- **Date**: 2026-09-04
- **Implements**: FR-EXP-001b, FR-EXP-001e (PRD §6.7)
- **Serves**: FR-EXP-001c (never blocks on extraction)
- **Constrained by**: NFR-031 (no floating point), CMP-014 (effective-dated rates), FR-GL-001
  (balance), IAM-005, IAM-090, ADR-012 (no invented permissions)
- **Supersedes**: [ADR-031](ADR-031-receipt-capture.md) §5's account of finalisation
- **Related**: [ADR-027](ADR-027-effective-dated-tax-rules.md) — where the rate comes from

## Context

> **FR-EXP-001b.** The expense form asks for the minimum — date, supplier, gross amount, VAT rate,
> category — with VAT and net calculated automatically and the category defaulting from the user's
> history. Everything else is optional and hidden by default.
>
> **FR-EXP-001e.** Payment method is captured at entry — paid by business account, business card,
> or personally (reimbursable) — because it determines the posting and cannot be reliably inferred
> later.

## Decision

### 1. "VAT rate" is a treatment, not a number somebody types

Migration 0032 gave `expense` a bare `vat_rate`. That was wrong, and 0033 corrects it while the
table holds nothing but drafts.

This system already models VAT as an **effective-dated treatment** (0028, ADR-027). The shipped
ruleset makes the case on its own:

| Treatment | From 2001-01-01 | From 2012-10-01 |
|---|---|---|
| `btw_21` | 19.00 | 21.00 |
| `btw_9` | 6.00 (from 2019-01-01: 9.00) | — |

A receipt dated 2012-09-30 is a 19% receipt and one dated 2012-10-01 is a 21% one. No amount of
care at the keyboard makes a typed rate track that. So the form captures a treatment and
`vat.rate_on(treatment, expense_date)` supplies the rate — **looked up by the expense's own date,
not today's**.

The resolved rate is still *stored*: CMP-014's "historical periods keep the rules that applied at
the time" has to survive a later ruleset load, and a value recomputed on read would not.

**`btw_marge` is excluded**, by a CHECK rather than by convention. The margin scheme charges VAT on
the margin, not the sale price, so extracting VAT from a gross receipt under it would compute the
tax on the whole amount — overstating it by whatever the goods cost. The resulting row would be
arithmetically consistent with itself and wrong only in its *scheme*, which nothing downstream
would catch.

### 2. One figure is rounded; the other is subtracted

    vat_amount = round(gross × rate / (100 + rate))
    net_amount = gross − vat            ← a GENERATED column

Rounding both halves independently can leave `net + vat` a cent from `gross`, and that cent is a
ledger that does not balance (FR-GL-001). Making net *generated* removes the possibility rather
than guarding against it: no writer supplies it, so it cannot disagree.

**VAT is the rounded half** because the VAT figure is what gets filed (FR-VAT-001's rubrieken 1a–5b).
The rounded number should be the one on the return; the residue belongs in net, where nobody files
it.

**The rounding mode is chosen, not inherited.** `ROUND_HALF_UP`, stated at every call site. Python's
default decimal context is `ROUND_HALF_EVEN`, so `quantize()` without an explicit mode rounds 0.025
to 0.02 — not how a Dutch tax figure rounds, and not how anyone checking the sum by hand rounds.
Postgres `round(numeric, 2)` rounds half away from zero and therefore agrees. A test sets the
ambient context to half-even and asserts the answer does not move.

The rule exists twice, so `tests/expenses/vat_cases.py` runs one table against both — the device
0029 (fiscal periods) and 0031 (retention) already use.

### 3. The category defaults from the user's own history

Two questions, in order: what did **this person** last file **this supplier** under, then what do
they file most things under. The user's history, not the administration's — FR-EXP-001b says "the
user's", §8.4 scopes an Expense Submitter to their own submissions, and a default drawn from a
colleague's habits would also leak what they have been claiming.

`expenses.suggest_category` is **not** `SECURITY DEFINER`, deliberately: it reads `expense`, which
carries RLS, so a suggestion cannot cross a tenant boundary.

A suggestion never overrides a choice. Once the person has picked a category the suggestion goes
away, rather than sitting beside their answer inviting a client to apply it.

### 4. `ready` means complete — which corrects how finalisation worked

An expense reaches `ready` only with FR-EXP-001b's minimum and FR-EXP-001e's payment method
present. Enforced by `expense_ready_is_complete`, so it binds every writer.

That makes ADR-031's finalisation wrong, and this is the correction: **closing a capture session no
longer readies its expenses.** Finalising ends the sitting; each expense becomes ready on its own
when its form is complete. That is also the only shape that works on a phone — photograph six
receipts on the train, fill them in later — which is what FR-EXP-001c's "never blocks" and
FR-EXP-001f's offline queue both describe.

### 5. Partial saves are the normal case; PATCH, not PUT

`update` takes any subset and refuses nothing for being incomplete (FR-EXP-001c). PUT would make an
omitted field mean "clear it", so a client saving one change would wipe the other five. Only fields
**present in the body** are written — and an explicit `null` still clears, because somebody has to
be able to undo a mistyped supplier.

A submitted claim cannot be edited. FR-EXP-001c's "always editable" is about extraction never
locking a field against the person filling the form in, not about a claim already in front of an
approver.

### 6. Amounts cross the wire as strings

JSON has one number type and it is a double. A client sending `1234.56` unquoted has already lost
the value before pydantic sees it, and NFR-031 covers the whole calculation path. `float` is
**refused** at the boundary rather than converted — the tripwire `api.ledger.model` puts at the
ledger's door, put at the form's.

## Alternatives considered

| Option | Rejected because |
|---|---|
| A typed `vat_rate` field | Cannot track CMP-014's effective dates. `btw_21` is 19% or 21% depending on the receipt's date. |
| Recompute the rate on read | A later ruleset load would restate a historical claim. |
| Round net and VAT independently | `net + vat` can miss `gross` by a cent — a ledger that does not balance. |
| Store net as an ordinary column | Something could write a value that disagrees. Generated cannot. |
| Inherit the decimal rounding mode | Half-even is the default and is the wrong answer; a mode somebody else can change is not a rule. |
| Accept `vat_amount` from the client | Lets a caller assert a figure the database is about to disagree with. |
| Validate that a rate of `0.21` "means" 21% | 0.21% is a rate `vat_rate` can hold. The protection is structural: the form has no rate field. |
| Keep finalisation readying every draft | Releases claims with no amount and no payment method — unapprovable and unpayable. |
| A separate category-suggestion endpoint | The suggestion is only meaningful beside the supplier it came from, and a form needing two calls to draw itself shows an empty field first. |
| A new permission for the form | ADR-012. Filling in the form is part of submitting the expense, which Appendix A's "Submit expenses" already covers. |

## Consequences

**Easier.** FR-EXP-002's approval workflow gets claims that are complete by construction.
FR-EXP-001c's extraction writes the same columns the form does and inherits every check. Adding a
jurisdiction's VAT is a ruleset load, not a code change.

**Harder.** A client must send amounts as strings and choose a treatment rather than typing a
percentage. Both are the right side of the trade.

**Known gaps, each with a trigger.**

- **"Everything else is optional and hidden by default" has no "everything else".** The PRD names
  no other expense field, and inventing some to hide would be worse than having none. The server's
  contribution is that nothing beyond the six is required (`Expense.REQUIRED_FIELDS`); which fields
  a client collapses behind a "more" control is rendering.
- **No UI.** This is the API the form talks to. The web app has no expense screen yet.
- **`btw_verlegd` stores the invoice as it was paid** — rate 0, so net equals gross. Correct for
  what changed hands; the reverse-charge posting where the buyer accounts for the VAT is FR-VAT's,
  not the form's.
- **The audit entry names fields, never values.** An entry holding a supplier and an amount would be
  a second copy of the claim in a table with different retention rules (IAM-093's seven years vs
  FR-DOC-002's). The submission entry does record the payment method and treatment, because those
  determine the posting and cannot be re-derived.
