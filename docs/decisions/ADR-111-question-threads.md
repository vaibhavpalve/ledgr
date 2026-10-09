# ADR-111: Question threads between firm and client, with the client side derived from the session

- **Status**: Accepted
- **Date**: 2026-10-08
- **Implements**: FR-FRM-005 (question/answer threads attached to a transaction or document,
  resolvable, with client notification); IAM-005 (isolation test per route), NFR-032
  (idempotency on every mutation), FR-LOC-001 (both languages)
- **Builds on**: [ADR-090](ADR-090-reminder-emails.md) (mail from an ops sweep, once per person),
  [ADR-109](ADR-109-firm-worklist-cross-client-reads.md) (cross-client reads: RLS, then the
  switcher's grant join, then `authorize()` per administration, via
  `require_portfolio_permission`) and the firm-home build contract
  (`docs/firm-home/contract.md`, §questions)

## Context

An accountant working through a client's books constantly needs one fact only the client has: what
was this €70.27 to KPN, where is the receipt for this card payment. Today that question goes by
e-mail or phone, detached from the transaction, and the answer has to be found again later.
FR-FRM-005 asks for the question to live on the record it is about, to be resolvable, and for the
client to be told.

Three things had to be settled: who is on which side of the conversation, how the client is
notified when the firm's own session cannot see who the client's users are, and how routes that
act on a thread are authorized by the one library (CLAUDE.md rule three).

## Decision

