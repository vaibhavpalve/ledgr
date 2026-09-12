# ADR-014: Segregation of duties

- **Status**: Accepted
- **Date**: 2026-08-23
- **Implements**: IAM-060 – IAM-065 (PRD §8.7)

## Context

§8.7 states four prohibitions and two meta-rules about them. The prohibitions look similar enough to
be tempting to factor into one "the actor already did an earlier step" check, and doing so would get
two of them wrong. The two meta-rules — configurable deviations and the single-user exemption — are
the parts most likely to be implemented as a quiet boolean somewhere, which is exactly what IAM-064
and IAM-065 forbid.

## Decision

### Read the qualifiers literally; they are not decoration

IAM-060 says the invoice creator cannot be the **sole** approver. IAM-062 says the expense submitter
cannot approve it, full stop. So a creator *may* approve their own invoice when a second approval is
still required — they will not end up the only approver — while a submitter may never approve their
own expense however many other approvals exist. `_creator_not_sole_approver` and
`_submitter_not_approver` are deliberately separate functions rather than one shared helper, because
a shared helper is how one of those two silently becomes wrong.

IAM-061 says the payment approver cannot release **"where the organization has more than two active
users."** That is a second exemption, narrower than IAM-065's and with its own threshold: it lifts at
two users, not one. Both thresholds are computed from the same live count and both are disclosed.

The obvious way around IAM-060's "sole" escape — approve, then approve again to satisfy a
two-approval rule — is closed by refusing any actor already in `approved_by`.

### Rules are pure functions; exemptions are the service's job

Each rule is a side-effect-free function from `DutyContext` to an optional refusal string. The
service applies exemptions and deviations **after** a rule objects, never before. Ordering it the
other way would make a single-user organization emit a permanent stream of "exempt" outcomes for
actions no rule would have blocked, which is noise that hides the cases that matter.

### SoD lives in `api.authz`, but is not `authorize()`

SoD answers a different question — not "may this user do X" but "may *this* user do X to *this*
record, given who did the earlier steps" — and needs facts `authorize()` never sees. It is still an
access decision, so it lives in the authorization package rather than becoming the second
authorization system CLAUDE.md's third non-negotiable forbids. The two **compose**: a caller needs
`authorize()` to say the actor holds the permission *and* `check()` to say duties are segregated.
Neither substitutes for the other, and `test_a_single_user_organization_still_cannot_self_grant_beyond_the_ceiling`
pins the case where that distinction matters — IAM-065 stops the SoD rule objecting, and IAM-036's
ceiling still refuses.

### Deviations are rows, not settings (IAM-064)

A deviation is a row in `sod_policy_deviation` naming the rule, the Owner who acknowledged it, a
non-blank reason (enforced by CHECK, not just NOT NULL — an empty string would satisfy the latter and
defeat the requirement), and when. It is immutable except for revocation, on the same reasoning as
`role_assignment`: rewriting the justification after an auditor asks would launder a bad reason into
a good one. A partial unique index allows at most one active deviation per (organization, rule).

**Authority is checked against the Owner *role*, by name — not against a permission.** That is
unusual in this codebase, where everything else asks the authorization library about a permission,
and it is deliberate. Any permission-based check could be satisfied by a custom role composed to hold
that permission (IAM-036 explicitly allows an admin to compose from what they hold), which would let
an organization route around "the Owner acknowledged this." `Owner` is a system role, fixed by
migration, not composable and not assignable beyond a granter's own ceiling — so requiring it by name
is the only formulation that cannot be arranged around.

Revoking a deviation needs the same authority as acknowledging one; otherwise the Owner's decision
could be undone by someone the requirement never named.

### The two IAM-063 rules are not deviable — an interpretation, flagged

IAM-064 says SoD rules are configurable per organization, unqualified. Taken literally, an Owner
could acknowledge a deviation on "nobody grants themselves permissions", after which an Organization
Admin could grant themselves anything — defeating IAM-036, an equally unqualified M requirement with
no deviation clause of its own.

Where two M requirements collide, the reading that keeps both intact wins. A deviation on the other
three rules costs an organization a control it consciously gave up; a deviation on these would
silently undo a guarantee the rest of the authorization system is built on. `DEVIABLE_RULES` therefore
excludes both IAM-063 rules.

**This is an interpretation, not something §8.7 states**, and it is the one decision in this ADR most
worth a second opinion. Making either rule deviable is a one-line change to `DEVIABLE_RULES`,
deliberately, so the literal reading is cheap to restore.

### Exemptions are disclosed structurally, not by event (IAM-065)

The single-user exemption is **not** recorded per action — in a one-person organization that is one
row per action forever, and an audit report nobody can read is not disclosure. Instead
`audit_report()` computes it from the live user count and states it in prose, in every report,
whether or not it is active.

