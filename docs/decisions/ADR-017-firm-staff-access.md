# ADR-017: Firm staff access

- **Status**: Accepted
- **Date**: 2026-08-26
- **Implements**: IAM-107, IAM-108 (PRD §8.6)
- **Closes**: the known limitation recorded in [ADR-016](ADR-016-client-rights-floor.md)

## Context

> **IAM-107.** Firm staff access is granted per client administration, not per firm. A new firm
> employee starts with access to zero clients.
>
> **IAM-108.** Firm staff grants may carry an expiry, and support seasonal or interim staff working
> on a defined client set for a defined period.

Most of IAM-107 was already true, and saying so is more useful than re-implementing it:

- **"Not per firm"** holds because an organization-scoped grant cascades only to the administrations
  that organization *owns* (ADR-011's `_scope_covers`), and a firm never owns its client's
  administration. No grant held at a firm has ever reached a client's books.
- **"Starts with access to zero clients"** holds because access is a `role_assignment` row or it does
  not exist. There is no bulk path, no wildcard scope, and nothing that runs on hire.
- **Expiry** has existed since 0009 and is a predicate in the evaluation query (IAM-035), so a lapsed
  grant stops working with no sweep.

Neither of the first two is enforced by a check; both are consequences of shapes chosen earlier. So
this ADR is mostly about the thing that *was* missing.

## Decision

### `granted_by_organization_id` — the gap ADR-016 flagged

An administration-scoped grant on a client's books looked identical whether the client's own Owner
made it for their bookkeeper or the firm made it for its accountant. ADR-016 recorded this as a real
defect: the client access profile cap could only *guess* which side a user was on, and guessed with a
heuristic (does the holder have an organization-scoped grant at an engaged firm?) that
misclassified a firm employee with no organization-scoped grant at their own firm.

IAM-107 makes the distinction a requirement rather than an inconvenience, so it becomes a column.
`role_assignment.granted_by_organization_id` is set by a trigger from `app.current_org_id()` — never
from a parameter. A caller cannot supply it, cannot forget it, and cannot spoof it; the trigger
overwrites whatever is passed. Same derive-don't-trust pattern as `sync_fiscal_year_tenant_columns`
in 0001.

The profile cap now reads provenance directly: a grant made by the owning organization is the
client's own and is capped; one made by an engaged firm is firm staff and is not. The heuristic and
its documented limitation are both gone.

It is left nullable rather than backfilled-then-`NOT NULL`, because the backfill would run as
`ledgr_migrator` with no tenant context and `FORCE ROW LEVEL SECURITY` would show it zero rows. No
`role_assignment` rows exist at this point in the sequence, so in practice every row has it, and a
NULL reads as client-side — the safe direction (capped, not exempt).

### The engagement is the bound, not a permission ceiling

ADR-013 made `assign_role` refuse to confer what the granter does not hold. That rule **cannot** apply
to firm staff assignment, and the reason is structural rather than a concession:

- A ceiling based on the **granter's** holdings makes Firm Manager unable to do the one thing §8.4
  gives them ("assigning firm staff to clients"), because the same section explicitly denies them
  ledger permissions.
- A ceiling based on the **firm's** holdings deadlocks: a newly engaged administration is one no firm
  staff member holds anything on yet, so nobody could be assigned the first role.

What bounds a firm staff grant is the **active engagement** — the client's own consent to this firm
doing this administration's books, which the client can revoke at any time (IAM-105). Within that
consent, how a firm allocates its own staff is the firm's business. The client's protections do not
depend on this ceiling: IAM-102 bounds what the firm may give the *client's* users, and IAM-105's
floor is untouchable by any of it.

`FirmStaffAccessService` therefore does not route through `assign_role`, whose authority and ceiling
are computed at the organization owning the scope — the client. Firm staff assignment is authorized
at the **firm** (`manage user_role` there) and bounded by the engagement.

One limit survives, and it is the important one: **IAM-063 still applies**. A Firm Manager may assign
Accountant to a colleague but not to themselves, which is exactly §8.4's "cannot post to a client's
ledger without also holding Accountant on it" read together with "nobody grants themselves
permissions they do not hold".

### A firm staff grant is a `role_assignment`, not a new table

"Granted per client administration" means the same row shape as any other grant, so it expires the
same way, is revoked the same way, and is evaluated by the same query. A parallel `firm_staff` table
would be a second access path to keep in step with the first — and the evaluation query would have to
consult both or silently miss one.

### The client set is validated in full before anything is written

"A defined client set for a defined period" is one decision. A seasonal grant covering six clients
where the engagement on the fourth has lapsed must not leave three granted — that would be a
different, silently smaller decision nobody made. All engagements are checked, then all rows written.

An `expires_at` already in the past is refused rather than written: a grant that confers nothing from
the moment it is made would still appear as access in a listing until someone checked the date.

### Revocation is keyed on provenance

`revoke_firm_staff_assignments` filters on `granted_by_organization_id`. Without it, a firm taking a
staff member off a client would also revoke that client's own users, who hold identically-shaped rows
on the same administration — a cross-tenant write dressed as housekeeping.

## Alternatives considered

| Option | Rejected because |
|---|---|
| A `firm_staff_access` table separate from `role_assignment` | Two access paths, and the evaluation query would have to read both or miss one. IAM-107's "per client administration" describes the grant's scope, not a different kind of object. |
| Applying ADR-013's granter ceiling to firm staff assignment | Makes Firm Manager unable to do its §8.4 job — that role holds no ledger permissions by design. |
| Bounding firm staff grants by what the firm's staff already hold on the administration | Deadlocks on a newly engaged client: nobody holds anything yet, so nobody can be granted the first role. |
| Inferring firm-vs-client from whether the holder has a grant at an engaged firm (ADR-016's heuristic) | Misclassifies a firm employee with no organization-scoped grant at their own firm. Provenance is exact; the heuristic was a guess made because the data was missing. |
| Letting the application supply `granted_by_organization_id` | Spoofable, and forgettable. Derived by trigger from tenant context instead, which is neither. |
| Writing the grants that pass and reporting the ones that failed | "A defined client set" is one decision; a partial application is a smaller decision nobody made. |
| Enforcing the engagement requirement only in the service | Would not bind psql, the operator scripts, or a future service writing the row directly. The trigger is the guarantee; the service check exists to give a good error message. |

## Consequences

- **Ten guards are mutation-tested**: engagement requirement removed and made lazy, firm-level
  `manage user_role` dropped, organization-scope roles made grantable per client, the IAM-063
  self-grant check dropped, an already-expired grant accepted, revocation ignoring provenance, the
  profile cap ignoring and inverting provenance, and the repository's engagement mirror removed. All
  ten produced real test failures.
- **One existing test needed updating, and the change is the point.** `test_firm_staff_are_not_capped_by_the_profile_they_set`
  previously relied on the heuristic; it now states explicitly that the grant was made from the
  firm's context. Provenance is declared rather than inferred.
- **IAM-109 is not built** — the client's view of which firm users hold access, with what role, since
  when, and last access. `list_firm_staff_grants` answers most of that shape from the firm's side;
  the client-facing listing, and "when each last accessed it" (which needs an access log that does not
  exist), are the missing pieces.
- **IAM-110 is not built** — revoking an engagement does not terminate firm sessions for that
  administration. `revoke_access` revokes grants, and the evaluation query stops honouring them
  immediately, but a session established beforehand is not itself invalidated; `api.auth.sessions`
  has `revoke_all_for_user` and nothing scoped to an administration.
- **Revoking a `firm_engagement` does not cascade to the grants made under it.** The engagement is
  checked at INSERT, not on every evaluation, so staff grants outlive the engagement that authorized
  them. That is a real gap and belongs with IAM-110's work; until then, revoking an engagement must
  be accompanied by revoking the grants.
- **`0016_firm_staff_access.sql` has not been executed.** Parse-checked against the Postgres dialect
  only; CI applies it and runs the triggers for real.