**Schema (migration 0080).** `question_thread` (subject, `status` open/resolved, `awaiting`
client/firm, optional `resource_type`/`resource_id`), `question_message` and `question_read`, plus
`question_notification` for the e-mail log. Every row carries `organization_id` (the
administration's OWNING organization, checked by trigger) and `administration_id`; RLS on all four
is `app.has_administration_access(administration_id)`.

- `question_message` and `question_read` are **append-only**: `ledgr_app` holds SELECT and INSERT
  only. A message is never edited or withdrawn; a correction is another message. Read receipts are
  rows, latest wins, so two devices reading at once cannot lose a write.
- `question_thread` keeps `status`, `awaiting`, `last_message_at` (and the resolution columns) on
  the row and these are UPDATEd. They are a cache of the message log, kept because the firm home
  reads them for every client on every page view; recomputing them from messages for 200 clients
  per request is the wrong trade. A trigger makes every other column immutable, refuses
  `last_message_at` moving backwards, and makes `resolved` final - a follow-up is a new thread.
- A thread may be about one `bank_transaction`, `document`, `expense` or `sales_invoice`. The API
  checks the record exists **in the same administration** before creating the thread (a closed
  table mapping; the table name never comes from the request).

**Who is the client side.** Decided server-side from the session, never from the request body:

- the session's organization **owns** the administration → `client`;
- any other session that can reach it at all → `firm`.

These are exactly the two ways `app.has_administration_access` admits a session (ownership, or an
active `firm_engagement`), so there is no third case. In grant terms it is the same split
`api.authz.service` already draws for IAM-100's profile cap: a grant from the administration's own
organization (organization-scoped at the owner, or administration-scoped with
`granted_by_organization_id` = the owner) is the client's; one made by an engaged firm is firm
staff. `author_side` and `awaiting` are not request fields (an extra `author_side` in a body is
ignored, and the integration test sends one to prove it).

`awaiting` is always the side that did not write the last message.

**Endpoints.** Thread routes are nested under the administration:

| Route | Permission | Audit |
|---|---|---|
| `POST /v1/administrations/{id}/questions` | `view administration` | configuration |
| `GET /v1/administrations/{id}/questions?status=` | `view administration` | - |
| `GET /v1/administrations/{id}/questions/{thread_id}` (marks read) | `view administration` | financial_read |
| `POST …/questions/{thread_id}/messages` | `view administration` | configuration |
| `POST …/questions/{thread_id}/resolve` | `view administration` | configuration |
| `GET /v1/firm/inbox?unread=&limit=&awaiting=` | `require_portfolio_permission("view", "administration")` | - |

- **Nested paths are a deviation from the contract** (`/v1/questions/{thread_id}`). The library
  scopes a check to an administration read from the path; a route that names none could only be
  authorized by a second, hand-written check, and would skip FR-FRM-000a's active-client guard
  and IAM-109's access record. The thread is looked up within the named administration, so a
  thread id from elsewhere is a 404.
- **`view administration`**: the baseline every role on an administration holds. The people in a
  question thread are exactly those who can reach the administration, and Appendix A has no
  question-specific permission to require.
- **IAM-090 category**: its nine categories have no "conversation". Mutations are recorded as
  `configuration` (as bank reconciliation and the switcher are); reading a thread is
  `financial_read`, since a question is about a transaction or document.
- **The inbox** spans administrations, so it names none. Like every firm-home route it declares
  `require_portfolio_permission("view", "administration")` (`api.firm.worklist_access`,
  ADR-109): the switcher's grant join, then `authorize()` once per administration on a
  request-scoped repository, and the route receives the resulting `Portfolio`. The inbox reads
  threads only in the portfolio's administration ids - there is no second grant join or authorize
  loop in `api.questions`, and no entry in `AUTHORIZATION_EXEMPT_PATHS`. Unread means a message from the **other** side newer than the caller's last read, so a
  colleague's message on the caller's own side is not "waiting for them".
- **`awaiting=firm|client|any`** (default `any`) on the inbox narrows the items **and**
  `unread_count` to threads awaiting that side. The firm home's "Client replies" panel and the
  sidebar's unread badge ask for `awaiting=firm`, so the firm's own questions still waiting on
  the client never show up as replies (QA, 2026-10-09). `/inbox` has two tabs: "Replies to you"
  (`firm`, default) and "Waiting on client" (`client`).

**Notification.** When the firm writes, the client's users are e-mailed; when the client writes,
nobody is (the firm's inbox is where a reply lands). Sending happens in a **sweep**,
`python -m api.questions.notifications`, run as `ledgr_ops` every few minutes - not in the
request. The client's Owner usually holds an organization-scoped grant at the client, and 0009's
RLS shows an organization's grants only to that organization: the firm's session cannot see who
to mail, by design. Reading them from the request would need a SECURITY DEFINER function or a
borrowed tenant context, which contract decision 1 rules out. The sweep:

- keys each mail on the thread's **latest** firm message, so two quick messages are one mail;
- inserts the `question_notification` row inside a savepoint before handing the mail over
  (ADR-090's pattern: a refusal rolls back and is retried, a second run sends nothing);
- skips a person who has already opened the thread since that message;
- ignores firm messages older than 7 days (a first run or an outage does not mail history);
- says only that there is a question and for which administration, asks the person to contact
  their accountant or sign in, and links the app's start page (`/`) - there is no client-side
  question screen yet (wave 2), so the mail promises none. Never the subject or the body, which may name an amount or a counterparty (FR-NTF-004's rule for
  push, applied to e-mail; PRIV-012 plain text). Strings are in `reminders.json`
  (`reminders.question.*`), in the recipient's language.

`ledgr_ops` gets column-level SELECT on the thread/message/read columns the sweep needs (no
subject, no body), plus `role_assignment.granted_by_organization_id` and `role.archived_at`.

## Alternatives considered

| Option | Rejected because |
|---|---|
| `/v1/questions/{thread_id}` as in the contract, authorized by a dependency that looks the thread up first | A second authorization path beside `require_permission`, without the active-client guard or IAM-109's access record. |
| `author_side` from the request body | A client could post as the firm and flip `awaiting`; the session already answers the question. |
| Side from the caller's grant provenance per request | Same answer as the session's organization in every reachable case (RLS admits only owner or engaged-firm sessions), at the cost of a grant query per message. |
| Send the e-mail inside the firm's request | The firm session cannot see the client's organization-scoped grants; making it see them needs a SECURITY DEFINER shortcut or a borrowed tenant context. |
| Include the question text in the e-mail | Subjects and bodies name amounts and counterparties; the mail leaves the EU-hosted product. |
| Reopen a resolved thread on reply | A second state transition to guard and audit; "resolved means done, ask again" is simpler for both sides and keeps firm-core's counts honest. |
| A new `question_thread` permission in the role catalogue | Needs an Appendix A extension and a regenerated 0010 catalogue (see 0066 for the cost of that); `view administration` expresses who participates. Revisit if a role must see an administration without taking part in its questions. |
| Mutable `question_read` (one row per user, UPDATE `read_at`) | Concurrent devices race; append-only rows cannot. |

## Consequences

- Firm-core counts `open_questions` and "questions awaiting the client" from `question_thread`
  (`status='open'`, `awaiting`), served by partial indexes on `(administration_id, …)`.
- FR-FRM-000a's active-client guard applies to posting and resolving: a firm user whose session
  has a different client open gets 409 `active_client_mismatch`. The web client must switch to the
  thread's client (or be at the firm home with no active client) before replying.
- The sweep needs a Railway cron service (e.g. every 5 minutes) running
  `python -m api.questions.notifications` with `OPS_DATABASE_URL`; until it runs, nobody is
  e-mailed and the thread still works in the app.
- E-mail delivery ignores `users.reminder_emails` (that switch is for reminders, ADR-090): a
  question from one's own accountant is not a reminder. FR-NTF-002's per-user channel preferences
  will govern it when built.
- `users` has no display name, so messages carry `author_email` beside `author_user_id`.
- A thread is attached to at most one record. Several records means several threads, or a mention
  in the body.
