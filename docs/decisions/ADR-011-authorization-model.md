# ADR-011: The authorization library

- **Status**: Accepted
- **Date**: 2026-08-23

## Context

CLAUDE.md's third non-negotiable — "one authorization library, used everywhere; there is no second
implementation" — has been an IOU since the first migration. Every prior auth ADR defers to it by
name: ADR-008's `AlwaysRequireMfaPolicy` is explicitly a stand-in "until the role/permission system
(IAM-030+) exists," ADR-010's `admin_reset_password` "does not verify the calling actor is
authorized," and 0001's `administration_update` policy comments that "whether a given role may edit
a given field is enforced by the application's authorization library." This ADR is that library.

The scope is PRD §8.3 (IAM-030 – IAM-037), §8.4's standard roles table, and Appendix A's
capability × role matrix. The design was reviewed before implementation at the user's request; this
records what was built and the decisions that were not obvious from the review.

## Decision

### Two things called "scope", separated

IAM-030 uses the word twice. `permission.resource_scope` is the level an action inherently operates
at — "manage organization settings" is organization-level, "post a journal entry" is
administration-level. `role_assignment.scope_type`/`scope_id` is *where a particular grant applies*.
Only the second is what IAM-032 governs. Conflating them was the single most likely way to get this
wrong, so they are separate columns on separate tables with different names.

Appendix A settles how they interact: Owner is an **organization**-scoped role showing **F** on
"Post journal entries," an administration-level capability. So an organization-scoped grant's
administration-level permissions must cascade to the administrations that organization owns. Org
Admin, also organization-scoped, shows **—** on the same row — not because the cascade is blocked
for that role, but because the permission is simply absent from its bundle. Scope and capability are
independent axes.

### IAM-032 is enforced in three places, structurally

"A grant at administration level cannot be widened to organization level by any code path" is a
claim that has to survive code nobody has written yet, so it is made true by construction rather
than by a check:

1. **The type system.** `AuthorizationRequest.target` is `OrganizationScope | AdministrationScope` —
   one value, not two nullable fields. A request naming both would be the widening bug (an
   administration grant matched against the administration while an organization grant matched
   against the organization, results unioned). It is not constructible.
2. **The evaluation code.** `_scope_covers` has no branch mapping an administration-scoped grant
   onto an organization target. The direction is absent from the function, not guarded against
   inside it.
3. **The database.** `role_assignment_immutable_trg` makes `user_id`, `role_id`, `scope_type`,
   `scope_id`, `conditions` and `expires_at` immutable after INSERT, so no UPDATE — from the
   application, from a support tool, from a psql session as `ledgr_app` — can widen an existing
   grant. Widening requires revoking and re-granting, which is a new row with a new
   `granted_by_user_id`. `role_permission_scope_guard_trg` separately refuses to bundle an
   organization-scope permission into an administration-scope role, so the widening cannot be
   smuggled in through role composition either.

The organization cascade keys on `administration.organization_id` — direct ownership — and never on
`firm_engagement`. That one choice is the whole of §8.4's "a Firm Manager cannot post to a client's
ledger without also holding Accountant on it": a firm never *owns* its client's administration, so
no organization-scoped grant held at the firm can reach it, and firm staff access is always a
separate, explicit, administration-scoped assignment. No special case for firms appears anywhere in
the authorization code.

### Nothing is cached (IAM-034, IAM-037)

`AuthorizationService.authorize()` reads live rows on every call. There is no permission set baked
into a token, no memoized effective-permissions object, no in-process grant cache. IAM-037's
60-second propagation bound is therefore met by construction rather than by tuning a TTL — a revoked
grant stops working on the *next request*, not within a minute of one. This costs a query per
decision; `api.mfa_middleware` and `api.auth.rate_limiting` already accept the same tradeoff for
the same reason.

### Attribute conditions narrow, never widen (IAM-033)

`role_assignment.conditions` is JSONB against a closed set of five keys — `amount_ceiling`,
`cost_centre_ids`, `journal_ids`, `period_ids`, `ip_allowlist` — validated by
`role_assignment_conditions_guard_trg` at write time and by `api.authz.conditions` at evaluation
time. A condition can only withhold a permission the role already carries, never add one, which is
what makes them safe to store per assignment.

Three rules make the evaluator fail closed, and each of them is a bug class rather than a
hypothetical:

- **An unknown key denies.** A typo'd `amount_cieling` that was silently ignored would turn a
  misspelling into unlimited approval authority. The trigger stops one being written; the evaluator
  is the second line.
- **A missing attribute denies.** A grant carrying an amount ceiling, evaluated against a request
  that supplied no amount, is not satisfied. This is what makes a route that forgot to declare where
  its amount comes from fail *safely*, with no additional machinery.
- **A malformed value denies.** A ceiling that will not parse is treated as unsatisfied rather than
  raised — a broken grant must not be able to take an endpoint down, and must not grant anything.

