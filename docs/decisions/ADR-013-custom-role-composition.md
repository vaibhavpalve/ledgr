# ADR-013: Custom role composition and the authoring ceiling

- **Status**: Accepted
- **Date**: 2026-08-23
- **Supersedes**: the "Custom roles" section of [ADR-011](ADR-011-authorization-model.md)

## Context

IAM-036: *"Custom roles can be composed by organization admins, but only from permissions they
themselves hold — no privilege escalation by role authoring."*

ADR-011 shipped a first pass: a subset check of the requested permissions against what the author
could exercise in the organization. Building the escalation tests this ADR is paired with exposed
two holes and one missing feature.

**Hole 1 — no authority check.** Nothing verified the author was an organization admin. Any
authenticated user could call `create_custom_role`. The subset check limited *what* they could put
in a role but never asked whether they could define one.

**Hole 2 — conditioned grants counted toward the ceiling.** ADR-011 argued explicitly that an
Approver capped at €5,000 composing an uncapped role was safe, "because the composed role is still
only useful once assigned, and assigning it is itself an authorized action (`manage user_role`) that
a capped Approver does not hold." That reasoning assumes the author is *only* an Approver. IAM-036's
subject is the organization admin — who holds `manage user_role` by definition. Someone holding both
could author an uncapped role and assign it to themselves. The conclusion was right about the
mechanism and wrong about who the actor is.

**Missing — nesting.** "Compose" implies building a role from other roles, not only from raw
permissions, and that is the escalation route the requirement's wording most invites: never name
`post journal_entry`, just include `Accountant`.

## Decision

### Three gates, in order

`AuthorizationService.create_custom_role` now runs:

1. **Authority.** The author must hold `manage user_role` in the target organization, checked
   through `authorize()` — the same library every other decision goes through, not a parallel check.
   Holding the permissions that would go into a role is not authority to define one.
2. **Validity.** Every component role must exist, be visible to this organization, not be archived,
   and not drag an organization-scope role into an administration-scope one. Failures raise
   `RoleCompositionError`, distinct from `PrivilegeEscalationError` so an audit trail can tell "you
   tried to grant yourself more" from "that role id is wrong."
3. **Ceiling.** The *flattened effective set* — explicit permissions plus everything every component
   bundles — must be a subset of what the author holds unconditionally.

Checking the flattened set at gate 3 is what closes nesting: a component contributing a permission
the author lacks is refused exactly as if they had typed it out. `PrivilegeEscalationError.via` maps
each missing permission to the component that contributed it, so an author nesting a sixty-permission
role learns which one was the problem rather than only that the composition was refused.

### Composition is flattened at creation, never resolved at evaluation

The effective set is written into `role_permission` as if listed by hand. `role_component` records
what it was built from and is **never consulted when a request is authorized**.

Resolving components at evaluation time would mean a component role gaining a permission later
silently widens every role that nests it — widening what the author was authorized to hand out,
with no new check and nobody accountable. Flattening pins a composed role to what its author
actually held at the moment they were checked.

Two consequences follow, both good. Evaluation stays a flat join — no recursive CTE ever runs on the
request path. And nesting is transitive *for free*: every stored role's `role_permission` rows are
already its full closure, so composing from a role that was itself composed picks up everything it
inherited in one flat union. Neither the service nor the repository recurses anywhere.

### A conditioned grant does not confer the permission

`unconditionally_held_permissions` (`ra.conditions = '{}'::jsonb`) replaces ADR-011's
`permissions_held_in_organization`. An Approver capped at €5,000 does not hold "approve purchase
invoices"; they hold "approve purchase invoices up to €5,000", which is not a thing this model can
put in a role — conditions live on assignments, not on permissions or roles.

This is deliberately strict: a Bookkeeper restricted to two journals cannot author any role
containing `post journal_entry`. That is the correct reading of "only from permissions they
themselves hold," and the same default-deny posture as the rest of the library. The restriction is
also escapable in the legitimate way — the same person holding an *unconditioned* grant of that
permission elsewhere in the organization may compose it, because they now do hold it outright.

### Assignment carries the same ceiling

