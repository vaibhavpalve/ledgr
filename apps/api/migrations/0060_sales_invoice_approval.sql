-- 0060_sales_invoice_approval.sql
-- SI-16 (Sales Invoicing TRD, "Firm workflow"). See
-- docs/decisions/ADR-078-sales-invoice-approval.md.
--
--   SI-16  A bookkeeper drafts on a client's behalf; the business owner approves before it
--          sends. Fits the existing firm-engagement access model.
--
-- ===========================================================================
-- What is here
-- ===========================================================================
--
--   administration.invoice_approval_required   opt-in policy, default off
--   sales_invoice_approval                     one request for approval of one draft
--
-- With the policy off nothing changes: whoever may send an invoice may issue it. With it on,
-- issuing needs either the authority to approve (the owner) or a CURRENT approval of exactly
-- this draft.
--
-- ===========================================================================
-- An approval is of a specific version of the draft
-- ===========================================================================
--
-- `content_hash` is a SHA-256 over what the customer would receive (customer snapshot, dates,
-- notes, every line). Approving binds to it. If the draft is edited afterwards the hash no
-- longer matches, the approval stops counting, and a new request is needed: otherwise a
-- bookkeeper could get a small invoice approved and then change it to a large one. The check
-- happens at issue, in the service, against the draft as it is at that moment.
--
-- ===========================================================================
-- A request moves once, and is never edited
-- ===========================================================================
--
--   pending  -> approved | rejected | superseded
--   approved -> superseded            (a new request replaces it)
--
-- `decided_by_user_id` / `decided_at` are set with the decision. Nothing else changes, and
-- there is no DELETE grant: "who approved this, and what exactly" is what an owner or an
-- auditor asks about an invoice the business now stands behind.
--
-- At most one pending request per draft (partial unique index), so two people cannot each be
-- waiting on a different answer to the same question.

begin;

alter table administration
    add column invoice_approval_required boolean not null default false;

comment on column administration.invoice_approval_required is
    'SI-16: when true, a sales invoice can only be issued by someone who holds approve '
    'sales_invoice, or from a draft whose current contents an approver has approved.';

create table sales_invoice_approval (
    id                   uuid primary key default gen_random_uuid(),
    organization_id      uuid not null references organization(id),
    administration_id    uuid not null references administration(id),
    invoice_id           uuid not null references sales_invoice(id),

    content_hash         text not null check (content_hash ~ '^[0-9a-f]{64}$'),
    status               text not null default 'pending'
                             check (status in ('pending', 'approved', 'rejected', 'superseded')),

    requested_by_user_id uuid not null references users(id),
    requested_at         timestamptz not null default now(),
    request_note         text,

    decided_by_user_id   uuid references users(id),
    decided_at           timestamptz,
    decision_reason      text,

    -- A decision names who and when, together. Approved and rejected always have one; pending
    -- never does; superseded may (an approval later replaced) or may not (a request replaced
    -- before anyone answered).
    constraint sales_invoice_approval_decision_is_complete check (
        (status not in ('approved', 'rejected') or decided_by_user_id is not null)
        and (status <> 'pending' or decided_by_user_id is null)
        and (decided_by_user_id is null) = (decided_at is null)
    ),
    constraint sales_invoice_approval_rejection_has_reason check (
        status <> 'rejected' or length(btrim(coalesce(decision_reason, ''))) > 0
    )
);

create unique index sales_invoice_approval_one_pending_idx
    on sales_invoice_approval(invoice_id) where status = 'pending';
create index sales_invoice_approval_invoice_idx
    on sales_invoice_approval(invoice_id, requested_at desc);
create index sales_invoice_approval_queue_idx
    on sales_invoice_approval(administration_id, status, requested_at);
create index sales_invoice_approval_organization_idx
    on sales_invoice_approval(organization_id);

comment on table sales_invoice_approval is
    'SI-16. A request for the owner to approve one draft sales invoice, bound to a hash of its '
    'contents. Moves once (pending to approved / rejected / superseded); never edited or deleted.';

