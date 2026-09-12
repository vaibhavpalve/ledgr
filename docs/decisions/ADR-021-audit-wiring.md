# ADR-021: Wiring the audit log, and making it unforgettable

- **Status**: Accepted
- **Date**: 2026-08-31
- **Implements**: IAM-090 (coverage), building on [ADR-020](ADR-020-audit-log.md)

## Context

ADR-020 built the audit log and left it with no callers. IAM-090 names nine kinds of activity that
must be covered, and an audit log nobody writes to is a schema, not a control.

Two problems, and they need different answers. **Wiring** the paths that exist is ordinary work.
Making sure the *next* endpoint is wired is not — it is the same class of problem as IAM-005's
isolation tests and CLAUDE.md rule three's authorization declaration, both of which this codebase
already solved by walking the route table at build time.

## Decision

### The route declares one thing; everything else is derived

`require_permission(..., audit=AuditCategory.POSTING)`. The event's action and resource type come
from the permission the route *already* requires — a route requiring `post journal_entry` is auditing
a post of a journal entry. Letting a route restate them would let it restate them wrongly, and then
the log would disagree with the authorization decision beside it.

### Recording is middleware, not the dependency that already runs everywhere

`require_permission` looks like the obvious home — it already resolves actor, tenant, administration
and permission, and already records the IAM-109 access there. It is the wrong home for two reasons,
both about the honesty of the record:

1. **It runs before the handler**, so it cannot know the outcome. A log that says an action was
   attempted but not whether it succeeded answers half the question.
2. **It runs inside the request's transaction.** A denied or failed request rolls that back — taking
   the audit entry with it. The entries most worth having are exactly the ones that would vanish.

`AuditMiddleware` runs after the response with its **own session**, so a rolled-back request still
leaves a durable record of having been attempted and refused. This is the opposite of the choice made
for IAM-109's access recording, which participates in the request transaction — and the difference is
principled: that one runs *before* the action and can still prevent it; this one runs after and
cannot.

### The CI check

`tests/test_audit_coverage.py` walks the route table and fails if a **mutating** route (POST, PUT,
PATCH, DELETE) declares no category and is not explicitly exempt. Third check of this shape in the
suite, needing no database, running on every CI invocation.

Mutating specifically: IAM-090's list is overwhelmingly things that *change* something. Reads of
financial records are on the list too and a GET *may* declare `financial_read` — `GET
/v1/administrations/{id}` does — but requiring one on every read would fill the log with people
looking at their own dashboard, and noise is how an audit log stops being read.

The check also asserts **it has routes to check**. Without that, every other assertion in the file
passes vacuously the moment `_mutating_routes()` stops finding anything, and a coverage check that
silently checks nothing is worse than none, because it reads as a guarantee. That test was added
because a mutation proved the original file did not have it.

### Service-layer wiring, for the paths with no route yet

Permission changes and configuration changes are fully built as services and reached by no endpoint.
Wiring them at the *service* rather than waiting for their routes matters because the record has to
be made where the decision is: a future route calling `assign_role` gets the entry for free, and one
that reimplements the grant does not — which is the right way round.

`AuditTrail` is optional (`audit: AuditTrail | None = None`) on every service that takes it, the same
shape as `sod` and `profiles` on `AuthorizationService`. Not to make auditing optional in production —
`api.main` wires it — but so the several hundred existing tests that construct these services without
a database keep working.

**Refusals are recorded, not just successes.** A refused grant, a blocked last-Owner revocation, an
SoD-denied self-grant: someone repeatedly trying to grant themselves a role they cannot have is
precisely what a reviewer is looking for, and a log of only the successes would not show it.

### Firm-side events are recorded against the *client*

A firm granting its staff access to a client's books writes to the **client's** audit log, and a
seasonal grant covering four clients writes **one entry per client**. IAM-094 lets a customer read
their own log, and "a firm employee was given access to my books" is an event about the client — it
is the one they most need to see. One row listing four other companies would be both confusing and a
disclosure.

