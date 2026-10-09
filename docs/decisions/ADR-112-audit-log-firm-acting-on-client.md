# ADR-112: A firm acting in a client's books appends to the client's audit chain

- **Status**: Accepted
- **Date**: 2026-10-09
- **Amends**: [ADR-059](ADR-059-onboarding-transaction-and-founding-firm-grant.md) (the "Seeding and
  year-opening run under the new client's tenant context" section, now superseded) and
  [ADR-020](ADR-020-audit-log.md) (the insert policy and the sealing trigger of migration 0019)

## Context

Services that act on an administration's books (`LedgerService.post_journal_entry`, capture, the
expense form, expense posting, chart seeding, fiscal-year opening) file their audit entry under the
administration's **owning** organization. That is right per IAM-094: the client reads "my
accountant posted this" in its own log. But `audit_log_insert` (0019) admitted a row only for
`app.current_org_id()`, which is the firm in a firm session. Every posting, capture and expense post
by firm staff in a Model A client's books therefore failed (QA: the firm's proposal approval came
back `posting_failed`, kept as a strict-xfail test).

ADR-059 hit the same gap in onboarding and worked around it with a transaction-local switch into the
client's tenant context. It said a second such site should prompt the policy widening instead of a
third workaround. The proposal approval and the expense routes are that second site.

Two traps make "just widen the policy" unsafe:

1. **The seal reads the chain head under the caller's RLS.** `audit_log_seal()` was a plain
   (invoker) trigger. In a firm session it sees none of the client's rows, so it would seal the
   firm's entry as sequence 1 with the zero hash. We checked on a scratch database: with only the
   policy widened, `audit_log_chain_unique` does catch this. The insert fails with a unique
   violation instead of silently forking the chain. That fails closed, but every firm posting
   still breaks. (On an empty client chain the first firm entry would succeed and the second would
   collide.)
2. **`INSERT ... RETURNING` also evaluates the SELECT policy**, and `SqlAuditRepository.append`
   used RETURNING to hand back the sealed entry.

## Decision

Migration `0081_audit_log_firm_acting_on_client.sql` makes three changes.

### 1. One extra branch in `audit_log_insert`

```sql
organization_id = app.current_org_id()
or (
    administration_id is not null
    and app.has_administration_access(administration_id)
    and exists (select 1 from administration a
                where a.id = audit_log.administration_id
                  and a.organization_id = audit_log.organization_id)
)
```

A row may name an organization other than the session's own only if it also names an
administration. The session must reach that administration (its own, or through an **active**
`firm_engagement`; a pending or revoked one admits nothing), and the row's organization must be
**exactly** that administration's owner. A firm cannot file under an arbitrary organization, under
a client it isn't engaged on, or under a client with no administration named. `audit_log` already
carries `administration_id`, and every service that files under the owning organization sets it,
so no new column or lookup was needed.

The policy is evaluated as the caller (`ledgr_app`), so the `administration` lookup is itself under
RLS. A firm sees an administration only through its engagement. The column is qualified as
`audit_log.organization_id` because an unqualified name inside the subquery would bind to
`administration.organization_id` and make the check vacuous.

### 2. The seal finds the true chain head, whoever is appending

`audit_log_seal()` becomes `SECURITY DEFINER`. It stays owned by `ledgr_audit`, and its
`search_path` is pinned to `public, app, pg_temp`, the same shape as 0020's ledger functions.
`FORCE ROW LEVEL SECURITY` binds the table's owner as well, so ownership alone doesn't let the
seal see the rows. A new policy does:

```sql
create policy audit_log_seal_read on audit_log for select to ledgr_audit using (true);
```

Policies are per role. This one matches only when `current_user` is `ledgr_audit`, which happens
only inside functions `ledgr_audit` owns. `ledgr_audit` stays `NOLOGIN`, `NOBYPASSRLS`, not a
superuser, and a member of no role (a test asserts all four), so nobody can open a session as it
or `SET ROLE` to it. `ledgr_app`, `ledgr_ops` and `ledgr_migrator` never match this policy, so
their reads are exactly what they were. The only new grant is `USAGE` on schema `app`, which the
definer needs to call `app.audit_field`.

We chose this over the alternatives because each one widens something broader:

| Option | Rejected because |
|---|---|
| Re-own the seal to `postgres` (QA's scratch patch) | A superuser-owned definer trigger; runs everything in it with full privilege. |
| `ALTER ROLE ledgr_audit BYPASSRLS` | Bypasses RLS for *every* function `ledgr_audit` owns, now and later, not just the one that needs it; reverses a property 0019 states explicitly. |
| Compute the head in the app and pass it in | 0019's rule: a caller that names its own sequence/previous hash can forge a chain. |

The advisory lock on `hashtextextended(organization_id)` is unchanged. Client-side and firm-side
appends to the same chain serialise on the same key. The hash payload is unchanged byte for byte,
so all existing chains verify.

### 3. The SELECT policy is not widened, and the app no longer uses RETURNING

Being allowed to append to a chain doesn't mean being allowed to read it. Whether a firm may read
an engaged client's audit log is a separate decision under IAM-094 and IAM-109, and this ADR
doesn't make it. `audit_log_select` is untouched. QA's scratch patch widened it, and we did not
adopt that part.

`SqlAuditRepository.append` now generates the entry's `id` in Python, inserts without `RETURNING`,
and reads the row back by id through RLS. For the session's own tenant that returns the sealed
entry exactly as before. For a client's chain it returns `None`. `AuditRepository.append` and
`AuditLog.record` are now typed `AuditEntry | None`. No production caller used the returned entry.
The cost is one primary-key lookup per audit write.

### ADR-059's onboarding switch is removed

`POST /v1/administrations` for a firm no longer sets `app.current_org_id` to the new client. The
founding-grant entry, the chart seed, the year opening and `app.ensure_posting_defaults` all run
under the firm's own context. The engagement that `app.create_firm_client_administration` creates
is active, so RLS admits all of it. `test_onboarding_isolation.py::test_a_firm_creating_a_client_can_work_in_it`
now also asserts that the client's chain holds those entries, that the chain verifies, that the
posting-default journals exist, and that the firm can read none of the client's audit rows. No
route changes `app.current_org_id` in the middle of a request any more.

## Consequences

- Firm staff can post, capture and post expenses in an engaged client's books. Each entry lands in
  the client's chain with the correct sequence number and `previous_hash`, including when
  client-side and firm-side entries are interleaved (`tests/integration/test_audit_firm_acting_on_client.py`).
- **What a firm session can read is unchanged.** It still can't `SELECT` a client's audit rows or
  read the chain head through `app.audit_chain_head`, and `INSERT ... RETURNING` from a firm session
  is refused. The new capability is append-only and limited by engagement.
- What a firm *can now write*: entries in the chain of any administration it is actively engaged
  on. Before, the same firm could already write that administration's ledger and documents. An
  audit entry about its own action is strictly less than that. A firm can't use this to write into
  any other tenant's log.
- Revoking an engagement (IAM-110) immediately ends the firm's ability to append to that client's
  chain, because `has_administration_access` requires `status = 'active'`.
- Down migration (documented in the 0081 header): restore 0019's insert policy, drop
  `audit_log_seal_read`, make the seal `SECURITY INVOKER` again, reset its `search_path`, and revoke
  `USAGE` on `app` from `ledgr_audit`. No data is touched. Entries filed by firms in the meantime
  remain valid links in their clients' chains. The app code shipped with 0081 works against either
  schema.
