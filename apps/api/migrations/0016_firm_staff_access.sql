-- 0016_firm_staff_access.sql
-- IAM-107: "Firm staff access is granted per client administration, not per
-- firm. A new firm employee starts with access to zero clients."
-- IAM-108: "Firm staff grants may carry an expiry, and support seasonal or
-- interim staff working on a defined client set for a defined period."
-- See docs/decisions/ADR-017-firm-staff-access.md.
--
-- Most of IAM-107 was already true and is worth naming rather than
-- re-implementing:
--   * "not per firm" - an organization-scoped grant cascades only to the
--     administrations that organization OWNS (ADR-011's _scope_covers), and
--     a firm never owns its client's administration. No grant held at a firm
--     has ever reached a client's books.
--   * "starts with access to zero clients" - there is no bulk or implicit
--     path to an administration. Access is a role_assignment row or it does
--     not exist, and nothing creates one on hire.
--   * expiry - role_assignment.expires_at has existed since 0009 and is
--     enforced as a predicate in the evaluation query (IAM-035), so a lapsed
--     firm grant stops working with no sweep and no administrative action.
--
-- What this migration adds is the thing that was missing: a record of which
-- organization a grant was made FROM.
--
-- --- Why granted_by_organization_id matters ---
--
-- An administration-scoped assignment on a client's books looks identical
-- whether the client's own Owner made it for their bookkeeper or the firm
-- made it for its accountant. That ambiguity was a real defect: ADR-016
-- recorded that api.authz.service could only guess which side a user was on
-- when deciding whether a client access profile capped them, and guessed
-- with a heuristic. IAM-107 makes the distinction a requirement rather than
-- an inconvenience, so it becomes a column.
--
-- It is set by a trigger from app.current_org_id(), never from a parameter.
-- A caller cannot supply it, cannot forget it, and cannot spoof it - the
-- same derive-do-not-trust pattern sync_fiscal_year_tenant_columns uses in
-- 0001 for the denormalized tenant columns.

begin;

alter table role_assignment
    add column granted_by_organization_id uuid references organization(id);

-- One string literal, not several concatenated across newlines: Postgres
-- accepts the latter, but it is the one construct in this schema that the
-- pre-flight dialect check cannot parse, and a migration nothing can verify
-- before CI is worth avoiding for a comment.
comment on column role_assignment.granted_by_organization_id is 'The organization whose tenant context created this grant. Derived from app.current_org_id() by role_assignment_firm_staff_guard_trg; never supplied by a caller. When it differs from the administration''s owning organization, this is a firm staff grant made under an active engagement (IAM-107).';

-- Left nullable rather than backfilled-then-NOT NULL: the backfill would run
-- as ledgr_migrator, which FORCE ROW LEVEL SECURITY subjects to the same
-- policies as everyone else with no tenant context set, so it would see zero
-- rows and update nothing. No role_assignment rows exist at this point in the
-- migration sequence (0010 and 0014 seed roles and profiles, not
-- assignments), so in practice every row has it. A NULL is read as
-- client-side by api.authz.service, which is the safe direction: capped by
-- the client access profile rather than exempt from it.

create index role_assignment_granted_by_idx
    on role_assignment(granted_by_organization_id)
    where revoked_at is null;

-- "Which of our staff can reach this client, and until when" - the IAM-108
-- question, and the shape IAM-109's client-facing listing will need.
create index role_assignment_firm_staff_idx
    on role_assignment(granted_by_organization_id, scope_id, expires_at)
    where revoked_at is null and scope_type = 'administration';

create or replace function role_assignment_firm_staff_guard() returns trigger as $$
declare
    v_owner uuid;
begin
    if tg_op = 'UPDATE' then
        -- Immutable, for the same reason every other field of a grant is
        -- (IAM-032): re-attributing a grant after the fact would let a firm
        -- staff assignment be relabelled as the client's own, or the
        -- reverse, changing whether a profile caps it.
        if new.granted_by_organization_id is distinct from old.granted_by_organization_id then
            raise exception
                'a grant''s granting organization is immutable - revoke it and create a '
                'new one instead';
        end if;
        return new;
    end if;

    new.granted_by_organization_id := app.current_org_id();
    if new.granted_by_organization_id is null then
        raise exception
            'a role assignment requires tenant context to attribute it to a granting '
            'organization (IAM-107)';
    end if;

    if new.scope_type <> 'administration' then
        return new;
    end if;

    select organization_id into v_owner from administration where id = new.scope_id;
    if v_owner is null then
        -- Not visible from this session. role_assignment_scope_guard_trg
        -- (0009) rejects that with its own message; this trigger fires
        -- first only because of alphabetical ordering and has nothing to
        -- add.
        return new;
    end if;

    if v_owner = new.granted_by_organization_id then
        -- The client granting inside its own administration. Ordinary.
        return new;
    end if;

    -- Granted from a FIRM context. IAM-107: this is the only shape firm
    -- staff access takes, and it requires the client's consent, which is
    -- what an active engagement is.
    if not exists (
        select 1 from firm_engagement fe
        where fe.administration_id = new.scope_id
          and fe.firm_organization_id = new.granted_by_organization_id
          and fe.status = 'active'
    ) then
        raise exception
            'organization % has no active engagement on administration % (IAM-107): '
            'firm staff access is granted per client administration, through the '
            'engagement the client agreed to',
            new.granted_by_organization_id, new.scope_id;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger role_assignment_firm_staff_guard_trg
    before insert or update on role_assignment
    for each row execute function role_assignment_firm_staff_guard();

commit;