### Authentication is wired where it can be, and the gap is named

Sign-in cannot be audited per tenant. Authentication happens *before* a tenant is chosen, and
`audit_log.organization_id` is NOT NULL by ADR-020's design. `AuthenticationService` has no
organization to attribute an entry to, and inventing one would mean choosing a tenant arbitrarily.

What *can* be audited is the one authentication event in the request path that has a tenant: **an MFA
denial**. `MfaEnforcementMiddleware` signals it and `AuditMiddleware` writes it, as
`AuditCategory.AUTHENTICATION` — taking precedence over the route's own category, because a request
refused at the MFA gate never reached the route and recording it as a posting would say something
that did not happen.

Pre-tenant attempts remain in `auth_attempt` (IAM-019), which is already append-only. That is the
honest boundary, and it is a consequence of ADR-020's per-tenant chain rather than an oversight.

### A registry, so an unwired category is a decision rather than a gap

`tests/audit/test_wiring.py`'s `WIRING` maps each of IAM-090's nine categories to its producer, or to
`None` with the requirement that will build it. A test pins exactly which five are unwired
(`export`, `posting`, `approval`, `filing`, `support_access`), so building FR-GL without wiring its
audit event is a **failing test** rather than a silent gap — the entry has to change when the feature
lands.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Recording inside `require_permission` | Runs before the handler (no outcome) and inside the request transaction (denials and failures roll back). |
| A per-route call to `audit.record(...)` in each handler | Forgettable, which is exactly what the CI check exists to prevent — and would put the declaration in twelve places instead of one. |
| Requiring an audit declaration on every route including GETs | Fills the log with people opening their own dashboard. Noise is how an audit log stops being read. |
| Making `AuditTrail` a required constructor argument | Would mean rewriting several hundred test construction sites to pass something they do not exercise, or a null object with the same effect as the optional parameter. |
| Recording firm staff grants against the firm's organization | The client cannot read the firm's log (IAM-094 is per tenant), so the party most affected would never see it. |
| One audit entry per batch for a multi-client seasonal grant | A client's log would name four other companies. |
| Making `organization_id` nullable so sign-in could be audited | Reopens a settled decision (ADR-020) and creates a chain no tenant can read or verify. `auth_attempt` already covers pre-tenant attempts. |
| Letting the audit write fail the request | The action already happened; refusing afterwards does not un-happen it. (The inverse choice from IAM-109's access recording, which runs before.) |

## Consequences

- **Twelve guards are mutation-tested**: a route losing its declaration, the coverage check going
  inert, grant/refused-grant/revoke recording nothing, firm grants attributed to the firm instead of
  the client, batch-instead-of-per-client entries, engagement revocation and profile assignment
  recording nothing, the reduction flag dropped, SoD deviations unrecorded, and MFA denials filed
  under the route's category. All produced real test failures.
- **Two initially passed and both exposed real gaps in my own work**, not just weak tests. The
  coverage check could go inert unnoticed — now asserted. And `AuditMiddleware` had **no tests at
  all**: the entire HTTP-side wiring was unverified until a mutation showed it. That is now
  `tests/test_audit_middleware.py`.
- **Writing those tests surfaced a design fact worth stating**: `AuditMiddleware` reads
  `request.state.tenant_context`, which only `TenantContextMiddleware` sets. A `dependency_overrides`
  entry for `get_tenant_context` does not reach it, because middleware runs outside the dependency
  system entirely. Tests must set the state.
- **`X-Correlation-ID` is now honoured and echoed** on every request (IAM-091), generated when the
  caller sends none, so a support conversation can quote one.
- **Five categories still have no producer**, all because the feature does not exist. The registry
  names which requirement will build each.
- **`AuditMiddleware` opens a second database session per audited request.** Unavoidable given it
  must survive the request's rollback, and the same tradeoff `MfaEnforcementMiddleware` already makes.
  Worth measuring before the first high-traffic endpoint ships.