Amount ceilings are stored as decimal **strings**. `jsonb`'s number type is IEEE 754 double
precision, and NFR-031 puts every monetary value in the calculation path on decimal types; a ceiling
compared against transaction amounts is squarely in that path. The trigger rejects a numeric
ceiling, the evaluator rejects a Python float rather than coercing it, and
`api.authz.dependencies._json_body` parses request bodies with `json.loads(..., parse_float=Decimal)`
so a JSON number in a payload never becomes a float on its way to the comparison.

Appendix A's **C** (conditional) cells are modelled as conditions on the *assignment*, not as weaker
permissions in the role. A Bookkeeper's permitted journals and an Approver's amount ceiling vary per
person; putting them in the role would freeze them for everyone holding it, which is exactly what
IAM-033 exists to avoid.

**Device trust** is named by IAM-033 and is deliberately *not* a recognized key. There is no device
registry in this codebase to check it against (sessions record a user agent, not an attested device
key). By the unknown-key rule above, a grant carrying it is denied — so the requirement is visibly
unmet rather than silently satisfied by a stub that always passes.

### A developer cannot forget to call it

A FastAPI dependency only runs on routes that declare it, which is precisely the "silent gap"
`api.tenancy.TenantContextMiddleware`'s docstring warns about. Three mechanisms close it, failing at
different moments on purpose:

1. **`AuthorizationEnforcementMiddleware`** resolves the matched route *before* dispatch and refuses
   any route whose dependency tree contains no authorization requirement. The handler never runs, so
   a forgotten check leaks nothing while it is being forgotten. This works because FastAPI resolves
   each route's dependency graph at registration time into a readable `APIRoute.dependant` tree —
   the question is answerable by introspection, with nothing executed and no side registry to keep
   in step. Route matching uses Starlette's own `route.matches(scope)`, the same call the router
   makes a moment later, so the two always agree on which route a request belongs to.
2. **`tests/test_authz_coverage.py`** walks the same route table at collection time and fails CI
   before the code ever runs, naming the route. It needs no database, so it runs on every CI
   invocation — the same design as `tests/test_isolation_coverage.py` for IAM-005.
3. **`AUTHORIZATION_EXEMPT_PATHS`** is the entire opt-out surface: one named list, in one file,
   each entry carrying a reason. There is no decorator or flag that exempts a route from its own
   definition site, because skipping authorization should be visible in a reviewable list rather
   than scattered across the routes that did it. A second test fails if the list names a route that
   no longer exists, so it cannot quietly accumulate dead entries that widen the opt-out.

(1) and (2) both read the *route table*, not a registry an author must remember to update — a route
that exists is a route that is checked. They are kept as two mechanisms because a route CI never
exercises is still caught by the coverage test, and a route registered dynamically after startup,
which the coverage test cannot see, is still caught by the middleware.

The middleware answers **500**, not 403: a missing declaration is an application bug, and a 403
would be indistinguishable from an ordinary permission denial and could sit unnoticed while an
endpoint quietly refused everyone. The body names the route and both available fixes.

### Custom roles (IAM-036)

> **Superseded by [ADR-013](ADR-013-custom-role-composition.md).** The paragraph below about
> conditioned grants was **wrong**, and is kept here rather than quietly edited because the error is
> instructive. It argued that a capped author composing an uncapped role is safe since "assigning it
> is itself an authorized action that a capped Approver does not hold" — which assumes the author is
> *only* an Approver. IAM-036 is specifically about **organization admins**, who hold `manage
> user_role` by definition; someone holding both a capped Approver grant and `manage user_role` could
> author an uncapped role and assign it to themselves. ADR-013 excludes conditioned grants from the
> authoring ceiling and adds the authority check this version also lacked.

`create_custom_role` computes the union of what the author can exercise anywhere in the target
organization in one query and requires the composed bundle to be a subset. It deliberately does not
call `authorize()` once per candidate permission: that would be slower and wrong, since `authorize()`
needs a concrete target and concrete resource attributes that a role *definition* does not have.

~~An author whose own grant is capped by an attribute condition may compose a role carrying the
uncapped permission, because the cap lives on their assignment rather than on the permission. That
is not an escalation, because a composed role is inert until **assigned**, and assigning it is
itself an authorized action (`manage user_role`) that a capped Approver does not hold.~~

