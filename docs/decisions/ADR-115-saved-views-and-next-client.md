# ADR-115: Saved views and "next client" evaluate the worklist's own query

- **Status**: Accepted
- **Date**: 2026-10-09
- **Builds on**: ADR-109 (firm worklist); siblings ADR-113 (approval rules) and ADR-114 (receipt chasing)

## Context

Wave 2 of the firm home (docs/firm-home/contract-wave2.md, decisions 6 and 7) adds saved views
(FR-FRM-000b: a person's named worklist filters with a live count) and "next client with work"
(FR-FRM-002: work by task across clients). Both are questions about the worklist: "how many rows
would this query show?" and "which row follows this one?". The failure to avoid is a second
implementation of the worklist's filters that drifts from the first - a view saying 12 that opens
on 11, or "next" jumping to a client the list would not show.

## Decision

**One query definition.** `api.firm.worklist_model.WorklistQuery` (chip, q, assigned, sort,
direction, vat_frequency) is what decides which rows show and in which order. `worklist_page`,
`count_rows` (a view's count) and `next_client` all evaluate it over the same rows, built by
`api.firm.worklist_routes.load_rows` from the same authorized portfolio (ADR-109's three layers).
A unit test asserts page total = view count = selected rows for a table of queries.

**Saved views (migration 0084, `saved_view`).**
- Stored: the query exactly as the worklist takes it (`SavedViewQuery`, unknown keys refused),
  never a derived figure. The count is computed per request; nothing to go stale.
- `assigned: "me"` is stored as `me` and resolved for the reader, who is always the owner.
- Per person: RLS is organization-level (`organization_id = app.current_org_id()`, as
  idempotency_key); `user_id = <verified token's user>` is in every repository statement. A
  colleague's view id reads as 404, the same as an id that does not exist.
- Never deleted: archive sets `archived_at`; ledgr_app has UPDATE on `name`, `position`,
  `archived_at` only and a trigger refuses un-archiving or changing the owner or query.
- Max 20 active views per person, checked under a transaction-scoped advisory lock on the person;
  names 1–60 characters after trimming.
- `GET /v1/firm/views` reads the portfolio ONCE per request and counts every view from it (one
  ~10-query portfolio load, not one per view). A stored query that no longer validates is listed
  with `count: null` rather than failing the list.
- Routes: `GET|POST /v1/firm/views`, `POST .../{id}/rename` (→ the view), `POST .../{id}/archive`
  (→ 204). All declare `require_portfolio_permission` (view report): the counts are portfolio
  reads. The writes touch only the caller's own rows and are audited as configuration.

**Next client (`GET /v1/firm/worklist/next?after=&<query>`).**
- The candidates are the query's rows in its order, minus snoozed clients and `after` itself.
- `after` is placed where the sort would put it, using its CURRENT row, even when it no longer
  matches the query. That is the normal case: finishing a client's work takes it off "My move",
  and "next" must still mean the one after it rather than jumping back to the top.
- An `after` not in the caller's authorized portfolio (unknown, revoked, another tenant's) has no
  position to use: the answer starts from the top. Its row is never read, so nothing about it is
  disclosed, and the answer can only ever name a portfolio client.
- `remaining` counts the returned client and every candidate after it; at the end
  `administration_id` is null and `remaining` 0.

**Worklist additions.** `vat_frequency=monthly|quarterly|yearly` filters rows and chip counts
alike (a client without a VAT period has no frequency and is excluded by any value; `yearly`
matches nobody until `fiscal_year.period_scheme` gains it). Rows gain `last_chased_at`
(`max(chase_send.sent_at)`, 0083) and `rules_count` (active `booking_rule` rows, 0082): two more
set-based queries in the portfolio load. The summary's activity gains `rule_postings`: proposals
with `rule_id` set, `status = 'approved'` and `decided_at` inside the window.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Store the count on the view, refreshed by a job | Stale by design, and a second definition of the filters. |
| A count query per view in SQL | N portfolio loads per request and a second implementation of chips/sort in SQL. |
| RLS on `user_id` as well | No `app.current_user_id()` exists in the session today; the contract fixed RLS at the organization and the repository predicate is tested. |
| "Next" = next row of the list the client opened from (a snapshot) | Stale against current data; decision 7 requires server-side, current evaluation. |
| "Next" with an `after` that left the list = start over | Sends the accountant back to the top after every finished client. |

## Consequences

- The worklist load now reads `chase_send` and `booking_rule`, so 0082 and 0083 must be applied
  before this build serves the firm home.
- Reversal of 0084 is documented in the migration header.
