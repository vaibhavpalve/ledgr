# ADR-090: Reminder e-mails from a daily job, sent once per person per reminder

- **Status**: Accepted
- **Date**: 2026-09-24
- **Implements**: FR-NTF (notifications), FR-VAT (the deadline side of the return), FR-EXP (receipts
  waiting); FR-LOC-001 (every e-mail in both languages)
- **Builds on**: [ADR-087](ADR-087-vat-return-from-the-ledger.md) (the return and its due date)

## Context

A bookkeeping product that only works when the customer remembers to open it loses to one that
tells them. The two things a Dutch SMB is most often late with are the BTW return (a fine) and
booking receipts (input BTW left off the return). LEDGR computed both and told nobody.

## Decision

- **`scripts/send_reminders.py`**, run once a day as `ledgr_ops` across all tenants (a Railway
  cron service, 07:00 Europe/Amsterdam), the way the document-retention sweep already runs.
- **What is due** is decided by pure rules (`api.reminders.model`):
  - *BTW due*: in the week before a return's due date, for a period that has ended, is not filed
    and has BTW postings.
  - *BTW overdue*: after the due date.
  - *Receipts waiting*: purchases captured but not booked for 3+ days, at most once per ISO week.
- **Who**: people with a live Owner or Accountant grant on the administration, whose address is
  verified and whose account is active, and who have not switched reminders off. A Bookkeeper
  prepares but does not file, so is not chased about the deadline.
- **Once**: `reminder_log` has a unique key `(administration, user, kind, key)`. The row is
  inserted inside a savepoint before the mail is handed over. If the insert found an existing
  row, nothing is sent. If the provider refuses, the savepoint rolls back and the next run
  retries. The job can run any number of times a day.
- **Opt-out** is the person's own: `users.reminder_emails`, set from Settings > Profile through
  `GET/PUT /v1/me/reminders` (authorization-exempt for the `/v1/me/language` reason: it touches
  only the caller's own row).
- **The mail** is plain text in the person's language from the catalogue, with a link to the
  screen that resolves it (`/vat`, `/purchases`) and one to switch reminders off (PRIV-012: no
  HTML, no tracking pixel).
- **ledgr_ops gets column-level SELECT** on exactly what decides a reminder: user
  id/email/language/status/verified/opt-out, grants, role names, and which journal lines in which
  period carry a VAT treatment. No amounts, no descriptions, no credentials.

## Alternatives considered

| Option | Rejected because |
|---|---|
| An in-process scheduler in the API | Several API replicas would each send; a crash mid-run has no owner. A cron job with a log is observable and idempotent. |
| Send on every page load (in-app only) | A person who does not open the app is the person who needs the reminder. |
| Remind every day in the window | Nagging; the unique key makes each reminder a single message. |
| Push notifications | There are no native apps yet (P2); e-mail reaches everyone today. |

## Consequences

**Operational step for the founder.** Create a Railway cron service that runs
`uv run python scripts/send_reminders.py` daily with `OPS_DATABASE_URL` and the production mail
provider set. Until then nothing is sent. `--dry-run` shows what would go out.

**Known gaps.** Only Owners and Accountants are reminded, and a firm's staff get one mail per
client administration (no digest across clients yet). The due-date rule is the monthly/quarterly
one in `api.vat.deadlines` (no annual filers or extensions).
