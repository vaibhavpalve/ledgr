# Firm home, wave 2 — build contract

Builds on `docs/firm-home/contract.md` (wave 1, committed fd9bc61..c38c285). Same rules: this file
is binding for the parallel agents; a needed change is reported, not silently made.

Requirements: FR-BNK-004 (auto-post above threshold, conservative defaults), FR-BNK-006 (learning
rules per tenant, never across tenants), FR-FRM-005 (questions, client side), FR-FRM-000b (saved
filters), FR-FRM-002 (work by task across clients), FR-NTF-001/004.

## Decisions (made; do not re-litigate)

1. **Approval rules are per administration** (FR-BNK-006: never across tenants). Approving a
   proposal group spanning 9 clients with "Always do this" creates 9 rules, one per client.
2. **A rule only auto-approves what would already be a proposal**: a single certain HIGH match of a
   bank line to an existing document (receipt or invoice) whose normalised counterparty and target
   account equal the rule's. Rules never invent bookings without a document. Optional
   `max_amount` cap; a match above it stays a normal pending proposal.
3. **Auto-approval posts through exactly the wave-1 approve path** (BankService reconcile → ledger
   service), in the same savepoint/idempotency discipline. Audit actor is the system, with
   `detail.rule_id` and `detail.rule_created_by`. At apply time the rule's creator must STILL hold
   `reconcile bank_transaction` on that administration (authorize() against current state); if not,
   the rule is skipped (stays pending) and marked `suspended` with a reason.
4. **Visible, reversible in effect.** Every rule-posted booking is listed (per client and in the firm
   summary activity as `rule_postings`). "Undo" uses whatever existing un-match/reversal path the
   bank module has; if none exists, undo is NOT built in this wave and the ADR says so. Ledger stays
   append-only: undo never deletes or edits a posting.
5. **Receipt chasing e-mails contain no amounts, counterparties or document contents** (FR-NTF-004
   spirit): "{n} bank payments still need a receipt" + a link to the client's in-app list. A
   "missing receipt" is an unmatched bank debit with no candidate receipt (no captured,
   not-discarded expense of exactly that amount that no other line has settled) and no pending
   HIGH proposal. That definition lives once, in `api.chasing.missing` (`PENDING_HIGH_PROPOSAL`,
   `NO_CANDIDATE_RECEIPT`, `MISSING_RECEIPT`); the worklist's bank buckets import it. Chasing
   is opt-in per client (`chase_enabled`, cadence `weekly` default), sent by an ops sweep, never more
   than once per cadence window, and stops when nothing is missing. A manual bulk "Request missing
   receipts" sends now (respecting a 24h minimum gap) after a preview.
6. **Saved views are per user**, store the worklist query (`chip`, `q`, `assigned`, `sort`, `dir`,
   plus new filters `vat_frequency`), and show a live count.
7. **Next client with work** = the next administration in the caller's current worklist query
   (same filters/sort), after the one currently open, skipping snoozed. Server-side, so it is
   always against current data.
8. Unchanged from wave 1: money is decimal strings; idempotency keys on mutations; every new route
   has an `@pytest.mark.isolation` test; cross-client routes declare `require_portfolio_permission`;
   per-client routes declare `require_permission` on the path's administration.

## Numbers and ownership

| Agent | ADR | Migration | Owns |
|---|---|---|---|
| backend-rules | ADR-113 | 0082 | `api/firm/rules*.py` (new), narrow edits in `api/firm/proposals*.py` and `api/bank/*` |
| backend-chasing | ADR-114 | 0083 | `api/chasing/` (new), its mail templates/catalogue keys |
| backend-firm-ext | ADR-115 | 0084 | `api/firm/views*.py` (new), `api/firm/worklist*`, `api/firm/summary_routes.py`, `api/questions/notifications.py` (link only) |
| frontend-firm | — | — | `apps/web/src/firm/**` |
| frontend-client | — | — | `apps/web/src/questions/**` (new), `apps/web/src/receipts/**` (new) |

Shared files, edit with re-Read immediately before each Edit, add only your own lines:
`api/main.py`, `packages/i18n/catalogue/errors.json`, `packages/i18n/catalogue/client.json`,
`packages/shared-types/src/index.ts`, `apps/web/src/App.tsx`, `apps/web/src/shell/AppShell.tsx`,
`apps/web/src/session/ServicesProvider.tsx`.

## Schema

**0082 (rules)**
```
booking_rule(id, organization_id, administration_id,
  counterparty_key text not null,      -- normalise_counterparty() output
  account_code text not null,
  max_amount numeric(18,2) null,
  status text check in ('active','suspended','retired') default 'active',
  suspended_reason text null,
  created_by_user_id, created_at, retired_by_user_id null, retired_at null)
  unique (administration_id, counterparty_key, account_code) where status <> 'retired'
booking_proposal gains: rule_id uuid null references booking_rule
```
Status moves active→suspended→active / any→retired (trigger); never deleted. RLS on
`app.has_administration_access`.

**0083 (chasing)**
```
chase_setting(organization_id, administration_id, enabled bool, cadence ('weekly','fortnightly'),
  set_by_user_id, created_at)                       -- append-only, latest row wins
chase_send(id, organization_id, administration_id, kind ('scheduled','manual'),
  missing_count int, recipient_count int, requested_by_user_id null, sent_at)   -- append-only
```

**0084 (views)**
```
saved_view(id, organization_id, user_id, name text, query jsonb, position int,
  created_at, archived_at null)      -- RLS: organization = current org AND user_id = the caller
```
(per-user filtering is enforced in the repository by `user_id = tenant.user_id`; RLS is org-level.)

