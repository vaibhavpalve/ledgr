# ADR-015: Client access profiles

- **Status**: Accepted
- **Date**: 2026-08-24
- **Implements**: IAM-100 – IAM-104 (PRD §8.6)

## Context

In Model A the firm decides what the client's own users may do inside their administration. §8.6
calls that a client access profile: firm-defined, reusable across clients, versioned, with three
built-ins shipping as defaults.

IAM-102 is the constraint that makes the rest safe: *"A firm cannot grant a client's users a
permission the firm itself does not hold on that administration."* Without it, delegated
administration is a privilege-escalation primitive — a firm with capture-level access to a client
could hand that client's users full ledger rights and then act through them.

## Decision

### A profile is a cap, not a grant

`authorize()` intersects the profile's permitted set with what the user's roles already carry. A
profile can only ever *withhold*. That asymmetry is why profiles compose safely with the existing
role model, why a permissive profile assigned to a user with no role grants nothing, and why IAM-102
is the only escalation surface here worth guarding — a bug that makes a profile *smaller* costs
access, never confers it.

### The cap lives inside `authorize()`

CLAUDE.md's third non-negotiable means every access decision resolves through the one library, so a
profile a caller could forget to consult would be no cap at all. Three conditions gate it, each
ruling out a case where capping would be wrong:

1. **The target is a specific administration.** Profiles govern access inside one administration;
   0013's permission guard refuses to store organization-scope permissions in one.
2. **The grant is the client's own** — its scope belongs to the administration's owning organization.
   §8.6 governs "what the client's own users may do"; firm staff reach the same administration
   through an engagement and are *not* capped by the cap the firm itself set.
3. **A profile is actually assigned.** No profile means no cap, never no access, so self-managed
   administrations are untouched.

### IAM-102's ceiling is the firm's, not the acting user's

"The firm itself" is the firm. A Firm Manager holds no ledger permissions at all (§8.4) yet assigning
profiles is precisely their job, so checking the acting user's own holdings — the IAM-036 rule for
role authoring (ADR-013) — would make the feature unusable and is not what the requirement says. The
ceiling is the union of live, **unconditioned** grants held by the firm's staff *on that
administration*. Conditioned grants are excluded on ADR-013's reasoning: a firm whose staff may only
approve up to €5,000 does not hold "approve purchase invoices" outright and cannot confer it
unconditionally.

It is enforced in **both** directions that can widen a client's access: assigning a profile, and
publishing a new version of a profile administrations already use. Checking only at assignment would
let a firm assign a modest profile and then edit it upward — the same escalation nesting was for
custom roles, one level up.

The ceiling measures the **resolved** set (after IAM-104 restrictions), not the raw permission list,
because the resolved set is what the client actually receives. A profile listing `view
bank_transaction` with bank detail switched off gives the client nothing of the sort.

### Versioned, with assignments pointing at the profile

Publishing an edit appends a new version; the old one is immutable, so "what could this client do in
March" stays answerable. An assignment references the **profile**, not a version, so a new version
takes effect for every administration using it. That is IAM-100's "reusable across clients" and
IAM-103's "within 60 seconds" working together — pinning an assignment to a version would defeat
both. Assignments themselves are append-only with a `superseded_at`, so IAM-111's audit trail has the
history rather than only the present.

### IAM-104 restrictions compile down; there is no second enforcement path

A restriction is firm-facing configuration. `resolve()` turns it into the two things the model
already speaks: the booleans withhold permissions (`bank_detail_visible=false` drops `view
bank_transaction`), and the journal and amount limits become ordinary **IAM-033 conditions**,
evaluated by the same `evaluate_conditions` a grant's own conditions use. Nothing downstream of
`resolve()` knows profiles exist.

### IAM-103's notification is part of the change, not a side effect

*"Silent reduction of a client's access is prohibited."* The change is diffed against the previous
effective set and described in plain language written for a client Owner, not a developer — no
permission triples reach the reader. A notifier that raises **aborts** the change rather than being
swallowed. That is deliberately the opposite of ADR-010's geolocation resolver, which degrades to
`None`: that one is enrichment, this one is the requirement.

### IAM-105's floor, partially

