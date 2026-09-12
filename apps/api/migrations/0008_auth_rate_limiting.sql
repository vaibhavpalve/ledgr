-- 0008_auth_rate_limiting.sql
-- IAM-019: rate limiting and progressive lockout on authentication
-- endpoints; credential-stuffing detection with anomaly alerting. See
-- docs/decisions/ADR-010-session-listing-recovery-and-rate-limiting.md.
--
-- auth_attempt is an append-only event log of every authentication
-- attempt (success and failure alike) against any auth endpoint. Three
-- things get computed from it, all in api.auth.rate_limiting:
--   - rate limiting: how many attempts an account_key has made recently;
--   - progressive lockout: how many CONSECUTIVE failures an account_key
--     has racked up since its last success, driving an increasing
--     lockout duration;
--   - credential-stuffing detection: how many DISTINCT account_keys one
--     source_ip has failed against recently - one attacker hammering many
--     different accounts, the opposite shape from one account under
--     brute-force attack.
--
-- Backed by Postgres, not an in-memory counter or Redis: this stack has
-- no cache/queue infrastructure (docker-compose.yml is Postgres + Azurite
-- only), and an in-memory counter would not be correct across multiple
-- API worker processes/instances, which a real deployment runs. A
-- Postgres row per attempt, indexed for the two query shapes above, is
-- the correct choice given what is actually in this stack.

begin;

create table auth_attempt (
    id              uuid primary key default gen_random_uuid(),
    endpoint        text not null,   -- e.g. 'login', 'password_recovery' - which auth action
    account_key     text not null,   -- normalized identifier of WHAT was targeted (usually email)
    source_ip       inet,            -- WHERE the attempt came from; null if genuinely unknown
    outcome         text not null check (outcome in ('success', 'failure')),
    occurred_at     timestamptz not null default now()
);

-- Rate limiting and progressive lockout both query by (endpoint,
-- account_key, occurred_at).
create index auth_attempt_account_key_idx on auth_attempt(endpoint, account_key, occurred_at);

-- Credential-stuffing detection queries by (endpoint, source_ip,
-- occurred_at) instead - the orthogonal dimension.
create index auth_attempt_source_ip_idx on auth_attempt(endpoint, source_ip, occurred_at);

alter table auth_attempt owner to ledgr_migrator;

-- No DELETE or UPDATE grant: an append-only log, same discipline as the
-- rest of this schema's audit-adjacent tables. A future retention job
-- purging old rows (this is not fiscal data - no 7-year requirement
-- applies) is not built here.
grant select, insert on auth_attempt to ledgr_app;

commit;