## Endpoints

### backend-rules
- `POST /v1/firm/proposals/decide` — each decision may carry `"remember": true` (approve only). On
  success the service creates the per-administration rule (or reactivates a suspended one). Response
  gains `"rules_created": n`.
- `GET /v1/administrations/{id}/rules` → `{ "rules": [ { id, counterparty_key, counterparty_label,
  account_code, account_name, max_amount, status, suspended_reason, created_by_name, created_at,
  postings_count, last_posted_at } ] }`
- `POST /v1/administrations/{id}/rules/{rule_id}/retire` → rule
- `POST /v1/administrations/{id}/rules/{rule_id}/max-amount` `{ "max_amount": "250.00"|null }` → rule
- `GET /v1/administrations/{id}/rule-postings?limit=` → `{ "items": [ { proposal_id, rule_id,
  date, amount, counterparty, account_code, posted_at, undoable: bool } ] }`
- `POST /v1/administrations/{id}/rule-postings/{proposal_id}/undo` — only if decision 4 found a path.
- Generation: when a new pending HIGH proposal matches an active rule, auto-approve it per decision 3.

### backend-chasing
- `GET /v1/administrations/{id}/missing-receipts` → `{ "items": [ { bank_transaction_id,
  booking_date, amount, counterparty, description } ], "count": n }` — the client's own list
  (debits with no candidate receipt and no pending HIGH proposal: decision 5).
- `GET|POST /v1/administrations/{id}/chase-setting` `{ "enabled": bool, "cadence": "weekly" }`
- `POST /v1/firm/chase/preview` `{ "administration_ids": [...] }` → `{ "items": [ {
  administration_id, display_name, missing_count, recipient_count, last_chased_at,
  blocked_reason: null|"nothing_missing"|"chased_recently"|"no_recipient" } ] }`
- `POST /v1/firm/chase/send` `{ "administration_ids": [...] }` → `{ "sent": n, "skipped": [ {
  administration_id, reason } ] }`
- Ops sweep `python -m api.chasing.sweep [--dry-run]` for scheduled sends.
- Link in mail: `{app_base_url}/receipts-needed?administration=<id>`. Recipients are the client
  organization's users with `users.reminder_emails` on; none left → `no_recipient`.

### backend-firm-ext
- `GET /v1/firm/views` → `{ "views": [ { id, name, query, count } ] }`; `POST /v1/firm/views`
  `{ name, query }`; `POST /v1/firm/views/{id}/rename` `{ name }`; `POST /v1/firm/views/{id}/archive`.
  Max 20 active views per user; name 1–60 chars.
- `GET /v1/firm/worklist` gains: `vat_frequency=monthly|quarterly|yearly`, and each row gains
  `last_chased_at` (from `chase_send`) and `rules_count` (active `booking_rule` rows).
- `GET /v1/firm/worklist/next?after=<administration_id>&<same query params>` →
  `{ "administration_id": "…"|null, "display_name": "…"|null, "remaining": n }`
- `GET /v1/firm/summary` activity gains kind `rule_postings` (count + clients).
- Question mail link → `{app_base_url}/questions?administration=<id>`. The web app switches to that
  administration first when the user has it and it is not the active one.

## Web

**frontend-firm** (`apps/web/src/firm/`):
- Review sheet: per group "Always do this for these clients" checkbox → `remember: true`; result
  line shows rules created. Short explanation text of what a rule does (decision 2).
- Bulk bar: "Request missing receipts" → preview dialog (per client: count, last chased, blocked
  reason) → send. Row shows "Chased 3 d ago" from `last_chased_at`.
- Saved views in the sidebar (name + count), "Save view" from the worklist's current query, rename,
  archive. Clicking applies the query. "More filters" gains VAT frequency.
- Next client: when a firm user is inside a client opened from the worklist, a persistent
  "Next client with work →" control in the client header area calls `/v1/firm/worklist/next` with
  the last worklist query (kept in sessionStorage, wrapped in try/catch) and switches to it.
  Shows "{remaining} left". Hidden for business users.
- Client rules screen inside a client: list rules (retire, set max amount) and rule postings (undo
  if `undoable`). Route `/rules` under RequireAdministration; reachable from client settings or the
  bank screen — pick the least intrusive existing entry point.
- Chase setting toggle on the same `/rules`-style client settings surface or in the client header
  menu (pick one, keep it findable).

**frontend-client** (`apps/web/src/questions/`, `apps/web/src/receipts/`):
- `/questions` (inside a client, for both client and firm users): open/resolved tabs, thread list,
  thread view with messages (author side shown with a word), reply box, resolve (firm side only if
  the API allows the client too, follow the API), mark-read on open. Uses the nested
  `/v1/administrations/{id}/questions...` routes.
- Firm: "New question" from the thread list, optionally attached to a resource when opened with
  `?resource_type=&resource_id=`.
- `/receipts-needed` (inside a client): the missing-receipts list with, per line, "Upload receipt"
  using the existing capture flow (reuse its component/route; do not build a second uploader).
- Firm inbox items (`/inbox`) link to `/questions/{thread_id}` after switching client.
- Nav entries for client users: "Questions" (with unread count if cheaply available) and
  "Receipts needed" (count) in the existing shell, following platform patterns.

## Wave 3 (not now)
Bank rules that book lines without a document (bank fees etc., VAT implications), push/in-app
notification channels (FR-NTF-002), mobile app screens, deadline calendar.
