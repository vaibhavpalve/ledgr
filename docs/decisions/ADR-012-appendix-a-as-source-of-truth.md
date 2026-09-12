# ADR-012: Appendix A as the source of truth for the standard roles

- **Status**: Accepted
- **Date**: 2026-08-23
- **Extends**: [ADR-011](ADR-011-authorization-model.md)

## Context

ADR-011 built the authorization model and seeded PRD §8.4's standard roles by hand-writing an
`INSERT ... VALUES` list in `0009_authorization.sql`, transcribed by eye from Appendix A's
capability × role matrix. That transcription was correct when written and had no mechanism to stay
correct. Appendix A is 31 capability rows × 10 role columns — 310 cells — plus §8.4's twelve roles
and their scopes. Nothing linked any of it to the PRD except care at the moment of writing.

That is the kind of drift that is invisible until it matters: a role quietly holding a permission
Appendix A denies it is not a crash, a failing test, or a log line. It is a security decision
nobody made.

## Decision

### The matrix is data, in one module

`api/authz/matrix.py` holds Appendix A as a literal grid — `MATRIX` maps each capability label to a
tuple of `Access` values in the PRD's column order, using single-letter aliases (`F`, `R`, `C`, `N`)
so the source reads like the document it mirrors. `CAPABILITIES` maps each row to the permission
triples it grants, `ROLES` carries §8.4's twelve roles and their scopes, and `CONDITIONAL_CELLS`
carries Appendix A's "Notes on the conditionals" — also as data, keyed by the exact cell each note
explains.

Everything downstream derives from those tables through `permissions_for_role()` and
`permission_catalogue()`. There is no conditional anywhere in the codebase that special-cases a role
by name; `Owner` holds everything because reading its column yields everything, not because anything
says so.

### An Appendix A cell becomes permissions by a stated rule

Each capability declares `read` (granted at `R`, `C`, `F`) and `full` (granted additionally at `C`
and `F`). `C` and `F` grant identical permissions — they differ only in whether the individual
assignment is expected to carry IAM-033 conditions, which is ADR-011's existing reasoning
(the limits vary per person, so they belong on the assignment).

Seven rows exhibit both `F` and `R`, which forced a judgment the PRD does not settle. Four are
functional (`Run access reviews`: view vs. run; `Read audit log`: read vs. export; `View chart of
accounts`: view vs. maintain; `Manage API clients`: view vs. manage) and get distinct permissions.
Three are stylistic — on `View purchase invoices`, `View bank transactions` and `View reports`,
Appendix A marks `F` for roles whose working area the data is and `R` for read-only roles, but the
PRD names no separate write capability for those resources (the write rows are `Code purchase
invoices`, `Reconcile bank`, and `Export data`). Those three declare an empty `full` and carry a
`note` saying why, and a test fails if any capability grants nothing extra at `F` without one. The
judgment is visible in the data rather than buried in a transcription.

`Read audit log` is administration-scoped even though three organization-scoped roles hold it: an
organization grant reaches the administrations it owns (ADR-011), so one capability covers both. The
alternative — inventing a second organization-level audit-log permission, as
`0009_authorization.sql` originally did — puts a permission in the catalogue that no Appendix A cell
grants, which the "every permission is held by some role" test now rejects.

### The catalogue migration is generated, not written

`scripts/generate_role_catalogue.py` renders `migrations/0010_role_catalogue.sql` from the module.
`0009_authorization.sql` keeps the DDL, triggers, RLS and grants — real design, hand-written — and
no longer seeds anything. Structure and catalogue are separate files because one is designed and the
other is derived, and mixing them would mean diffing a regenerated file around hand-written DDL.

Generating a checked-in `.sql` rather than seeding at runtime keeps two properties that matter:
migrations stay plain files applied in order by `bootstrap_test_db.py` and CI, and a reviewer
diffing a schema change sees the actual rows that will be written.

### Four artifacts, three mechanical checks

    prd.md  ──①──▶  api/authz/matrix.py  ──②──▶  0010_role_catalogue.sql  ──③──▶  database

1. `tests/authz/test_appendix_a_conformance.py` parses Appendix A's and §8.4's markdown tables out
   of `prd.md` and asserts the module matches — every cell, the column order, the row order and
   labels, the twelve role names, and each role's scope. Editing either side without the other
   fails CI naming the cell. It parses the document rather than comparing against a second
   transcription, because a transcription is one more thing that can drift.
