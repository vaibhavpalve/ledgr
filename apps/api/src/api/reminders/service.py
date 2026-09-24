"""The daily reminder run (ADR-090): who is reminded of what, and sending each reminder once.

Runs as ledgr_ops across every tenant (scripts/send_reminders.py), the way the document-retention
sweep does - due dates and waiting receipts are read for all administrations at once. Nothing here
writes a posting table or any tenant data; the one write is `reminder_log`.

--- Who ---

The people who act on a BTW return: a live Owner or Accountant grant on the administration
(organization-wide or on that administration), a verified e-mail address, an active account, and
`reminder_emails` left on. A bookkeeper prepares but does not file, so is not chased about the
deadline; an Expense Submitter is not told about receipts they cannot book.

--- Once ---

For each (person, reminder) the log row is inserted first, inside a savepoint; only when the
insert took (it did not already exist) is the mail handed over. A provider refusal rolls the
savepoint back, so the next run tries again; a second run the same day inserts nothing and sends
nothing.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.i18n.catalogue import translate
from api.i18n.language import Language, parse_language
from api.mail.sender import EmailMessage, EmailSender, UnsendableMessage
from api.reminders.model import (
    OpenPeriod,
    Reminder,
    ReminderKind,
    receipts_reminder,
    vat_reminders,
)


@dataclass(frozen=True, slots=True)
class Recipient:
    administration_id: uuid.UUID
    organization_id: uuid.UUID
    administration_name: str
    user_id: uuid.UUID
    email: str
    language: Language


@dataclass
class RunReport:
    sent: int = 0
    already_sent: int = 0
    failed: int = 0
    details: list[str] = field(default_factory=list)


class DeliveryRefused(Exception):
    pass


async def _recipients(session: AsyncSession) -> list[Recipient]:
    result = await session.execute(
        text(
            """
            SELECT DISTINCT a.id AS administration_id, a.organization_id,
                   COALESCE(a.trade_name, a.legal_name) AS name,
                   u.id AS user_id, u.email, u.language
              FROM administration a
              JOIN role_assignment ra
                ON ra.revoked_at IS NULL
               AND (ra.expires_at IS NULL OR ra.expires_at > now())
               AND ((ra.scope_type = 'organization' AND ra.scope_id = a.organization_id)
                 OR (ra.scope_type = 'administration' AND ra.scope_id = a.id))
              JOIN "role" r ON r.id = ra.role_id AND r.name IN ('Owner', 'Accountant')
              JOIN users u ON u.id = ra.user_id
             WHERE u.status = 'active'
               AND u.email_verified_at IS NOT NULL
               AND u.reminder_emails
            """
        )
    )
    return [
        Recipient(
            administration_id=row.administration_id,
            organization_id=row.organization_id,
            administration_name=row.name,
            user_id=row.user_id,
            email=row.email,
            language=parse_language(row.language) or Language.NL,
        )
        for row in result
    ]


async def _open_periods(session: AsyncSession, *, today: date) -> dict[uuid.UUID, list[OpenPeriod]]:
    result = await session.execute(
        text(
            """
            SELECT p.id, p.administration_id, p.start_date, p.end_date,
                   EXISTS (SELECT 1 FROM journal_entry e
                             JOIN journal_line l ON l.journal_entry_id = e.id
                            WHERE e.period_id = p.id AND l.vat_treatment IS NOT NULL) AS has_vat
              FROM period p
             WHERE p.status <> 'vat_filed'
               AND p.end_date < :today
               AND p.end_date >= :since
            """
        ),
        # A return more than a year late is the accountant's conversation, not a daily e-mail.
        {"today": today, "since": today - timedelta(days=400)},
    )
    periods: dict[uuid.UUID, list[OpenPeriod]] = defaultdict(list)
    for row in result:
        periods[row.administration_id].append(
            OpenPeriod(
                period_id=row.id,
                start=row.start_date,
                end=row.end_date,
                has_vat_postings=bool(row.has_vat),
            )
        )
    return periods


async def _waiting_receipts(session: AsyncSession) -> dict[uuid.UUID, tuple[int, date]]:
    result = await session.execute(
        text(
            "SELECT administration_id, COUNT(*) AS waiting, MIN(created_at)::date AS oldest "
            "  FROM expense WHERE status IN ('draft', 'ready') GROUP BY administration_id"
        )
    )
    return {row.administration_id: (int(row.waiting), row.oldest) for row in result}


def build_message(recipient: Recipient, reminder: Reminder) -> EmailMessage:
    """Plain text in the person's language, from the catalogue (FR-LOC-001) - the same posture
    as the verification mail: no HTML, no tracking surface (PRIV-012)."""
    language = recipient.language
    base = settings.app_base_url.rstrip("/")
    name = recipient.administration_name

    def d(value: date | None) -> str:
        return value.strftime("%d-%m-%Y") if value else ""

    if reminder.kind is ReminderKind.RECEIPTS_WAITING:
        subject = translate("reminders.receipts.subject", language, name=name)
        lines = [
            translate("reminders.receipts.body", language, count=reminder.count or 0, name=name),
            "",
            f"{base}/purchases",
        ]
    else:
        subject_key, body_key = (
            ("reminders.vat_due.subject", "reminders.vat_due.body")
            if reminder.kind is ReminderKind.VAT_DUE
            else ("reminders.vat_overdue.subject", "reminders.vat_overdue.body")
        )
        subject = translate(subject_key, language, name=name, due=d(reminder.due))
        lines = [
            translate(
                body_key,
                language,
                name=name,
                start=d(reminder.period_start),
                end=d(reminder.period_end),
                due=d(reminder.due),
            ),
            "",
            f"{base}/vat",
        ]
    body = "\n".join(
        [
            translate("reminders.greeting", language),
            "",
            *lines,
            "",
            translate("reminders.why", language, product=settings.webauthn_rp_name),
            f"{base}/settings/profile",
            "",
            translate("reminders.signoff", language),
            settings.webauthn_rp_name,
        ]
    )
    return EmailMessage(
        to=recipient.email,
        subject=subject,
        body=body,
        from_address=settings.email_from_address,
        from_name=settings.webauthn_rp_name,
    )


async def run_reminders(
    session: AsyncSession, sender: EmailSender, *, today: date, dry_run: bool = False
) -> RunReport:
    report = RunReport()
    recipients = await _recipients(session)
    periods = await _open_periods(session, today=today)
    receipts = await _waiting_receipts(session)

    for recipient in recipients:
        due: list[Reminder] = vat_reminders(
            periods.get(recipient.administration_id, []), today=today
        )
        waiting = receipts.get(recipient.administration_id)
        if waiting is not None:
            reminder = receipts_reminder(waiting=waiting[0], oldest=waiting[1], today=today)
            if reminder is not None:
                due.append(reminder)

        for reminder in due:
            savepoint = await session.begin_nested()
            inserted = await session.execute(
                text(
                    "INSERT INTO reminder_log (organization_id, administration_id, user_id, "
                    "  kind, reminder_key) VALUES (:org, :admin, :user, :kind, :key) "
                    "ON CONFLICT DO NOTHING RETURNING id"
                ),
                {
                    "org": str(recipient.organization_id),
                    "admin": str(recipient.administration_id),
                    "user": str(recipient.user_id),
                    "kind": reminder.kind.value,
                    "key": reminder.key,
                },
            )
            if inserted.first() is None:
                await savepoint.rollback()
                report.already_sent += 1
                continue
            label = f"{reminder.key} -> {recipient.email}"
            if dry_run:
                await savepoint.rollback()
                report.details.append(f"would send {label}")
                report.sent += 1
                continue
            try:
                outcome = await sender.send(build_message(recipient, reminder))
                if not outcome.accepted:
                    raise DeliveryRefused(outcome.detail or outcome.provider)
            except (DeliveryRefused, UnsendableMessage, OSError) as exc:
                await savepoint.rollback()
                report.failed += 1
                report.details.append(f"failed {label}: {exc}")
                continue
            await savepoint.commit()
            report.sent += 1
            report.details.append(f"sent {label}")
    return report