That last part is the point of "disclosed rather than hidden": a reader must never have to infer
enforcement from the *absence* of a warning. A fully-enforcing organization's report says so
positively, listing which rules are enforced; an exempt one says which exemption applies, why, and
that actions were permitted on that basis rather than because duties were segregated.

What *is* recorded per occurrence: blocked actions, and every use of a deviation (with the deviation
that permitted it, enforced by a CHECK — an unattributed bypass is unauditable). Permitted actions
are not recorded at all.

The exemption is a live fact rather than a stored flag, so it lapses the moment a second user joins,
with no administrative action — the same property IAM-035's expiring grants have.

### Membership is holding a grant

Both exemptions depend on the active user count, and an *undercount* silently exempts an organization
from rules it should be subject to. There is no membership table (users are global —
`0003_authentication.sql`), so the count is derived from `role_assignment`: the same rows
authorization itself reads, which means the figure the exemptions depend on cannot disagree with the
figure access decisions use.

### One live caller today

Purchase invoices (FR-AP), payment batches and expenses (FR-EXP) do not exist yet, so IAM-060/061/062
have no callers — this module is the enforcement point those modules will call, consistent with every
prior ADR's stated boundary. IAM-063's self-grant rule *does* have one: `assign_role` consults it, so
a refused self-grant is **recorded** rather than merely refused. The ceiling check (ADR-013) already
prevented it, but silently, and IAM-064's report is built from these rows.

`AuthorizationService`'s `sod` parameter is optional so the permission model can still be exercised
alone; production wiring supplies it.

## Alternatives considered

| Option | Rejected because |
|---|---|
| One shared "the actor performed an earlier step" rule for IAM-060/061/062 | IAM-060 says "sole" and IAM-062 does not; IAM-061 has a user-count qualifier neither other rule has. A shared helper makes at least one of the three wrong, silently. |
| Modelling SoD as IAM-033 attribute conditions on grants | Conditions are evaluated against request attributes; SoD needs the record's *history* (who created it, who approved it), which is not an attribute of the request and does not belong on a grant. |
| A separate top-level SoD service outside `api.authz` | It is an access decision. Putting it outside the authorization package is how a codebase ends up with the second authorization implementation CLAUDE.md forbids. |
| A per-organization boolean column for each rule instead of a deviation table | Cannot carry who acknowledged it or why, which IAM-064 requires, and a flag flipped back and forth leaves no history for an auditor reviewing a past period. |
| Checking deviation authority with a `manage security_policy` (or similar) permission | Routable around: an admin could compose a custom role holding that permission from what they already hold (IAM-036) and acknowledge their own deviation. The Owner role cannot be composed. |
| Recording a `single_user_exempt` event per action | One row per action forever in a one-person organization. Disclosure that buries the signal is not disclosure; the report states the exemption from the live count instead. |
| Applying exemptions before evaluating rules | Would report exemptions for actions no rule objected to, making it impossible to tell from the record which exemptions actually mattered. |
| Letting the IAM-063 rules be deviable, per IAM-064's literal wording | Would let an Owner switch off the control IAM-036 depends on. Flagged as an interpretation and made trivially reversible. |
| Storing the active user count on the organization row | Would go stale, and a stale *low* value silently exempts an organization from SoD. Deriving it from live grants means it cannot disagree with what authorization sees. |

## Consequences

- **Every guard is mutation-tested.** Eighteen deliberate breakages — each rule neutered, IAM-060's
  "sole" escape removed and its double-approval hole opened, IAM-061's threshold moved by one,
  IAM-062 given IAM-060's escape, both IAM-063 rules disabled and over-blocked, the Owner check and
  reason check removed, deviation recording removed, the self-grant rules made deviable, the
  exemption hidden from the report and made silent, and blocked actions left unrecorded — all
  produced real test failures.
- **`SegregationOfDutiesError` is distinct from `PrivilegeEscalationError`.** "You may not hold this"
  and "you may hold it, but not by this route, from this actor" mean different things to whoever
  reads the error and to an audit trail.
- **No HTTP endpoint**, consistent with every prior auth ADR. `audit_report()` returns a structured
  `SodAuditReport`; rendering it is a UI concern, and the wider IAM-090+ hash-chained audit system it
  would eventually feed is not built.
- **`required_approvals` is supplied by the caller**, not stored anywhere — there is no approval-policy
  table yet. When FR-AP is built, that number must come from a stored policy rather than from request
  input, or IAM-060's protection can be bypassed by sending `required_approvals: 2`.
- **`DutyContext` defaults to empty history**, and a rule whose facts are absent does not fire. Safe
  here because a rule is a prohibition and the action still needs `authorize()` to happen at all — but
  it means the future FR-AP/FR-EXP modules must build the context from **stored records**, never from
  request input, or a caller could omit `created_by` and skip the check.
- **`0012_segregation_of_duties.sql` has not been executed.** Parse-checked against the Postgres
  dialect only; CI applies it for real, as with 0009–0011.
