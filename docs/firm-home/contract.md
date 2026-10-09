# Firm home ("week-back view") — build contract

Source spec: the accountant-home spec + `accountant-home.html` mockup, reviewed 2026-10-08 from the
perspective of an accountant with 200 clients. This file is the single source of truth the parallel
build agents code against. If an agent needs to change a shape here, it says so in its final report
instead of silently diverging.

Requirements served: FR-FRM-001 (portfolio status), FR-FRM-002 (cross-client work queue),
FR-FRM-000b (grouping/saved filters, partial), FR-FRM-004b (bulk assignment, partial),
FR-FRM-005 (question threads), FR-BNK-003/004 (confidence, propose), FR-UX-005.

## Decisions already made (do not re-litigate)

1. **Cross-client reads.** RLS already scopes a firm session to every administration the firm has an
   active `firm_engagement` on (`app.has_administration_access`). The firm endpoints run in that
   ordinary tenant session and then restrict, in SQL, to the administrations the *user* holds a live
   grant on — the same join as `api.firm.switcher_repository._BASE`. RLS is the first line; the grant
   join plus `authorize()` per administration is the second. No SECURITY DEFINER shortcut, no second
   authorization implementation. Recorded in **ADR-109**.
2. **Auto-bookings are proposals, not postings.** A `booking_proposal` row is outside the ledger.
   Approving one posts it through the existing bank-match/book path (ledger service only). Rejecting
   one leaves the ledger untouched. Nothing posted is ever flagged, edited or deleted. Recorded in
   **ADR-110**.
3. **"Waiting on me" vs "waiting on client"** are separate. *My move*: auto_bookings, to_book,
   bank_to_match, VAT ready_to_review, open questions awaiting the firm (`awaiting='firm'`: the
   client replied). *Waiting on client*: missing_receipts, open questions awaiting the client
   (`awaiting='client'`, which also feed `waiting_on_client_since`), broken bank feeds.
   `counts.open_questions` stays the total of open threads, whichever side they await.
4. **booked_until is capped by the feed.** `booked_until = least(last fully-matched date,
   min(last_synced_at) over the client's linked feeds)` — a broken feed must never make books look
   complete. `booked_until_capped_by_feed` says when the cap applied.
5. **Buckets are mutually exclusive** for an unmatched bank transaction:
   pending HIGH proposal → `auto_bookings`; else debit with no candidate document → `missing_receipts`;
   else → `bank_to_match`. `to_book` = uploaded documents/draft expenses not yet booked and not covered
   by a pending proposal.
6. **"Since" window** = `users.firm_activity_seen_at` (set by `POST /v1/firm/summary/seen`), falling
   back to `users.previous_login_at`, then 7 days. Peeking on a phone does not reset it.
7. **Risk sort** (default): ascending `risk`, where risk is days to the client's next unfiled deadline
   minus a work penalty (`ceil(open_items_mine / 10)`), broken feed −3, books >1 month behind −5.
   Snoozed clients (snoozed_until > today) are excluded from `my_move` and `waiting_on_client`.
8. Money is always a decimal **string** on the wire (`"1234.56"`), never a float (NFR-031).
9. Every mutating endpoint takes `Idempotency-Key` (NFR-032). Every endpoint ships with an
   `@pytest.mark.isolation` tenant-isolation test (IAM-005).

## Numbers reserved per agent

| Agent | ADR | Migration | Python package |
|---|---|---|---|
| backend-firm-core | ADR-109 | 0078 | `api/firm/worklist*.py`, `api/firm/summary*.py`, `api/firm/assignment*.py` |
| backend-proposals | ADR-110 | 0079 | `api/firm/proposals*.py` (+ narrow hooks in `api/bank`) |
| backend-questions | ADR-111 | 0080 | `api/questions/` (new package) |
| frontend-firm-home | — | — | `apps/web/src/firm/` (new), router, shell, i18n catalogue, shared-types |

## Schema (cross-agent; each agent creates only its own)