2. `tests/authz/test_role_catalogue_generation.py` re-renders the migration in memory and compares
   it to the checked-in file. CI also runs `--check` before applying migrations, so a stale
   catalogue fails with an actionable message rather than as a confusing conformance mismatch
   later.
3. `tests/integration/test_authorization_isolation.py` reads the live `permission`, `role` and
   `role_permission` rows and asserts they match what the module derives — the only link the other
   two cannot check, because a migration is a file until it is applied.

Only (3) needs a database. (1) and (2) run on every CI invocation.

All six drift scenarios were verified by deliberately introducing each one and confirming the suite
fails with a specific message: a changed matrix cell, a changed PRD cell, a new PRD capability row,
a changed §8.4 role scope, a hand-edited generated migration, and a dropped conditional note.

### The test world uses the real roles

`tests/authz/helpers.py` previously defined its own small set of roles and permissions. It now builds
the in-memory fake from `matrix.py`, so a test asserting "a Bookkeeper cannot approve" means what it
says in production. A local copy would let the unit tests keep passing against a role definition the
PRD no longer describes — the exact failure this ADR exists to prevent, reintroduced in the test
suite.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Keeping the hand-written seed and adding a test that compares it to a hand-written expectation | Two transcriptions drift from the PRD together, and the test passes while both are wrong. The check has to reach the document. |
| Encoding the matrix in YAML/JSON rather than a Python module | It would need a schema, a loader, and its own validation to get the type safety `Access` and the dataclasses give for free — and the file would still need a test against `prd.md`. The gain is portability nothing here needs. |
| Parsing `prd.md` at runtime and deriving permissions from it directly, with no module | Makes a product document a runtime dependency of the authorization system: a malformed table becomes a service outage, and a PRD edit changes production behaviour with no code review. Parsing belongs in a test, where a mismatch is a failure rather than a deployment. |
| Seeding the catalogue at application startup from `matrix.py` instead of generating SQL | Reference data would stop being visible in a schema diff, and the seeding path would need its own idempotency and ordering guarantees that migrations already have. |
| Putting the generated catalogue inside `0009_authorization.sql` | A file that is half hand-designed and half generated cannot be regenerated safely, and every regeneration would produce a diff a reviewer has to read past DDL to understand. |
| Modelling only the ten roles Appendix A has columns for | §8.4 defines twelve. Firm Manager and Service Account exist and need bundles; omitting them would leave two roles the PRD specifies with no implementation. |
| Treating every `F`/`R` split as functional and inventing a write permission for each | Three of the seven would be permissions no Appendix A cell actually grants and no PRD sentence describes — inventing capabilities to satisfy a symmetry the document does not have. |

## Consequences

- **PRD §8.4 has twelve roles, not eleven.** The twelfth is `Service Account` ("Per administration,
  per integration"), which is not a human role and has no Appendix A column. It is implemented with
  the baseline capability only, since the "named endpoints" its §8.4 row scopes it to do not exist
  yet. `Firm Manager` is the other role with no Appendix A column; its bundle comes from §8.4's own
  prose, named as capability labels so it stays data.
- **`0009_authorization.sql` no longer seeds anything**, and the permission `read
  organization_audit_log` it used to create is gone — merged into `read audit_log` at administration
  scope, per the reasoning above. Both files are new in this change set and neither has been applied
  anywhere, so this is an edit rather than a migration-on-top.
- **`view administration` is declared as a baseline capability outside the matrix**, plainly marked.
  Appendix A grades what a role may do *with* an administration's contents and has no row for seeing
  that the administration exists, which every role and the `/v1/administrations` routes need.
  Putting it in a matrix row would make the conformance test reject it, correctly.
- **Two conditional cells are recorded as documented-but-unenforced.** `Release payment to bank` for
  Accountant needs IAM-061's same-actor exclusion, which no condition key reads, and `Export data`
  for Viewer needs per-organization export disabling and data-access logging (IAM-038+). Both carry
  an empty `condition_keys` with a note, so a reader of `CONDITIONAL_CELLS` can see which
  conditionals are enforced and which are only described.
- **A PRD revision now breaks the build until the code is updated.** That is the intent, but it
  means editing `prd.md` is no longer a documentation-only change — Appendix A and §8.4 specifically
  are executable specification.
- **`0010_role_catalogue.sql` has not been executed.** As with ADR-011, no Postgres is available in
  the environment this was written in. It was parse-checked against the Postgres dialect and its
  contents cross-checked against the module in memory; CI applies it for real.
