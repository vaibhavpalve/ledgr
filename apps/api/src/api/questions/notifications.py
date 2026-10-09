"""FR-FRM-005's "with client notification": e-mail the client when the firm asks (ADR-111).

When the FIRM writes in a thread, the client's users are told by e-mail. When the CLIENT writes,
nobody is e-mailed - the firm's inbox (GET /v1/firm/inbox) is where a reply lands, and an
accountant with 200 clients does not want 200 clients' replies in their mailbox as well.

--- Why a sweep, and not a send inside the request ---

"The client's users" are the people holding a live grant on the administration from the client's
own organization - most often the Owner, whose grant is ORGANIZATION-scoped at the client. A
firm's session cannot read the client organization's organization-scoped grants: 0009's RLS shows
an organization's grants only to that organization, and that is the property that keeps one
tenant's staff list out of another's hands. Reading them from the firm's request would need a
SECURITY DEFINER shortcut or a borrowed tenant context, and contract decision 1 rules out both.

So the request only records the message (durably, append-only), and this sweep - run as ledgr_ops
every few minutes, the posture ADR-090's reminder job takes - finds firm messages nobody on the
client side has been told about and mails them. ledgr_ops is granted only the columns this needs
(0080): never a message body.

--- Once ---

Per (firm message, client user), keyed on the LATEST firm message in a thread that awaits the
client: two quick messages from the accountant between sweeps are one e-mail. The
question_notification row is inserted inside a savepoint before the mail is handed over; a
provider refusal rolls it back and the next run retries; a second run sends nothing. A user who
has already opened the thread since that message is not mailed about it.

--- What the mail says ---

That there is a question, for which administration, and a link to the app's start page (there is
no client-side question screen yet, so the mail promises none). Not the subject and
not the body: either may name an amount or a counterparty, and the mail leaves the EU-hosted
product for whatever mailbox the person reads (FR-NTF-004's rule for push, applied to e-mail;
PRIV-012's plain text, no tracking).

Run:

    OPS_DATABASE_URL=postgresql+asyncpg://ledgr_ops:...@.../ledgr \\
        python -m api.questions.notifications [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.i18n.catalogue import translate
from api.i18n.language import Language, parse_language
from api.mail.sender import EmailMessage, EmailSender, UnsendableMessage

#: A firm message older than this is not mailed about on a first run (or after an outage): by
#: then the conversation has moved on, and the inbox and the client's own screen show it.
LOOKBACK = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class PendingQuestion:
    thread_id: uuid.UUID
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    administration_name: str
    message_id: uuid.UUID
    message_at: datetime


@dataclass(frozen=True, slots=True)
class ClientRecipient:
    user_id: uuid.UUID
    email: str
    language: Language
    read_at: datetime | None


@dataclass
class NotificationReport:
    sent: int = 0
    already_sent: int = 0
    already_read: int = 0
    failed: int = 0
    details: list[str] = field(default_factory=list)


class DeliveryRefused(Exception):
    pass


def build_message(
    *, recipient_email: str, language: Language, administration_name: str
) -> EmailMessage:
    base = settings.app_base_url.rstrip("/")
    product = settings.webauthn_rp_name
    body = "\n".join(
        [
            translate("reminders.greeting", language),
            "",
            translate(
                "reminders.question.body", language, name=administration_name, product=product
            ),
            "",
            # The app's start page: there is no client-side question screen yet (wave 2).
            f"{base}/",
            "",
            translate("reminders.question.why", language, product=product),
            "",
            translate("reminders.signoff", language),
            product,
        ]
    )
    return EmailMessage(
        to=recipient_email,
        subject=translate("reminders.question.subject", language, name=administration_name),
        body=body,
        from_address=settings.email_from_address,
        from_name=product,
    )


async def pending_questions(session: AsyncSession, *, now: datetime) -> list[PendingQuestion]:
    """Open threads awaiting the client, with their latest firm message - recent enough to mail."""
    result = await session.execute(
        text(
            """
            SELECT t.id AS thread_id, t.organization_id, t.administration_id,
                   COALESCE(a.trade_name, a.legal_name) AS name,
                   m.id AS message_id, m.created_at AS message_at
              FROM question_thread t
              JOIN administration a ON a.id = t.administration_id
              JOIN LATERAL (
                    SELECT qm.id, qm.created_at FROM question_message qm
                     WHERE qm.thread_id = t.id AND qm.author_side = 'firm'
                     ORDER BY qm.created_at DESC, qm.id DESC
                     LIMIT 1
              ) m ON true
             WHERE t.status = 'open'
               AND t.awaiting = 'client'
               AND m.created_at >= :since
            """
        ),
        {"since": now - LOOKBACK},
    )
    return [
        PendingQuestion(
            thread_id=row.thread_id,
            organization_id=row.organization_id,
            administration_id=row.administration_id,
            administration_name=row.name,
            message_id=row.message_id,
            message_at=row.message_at,
        )
        for row in result
    ]


async def client_recipients(
    session: AsyncSession, *, thread_id: uuid.UUID, administration_id: uuid.UUID
) -> list[ClientRecipient]:
    """The client side of an administration: a live grant from its OWN organization - an
    organization-scoped grant at the owner, or an administration-scoped grant the owner made
    (granted_by_organization_id, 0016). Firm staff grants are made by the firm and are excluded:
    the same split api.authz.service draws for IAM-100's profile cap."""
    result = await session.execute(
        text(
            """
            SELECT DISTINCT u.id AS user_id, u.email, u.language,
                   (SELECT max(r.read_at) FROM question_read r
                     WHERE r.thread_id = :thread AND r.user_id = u.id) AS read_at
              FROM administration a
              JOIN role_assignment ra
                ON ra.revoked_at IS NULL
               AND (ra.expires_at IS NULL OR ra.expires_at > now())
               AND ((ra.scope_type = 'organization' AND ra.scope_id = a.organization_id)
                 OR (ra.scope_type = 'administration' AND ra.scope_id = a.id
                     AND (ra.granted_by_organization_id IS NULL
                          OR ra.granted_by_organization_id = a.organization_id)))
              JOIN "role" ro ON ro.id = ra.role_id AND ro.archived_at IS NULL
              JOIN users u ON u.id = ra.user_id
             WHERE a.id = :admin
               AND u.status = 'active'
               AND u.email_verified_at IS NOT NULL
            """
        ),
        {"thread": str(thread_id), "admin": str(administration_id)},
    )
    return [
        ClientRecipient(
            user_id=row.user_id,
            email=row.email,
            language=parse_language(row.language) or Language.NL,
            read_at=row.read_at,
        )
        for row in result
    ]


