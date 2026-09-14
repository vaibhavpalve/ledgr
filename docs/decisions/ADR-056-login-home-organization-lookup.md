# ADR-056: A third `ledgr_bootstrap` function for login's home-organization lookup

- **Status**: Accepted
- **Date**: 2026-09-14

## Context

Standing up a local Postgres for the first time in this repository's history (previously blocked by
Docker's absence on the development machine — see the "local dev stack" note) and running the
login flow end-to-end for the first time, rather than against `tests/auth/`'s fake repositories,
found that **every returning-user login refused with `errors.no_organization`** — password, passkey
and Google sign-in alike, for every account, unconditionally, including one that had just completed
signup and MFA enrollment moments before.

`api.auth.routes._home_organization_id` — the one helper `login`, `login_passkey_finish` and
`login_google_callback` all call to answer "which organization does this already-authenticated user
belong to" — ran a plain `SELECT scope_id FROM role_assignment WHERE ...` through
`api.db.get_bootstrap_db_session`, deliberately the session with no `app.current_org_id` set (there
is genuinely no tenant context yet at this point in the request; finding one is the query's job).
`role_assignment_select`'s RLS policy (`0009_authorization.sql`) reads `scope_id =
app.current_org_id()`, which evaluates to `scope_id = NULL` on that session — false for every row,
for every caller, always. This is RLS working exactly as designed, asked a question it cannot answer
without already knowing the answer.

Signup never hit this: it already has `organization_id` from `SignupService.signup()`'s own return
value and never calls `_home_organization_id` at all. The bug was in the one place every *other*
login path converges, invisible until now because (a) no migration had ever run against a real
Postgres before, and (b) `tests/auth/`'s fake-repository suite never modelled RLS. Zero test
coverage existed for the login route's success path at all.

Fixing it surfaced a **second, independent, equally-unreached bug**: `login`'s and
`login_google_callback`'s success branch (when `mfa_verified` is still `False`) called
`session.commit()` and then ran one more read (`_enrollment_status`) on the same session —
`get_bootstrap_db_session` opens its session as `session.begin()`, and a query after an explicit
`commit()` inside that block fails with SQLAlchemy's "Can't operate on closed transaction inside
context manager." This branch was unreachable before the first fix, for the same reason: the 403
above always fired first.

## Decision

**Give `login`'s home-organization lookup a `SECURITY DEFINER` function, owned by
`ledgr_bootstrap`, and use it instead of the RLS-bound query** —
`app.user_home_organization_id(p_user_id uuid) returns uuid`, in
`migrations/0048_login_home_organization_lookup.sql`. This is the exact pattern
`0001_tenancy_core.sql` already established for the opposite direction of the same problem — RLS
correctly blocking a bootstrap-time operation that has no tenant context yet — for signup's two
writes (`app.signup_self_managed_organization`, `app.create_firm_client_administration`). The new
function is `ledgr_bootstrap`'s third, not "exactly two" as `0001`'s own comment claimed; that
comment (prose only, no schema change) is corrected in place to point here.

The function does one narrow thing and nothing else: given a user id, return the single
organization id their live founding-or-invited grant scopes to, or `NULL`. It reveals no more than
that — not the row, not the role name, not any other user's assignments — matching the existing
functions' minimal-surface posture, and every Python caller only ever passes the id of a user who
has *just* been authenticated by password, passkey signature, or a verified Google identity token in
the same request, never an id taken from unauthenticated input.

Writing the function's body was not enough by itself. `0025_bootstrap_privileges.sql` had already
documented this exact class of mistake once — *"BYPASSRLS exempts a role from row-level security
POLICIES; it grants no privilege on any table"* — for the original two functions, which failed with
`permission denied for table organization` on first real use for precisely this reason. The same
migration repeated that mistake for `role_assignment` on first run here, caught the same way
`0025` was originally found: a live Postgres, not a reasoning-from-the-schema review. `0048` now
also grants `ledgr_bootstrap` plain `SELECT` on `role_assignment`, following `0025`'s own precedent
directly rather than re-discovering it.

**The session-lifecycle bug is fixed by reordering**: `_enrollment_status` now runs before
`session.commit()` in both `login` and `login_google_callback`, not after. It is a pure read with no
dependency on anything written earlier in the same transaction, so there is no reason it needs to
follow the commit.

**A dedicated regression suite was added** —
`tests/integration/test_login_home_organization.py` — exercising the real HTTP route
(`httpx.ASGITransport` against the live `app`, the same shape `test_auth_mfa_isolation.py` and
`test_google_initiated_signup.py` already use) against a real Postgres: signup, TOTP enrollment,
re-login (the exact reproduction, asserting both that it succeeds and that the reported enrollment
status is correct — not just "didn't crash"), step-up verification, and use of the resulting token
against a real protected route. Wrong-password, nonexistent-email and wrong-TOTP-code cases are
included so the fix is proven not to have loosened anything on the failure side. This is deliberately
gated the same way every other DB-backed test in this repository is
(`TENANT_ISOLATION_TESTS_ENABLED=1`) — a fake-repository unit test cannot exercise RLS at all, which
is the entire reason this bug survived undetected.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Set `app.current_org_id` to the target org right before the lookup, inside the bootstrap session | Backwards — the whole point of the query is to discover that value; a caller cannot set the GUC to the answer before knowing it. |
| Query `role_assignment` through a connection using `ledgr_ops` (which already holds `BYPASSRLS`-equivalent cross-tenant read access, per `SET ROLE` grants elsewhere) | Widens the standing privilege surface of the LOGIN path — the single most exposed route in the system — to everything `ledgr_ops` can reach, for the sake of one column on one table. `CLAUDE.md`'s architectural rule on RLS as the first line of defense argues for the narrowest possible bypass, which is exactly what a single-purpose `SECURITY DEFINER` function is and a role switch is not. |
| Have `TenantContextMiddleware` resolve organization membership generically, for every route, instead of a login-specific helper | A much larger change to the hot path of every authenticated request, unrelated to the bug at hand — this is deliberately the same login-specific, narrowly-scoped fix `_home_organization_id` already was, not an opportunity to redesign tenant resolution generally. |
| Skip the regression test, since the fix is small | The bug had zero coverage for exactly the reason it existed undetected: fake-repository tests cannot see an RLS policy. A fix with the same test posture as the code that shipped the bug offers no reason to believe the next one won't repeat it. |

## Consequences

**`ledgr_bootstrap` now owns three functions instead of two.** Its role-level comment in
`0001_tenancy_core.sql` is updated (prose only) to say so and point here; the security review
surface named in that comment — "not a role, not a connection string... the bodies of those
functions" — grows by one function body, reviewable in isolation the same way the first two were.

**Login, passkey sign-in and Google sign-in for a returning user now work at all.** Before this
fix they did not, for any account, under any condition — this was not a partial or edge-case
regression.

**What this does not fix.** `docs/decisions/ADR-054-signup-and-login.md`'s already-named limitation
stands unchanged: a session revoked by `/v1/auth/logout` does not immediately invalidate a JWT
already issued from it, because nothing re-checks the `sessions` row on requests in between. That
was tested again as part of this work (a logged-out token still authorizes a request) and is exactly
the documented, deliberate trade-off ADR-054 already recorded — this ADR does not reopen it.

**Fourteen unrelated integration tests fail on this same freshly-provisioned database** —
`test_sales_invoice_posting.py` and `test_expense_form_isolation.py::test_net_is_generated_and_always_completes_the_gross`
— all on the same root cause, a foreign-key violation against `vat_treatment`, because no VAT
ruleset has been loaded (`make load-vat` / `scripts/load_vat_rules.py`). Confirmed unrelated to this
change (the error is a missing reference-data row, not a code path this migration touches) and left
unaddressed — loading VAT/RGS reference data is a separate, orthogonal setup step this task's scope
does not extend to.
