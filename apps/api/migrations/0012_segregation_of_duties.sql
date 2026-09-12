-- 0012_segregation_of_duties.sql
-- IAM-060 through IAM-065 (PRD §8.7). See
-- docs/decisions/ADR-014-segregation-of-duties.md.
--
-- Two tables:
--   sod_policy_deviation  IAM-064: an organization switching off one SoD
--                         rule, acknowledged by its Owner with a reason.
--   sod_event             the append-only record every refused action and
--                         every use of a deviation lands in, and the
--                         evidence the IAM-064/065 audit report is built
--                         from.
--
-- Neither exemption is silent, which is the whole point of IAM-064 and
-- IAM-065. A deviation is a ROW someone had to write, naming themselves and
-- a reason; the single-user exemption is computed live from the active user
-- count and stated in the report whether or not anything was blocked. There
-- is no configuration flag anywhere that turns SoD off without leaving a
-- trace.

begin;

-- ---------------------------------------------------------------------------
-- sod_policy_deviation
-- ---------------------------------------------------------------------------
create table sod_policy_deviation (
    id                      uuid primary key default gen_random_uuid(),
    organization_id         uuid not null references organization(id),
    rule                    text not null,
    -- IAM-064: "explicitly acknowledged by the Owner ... recorded with a
    -- reason". Both are NOT NULL, and the reason must contain something -
    -- an empty string would satisfy a bare NOT NULL and defeat the point.
    acknowledged_by_user_id uuid not null references users(id),
    reason                  text not null check (length(btrim(reason)) > 0),
    acknowledged_at         timestamptz not null default now(),
    revoked_at              timestamptz,
    revoked_by_user_id      uuid references users(id),
    constraint sod_deviation_revocation_complete check (
        (revoked_at is null and revoked_by_user_id is null)
        or (revoked_at is not null and revoked_by_user_id is not null)
    )
);

-- One live deviation per rule per organization. The same partial-unique
-- pattern as administration_encryption_key's one-active-key rule (0002) and
-- for the same reason: "at most one current row" is a database invariant,
-- not something application code should be trusted to maintain.
create unique index sod_deviation_one_active_idx
    on sod_policy_deviation(organization_id, rule)
    where revoked_at is null;

create index sod_deviation_organization_idx on sod_policy_deviation(organization_id);

-- Same immutability discipline as role_assignment (0009): a deviation's
-- scope and its justification are fixed at acknowledgement. Widening one -
-- changing which rule it covers, or rewriting the reason after the fact -
-- would break the audit trail IAM-064 exists to produce. Revoking and
-- re-acknowledging leaves both rows.
create or replace function sod_deviation_immutable() returns trigger as $$
begin
    if new.organization_id is distinct from old.organization_id
        or new.rule is distinct from old.rule
        or new.acknowledged_by_user_id is distinct from old.acknowledged_by_user_id
        or new.reason is distinct from old.reason
        or new.acknowledged_at is distinct from old.acknowledged_at
    then
        raise exception
            'a SoD deviation''s rule, acknowledger and reason are immutable (IAM-064) - '
            'revoke it and acknowledge a new one instead';
    end if;

    if old.revoked_at is not null and new.revoked_at is distinct from old.revoked_at then
        raise exception 'a revoked SoD deviation cannot be un-revoked or re-dated';
    end if;

    return new;
end;
$$ language plpgsql;

create trigger sod_deviation_immutable_trg
    before update on sod_policy_deviation
    for each row execute function sod_deviation_immutable();

-- ---------------------------------------------------------------------------
-- sod_event
-- ---------------------------------------------------------------------------
-- Append-only. Records what SoD actually did, as opposed to what it is
-- configured to do:
--   'blocked'            an action refused by a rule
--   'deviation_applied'  an action a rule would have refused, permitted
--                        because the organization has an acknowledged
--                        deviation - the row IAM-064 requires to surface in
--                        the audit report
--
-- Permitted actions are NOT recorded. A row here always means something a
-- reviewer needs to look at, which keeps the audit report readable; the
-- IAM-065 single-user exemption is likewise not recorded per action (it
-- would be one row per action forever in a one-person organization) but is
-- computed and stated in the report from the live user count.
create table sod_event (
    id                  uuid primary key default gen_random_uuid(),
    organization_id     uuid not null references organization(id),
    rule                text not null,
    outcome             text not null check (outcome in ('blocked', 'deviation_applied')),
    actor_user_id       uuid not null references users(id),
    resource_type       text not null,
    resource_id         uuid,
    detail              text not null,
    deviation_id        uuid references sod_policy_deviation(id),
    occurred_at         timestamptz not null default now(),
    -- A deviation_applied event without the deviation that permitted it
    -- would be unauditable: the report could say a rule was bypassed but
    -- not on whose authority.
    constraint sod_event_deviation_attributed check (
        outcome <> 'deviation_applied' or deviation_id is not null
    )
);

create index sod_event_organization_idx on sod_event(organization_id, occurred_at desc);
create index sod_event_rule_idx on sod_event(organization_id, rule, occurred_at desc);

-- ---------------------------------------------------------------------------
-- Ownership, grants, RLS
-- ---------------------------------------------------------------------------
alter table sod_policy_deviation owner to ledgr_migrator;
alter table sod_event            owner to ledgr_migrator;

-- UPDATE on the deviation table is revocation only (the trigger above
-- rejects everything else). sod_event gets no UPDATE and no DELETE at all.
grant select, insert, update on sod_policy_deviation to ledgr_app;
grant select, insert on sod_event to ledgr_app;

alter table sod_policy_deviation enable row level security;
alter table sod_policy_deviation force row level security;
alter table sod_event enable row level security;
alter table sod_event force row level security;

create policy sod_deviation_select on sod_policy_deviation
    for select using (organization_id = app.current_org_id());

create policy sod_deviation_insert on sod_policy_deviation
    for insert with check (organization_id = app.current_org_id());

create policy sod_deviation_update on sod_policy_deviation
    for update
    using (organization_id = app.current_org_id())
    with check (organization_id = app.current_org_id());

create policy sod_event_select on sod_event
    for select using (organization_id = app.current_org_id());

create policy sod_event_insert on sod_event
    for insert with check (organization_id = app.current_org_id());

commit;