**0078 (firm-core)**
- `users.last_login_at timestamptz`, `users.previous_login_at timestamptz`,
  `users.firm_activity_seen_at timestamptz` (all nullable).
- `client_assignment(organization_id, administration_id, assigned_user_id, assigned_by_user_id,
  created_at)` — append-only; current assignment = latest row per administration.
- `client_snooze(organization_id, administration_id, snoozed_until date null, reason text,
  created_by_user_id, created_at)` — append-only; current = latest row; `snoozed_until null` clears.
- Both tables: RLS on `app.has_administration_access(administration_id)`, no UPDATE/DELETE grant.

**0079 (proposals)**
```
booking_proposal(
  id uuid pk, organization_id, administration_id,
  bank_transaction_id uuid null, document_id uuid null,
  proposal_kind text check in ('bank_match'),
  confidence text check in ('high','medium'),
  status text check in ('pending','approved','rejected','superseded') default 'pending',
  group_key text not null,          -- normalised counterparty + target account, for review grouping
  counterparty text, account_code text, amount numeric(18,2) not null,
  created_at, decided_at null, decided_by_user_id null)
```
Unique: one `pending` row per `bank_transaction_id`. RLS as above. Status may move pending→other
only (trigger), never back.

**0080 (questions)**
```
question_thread(id, organization_id, administration_id, subject, status ('open','resolved'),
  awaiting ('client','firm'), resource_type null, resource_id null,
  created_by_user_id, created_at, last_message_at, resolved_at null)
question_message(id, thread_id, organization_id, administration_id, author_user_id,
  author_side ('firm','client'), body, created_at)            -- append-only
question_read(thread_id, user_id, read_at)                   -- latest row wins
```

firm-core reads `booking_proposal` and `question_thread/question_message` for counts. Until those
migrations exist in the same DB, its repository must still compile; integration tests run after all
three migrations are applied together.

## Endpoints

All under the normal tenant session. Results contain only administrations the caller holds a live
grant on. Non-firm callers with grants (a business owner with two administrations) get the same
shapes over their own administrations.

### firm-core
`GET /v1/firm/summary?since=<iso>` →
```json
{ "previous_login_at": "…|null", "since": "…", "client_count": 200,
  "counts": { "auto_bookings": 48, "receipts_to_book": 62, "missing_receipts": 37,
              "bank_to_match": 31, "open_questions": 9, "broken_feeds": 2 },
  "activity": [ { "kind": "receipts_uploaded|auto_bookings_ready|client_replies|bank_feeds_broken|possible_duplicates",
                  "count": 86, "client_count": 14,
                  "clients": [ { "administration_id": "…", "display_name": "…" } ] } ] }
```
(`clients` lists at most 3.) `POST /v1/firm/summary/seen` → `204`.

