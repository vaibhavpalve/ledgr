# ADR-109: The firm home reads across clients through RLS, the grant join and authorize() per administration

- **Status**: Accepted
- **Date**: 2026-10-08
- **Builds on**: ADR-003 (RLS), ADR-011 (scope cascade), ADR-059 (the switcher's grant join), ADR-087 (VAT returns), ADR-108 (bank feed); siblings ADR-110 (proposals) and ADR-111 (question threads)

## Context

The accountant's firm home (docs/firm-home/contract.md) answers, for every client at once,
"what needs me, what am I waiting on, what is due": FR-FRM-001 (portfolio status), FR-FRM-002
(the cross-client work queue), FR-FRM-004b (bulk assignment, partial), FR-UX-005. A firm can have
200 clients, so it must be fast at that size, and it is the first surface whose target is not one
administration but "all of mine".

Three non-negotiables meet here: tenant context on every request with RLS as the first line
(IAM-001-005), one authorization library with default deny (IAM-030-037), and no float money
(NFR-031). `require_permission` resolves exactly one target - an organization, or the
administration named in the path - and neither fits: an organization-scoped check at the firm
finds nothing for firm staff, whose grants are administration-scoped by design (IAM-107).

## Decision

**Reads: three layers, no shortcut.**

1. **RLS.** The firm home runs in the ordinary tenant session. `app.has_administration_access`
   already scopes a firm session to every administration the firm has an active engagement on.
   No SECURITY DEFINER function, no ops connection.
2. **The person's grants.** The candidate set is the administrations the user holds a live grant
   on - the switcher's own join (`api.firm.switcher_repository._BASE`, read through
   `SqlSwitcherRepository`), so the firm home and the switcher can never disagree.