IAM-105 is **not** in scope and is not fully implemented. Its administration-scoped part is, because
a profile could otherwise withhold it: the client's Owner keeps `view administration`, `view
document`, `export report_data` and `read audit_log` through any profile. The floor is applied
**after** restrictions, so a firm switching reports off still cannot take away the Owner's export of
their own data, and it is excluded from the IAM-102 ceiling — those rights are the client's own, not
the firm's to confer.

The rest of IAM-105 — revoking the firm's access, managing the client's own sign-in security — is
organization-scope and beyond a per-administration profile's reach by construction, which 0013's
permission guard enforces.

### Three catalogue additions

IAM-101 defines the built-ins in terms of "upload documents" and "customers", which Appendix A has no
row for. `upload document`, `view document` and `manage customer` are therefore declared in
`matrix.py` as `EXTENSION_CAPABILITIES` — explicitly outside the Appendix A grid, so
`test_appendix_a_conformance.py` still compares the matrix to the document and rejects invented rows.
Which roles hold them is stated per role as interpretation, from §8.4's prose.

The built-in profiles themselves are **generated** into `0014_builtin_client_access_profiles.sql`
from the same `BUILTIN_PROFILES` definition the service and the test fake read, on ADR-012's pattern
— the profiles a firm can select cannot differ from the ones the code describes.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Checking IAM-102 against the acting user's own permissions | A Firm Manager holds no ledger permissions but assigning profiles is their job. The requirement says "the firm", not "the user". |
| Checking IAM-102 only when a profile is assigned | A firm could assign a modest profile and then edit it upward; the version-publish path is the second door. |
| Pinning an assignment to a specific profile version | Defeats IAM-100's "reusable across clients" and IAM-103's "changes take effect within 60 seconds" — every client would need reassigning after every edit. |
| Modelling profiles as roles the client's users are assigned | A role grants; a profile caps. Making it a grant would mean a firm could *add* to what a client's own Owner can do, which §8.6 never contemplates, and would put profile permissions inside the IAM-036 ceiling where they do not belong. |
| Applying the cap in the callers rather than in `authorize()` | A cap a caller can forget is not a cap. Same reasoning as `AuthorizationEnforcementMiddleware` (ADR-011). |
| Giving IAM-104's restrictions their own evaluator | A second enforcement path to keep in step with IAM-033's. Compiling restrictions into the existing condition mechanism means a profile's journal limit and a grant's behave identically because they *are* identical. |
| Storing `approval_amount_ceiling` as a JSON number | jsonb numbers are IEEE 754 doubles; NFR-031 forbids float anywhere in the calculation path. Stored as a decimal string, rejected by CHECK otherwise. |
| Best-effort notification (log and continue) on IAM-103 | "Silent reduction of a client's access is prohibited" is the requirement; a swallowed failure is exactly the silence it forbids. |
| Adding `upload document` / `manage customer` as Appendix A rows | The conformance test would reject them, correctly — inventing matrix rows is the drift that test exists to catch. |
| Capping firm staff with the profile too | §8.6 scopes profiles to "the client's own users". Capping the firm with the cap it set is incoherent and would break firm access the moment a restrictive profile was chosen. |

## Consequences

- **Fifteen guards are mutation-tested.** Each was broken in turn — the ceiling removed, not
  re-checked on publish, measured against the raw set; foreign and archived profiles made assignable;
  both notification paths dropped; the summary stopped naming removals; both kinds of IAM-104
  restriction neutered; the floor applied before restrictions; built-ins made editable; the cap
  removed from `authorize()`, applied to firm staff, and turned into a grant — and all fifteen
  produced real test failures.
- **`Organization Admin` can no longer assign the built-in `Expense Submitter` role**, because that
  role now holds `view document` and an Org Admin does not. This surfaced as a test failure and is
  the IAM-036 ceiling working correctly, not a regression; an admin delegates what they hold, which
  means composing a role rather than handing out a built-in that exceeds them.
- **Row-level "own" scoping does not exist.** IAM-101's "view own submissions" and §8.4's "Expense
  Submitter cannot see any data belonging to another person" both need a per-record owner column on
  tables that do not exist yet (FR-DOC, FR-EXP). `view document` is currently all-or-nothing within
  an administration, so an Expense Submitter sees more than §8.4 intends. Flagged in `matrix.py` at
  the point of declaration; whoever builds FR-DOC must add it.
- **IAM-103's "logged" is satisfied only by the notifier's structured log line.** There is no
  `AuditEvent` system (IAM-090+) and IAM-111's requirement that profile changes appear in *both*
  audit logs is not built — `administration_access_profile` keeps the history those logs would be
  derived from.
- **No HTTP endpoint**, consistent with every prior auth ADR.
- **`0013` and `0014` have not been executed.** Parse-checked against the Postgres dialect only; CI
  applies them for real.
