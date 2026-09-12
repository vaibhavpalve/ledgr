# ADR-016: The client rights floor

- **Status**: Accepted
- **Date**: 2026-08-25
- **Implements**: IAM-105 (PRD §8.6)
- **Completes**: the partial floor in [ADR-015](ADR-015-client-access-profiles.md)

## Context

> **IAM-105.** Regardless of profile, the client's Owner always retains: read access to their own
> source documents and filed returns, export of their complete data, visibility of their own audit
> log, the ability to revoke the firm's access, and the ability to manage their own users' sign-in
> security. No firm setting can remove these.

Everywhere else in this system access is granted and can be lost. The floor inverts that: six rights
that survive every mechanism the codebase has for taking access away. The inversion is statutory
rather than aesthetic — a Dutch business is legally required to retain its own books (CMP-001, PRD
§10.4's seven-year retention), so an accountant who could lock a client out of their own filed
returns would put the client in breach of an obligation they cannot delegate.

ADR-015 implemented the administration-scoped part as a side effect of getting profiles right. This
completes it and defends it against every write path, not only the profile one.

## Decision

### The floor spans two scopes, and that is load-bearing

Four rights are administration-scoped (documents, filed returns, export, audit log); two are
organization-scoped (revoking the firm's access, managing sign-in security). The split is what makes
the last two **structurally** unreachable by a firm: a client access profile governs one
administration and cannot contain an organization-scope permission at all (0013's permission guard),
so no profile a firm can write even has a place to put them.

### Four independent layers

"No firm setting, no API call, no admin action" is a claim about every write path, so the floor is
defended at four:

1. **`authorize()` short-circuits.** A client Owner asking for a floor right is allowed *before
   grants are loaded*, before profile caps, before conditions. This is the guarantee that holds even
   if a later layer is wrong.
2. **`profiles.resolve()` adds the floor back** after IAM-104 restrictions — order matters, so a firm
   switching reports off still cannot remove the Owner's export — and `_enforce_ceiling` excludes it
   from IAM-102, because the floor is the client's own and not the firm's to confer or withhold.
3. **Migration 0015's triggers** stop the Owner *assignment* that carries the floor from being
   dismantled: no expiry, no attribute conditions, and the last Owner of an organization cannot be
   revoked.
4. **Absence of privilege.** `role_permission` has no DELETE grant, so a floor permission cannot be
   removed from the Owner role's bundle; the `role` update policy refuses writes to system roles, so
   `Owner` cannot be archived.

### Triggers, not policies — this is the "no admin action" clause

Layer 3 uses triggers deliberately. Row-level security is bypassed by `BYPASSRLS`, which `ledgr_ops`
holds and the operator scripts in `apps/api/scripts` connect as. A trigger is not: Postgres runs
`BEFORE` triggers for every writer, superusers included. So these guards bind support tooling and a
DBA at a psql prompt, which a policy would not.

The service-level check in `revoke_assignment` duplicates the trigger on purpose: a trigger cannot
produce a good error message, and an application check cannot bind psql.

### Three specific removals, and why each is defended

| Removal | Why it is the dangerous one |
|---|---|
| An expiry on the Owner assignment | The most *silent* removal available — the floor lapses on a date nobody remembers, with no action taken and nothing to notice. |
| A condition on the Owner assignment | Withholds a floor right without touching a profile at all: an `ip_allowlist` matching nothing would do it. |
| Revoking the last Owner | An organization with no Owner has nobody holding the floor, so every other layer becomes vacuous. |

Handover stays possible — the floor requires *an* Owner, not a particular one. A requirement that
froze an organization's ownership permanently would be its own kind of harm.

### The floor follows the Owner ROLE, not a permission

Checked by role membership, not by "holds these permissions". A permission-based test could be
satisfied by composing a custom role that happens to hold them (IAM-036 permits exactly that), which
would let someone manufacture the floor for themselves. §8.4's Owner is a specific system role, fixed
by migration and not composable — the same reasoning as IAM-064's Owner acknowledgement (ADR-014).

### `view vat_return` added to the catalogue

IAM-105 says "source documents **and filed returns**". Appendix A grades *preparing* and *filing* a
return but not reading one back, so `view vat_return` is declared as an `EXTENSION_CAPABILITY` — an
addition outside the Appendix A grid, so `test_appendix_a_conformance.py` still compares the matrix
to the document. A statutory filing the client cannot read is not a record they can be said to
retain.

### A bug this work exposed, and fixed

Writing the "a non-Owner is still capped" test surfaced a real defect in ADR-015's profile cap: it
identified "the client's own users" as *holding an organization-scoped grant at the owning
organization*, which is **under-inclusive**. Client staff commonly hold only an administration-scoped
role (a client Bookkeeper on their own books), and those users escaped the cap entirely — a profile
that was supposed to restrict them restricted nothing.

The discriminator is now inverted: an administration-scoped grant is *firm-side* if the holder has an
organization-scoped grant at a firm with an **active engagement** on that administration, and
client-side otherwise. That correctly caps client staff however their role is scoped, and correctly
exempts firm staff.

**Known limitation, deliberately not solved here.** A firm employee holding no organization-scoped
grant at their own firm is indistinguishable from client staff and would be capped. Both kinds of
user can hold an administration-scoped grant on the same administration, and `role_assignment` does
not record which organization a grant was made *from*. Settling it properly means adding that —
either a `granted_by_organization_id` column or linking firm-staff grants to the `firm_engagement`
they derive from, which IAM-107/IAM-108 (per-client firm staff grants, with expiry) will want anyway.
Flagged at the point of the check in `service.py`.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Enforcing the floor only in `profiles.resolve()` (ADR-015's approach) | Covers the profile path and nothing else. IAM-105 says *no* firm setting, API call, or admin action — a revoked Owner assignment removes the floor without any profile being involved. |
| Enforcing it only as database triggers | Triggers cannot express "grant this permission"; they can only stop the Owner assignment being dismantled. The `authorize()` short-circuit is what makes the right actually apply. |
| Making the floor a row in a table the application reads | One more thing that could be edited to shrink the floor. `client_rights_floor_statement` exists so an auditor can read the floor in SQL, but it is never consulted when authorizing and has no write path from `ledgr_app` at all. |
| Granting the floor to anyone holding the floor permissions | Manufacturable by composing a custom role. The floor follows the Owner role. |
| Granting the floor to any Owner, at any organization | An Owner at a *firm* would inherit the floor on every client's books. The organization checked is always the one that owns the target. |
| Refusing to revoke any Owner assignment at all | Would freeze ownership permanently — no handover, no removing a departed founder. The floor needs *an* Owner, so only the last one is protected. |
| Letting an Owner assignment carry an expiry, and warning near the date | A warning nobody reads is not a guarantee, and the failure mode is total loss of the floor. Refused at INSERT instead. |
| Adding `view vat_return` as an Appendix A row | The conformance test would reject it, correctly — inventing matrix rows is the drift that test exists to catch. |

## Consequences

- **Thirteen defences are mutation-tested.** Each was broken in turn — the short-circuit removed, the
  floor granted to non-Owners, granted at the wrong organization, widened to every permission; both
  last-Owner guards removed; Owner expiry and conditions allowed; the profile's restore removed; the
  IAM-102 exemption removed; two floor clauses dropped; the organization/administration split
  mis-declared — and all thirteen produced real test failures.
- **The floor is an exception to the permission model**, so its blast radius is asserted explicitly:
  `test_the_floor_grants_only_its_six_rights` pins that an Owner under `Capture only` still cannot
  post, reverse, file, release or lock.
- **IAM-036 still applies to the Owner role itself.** An Organization Admin cannot assign themselves
  Owner to acquire the floor, because Owner confers far more than they hold.
- **The seeded test organizations are now single-Owner**, which means `two_organizations` cannot have
  its Owner revoked. That is correct behaviour, and any future test needing a revocation must seed a
  second Owner first.
- **IAM-105's "revoke the firm's access" is a permission, not an implemented action.** The client's
  Owner holds `revoke firm_engagement` and no firm setting can take it away — but the endpoint that
  performs the revocation, and IAM-110's immediate session termination, are not built.
- **`0015_client_rights_floor.sql` has not been executed.** Parse-checked against the Postgres
  dialect only; CI applies it and runs the triggers for real. The DB-level attacks in
  `tests/integration/test_authorization_isolation.py` are the ones that verify layers 3 and 4, and
  they skip without a database.