Custom roles are immutable once composed (archive and replace), for the same reason grants are:
editing a role already assigned to many users silently re-grants to all of them, with no new
authorization check and nothing attributable.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Baking the effective permission set into a short-lived token and re-validating it on a timer, as IAM-037's "token claims are short-lived and re-validated" hints at | IAM-037 sets 60 seconds as an upper bound, not a target. Evaluating live meets it trivially and removes an entire class of bug (a revoked grant that keeps working until a TTL lapses). If the per-request query ever becomes a measured bottleneck, a cache can be added behind this interface without changing a single call site — the reverse migration is far harder. |
| A general expression language for attribute conditions (CEL, a mini-DSL) | IAM-033 enumerates its conditions; a closed set of five keys matches the requirement exactly, is checkable by a database trigger at write time, and cannot express a condition that *widens*. An expression evaluator would need its own sandbox, its own review process for stored expressions, and could not be validated at INSERT. |
| A single `scope_id` foreign key, with separate nullable `organization_id`/`administration_id` columns instead of the polymorphic pair | A nullable pair invites exactly the request shape the type system now forbids — both set, both matched, results unioned. The polymorphic column costs a guard trigger for referential integrity (the same trigger-over-FK tradeoff `firm_engagement_guard` already makes in 0001) and buys an unambiguous "this grant applies at exactly one place." |
| Making the enforcement middleware a 403 rather than a 500 | Indistinguishable from a real permission denial in logs and dashboards; a route that forgot its check would look like a route users simply lacked access to. |
| Enforcing the declaration only in the coverage test, with no runtime middleware | The test cannot see routes registered after startup, and it can be skipped, deselected, or run against a stale import. A guarantee about authorization should not be one `-k` flag away from not holding. |
| Enforcing it only in middleware, with no coverage test | The failure would surface as a 500 in production on the first call rather than a named route in CI. The point is to catch the mistake before it ships, not to fail well once it has. |
| Deferring the resource-attribute check to the handler (`decision.require(amount=...)` after loading the record) | Anything checked after the handler starts is checked after side effects have begun; rejecting a mutation post-hoc would need transaction rollback coordination the dependency layer has no business owning. Declaring attribute *sources* up front keeps every decision pre-handler, and an unsupplied attribute already denies. |
| Modelling Appendix A's "C" cells as separate, weaker permissions | The limits vary per person (this Approver is capped at €5,000, that one at €50,000). A weaker permission would freeze the limit for everyone holding the role. |
| Shipping `device_trust` as a condition that passes when no device registry is present | A condition named after a control that does not exist, silently satisfied, is worse than an absent one — it would read as implemented in a review of the grant. |

## Consequences

- **`AlwaysRequireMfaPolicy` (ADR-008) can now be replaced.** IAM-011's "mandatory for any user with
  write permission" is finally computable: a `RoleBasedMfaRequirementPolicy` would ask this library
  whether the user holds any write permission. Deliberately **not** done here — that policy is a
  behaviour change to MFA enforcement and belongs in its own change with its own tests. The
  conservative superset stays in force until then.
- **Two demo routes now carry real checks**, and the IAM-005 isolation tests needed real users and
  grants to reach them. `tests/support/seed.py` now seeds an Owner per organization and grants
  through the same tenant-scoped INSERT the application uses, so RLS and every 0009 guard trigger
  apply to seeding exactly as they would in production.
- **A latent break in those integration tests was found and fixed in passing.** `make_token` issued
  tokens with no `sub` claim, which `MfaEnforcementMiddleware` (added in ADR-008) rejects with a 403
  — so the DB-backed isolation tests could not have been passing in CI since that middleware was
  wired in. They skip without a database, which is why it went unnoticed locally. `make_token` now
  carries `sub` and `mfa_verified`.
- **`assert_cannot_fetch_foreign_record` gained an `expected_status` parameter.** A route with an
  administration-scoped check answers 403 (the permission check runs before the query) rather than
  404 (RLS filtered the row). Both are indistinguishable between "another tenant's record" and "a
  record you lack permission on," which is the property that matters, but the test now asserts the
  status its route actually produces instead of shrugging at either.
- **No HTTP endpoint exists for granting or revoking roles.** `AuthorizationService` evaluates and
  composes; the routes that would let an Org Admin assign a role are not built, consistent with
  every prior auth ADR's stated boundary. `role_assignment`'s RLS and guard triggers are what those
  routes will be built against.
- **Access reviews (IAM-040+) and authorization audit events (IAM-038+) are not built.** The schema
  is shaped for them — `role_assignment_by_scope_idx` answers "who holds anything here," nothing is
  ever deleted, and `AuthorizationDecision.detail` carries the specific reason a denial happened —
  but neither system exists.
- **`ip_allowlist` is only as trustworthy as the source address.** `api.authz.dependencies` reads
  the raw connection address and never a proxy header, so behind an unconfigured reverse proxy an
  allowlist condition denies (the proxy's own address will not be in the list) rather than admits.
  Trusted-proxy configuration is a deployment decision, deliberately not made here — the same caveat
  ADR-010 records for rate limiting, but with sharper consequences.
- **The `permission` catalogue is migration-owned.** `ledgr_app` has SELECT and nothing else, so a
  running application cannot invent a capability for itself; adding one is a migration and a code
  change.
- **The SQL in `0009_authorization.sql` has not been executed anywhere.** No Docker or Postgres is
  available in the environment this was written in. It was parse-checked against the Postgres dialect
  and its seed data was cross-checked programmatically (every role/permission triple resolves, and no
  administration-scope role bundles an organization-scope permission), but the triggers, policies and
  `"role"` quoting are first exercised by CI.