async def run_question_notifications(
    session: AsyncSession,
    sender: EmailSender,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> NotificationReport:
    report = NotificationReport()
    now = now or datetime.now(UTC)
    for pending in await pending_questions(session, now=now):
        recipients = await client_recipients(
            session, thread_id=pending.thread_id, administration_id=pending.administration_id
        )
        for recipient in recipients:
            if recipient.read_at is not None and recipient.read_at >= pending.message_at:
                report.already_read += 1
                continue
            savepoint = await session.begin_nested()
            inserted = await session.execute(
                text(
                    "INSERT INTO question_notification (organization_id, administration_id, "
                    "  thread_id, message_id, user_id) "
                    "VALUES (:org, :admin, :thread, :message, :user) "
                    "ON CONFLICT DO NOTHING RETURNING id"
                ),
                {
                    "org": str(pending.organization_id),
                    "admin": str(pending.administration_id),
                    "thread": str(pending.thread_id),
                    "message": str(pending.message_id),
                    "user": str(recipient.user_id),
                },
            )
            if inserted.first() is None:
                await savepoint.rollback()
                report.already_sent += 1
                continue
            label = f"thread {pending.thread_id} -> {recipient.email}"
            if dry_run:
                await savepoint.rollback()
                report.sent += 1
                report.details.append(f"would send {label}")
                continue
            try:
                outcome = await sender.send(
                    build_message(
                        recipient_email=recipient.email,
                        language=recipient.language,
                        administration_name=pending.administration_name,
                    )
                )
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


async def _main() -> int:  # pragma: no cover - exercised by hand and by the cron service
    from api.db import get_ops_engine
    from api.mail.outbox import get_email_sender

    parser = argparse.ArgumentParser(description="E-mail clients about new questions (ADR-111).")
    parser.add_argument("--dry-run", action="store_true", help="report, send and write nothing")
    args = parser.parse_args()
    try:
        engine = get_ops_engine()
    except RuntimeError as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2
    async with engine.connect() as conn:
        session = AsyncSession(bind=conn)
        report = await run_question_notifications(session, get_email_sender(), dry_run=args.dry_run)
        await session.commit()
    await engine.dispose()
    for line in report.details:
        print(line)
    print(
        f"sent {report.sent}, already sent {report.already_sent}, already read "
        f"{report.already_read}, failed {report.failed}{' (dry run)' if args.dry_run else ''}"
    )
    return 1 if report.failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(asyncio.run(_main()))