3. **`AuthorizationService.authorize()` per administration**, with `view report` (the permission
   FR-UX-005's mobile home already rides: Owner, Accountant, Bookkeeper, Viewer hold it). A denial
   leaves that client out. An empty portfolio is an empty answer, not a 403, as the switcher.

The edge is `api.firm.worklist_access.require_portfolio_permission`: a dependency carrying the same
`PERMISSION_MARKER` as `require_permission` (scope "administration", no path parameter), so the
enforcement middleware and the authz/audit coverage checks see a declared requirement. The
decision is still the library's; the module adds no rule of its own.

To keep 200 `authorize()` calls cheap, the service runs on a `SqlAuthorizationRepository` subclass
that remembers, **for one request only**, the grant rows per (user, permission), the owners of the
portfolio's administrations (read in one query) and the Owner check. Nothing outlives the request,
so every decision is still against current state (IAM-034).

IAM-109's access register is not written for portfolio reads: counting a client's open items is not
opening their books, and a row per client per page load would make the register meaningless.

**Writes.** Snooze is checked like any action on one client - `view report` on the administration
in the path, `allows_cross_client` (the firm home is inside no client, and nothing here writes to
books), audited as configuration. Assignment is §8.4's "assigning firm staff to clients": `manage
user_role` at the caller's organization; then per administration it must be visible (RLS) and the
assignee must hold a live grant on it, failures reported per administration. Neither grants
anything: `client_assignment` is a label on the queue, not a `role_assignment`. Marking the summary
read writes only the caller's own `users` row (declared as a portfolio route, audited).

**Precompute strategy: none - set-based reads.** No summary table, no materialized view, no job.
Every read takes the whole portfolio as one `uuid[]` and answers with GROUP BY / DISTINCT ON /
LATERAL, so a page costs a fixed ~8-9 queries whatever the number of clients; filtering, chips,
sorting and paging happen in memory over at most a few hundred rows (`api.firm.worklist_model`,
pure and table-tested). Migration 0078 adds the indexes the predicates need
(`expense(administration_id, gross_amount)`, unmatched `bank_transaction(administration_id,
booking_date)`, `role_assignment(granted_by_organization_id)`); the rest existed. A cache is
the next step only if measurement says so; it would have to be invalidated by every posting,
import, proposal and message, which is exactly the staleness this avoids.

**Bucket exclusivity (contract decision 5).** Per unmatched bank line, in SQL: a pending HIGH
`booking_proposal` → `auto_bookings`; else a debit with no receipt of exactly that amount that no
other line has settled (discarded captures excluded) → `missing_receipts`; else `bank_to_match`.
`to_book` is draft/ready receipts not covered by a pending proposal (`document_kind = 'expense'`).

**booked_until (decision 4).** The day before the oldest unmatched line, or the last line's date
when none is unmatched; capped at the least recent `last_synced_at` over the client's current
linked/expired/failed feeds, with `booked_until_capped_by_feed` saying so. `months_behind` counts
the whole calendar months between it and the last completed month (a mid-month date leaves that
month unfinished); "books behind" is more than one.

**VAT status.** Only filed returns are stored (ADR-087), so the rest is derived from the period:
filed (vat_return or `vat_filed`) → locked = `ready_to_file` → nothing posted = `not_started` →
running, or unmatched lines / unposted receipts in it = `in_progress` → else `ready_to_review`.
The queue shows one period per client: the earliest ended unfiled period after the last filed one
(or, with nothing filed in Boeklite, the most recent ended one - a client onboarded mid-year filed
earlier periods elsewhere), else the running one, else the last filed one. Deadlines list every
period due in the window, grouped by (label, due date). VAT only: ICP is not modelled and payroll
is not in Boeklite.

**My move vs waiting on client (decision 3).** My move: auto_bookings, to_book, bank_to_match,
VAT ready_to_review, and any open question thread awaiting the firm (the client replied).
Waiting on client: missing_receipts, open threads awaiting the client (their oldest
`last_message_at` feeds `waiting_on_client_since`), and a broken feed. `counts.open_questions` is
the total of open threads either way.

**`assigned=me` is "Mine + unassigned".** It admits clients assigned to the caller **or to
nobody** (`AssignedFilter.mine`), and `chip_counts` follow the same rule. A firm that has not
assigned anyone yet would otherwise land on an empty "Mine" list with every chip at 0 while the
count strip shows work (QA, 2026-10-09). `assigned=<user_id>` stays exact. The summary takes no
`assigned`: its count strip covers the whole portfolio, and the web labels it so.

**Risk (decision 7).** `days to the next unfiled VAT deadline (90 when none) − ceil(my-move items /
10) − 3 for a broken feed − 5 when more than one month behind`; ascending. Snoozed clients leave
`my_move` and `waiting_on_client` only.

**Login hook.** `SqlSessionRepository.create` shifts `last_login_at → previous_login_at` in a
savepoint that never fails a sign-in (a build deployed before 0078 still signs people in).

## Alternatives considered

| Option | Rejected because |
|---|---|
| A SECURITY DEFINER function returning the portfolio | A second path around RLS; contract decision 1 forbids it. |
| Organization-scoped `require_permission` on the firm | Firm staff hold no organization-scoped grant (IAM-107); it would deny exactly the users the page exists for. |
| Adding the routes to `AUTHORIZATION_EXEMPT_PATHS` | The routes do read client data; per-administration authorization is the point, and an exemption would hide it. |
| `require_permission` per administration in a loop of sub-requests | 200 grant queries per page; the request-scoped repository gives the same decisions with one. |
| A precomputed summary table refreshed by a job | Stale by design, and invalidated by every write in five modules; not needed at 200 clients. |
| A new `view firm_worklist` permission | Needs a catalogue/matrix change and Appendix A conformance work for no difference in who may see it. |

## Consequences

- Other cross-client routes (ADR-110's proposals, ADR-111's inbox) can declare
  `require_portfolio_permission(...)` and get the same three layers.
- `users` has no name column; staff and assignee names are the e-mail's local part until one is
  added.
- Reads `booking_proposal` (0079) and `question_thread`/`question_message` (0080): the firm home
  needs all three migrations applied; `question_message(administration_id, created_at)` should be
  indexed for the summary's "client replies".
- "Possible duplicates" in the summary is the exact half of FR-EXP-001g only; the trigram half
  stays per receipt.
- Reversal of 0078 is documented in the migration header.
