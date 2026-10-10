# ADR-114: Receipt chasing - opt-in e-mails with counts only, delivered by a sweep in the client's own tenant context

- **Status**: Accepted
- **Date**: 2026-10-09
- **Implements**: firm home wave 2, decision 5 (`docs/firm-home/contract-wave2.md`); FR-NTF-001,
  FR-NTF-004 (no amounts or counterparties in a message that leaves the product), FR-FRM-002;
  IAM-005 (isolation test per route), IAM-090 (every send audited), NFR-032 (idempotency),
  NFR-031 (money as decimal strings), FR-LOC-001 (both languages)
- **Builds on**: [ADR-090](ADR-090-reminder-emails.md) (mail from a sweep, once),
  [ADR-109](ADR-109-firm-worklist-cross-client-reads.md) (`require_portfolio_permission`),
  [ADR-110](ADR-110-auto-bookings-are-proposals.md) and the bank feed sync (the two-role job:
  ledgr_ops discovers, ledgr_app acts in the tenant's context),
  [ADR-111](ADR-111-question-threads.md) (why a firm session cannot mail the client itself),
  [ADR-112](ADR-112-audit-log-firm-acting-on-client.md) (firm actions filed in the client's chain)

## Context

"Missing receipts" is the firm worklist's biggest *waiting on client* bucket: money went out of
the client's bank account and no receipt exists for it. Today the accountant chases each client
by hand. Decision 5 asks for two things: an opt-in, per-client automatic chase on a cadence, and
a manual bulk "Request missing receipts" from the worklist, after a preview. Both must say only
*how many* lines need a receipt, never which, and must never pester a client.

Three constraints shape the design:

1. **The firm's session cannot see who the client's people are.** The client's Owner usually
   holds an organization-scoped grant at the client, and 0009's RLS shows those only to that
   organization (ADR-111). Request-serving code never uses `ledgr_ops` (`api.db.get_ops_engine`),
   and decision 1 rules out SECURITY DEFINER shortcuts and borrowed tenant contexts.
2. **One definition of "missing".** The worklist already counts the bucket (wave-1 decision 5);
   the client's list, the mail's count and the worklist column must agree.
3. **Once.** A cron that runs every few minutes, possibly twice at once, must send at most one
   scheduled chase per window, and a manual request exactly once.

## Decision

### The bucket: one definition

`api.chasing.missing` holds the two predicates of wave-1 decision 5 (`PENDING_HIGH_PROPOSAL`,
`NO_CANDIDATE_RECEIPT`) and composes `MISSING_RECEIPT` = unmatched AND NOT pending-HIGH AND (debit
with no unsettled, non-discarded expense of exactly that amount). The worklist's `_BANK_BUCKETS`
imports both predicates from here (integration, wave 2) rather than keeping a copy, and the
integration test asserts the list's `count` equals the worklist's `missing_receipts` for the
same client.

The contract's endpoint text says "no pending proposal"; the worklist's definition (and this one)
excludes only a pending **HIGH** proposal. The worklist wins: the instruction is one definition.

### Schema (migration 0083)

| Table | Kind | Written by |
|---|---|---|
| `chase_setting(enabled, cadence weekly\|fortnightly, set_by_user_id)` | append-only, latest wins | the routes (firm or client), under RLS |
| `chase_request(requested_by_user_id, missing_count, requested_at)` | append-only | the firm's send route, under RLS |
| `chase_send(kind scheduled\|manual, missing_count, recipient_count, requested_by_user_id, request_id, window_key, sent_at)` | append-only | the sweep, in the client's own tenant context |
| `chase_recipient_count(recipient_count, counted_at)` | cache (upsert) | the sweep, in the client's own tenant context |

All rows carry `organization_id` (the owner, checked by trigger) and `administration_id`; SELECT
is `app.has_administration_access`. INSERT on `chase_send` and writes to `chase_recipient_count`
additionally need `organization_id = app.current_org_id()`, so only a session in the client's own
context - the sweep's - can record a send; a firm session cannot forge one (tested). No UPDATE or
DELETE is granted on the three records. Two partial unique indexes make "once" a database fact:
`(administration_id, window_key) WHERE kind='scheduled'` and `(request_id)`.

`chase_request`, `chase_recipient_count`, and `chase_send.request_id`/`window_key` are additions
to the contract's schema (constraint 1 and 3 above).

### Who is mailed (the recipient rule)

The **client's own people** for whom the authorization library allows **both** `submit expense`
(the receipt capture the link leads to) **and** `view bank_transaction` (the list itself):

- a live grant from the administration's owning organization (organization-scoped at the owner,
  or administration-scoped and granted by the owner - the split `api.questions.notifications` and
  IAM-100's profile cap draw); firm staff are never mailed, they are the ones asking;
- an active account with a verified e-mail address;
- `users.reminder_emails` on (0069, ADR-090's switch in Settings > Profile): a person who has
  turned reminder e-mails off is not chased either;
- `AuthorizationService.authorize()` for each of the two permissions on the administration,
  evaluated against current state in the client's tenant context - no hand-written role list.

In the standard roles that is the client's Owner, Accountant and Bookkeeper. A Viewer (can see the
list, cannot submit) and an Expense Submitter (can submit, may not see the company's bank lines,
§8.4) are not mailed. Unlike ADR-111's question mails (a reply in a conversation the person is
part of), a chase is a recurring reminder, so the person's own reminder opt-out is respected
(integration decision, wave 2). When that leaves nobody, the recount stores 0 and the preview
shows `no_recipient`.

### The sweep

`python -m api.chasing.sweep [--dry-run]`, every 5 minutes, with `DATABASE_URL` (ledgr_app) and
`OPS_DATABASE_URL` (ledgr_ops):

1. **Discovery as ledgr_ops** (SELECT only): manual requests younger than 24 hours with no send;
   during send hours, clients whose latest setting is enabled; firm-engaged clients whose
   recipient count is missing or older than an hour (500 per run).
2. **Per client, as ledgr_app with `app.current_org_id` = the owner**, one transaction under
   `pg_advisory_xact_lock` on the administration (overlapping runs serialize; the second sees the
   first's row): resolve recipients, refresh the count, then send if all hold -
   - a manual request not yet sent, or (scheduled) chasing still enabled in the client's context;
   - no chase of any kind in the last 24 hours;
   - scheduled only: no chase of any kind yet in the current cadence window, and within send
     hours (weekdays 08:00-16:00 UTC);
   - at least one missing line, at least one recipient.

   The `chase_send` row and an audit entry (actor `system`, category `configuration`, filed in
   the client's chain, `detail` = kind, counts, window, request and requester) are written only
   if at least one mail was accepted, with `recipient_count` = mails accepted. If every mail is
   refused nothing is written and the next run retries. `--dry-run` writes nothing, not even the
   recount.

**Windows** are ISO weeks (`2026-W41`) or pairs of ISO weeks (`2026-F21`) in **UTC**: the API has
no tz-database dependency (Windows hosts ship none), and a window turning over at 01:00/02:00
Dutch time changes nothing a person notices. A manual chase carries the window key too, so it
counts as that window's chase. Changing cadence mid-window can, at worst, allow a second chase
24 hours after the first.

### The manual bulk request

`POST /v1/firm/chase/preview` answers per ticked client in a fixed number of queries:
`missing_count`, `recipient_count` (from the cache; `null` = not counted yet), `last_chased_at`,
and `blocked_reason` in the order a person wants to hear it - `nothing_missing`,
`chased_recently` (a send **or a request** in the last 24 hours), `no_recipient` (counted, zero;
an uncounted client is not blocked - the sweep decides at delivery).

`POST /v1/firm/chase/send` re-evaluates the same rules, records a `chase_request` per unblocked
client, audits each under the client's organization (actor the firm user, `detail.acting_organization_id`,
ADR-112), and returns `{sent, skipped[]}`. **`sent` counts requests accepted for delivery**: the
sweep delivers them within one run (about 5 minutes) - "sends now" in the sense the cron allows,
because the request itself cannot know whom to mail (constraint 1). Ids outside the caller's
portfolio are reported as `administration_not_found` and never read. At most 1000 ids per call
(the worklist's "Show all").

### Endpoints and permissions

| Route | Declares | Audit |
|---|---|---|
| `GET /v1/administrations/{id}/missing-receipts` | `require_permission("view", "bank_transaction")` | - |
| `GET /v1/administrations/{id}/chase-setting` | `require_permission("view", "bank_transaction")` | - |
| `POST /v1/administrations/{id}/chase-setting` | `require_permission("reconcile", "bank_transaction")` | configuration |
| `POST /v1/firm/chase/preview` | `require_portfolio_permission("reconcile", "bank_transaction")` | financial_read |
| `POST /v1/firm/chase/send` | `require_portfolio_permission("reconcile", "bank_transaction")` | configuration (+ one entry per client) |

- **`view bank_transaction`** for the list: it shows bank lines (date, amount, counterparty,
  description) - the Bank screen's data. Owner, Accountant, Bookkeeper and Viewer hold it.
- **`reconcile bank_transaction`** for opting in and for chasing: chasing exists to finish
  reconciling the bank, and these are the people who do that (Owner, Accountant, Bookkeeper) -
  the permission the auto-bookings review decides with (ADR-110). A Viewer can read but cannot
  make the product e-mail a client. No new permission, so no Appendix A or 0010 change.
- **IAM-090 has no "notification" category**; following ADR-111, changing how the product acts
  toward a tenant is `configuration`. The preview is a POST only because it carries an id list,
  and every POST must declare a category; it reads per-client bank counts, so `financial_read`.

### What the mail says

Subject "Your accountant needs receipts for {name}"; body "{n} bank payment(s) of {name} still
need a receipt or invoice. For privacy, this e-mail does not say which." and the link
`{app_base_url}/receipts-needed?administration=<id>` - the web app switches to that
administration first when the person has it and it is not their active one. Never an amount, counterparty, description or document content
(FR-NTF-004's rule applied to e-mail, as ADR-111 does; PRIV-012 plain text, no tracking). Keys
`reminders.chase.*` in `reminders.json`, NL and EN, plural-aware; the recipient's own language.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Send the manual chase inside the firm's request | The firm session cannot see the client's organization-scoped grants; making it see them needs ledgr_ops in a request path, a SECURITY DEFINER function or a borrowed tenant context (decision 1, ADR-111). |
| Do the whole sweep as ledgr_ops, like the question notifications | Resolving recipients through `authorize()` (one library, rule 3) and appending to the audit chain need the client's tenant context; ledgr_ops has no INSERT on `audit_log`, and a role-name list in SQL would be a second authorization implementation. |
| Rolling "7 days since the last send" instead of calendar windows | Drifts: a send at 09:00:03 makes next week's 09:00:01 run skip, and the chase walks through the week. Calendar windows plus a unique index are deterministic and idempotent. |
| Preview recipient count by querying grants visible to the firm | The firm sees only administration-scoped grants; the Owner's organization-scoped grant is invisible, so the number would be wrong exactly in the common case. |
| Include the lines (amounts, counterparties) in the mail | FR-NTF-004; the list is one sign-in away behind authorization. |
| A new `chase receipts` permission | Needs an Appendix A extension and a regenerated 0010; `reconcile bank_transaction` names the right people. |

## Consequences

- **Cron to schedule** (Railway cron service, every 5 minutes, from `apps/api` with
  `PYTHONPATH=src`): `python -m api.chasing.sweep`, with `DATABASE_URL` and `OPS_DATABASE_URL`.
  Until it runs, opt-ins and requests are recorded but nobody is mailed, and preview recipient
  counts stay `null`.
- Migration 0083 must be applied before the routes are served (not auto-applied on deploy).
- The worklist's `last_chased_at` (backend-firm-ext) reads `max(chase_send.sent_at)`; it moves
  when the sweep sends, not when the request is made.
- A manual request the sweep cannot deliver within 24 hours (nothing missing any more, nobody to
  mail, provider down all day) lapses silently; the accountant sees no `last_chased_at` change
  and can ask again.
- `chase_recipient_count` is up to an hour stale; the sweep re-resolves recipients at delivery,
  so a stale count can only make the preview wrong, never the send.
