-- 0018_client_switcher.sql
-- FR-FRM-000: "Client switcher: searchable by client name, KvK number or
-- trade name, keyboard-reachable, showing only granted administrations.
-- Switching preserves the current screen type where it exists for the
-- target client."
-- FR-FRM-000a: "The active client is unmistakable at all times - persistent
-- name and colour marker in the header on every screen, on web and mobile.
-- Posting to the wrong client is the single worst usability failure in this
-- product."
-- See docs/decisions/ADR-019-client-switcher.md.
--
-- Two columns and two indexes. The switcher itself (which administrations,
-- for whom) already existed from IAM-110; what it could not do was be
-- searched or be told apart at a glance.
--
-- --- trade_name ---
--
-- FR-FRM-000 names three search keys and the schema had two. A Dutch
-- business commonly trades under a name that is not its statutory one
-- ("Bakker Consultancy B.V." trading as "Bakker IT"), and a bookkeeper
-- looking for a client types the name on the invoice, not the one in the
-- deed.
--
-- --- colour_token ---
--
-- A TOKEN, not a hex value: the API says 'amber' and the clients decide
-- what amber looks like. That keeps contrast and palette decisions in the
-- design layer where web and mobile can honour their own conventions, and
-- it means a palette change is not a data migration.
--
-- Allocated least-used-first within the OWNING organization, so a business
-- with several administrations gets visibly different markers. It cannot
-- guarantee distinctness inside a FIRM's portfolio - that spans many owning
-- organizations - which is why api.firm.switcher flags a collision when one
-- appears in a switcher rather than pretending it cannot happen. Colour is
-- a fast secondary signal; the name is what is authoritative, and it is
-- always shown.

begin;

create extension if not exists pg_trgm;  -- CLAUDE.md's search decision

alter table administration add column trade_name text;

comment on column administration.trade_name is 'The name the business actually trades under, where it differs from legal_name. FR-FRM-000 makes it a switcher search key.';

-- ---------------------------------------------------------------------------
-- Colour markers (FR-FRM-000a)
-- ---------------------------------------------------------------------------
-- A closed set, mirrored by api.firm.colours.PALETTE. Chosen to stay
-- distinguishable under the common colour-vision deficiencies, which is why
-- there is no red/green pair adjacent in the ordering - but the real
-- accessibility answer is that the marker never carries meaning alone: every
-- surface showing it also shows the client's name.
create table client_colour (
    token       text primary key,
    position    integer not null unique
);

insert into client_colour (token, position) values
    ('indigo', 1),
    ('amber',  2),
    ('teal',   3),
    ('rose',   4),
    ('lime',   5),
    ('violet', 6),
    ('cyan',   7),
    ('orange', 8),
    ('emerald',9),
    ('fuchsia',10);

alter table administration add column colour_token text references client_colour(token);

create or replace function administration_assign_colour() returns trigger as $$
begin
    if new.colour_token is not null then
        return new;
    end if;

    -- Least-used within the owning organization, ties broken by the palette
    -- order so the choice is deterministic rather than whatever the planner
    -- returns first.
    select c.token into new.colour_token
    from client_colour c
    left join administration a
           on a.colour_token = c.token
          and a.organization_id = new.organization_id
    group by c.token, c.position
    order by count(a.id), c.position
    limit 1;

    return new;
end;
$$ language plpgsql;

create trigger administration_assign_colour_trg
    before insert on administration
    for each row execute function administration_assign_colour();

-- Existing rows predate the trigger. Assigned by palette position cycling
-- through each organization's administrations, which is deterministic and
-- gives the same least-used-first shape the trigger produces.
update administration a
set colour_token = c.token
from (
    select id,
           row_number() over (partition by organization_id order by created_at, id) as n
    from administration
) ranked
join client_colour c on c.position = ((ranked.n - 1) % 10) + 1
where a.id = ranked.id and a.colour_token is null;

-- ---------------------------------------------------------------------------
-- Search (FR-FRM-000)
-- ---------------------------------------------------------------------------
-- Trigram indexes on the two name columns: a bookkeeper types a fragment,
-- not a prefix, and pg_trgm is what CLAUDE.md chose over a second datastore.
create index administration_legal_name_trgm_idx
    on administration using gin (legal_name gin_trgm_ops);
create index administration_trade_name_trgm_idx
    on administration using gin (trade_name gin_trgm_ops)
    where trade_name is not null;

-- KvK numbers are eight digits and are searched by prefix, not fuzzily -
-- "1234" should find 12345678, and trigram similarity on digit strings
-- produces noise rather than matches. text_pattern_ops is what makes a
-- LIKE 'prefix%' use an index.
create index administration_kvk_prefix_idx
    on administration (kvk_number text_pattern_ops)
    where kvk_number is not null;

alter table client_colour owner to ledgr_migrator;

-- Reference data, read by everyone, written by migrations only.
grant select on client_colour to ledgr_app;

alter table client_colour enable row level security;
alter table client_colour force row level security;

-- No tenant dimension: the palette is the same for every tenant and reveals
-- nothing about any of them. Stated explicitly rather than left unprotected,
-- as with the permission catalogue in 0009.
create policy client_colour_select on client_colour for select using (true);

commit;
