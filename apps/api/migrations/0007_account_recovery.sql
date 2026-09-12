-- 0007_account_recovery.sql
-- Account recovery (IAM-018): "recovery never grants access on the
-- strength of an email alone; recovery requires a second verified factor
-- or an admin-initiated, logged reset." See
-- docs/decisions/ADR-010-session-listing-recovery-and-rate-limiting.md.
--
-- account_recovery_event is a dedicated, narrowly-scoped append-only log
-- for this one action - NOT the general-purpose, hash-chained AuditEvent
-- system IAM-090+ describes (not built anywhere in this codebase).
-- Building that system is out of scope for implementing IAM-018
-- specifically; if/when it exists, it likely subsumes this table's
-- purpose. Every row here is "the logged reset" IAM-018 requires,
-- regardless of which path (self-service or admin) produced it.

begin;

create table account_recovery_event (
    id                      uuid primary key default gen_random_uuid(),
    target_user_id          uuid not null references users(id),
    method                  text not null check (method in ('totp', 'passkey', 'admin_reset')),
    -- Null for self-service recovery (the actor IS the target - proving
    -- their own factor). Required for admin_reset - "admin-initiated"
    -- means an identified actor, always, never anonymous.
    performed_by_user_id    uuid references users(id),
    reason                  text,
    created_at              timestamptz not null default now(),
    constraint account_recovery_event_admin_reset_requires_actor
        check (method <> 'admin_reset' or performed_by_user_id is not null)
);

create index account_recovery_event_target_user_id_idx
    on account_recovery_event(target_user_id);

alter table account_recovery_event owner to ledgr_migrator;

-- No DELETE or UPDATE grant: this is a log, not a mutable record - every
-- entry, once written, stays exactly as it was written.
grant select, insert on account_recovery_event to ledgr_app;

commit;