`GET /v1/firm/worklist?chip=&q=&assigned=me|any|<user_id>&sort=&dir=asc|desc&page=1&page_size=50`
- `chip` ∈ `my_move` (default) | `waiting_on_client` | `vat_not_filed` | `books_behind` | `up_to_date` | `snoozed` | `all`
- `sort` ∈ `risk` (default) | `name` | `booked_until` | `to_book` | `missing_receipts` | `bank_to_match` | `open_questions` | `vat_due`
```json
{ "rows": [ {
    "administration_id": "…", "display_name": "…", "legal_name": "…", "kvk_number": "…|null",
    "initials": "EM", "colour": "…",
    "assigned_user_id": "…|null", "assigned_name": "…|null",
    "booked_until": "2026-06-30|null", "booked_until_capped_by_feed": false, "months_behind": 3,
    "counts": { "auto_bookings": 4, "to_book": 14, "missing_receipts": 6, "bank_to_match": 7, "open_questions": 3 },
    "waiting_on_client_since": "2026-09-29|null",
    "broken_feed": { "bank_name": "ING", "status": "expired|failed" } ,
    "vat": { "period_label": "2026-Q3", "frequency": "monthly|quarterly|yearly",
             "status": "not_started|in_progress|ready_to_review|ready_to_file|filed",
             "due_date": "2026-10-31", "days_to_due": 23 },
    "snoozed_until": "2026-10-15|null", "snooze_reason": "…|null",
    "last_chased_at": null,
    "risk": 12 } ],
  "total": 38, "page": 1, "page_size": 50,
  "chip_counts": { "my_move": 38, "waiting_on_client": 21, "vat_not_filed": 18, "books_behind": 7,
                   "up_to_date": 62, "snoozed": 4, "all": 200 } }
```
`page_size` defaults to 50, max 1000 (the UI's "Show all" asks for 1000). `chip_counts` are computed
with `assigned` and `q` applied but WITHOUT `chip`, so the chips show what each tab would contain.
`broken_feed` and `vat` may be `null`. `last_chased_at` is always `null` in this wave (chasing is wave 2).

`POST /v1/firm/clients/{administration_id}/snooze` `{ "until": "2026-10-15|null", "reason": "…" }` → `204`
`POST /v1/firm/clients/assign` `{ "administration_ids": [...], "user_id": "…|null" }` →
`{ "assigned": n, "failed": [ { "administration_id": "…", "reason": "…" } ] }`
`GET /v1/firm/staff` → `[ { "user_id": "…", "name": "…", "email": "…" } ]` (users of the caller's org)
`GET /v1/firm/deadlines?from=&to=` →
```json
[ { "kind": "vat", "period_label": "2026-Q3", "due_date": "2026-10-31", "days_to_due": 23,
    "client_count": 100,
    "buckets": { "filed": 64, "ready_to_file": 6, "ready_to_review": 12, "in_progress": 0, "not_started": 18 } } ]
```
Only obligations Boeklite actually tracks (VAT now; ICP only if `vat_returns` models it). No payroll.

Login hook: on successful session creation, `previous_login_at := last_login_at, last_login_at := now()`.

### proposals
`GET /v1/firm/proposals?administration_ids=a,b` (omitted = all granted) →
```json
{ "total": 48, "groups": [ {
    "group_key": "…", "counterparty": "KPN B.V.", "account_code": "4500", "account_name": "Telefoon",
    "count": 12, "client_count": 9, "total_amount": "843.20",
    "proposals": [ { "id": "…", "administration_id": "…", "display_name": "…",
                     "date": "2026-09-30", "amount": "70.27", "description": "…" } ] } ] }
```
`POST /v1/firm/proposals/decide` `{ "decisions": [ { "proposal_id": "…", "decision": "approve|reject" } ] }` →
`{ "approved": n, "rejected": n, "failed": [ { "proposal_id": "…", "reason": "…" } ] }`
Each approval is authorized against its own administration, posts via the ledger service, and is
audited individually. Partial failure is reported, never rolled up into a 500.
`GET /v1/administrations/{id}/proposals` → same group shape, one client.

### questions
`POST /v1/administrations/{id}/questions` `{ "subject", "body", "resource_type?", "resource_id?" }` → thread
`GET /v1/administrations/{id}/questions?status=open|resolved|all` → threads
`GET /v1/questions/{thread_id}` → thread + messages (marks read for caller)
`POST /v1/questions/{thread_id}/messages` `{ "body" }` → message (flips `awaiting`)
`POST /v1/questions/{thread_id}/resolve` → thread
`GET /v1/firm/inbox?unread=true&limit=20` →
```json
{ "unread_count": 5, "items": [ { "thread_id": "…", "administration_id": "…", "display_name": "…",
   "subject": "…", "excerpt": "first 120 chars", "last_message_at": "…", "unread": true } ] }
```

## Wave 2 (not now)
Approve-as-rule, automatic receipt chasing + `last_chased_at`, client-side question UI on mobile,
saved views (FR-FRM-000b), work-through mode, anomaly rules beyond existing duplicate detection.
