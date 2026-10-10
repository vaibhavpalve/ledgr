# ADR-113: Booking rules are per administration and auto-approve through the proposal path

- **Status**: Accepted
- **Date**: 2026-10-09
- **Builds on**: [ADR-110](ADR-110-auto-bookings-are-proposals.md) (booking proposals),
  [ADR-112](ADR-112-audit-log-firm-acting-on-client.md) (a firm appending to a client's audit
  chain); docs/firm-home/contract-wave2.md decisions 1-4

## Context

FR-BNK-004 asks for auto-posting above a high threshold with conservative defaults. FR-BNK-006
asks for learning rules per tenant, never across tenants. ADR-110 made every certain match a
*proposal* that a person approves. An accountant who approves "Staples -> 4100 Kantoorkosten" every
month, for every client, wants to say "always do this" once.

A rule that books without a person is the riskiest thing in the firm home:

- the ledger is append-only (FR-GL-003, CMP-009), so a wrong automatic booking can only be undone by
  a reversing entry;
- a rule outlives the moment its creator approved it, so the creator's authority can lapse while
  the rule keeps working;
- a firm reaches many clients through one session, so a rule keyed only on counterparty would leak
  one client's decision into another's books.

## Decision

### Per administration, never per firm (FR-BNK-006)

`booking_rule` (migration 0082) belongs to one administration and carries its owning
`organization_id`. Approving a review-sheet group spanning nine clients with `remember: true` makes
nine rules, one per client, each made only after that client's own approval succeeded and was
authorized against that client (ADR-110's per-item `authorize()`). Every query that reads, applies
or changes a rule names the administration as well as relying on RLS
(`app.has_administration_access`). A rule id named under another client's path is not found.

The rule key is `(administration_id, normalise_counterparty(counterparty), account_code)`, the same
normalisation ADR-110's `group_key` uses. A partial unique index allows one non-retired rule per key.
A proposal whose counterparty or account is unknown cannot be made into a rule.

### A rule only approves what is already a proposal

A rule approves a pending booking proposal and nothing else. That proposal is a single certain HIGH
match of a bank line to an existing document (contract decision 2). The rule's counterparty and
account must equal the proposal's, and its amount must be at most `max_amount` when one is set.
Anything else stays a normal pending proposal. Rules never invent a booking without a document.
Only proposals created in the current generation run are offered to rules. A proposal that was
already pending when the rule was made is left for a person.

`plan_auto_approvals` is pure and property-tested (Hypothesis). It never plans one bank line twice,
never plans above `max_amount`, and never crosses administrations.

### Auto-approval is the wave-1 approve path, run by the system

Generation (`ProposalGenerator.refresh`) runs where ADR-110 put it: the statement import and the
PSD2 sync (`BankService.import_rows`), the expense posting route, and the backfill job. All of them
are now wired through `api.bank.compose`, so they share one `RuleApplier` on the same session. For
each newly proposed line that an active rule covers:

1. **The creator's authority is re-checked at apply time.** `authorize()` from the shared library is
   asked whether `created_by_user_id` still holds `reconcile bank_transaction` on that
   administration, against current state. It is asked once per rule per run.
2. **If the creator is no longer authorized, nothing is booked.** The proposal stays pending for a
   person. The rule moves to `suspended` with `suspended_reason = 'creator_not_authorized'`, audited
   as the system (`suspend_booking_rule`).
3. **If the creator is authorized**, the proposal is claimed (`pending -> approved`,
   `rule_id` set, `decided_by_user_id` NULL) and booked with `api.firm.proposals_service.book`.
   This is the function a person's approval calls: `BankService.reconcile_with_expense` /
   `reconcile_with_invoice` -> `LedgerService.post()` / `SalesPaymentService.record()`. The claim and
   the booking share one savepoint, so a failed booking rolls the claim back. The creator is the
   posting actor, so the ledger and payment service checks run for the person whose authority the
   rule carries.
4. **Audit.** `approve_booking_proposal` is recorded with `actor_type = system`, no actor user, and
   `detail.rule_id` and `detail.rule_created_by`. It is filed in the client's own chain (ADR-112
   admits that from a firm session). The reconciliation keeps its own audit entry.

Rules are advisory. Reading rules and applying each one run in their own savepoints. A failure is
logged and audited, and it never fails the import that triggered it. This also holds on a
deployment where 0082 is not applied yet (NFR-044).