create or replace function sales_invoice_approval_guard() returns trigger as $$
declare
    v_status text;
    v_admin  uuid;
    v_org    uuid;
begin
    select status, administration_id, organization_id into v_status, v_admin, v_org
      from sales_invoice where id = new.invoice_id;
    if not found then
        raise exception 'sales invoice % does not exist', new.invoice_id;
    end if;
    if v_admin is distinct from new.administration_id then
        raise exception
            'approval belongs to administration %, but its invoice belongs to % (IAM-001)',
            new.administration_id, v_admin;
    end if;
    -- Only a draft is approved for issue; an issued invoice is beyond asking.
    if v_status <> 'draft' then
        raise exception 'invoice % is % and cannot be put up for approval', new.invoice_id, v_status;
    end if;
    if new.status <> 'pending' then
        raise exception 'an approval request starts pending';
    end if;
    new.organization_id := v_org;
    return new;
end;
$$ language plpgsql;

create trigger sales_invoice_approval_guard_trg
    before insert on sales_invoice_approval
    for each row execute function sales_invoice_approval_guard();

create or replace function sales_invoice_approval_immutable() returns trigger as $$
begin
    if new.id                   is distinct from old.id
       or new.organization_id   is distinct from old.organization_id
       or new.administration_id is distinct from old.administration_id
       or new.invoice_id        is distinct from old.invoice_id
       or new.content_hash      is distinct from old.content_hash
       or new.requested_by_user_id is distinct from old.requested_by_user_id
       or new.requested_at      is distinct from old.requested_at
       or new.request_note      is distinct from old.request_note
    then
        raise exception
            'approval % records what was asked and cannot be edited; only its decision is set',
            old.id;
    end if;
    if not (
        (old.status = 'pending'  and new.status in ('approved', 'rejected', 'superseded'))
        or (old.status = 'approved' and new.status = 'superseded')
    ) then
        raise exception 'approval % cannot go from % to %', old.id, old.status, new.status;
    end if;
    -- A supersede keeps the decision already recorded; it does not rewrite who decided.
    if old.status = 'approved' and (
        new.decided_by_user_id is distinct from old.decided_by_user_id
        or new.decided_at is distinct from old.decided_at
        or new.decision_reason is distinct from old.decision_reason
    ) then
        raise exception 'approval % keeps its recorded decision', old.id;
    end if;
    return new;
end;
$$ language plpgsql;

create trigger sales_invoice_approval_immutable_trg
    before update on sales_invoice_approval
    for each row execute function sales_invoice_approval_immutable();

alter table sales_invoice_approval owner to ledgr_migrator;

-- No DELETE: the record of who approved what.
grant select, insert, update on sales_invoice_approval to ledgr_app;
grant select on sales_invoice_approval to ledgr_ops;

alter table sales_invoice_approval enable row level security;
alter table sales_invoice_approval force row level security;

create policy sales_invoice_approval_select on sales_invoice_approval
    for select using (app.has_administration_access(administration_id));
create policy sales_invoice_approval_insert on sales_invoice_approval
    for insert with check (app.has_administration_access(administration_id));
create policy sales_invoice_approval_update on sales_invoice_approval
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

-- ===========================================================================
-- The permission that approves, for databases that predate it
-- ===========================================================================
-- 0010 is GENERATED from api.authz.matrix and regenerated in place, so a fresh database gets
-- `approve sales_invoice` there. A database that already ran an older 0010 does not, so it is
-- added here, idempotently (a fresh database finds both rows present and does nothing).
insert into permission (action, resource_type, resource_scope, description)
select 'approve', 'sales_invoice', 'administration', 'Approve sales invoices'
 where not exists (
     select 1 from permission where action = 'approve' and resource_type = 'sales_invoice'
 );

insert into role_permission (role_id, permission_id)
select r.id, p.id
  from "role" r, permission p
 where r.name = 'Owner' and r.is_system
   and p.action = 'approve' and p.resource_type = 'sales_invoice'
   and not exists (
       select 1 from role_permission rp where rp.role_id = r.id and rp.permission_id = p.id
   );

commit;