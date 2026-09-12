-- 0041_invoice_delivery.sql
-- FR-AR-005 (PRD §6.3), FR-LOC-003 (PRD §19).
-- See docs/decisions/ADR-040-invoice-delivery.md.
--
--   FR-AR-005   Send as PDF by email in P0; structured e-invoice over Peppol
--               (BIS Billing 3.0 / NLCIUS, EN 16931) added in P2. DELIVERY
--               STATUS TRACKED PER CHANNEL.
--   FR-LOC-003  Invoice templates and email localised per recipient,
--               independent of the sender's UI language.
--
-- ===========================================================================
-- Sending is NOT part of issuing, and that is the opposite call from 0040
-- ===========================================================================
--
-- Migration 0040 made posting atomic with issuing: an invoice and its ledger
-- entry are written in one transaction, because an issued invoice the books do
-- not know about is a receivable nobody chases.
--
-- Delivery is deliberately the other way, and the difference is not taste. A
-- posting is a database write that a ROLLBACK undoes. An email is an
-- irreversible act performed by somebody else's server: once a provider has
-- accepted it, no transaction outcome here can recall it. Sending inside the
-- issue transaction would mean
--
--   * an SMTP round trip holding a write transaction open, and
--   * a customer holding an invoice that was rolled back and does not exist.
--
-- So delivery is its own act, its own endpoint, and its own row. NFR-026's
-- "queued work resumes automatically" is the shape this table is built for:
-- `status`, `attempts` and `next_attempt_at` are a work queue, whatever drains
-- it.
--
-- ===========================================================================
-- One row per DISPATCH, not one per invoice and not one per attempt
-- ===========================================================================
--
-- FR-AR-005 says status is tracked PER CHANNEL, which rules out a single
-- status column on `sales_invoice`: one invoice may be emailed today and sent
-- over Peppol in P2, and collapsing those into one field loses which of them
-- actually reached the customer.
--
-- Three shapes were possible and this is the middle one:
--
--   per invoice+channel, updated   loses the history. An invoice that bounced
--                                  and was then resent to a corrected address
--                                  would show only "sent", and the bounce -
--                                  the interesting event - would be gone.
--   per transport attempt          every SMTP retry is a row. The history is
--                                  complete and unreadable: "has this invoice
--                                  been sent" becomes an aggregate query.
--   per DISPATCH (this)            one row each time somebody asks for the
--                                  invoice to go out. Transport retries
--                                  increment `attempts` on that row; a RESEND
--                                  is a new row. So the history is one row per
--                                  human decision, which is the grain somebody
--                                  reading it is actually asking about.
--
-- "The current state of email delivery for this invoice" is therefore the most
-- recent row for that channel - `invoicing.delivery_state_of` below.
--
-- ===========================================================================
-- `sent` and `delivered` are different facts, and P0 only knows the first
-- ===========================================================================
--
--   queued     a dispatch exists; the provider has not accepted it yet
--   sent       the provider ACCEPTED it. This is all a synchronous send knows.
--   delivered  the provider CONFIRMED it reached the recipient
--   bounced    it did not, permanently
--   failed     we gave up trying to hand it over
--
-- Conflating `sent` with `delivered` is the tempting simplification and it is
-- how somebody ends up telling a customer "we sent it" about an invoice that
-- bounced an hour later. The two states are separated now, before anything
-- depends on the distinction; `delivered` and `bounced` are written by the
-- provider webhook, which is NOT built (see the ADR's gaps).

begin;

-- ===========================================================================
-- invoice_delivery - FR-AR-005
-- ===========================================================================
create table invoice_delivery (
    id                  uuid primary key default gen_random_uuid(),

    -- CLAUDE.md rule 1. Both, on every row, and RLS reads administration_id.
    organization_id     uuid not null references organization(id),
    administration_id   uuid not null references administration(id),

    invoice_id          uuid not null references sales_invoice(id),

    -- FR-AR-005's "per channel". Mirrors `customer.delivery_channel` (0039)
    -- so a customer's preference and a dispatch's channel are the same
    -- vocabulary - two spellings of "peppol" would make the P2 cutover a data
    -- migration.
    channel             text not null check (channel in ('email', 'peppol', 'post')),

    status              text not null default 'queued'
                            check (status in (
                                'queued',     -- not yet handed over
                                'sent',       -- the provider accepted it
                                'delivered',  -- the provider confirmed arrival
                                'bounced',    -- it did not arrive, permanently
                                'failed'      -- we gave up handing it over
                            )),

    -- Where it went, in the channel's own terms: an email address, or a Peppol
    -- participant id. SNAPSHOTTED, not joined from `customer`: the question a
    -- month later is "what address did this actually go to", and a customer
    -- who has since corrected their address must not rewrite the answer. The
    -- same argument 0037 makes for the invoice's own customer block.
    recipient           text not null check (length(btrim(recipient)) > 0),

    -- FR-LOC-003: which language the covering message was written in. Recorded
    -- because it is the recipient's and not the sender's, so "why did my
    -- customer get a Dutch email" has an answer.
    language            text not null check (language in ('nl', 'en')),

    -- What was actually handed over. For email this is the PDF from
    -- FR-TPL-017; for Peppol it will be the UBL. Nullable because a `failed`
    -- dispatch may not have got as far as resolving one.
    document_id         uuid references document(id),

    -- NFR-026's queue. `attempts` counts TRANSPORT retries within this
    -- dispatch, not resends - a resend is a new row.
    attempts            integer not null default 0 check (attempts >= 0),
    next_attempt_at     timestamptz,

    -- Which adapter answered, and its own reference for this message. The
    -- reference is what a provider webhook arrives quoting, so without it a
    -- delivery confirmation cannot be matched back to a dispatch.
    provider            text,
    provider_reference  text,

    -- Operator-facing. Never rendered to a user (FR-UX-007); the user-facing
    -- sentence comes from the catalogue, keyed on `status`.
    last_error          text,

    requested_by_user_id uuid references users(id),
    requested_at        timestamptz not null default now(),
    last_attempt_at     timestamptz,
    sent_at             timestamptz,
    settled_at          timestamptz,

    -- A dispatch that has been accepted has a moment at which that happened.
    constraint invoice_delivery_sent_is_dated check (
        (status in ('sent', 'delivered')) = (sent_at is not null)
    ),

    -- A settled dispatch is one nothing further will happen to.
    constraint invoice_delivery_settled_is_dated check (
        (status in ('delivered', 'bounced', 'failed')) = (settled_at is not null)
    ),

    -- Only a queued dispatch is waiting for anything. A schedule on a settled
    -- row would make a drained queue re-send an invoice that already arrived.
    constraint invoice_delivery_only_queued_is_scheduled check (
        status = 'queued' or next_attempt_at is null
    )
);

create index invoice_delivery_invoice_idx
    on invoice_delivery(invoice_id, channel, requested_at desc);
create index invoice_delivery_administration_idx
    on invoice_delivery(administration_id, requested_at desc);
create index invoice_delivery_organization_idx on invoice_delivery(organization_id);

-- The work queue, as an index. Partial, so it stays the size of the backlog
-- rather than the size of the history - a year of sent invoices does not slow
-- down finding the three that still need a retry.
create index invoice_delivery_due_idx
    on invoice_delivery(next_attempt_at)
    where status = 'queued';

comment on table invoice_delivery is
    'FR-AR-005. One row per DISPATCH of one invoice over one channel. The '
    'current state of a channel is the most recent row for it - see '
    'invoicing.delivery_state_of.';

-- ---------------------------------------------------------------------------
-- Only an issued invoice is delivered
-- ---------------------------------------------------------------------------
-- A draft has no number, has asserted nothing and owes nobody. Sending one
-- would put a document with no invoice reference in a customer's hands, which
-- FR-AR-004 spends a whole numbering scheme preventing.
--
-- A trigger rather than a CHECK because the condition lives on another table.
create or replace function invoice_delivery_needs_an_issued_invoice() returns trigger as $$
declare
    v_status text;
    v_admin  uuid;
    v_org    uuid;
begin
    select status, administration_id, organization_id
      into v_status, v_admin, v_org
      from sales_invoice where id = new.invoice_id;

    if not found then
        raise exception 'sales invoice % does not exist', new.invoice_id;
    end if;

    -- Tenant coherence, the guard 0037 puts on its lines and 0039 on its
    -- customer link: a dispatch carrying a different administration from its
    -- invoice would send one tenant's document under another tenant's name.
    if v_admin is distinct from new.administration_id then
        raise exception
            'delivery belongs to administration %, but its invoice belongs to % '
            '(IAM-001)', new.administration_id, v_admin;
    end if;

    if v_status <> 'issued' then
        raise exception
            'invoice % is a % and cannot be delivered; only an issued invoice '
            'carries a number to send (FR-AR-004, FR-AR-005)',
            new.invoice_id, v_status;
    end if;

    -- Derived rather than trusted, so the two can never disagree.
    new.organization_id := v_org;
    return new;
end;
$$ language plpgsql;

create trigger invoice_delivery_needs_an_issued_invoice_trg
    before insert on invoice_delivery
    for each row execute function invoice_delivery_needs_an_issued_invoice();

-- ---------------------------------------------------------------------------
-- A settled dispatch is history
-- ---------------------------------------------------------------------------
-- Once a provider has accepted a message, no outcome here can recall it - so
-- the record of that must not be editable either. `sent` may still advance to
-- `delivered` or `bounced`, because those are the provider telling us what
-- became of the same message; nothing else moves.
create or replace function invoice_delivery_transition_is_forward() returns trigger as $$
begin
    if old.status in ('delivered', 'bounced', 'failed')
       and new.status is distinct from old.status then
        raise exception
            'delivery % is settled as %; a further attempt is a new dispatch, not '
            'a rewritten one (FR-AR-005)', old.id, old.status;
    end if;

    if old.status = 'sent' and new.status not in ('sent', 'delivered', 'bounced') then
        raise exception
            'delivery % was accepted by the provider at %; it cannot return to %',
            old.id, old.sent_at, new.status;
    end if;

    -- What was sent, and where, are facts about a message already handed over.
    if old.status <> 'queued' and (
        new.invoice_id  is distinct from old.invoice_id
        or new.channel   is distinct from old.channel
        or new.recipient is distinct from old.recipient
        or new.language  is distinct from old.language
        or new.sent_at   is distinct from old.sent_at
    ) then
        raise exception
            'delivery % has been handed over; what was sent and where it went '
            'cannot change', old.id;
    end if;

    return new;
end;
$$ language plpgsql;

create trigger invoice_delivery_transition_is_forward_trg
    before update on invoice_delivery
    for each row execute function invoice_delivery_transition_is_forward();

-- ===========================================================================
-- Reading the status back - FR-AR-005's "tracked"
-- ===========================================================================
-- The current state of every channel this invoice has been dispatched over.
-- DISTINCT ON rather than a window function because the answer is one row per
-- channel and that is exactly what DISTINCT ON returns.
create or replace function invoicing.delivery_state_of(p_invoice_id uuid)
returns table (
    channel            text,
    status             text,
    recipient          text,
    language           text,
    attempts           integer,
    provider           text,
    provider_reference text,
    requested_at       timestamptz,
    sent_at            timestamptz,
    settled_at         timestamptz,
    last_error         text
)
language sql
stable
as $$
    select distinct on (d.channel)
           d.channel, d.status, d.recipient, d.language, d.attempts,
           d.provider, d.provider_reference, d.requested_at, d.sent_at,
           d.settled_at, d.last_error
      from invoice_delivery d
     where d.invoice_id = p_invoice_id
     order by d.channel, d.requested_at desc;
$$;

comment on function invoicing.delivery_state_of(uuid) is
    'FR-AR-005. The latest dispatch per channel for one invoice - what a screen '
    'showing "sent / bounced / not sent" reads.';

-- ---------------------------------------------------------------------------
-- Invoices that were issued and never went out
-- ---------------------------------------------------------------------------
-- The mirror of `invoicing.unposted_invoices` from the other side. Unlike that
-- one this is NOT structurally empty: issuing and sending are separate acts on
-- purpose, so an invoice can legitimately sit here for a while. It is a work
-- list rather than an alarm - an invoice nobody sent is an invoice nobody will
-- pay.
create or replace function invoicing.undelivered_invoices(p_administration_id uuid)
returns table (
    invoice_id        uuid,
    invoice_reference text,
    invoice_date      date,
    customer_name     text,
    issued_at         timestamptz,
    last_status       text,
    last_error        text
)
language sql
stable
as $$
    select si.id, si.invoice_reference, si.invoice_date, si.customer_name,
           si.issued_at, latest.status, latest.last_error
      from sales_invoice si
      left join lateral (
            select d.status, d.last_error
              from invoice_delivery d
             where d.invoice_id = si.id
             order by d.requested_at desc
             limit 1
      ) latest on true
     where si.administration_id = p_administration_id
       and si.status = 'issued'
       and (latest.status is null or latest.status in ('queued', 'bounced', 'failed'))
     order by si.issued_at;
$$;

comment on function invoicing.undelivered_invoices(uuid) is
    'FR-AR-005. Issued invoices that have not reached the customer: never '
    'dispatched, still queued, bounced, or given up on. A work list, not an '
    'alarm - issuing and sending are separate acts.';

-- ===========================================================================
-- Grants and RLS
-- ===========================================================================
alter table invoice_delivery owner to ledgr_migrator;

-- No DELETE: a dispatch is the record that a document left the building, and
-- CMP-009's audit posture applies to it - "we never sent that" is exactly the
-- question this table exists to answer. Superseded by a new row, never removed.
grant select, insert, update on invoice_delivery to ledgr_app;
grant select on invoice_delivery to ledgr_ops;

grant execute on function invoicing.delivery_state_of(uuid)      to ledgr_app, ledgr_ops;
grant execute on function invoicing.undelivered_invoices(uuid)   to ledgr_app, ledgr_ops;

alter table invoice_delivery enable row level security;
alter table invoice_delivery force row level security;

create policy invoice_delivery_select on invoice_delivery
    for select using (app.has_administration_access(administration_id));
create policy invoice_delivery_insert on invoice_delivery
    for insert with check (app.has_administration_access(administration_id));
create policy invoice_delivery_update on invoice_delivery
    for update using (app.has_administration_access(administration_id))
    with check (app.has_administration_access(administration_id));

commit;