### Remember, suspend, reactivate, retire, cap

- `POST /v1/firm/proposals/decide` accepts `"remember": true` on an approve decision; on a reject it
  is a 422. The response gains `rules_created` (created + reactivated).
- If the remembering person created the suspended rule, remembering reactivates it. If someone else
  remembers, the old rule is retired and that person gets a new one. A rule books under its
  creator's authority and nobody lends theirs to someone else's rule. `created_by_user_id` is
  immutable.
- `POST .../rules/{id}/retire` is final. `POST .../rules/{id}/max-amount` sets or lifts the cap
  (a decimal string, never a float, NFR-031). Both take `reconcile bank_transaction` on the path's
  administration and are audited as CONFIGURATION.
- 0082's trigger allows these status moves: `active <-> suspended`, and `active|suspended ->
  retired`. A retired rule is final. Every other column except `max_amount` is frozen. There is no
  DELETE grant.
- `booking_proposal.rule_id` is NULL on insert. It can be set only in the update that moves a
  proposal from pending to approved, and only to a rule of the same administration. 0079's guard is
  replaced by one that enforces all of 0079's rules unchanged plus this one.

### Visible; undo is NOT built in this wave

Every rule booking is listed per client (`GET .../rule-postings`). The firm summary reads the same
rows (`booking_proposal.rule_id IS NOT NULL AND status = 'approved'`) for its `rule_postings`
activity, which backend-firm-ext owns.

Contract decision 4 makes undo depend on an existing un-match or reversal path in the bank module.
There is none. `bank_transaction_immutable_once_imported` (0065) refuses `reconciled -> unmatched`
("correct it with a reversing entry, not by unreconciling"), and `BankService` has no reversal
method. **So undo is not built.** No `rule-postings/{id}/undo` route exists, and every item reports
`undoable: false`. To correct a wrong rule booking today, a person posts a reversing journal entry
by hand and retires or caps the rule. The ledger stays append-only either way.

## Alternatives considered

| Option | Rejected because |
|---|---|
| One rule per firm, applied to every engaged client | Crosses tenants (FR-BNK-006). One client's "Staples is office supplies" is not another's. |
| Check the creator's authority only when the rule is made | A revoked accountant's rule would keep booking in a client's ledger. IAM-030 evaluates authorization per request against current state, and a rule is a standing request. |
| Book as an anonymous system actor | Would skip the ledger's and the payment service's own authorization of the actor. The creator is the person whose authority the booking carries. The audit still says the system did it. |
| A dedicated posting path for rule bookings | A second write path into the ledger beside the Bank screen's (non-negotiable #1). |
| Apply rules to every pending proposal on each refresh | A rule made today would sweep up a backlog nobody chose to auto-approve. Only proposals created after the rule exists are offered to it. |
| Reactivate a suspended rule whoever remembers it | The rule would then book under the original creator's lapsed authority. |
| Build undo by deleting or unmatching the reconciliation | 0065 forbids unreconciling, and deleting a posting violates FR-GL-003/CMP-009. |

## Consequences

- A rule books only as long as its creator holds `reconcile bank_transaction` on that client.
  Offboarding an accountant suspends their rules on the next import, and someone else must remember
  them again.
- **RLS limits what the authority check can see.** `authorize()` reads `role_assignment` under the
  session's RLS. A firm creator's grant is administration-scoped (IAM-107), so it is visible from
  the client's session, the firm's session and the jobs. A rule created by one of the client's own
  users has an organization-scoped grant. That grant is not visible from a firm session, so an
  import made by firm staff would suspend that rule. This fails closed: nothing is booked, the rule
  shows as suspended, and remembering again reactivates it. Rules are created through the firm
  route, so this is an edge case.
- A sales invoice issued after its payment was imported is still offered to rules only at the next
  import, feed sync or backfill (ADR-110's existing gap).
- Undo needs a bank-module un-match or reversal path first. When one exists, `undoable` and the undo
  route can be added without changing this table.
- Down migration: documented in the 0082 header. Restore 0079's guard, drop
  `booking_proposal.rule_id`, then drop the trigger, function and table. The import, generation
  and decide paths shipped with 0082 treat a missing table as "no rules" (savepoint, logged); only
  the new `/rules` and `/rule-postings` routes need the table.