`assign_role` is included even though the requirement names authoring, because without it every
composition check is theatre: an admin barred from *writing* a role containing `post journal_entry`
could simply assign the existing `Accountant` role instead. Same two gates — `manage user_role` in
the organization, then the role's whole bundle within the granter's unconditional ceiling.

For an administration-scoped assignment the authority is checked at the organization that **owns**
that administration, since `manage user_role` is organization-level and an administration target
would be a resource-scope mismatch. Resolving the owner through the repository means an
administration the granter cannot see yields no owner and the assignment is refused.

### Cycles

Structurally impossible: `role_immutable_trg` fixes a role's identity at creation and
`role_component` has no UPDATE or DELETE path, so an edge can only ever point at a role that already
existed. A recursive guard exists anyway, because "impossible" here rests on two separate invariants
holding at once and a cycle would mean an unbounded walk in any future consumer of the table. The
guard runs with invoker privileges (RLS applies to its walk), which is acceptable precisely because
it is a second line rather than the primary guarantee — making it `SECURITY DEFINER` would widen the
BYPASSRLS surface ADR-003 deliberately confined to two functions.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Resolving components at evaluation time (true dynamic inheritance) | A component gaining a permission later would silently widen every role nesting it, with no new authorization check and nobody accountable — and it would put a recursive CTE on the request path. |
| Copying a component's permissions in the UI and storing no link | Identical rows, but nothing records that the role *was* composed, which access reviews (IAM-040+) need to answer "where did this bundle come from." `role_component` costs one table and buys provenance. |
| Letting a conditioned grant count toward the ceiling, and instead carrying the author's conditions onto the composed role | Conditions vary per assignment by design (ADR-011); baking one author's cap into a role definition would freeze it for everyone assigned that role, which is the modelling error IAM-033 exists to avoid. |
| Treating a conditioned grant as conferring nothing at all, including for `authorize()` | The condition is a narrowing of a real grant — the Approver genuinely may approve within their cap. Only the *authoring ceiling* needs the stricter reading, because a role definition cannot carry the narrowing. |
| Checking authority with a direct `manage user_role` repository lookup rather than through `authorize()` | Would be a second authorization implementation, which CLAUDE.md's third non-negotiable forbids. Going through `authorize()` means composition authority inherits expiry, revocation, scope narrowing and conditions automatically. |
| Allowing an administration-scope role to include an organization-scope one, relying on `role_permission_scope_guard_trg` to reject the resulting rows | The database would refuse it, but with an error about permissions rather than about the component the author actually named. |
| Distinguishing "no such role" from "another organization's role" in the error | Would let an author probe for the existence of roles in organizations they cannot see — the same SEC-008 reasoning as `InvalidCredentialsError`. |

## Consequences

- **ADR-011's conditioned-grant paragraph is struck through rather than deleted**, with a note
  explaining the error. The reasoning was wrong in a specific, instructive way — it got the
  mechanism right and the actor wrong — and silently editing it would erase the lesson.
- **Escalation routes are covered by mutation testing, not just by tests passing.** Eight guards
  were each broken in turn to confirm a test catches them: authority check removed; ceiling checked
  against listed permissions only (ignoring nesting); conditioned grants counted; `assign_role`
  ceiling removed; component scope guard removed; components resolved dynamically; cross-organization
  component allowed; archived component allowed. All eight were caught, each by the test that should
  catch it.
- **`Grant`-time conditions and authoring interact in a way worth knowing.** An organization whose
  admins all hold only conditioned grants can compose nothing containing those permissions. That is
  intended, but it means an organization that leans heavily on conditions may find role authoring
  needs an Owner. Owner holds everything unconditionally, so this is a friction, not a dead end.
- **No HTTP endpoint**, consistent with every prior auth ADR. `create_custom_role` and `assign_role`
  are the layer a future role-management UI calls.
- **`role_component` is written but never read by anything yet** beyond the tests. Its consumers are
  access reviews (IAM-040+) and a role-management UI, neither built.
- **`0011_custom_role_composition.sql` has not been executed.** Parse-checked against the Postgres
  dialect only; CI applies it for real, as with 0009 and 0010.
