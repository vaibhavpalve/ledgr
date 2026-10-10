"""The receipt-chasing sweep: scheduled chases, manual requests, recipient counts (ADR-114).

    DATABASE_URL=postgresql+asyncpg://ledgr_app:...@.../ledgr \\
    OPS_DATABASE_URL=postgresql+asyncpg://ledgr_ops:...@.../ledgr \\
        python -m api.chasing.sweep [--dry-run]          (from apps/api, PYTHONPATH=src)

Run every 5 minutes. Two roles, the shape of `api.firm.proposals_job` (ADR-110) and the bank feed
sync (ADR-108):

1. **Discovery, as ledgr_ops** (BYPASSRLS, SELECT-only on the chasing tables): which clients
   are opted in, which manual requests are waiting, and which firm-served clients' recipient
   counts are missing or stale.
2. **Everything else, as ledgr_app in the client's OWN tenant context** (`app.current_org_id` =
   the administration's owner), one transaction per client. In that context RLS shows the
   client organization's grants, so the recipients can be resolved through the one
   authorization library; the send is recorded, and audited, under RLS like a request.

--- Who is mailed (the recipient rule) ---

The client's own people: a live grant from the administration's OWNING organization
(organization-scoped at the owner, or administration-scoped and granted by the owner - the split
api.questions.notifications draws), active, with a verified e-mail address and reminder e-mails
left on (`users.reminder_emails`, the person's own opt-out from 0069), for whom
`authorize()` allows BOTH `submit expense` (the receipt capture flow the link leads to) AND
`view bank_transaction` (the list itself). Firm staff are never mailed: the accountant is the one
asking.

--- Once ---

Per client, one transaction, under `pg_advisory_xact_lock` on the administration, so two
overlapping sweeps serialize and the second sees the first's row:

* nothing is sent when nothing is missing, or nobody qualifies;
* nothing is sent within 24 hours of the client's last chase, whatever its kind;
* a scheduled chase needs chasing enabled (the latest setting, re-read in the client's context),
  the send hours, and no chase yet in the current cadence window - a manual chase in the window
  counts. `chase_send_one_per_window_idx` makes "one scheduled send per window" a database fact;
* a manual request is sent once (`chase_send_one_per_request_idx`) and lapses after 24 hours.

The chase_send row and its audit entry are written only when at least one mail was accepted, with
`recipient_count` = mails accepted; a provider refusal of every mail leaves nothing, and the next
run retries.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.audit.repository import SqlAuditRepository
from api.authz.model import AdministrationScope, AuthorizationRequest
from api.authz.repository import SqlAuthorizationRepository
from api.authz.service import AuthorizationService
from api.chasing.mail import build_message
from api.chasing.missing import missing_count
from api.chasing.model import (
    MANUAL_GAP,
    REQUEST_LIFETIME,
    Cadence,
    SendKind,
    in_send_hours,
    window_key,
)
from api.i18n.language import Language, parse_language
from api.mail.sender import EmailSender, UnsendableMessage

#: A recipient count older than this is recounted (grants change; the preview shows the count).
RECOUNT_AFTER = timedelta(hours=1)
#: Recounts per run, so a first run over a large tenant base spreads over a few runs.
RECOUNT_BATCH = 500

#: What a recipient must be allowed, through authorize(), on the administration.
RECIPIENT_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("submit", "expense"),
    ("view", "bank_transaction"),
)


@dataclass(frozen=True, slots=True)
class PendingRequest:
    request_id: uuid.UUID
    requested_by_user_id: uuid.UUID


@dataclass
class Due:
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    request: PendingRequest | None = None
    scheduled: bool = False
    recount: bool = False


@dataclass(frozen=True, slots=True)
class Recipient:
    user_id: uuid.UUID
    email: str
    language: Language


@dataclass
class SweepReport:
    sent: int = 0
    mails: int = 0
    skipped: int = 0
    failed: int = 0
    recounted: int = 0
    details: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. Discovery (ledgr_ops)
# ---------------------------------------------------------------------------

_PENDING_REQUESTS = """
    SELECT DISTINCT ON (r.administration_id)
           r.id, r.organization_id, r.administration_id, r.requested_by_user_id
      FROM chase_request r
      JOIN administration a ON a.id = r.administration_id AND a.status = 'active'
     WHERE r.requested_at > :since
       AND (CAST(:only AS uuid[]) IS NULL OR r.administration_id = ANY(CAST(:only AS uuid[])))
       AND NOT EXISTS (SELECT 1 FROM chase_send s WHERE s.request_id = r.id)
     ORDER BY r.administration_id, r.requested_at DESC
"""

_ENABLED = """
    SELECT s.organization_id, s.administration_id
      FROM (SELECT DISTINCT ON (administration_id) organization_id, administration_id, enabled
              FROM chase_setting
             ORDER BY administration_id, created_at DESC, id DESC) s
      JOIN administration a ON a.id = s.administration_id AND a.status = 'active'
     WHERE s.enabled
       AND (CAST(:only AS uuid[]) IS NULL OR s.administration_id = ANY(CAST(:only AS uuid[])))
"""

# Only firm-served clients are ever previewed, so only theirs are counted.
_STALE_COUNTS = """
    SELECT a.organization_id, a.id AS administration_id
      FROM administration a
     WHERE a.status = 'active'
       AND (CAST(:only AS uuid[]) IS NULL OR a.id = ANY(CAST(:only AS uuid[])))
       AND EXISTS (SELECT 1 FROM firm_engagement f
                    WHERE f.administration_id = a.id AND f.status = 'active')
       AND NOT EXISTS (SELECT 1 FROM chase_recipient_count c
                        WHERE c.administration_id = a.id AND c.counted_at > :fresh_since)
     ORDER BY a.id
     LIMIT :limit
"""


async def discover(
    ops: AsyncSession, *, now: datetime, only: Sequence[uuid.UUID] | None = None
) -> list[Due]:
    """`only` narrows discovery to some administrations (tests share one database)."""
    narrow = [str(a) for a in only] if only is not None else None
    due: dict[uuid.UUID, Due] = {}

    def entry(organization_id: uuid.UUID, administration_id: uuid.UUID) -> Due:
        if administration_id not in due:
            due[administration_id] = Due(organization_id, administration_id)
        return due[administration_id]

    for row in await ops.execute(
        text(_PENDING_REQUESTS), {"since": now - REQUEST_LIFETIME, "only": narrow}
    ):
        entry(row.organization_id, row.administration_id).request = PendingRequest(
            request_id=row.id, requested_by_user_id=row.requested_by_user_id
        )
    if in_send_hours(now):
        for row in await ops.execute(text(_ENABLED), {"only": narrow}):
            entry(row.organization_id, row.administration_id).scheduled = True
    for row in await ops.execute(
        text(_STALE_COUNTS),
        {"fresh_since": now - RECOUNT_AFTER, "limit": RECOUNT_BATCH, "only": narrow},
    ):
        entry(row.organization_id, row.administration_id).recount = True
    return list(due.values())


# ---------------------------------------------------------------------------
# 2. Per client, in the client's own tenant context (ledgr_app)
# ---------------------------------------------------------------------------

# The client side of the administration: grants from its OWN organization (see the docstring).
_CANDIDATES = """
    SELECT DISTINCT u.id AS user_id, u.email, u.language
      FROM administration a
      JOIN role_assignment ra
        ON ra.revoked_at IS NULL
       AND (ra.expires_at IS NULL OR ra.expires_at > now())
       AND ((ra.scope_type = 'organization' AND ra.scope_id = a.organization_id)
         OR (ra.scope_type = 'administration' AND ra.scope_id = a.id
             AND (ra.granted_by_organization_id IS NULL
                  OR ra.granted_by_organization_id = a.organization_id)))
      JOIN users u ON u.id = ra.user_id
     WHERE a.id = :admin
       AND u.status = 'active'
       AND u.email_verified_at IS NOT NULL
       AND u.reminder_emails
     ORDER BY u.email
"""


async def recipients(session: AsyncSession, administration_id: uuid.UUID) -> list[Recipient]:
    """The recipient rule. Must run in the administration owner's tenant context."""
    authorization = AuthorizationService(SqlAuthorizationRepository(session))
    found: list[Recipient] = []
    for row in await session.execute(text(_CANDIDATES), {"admin": str(administration_id)}):
        allowed = True
        for action, resource_type in RECIPIENT_PERMISSIONS:
            decision = await authorization.authorize(
                AuthorizationRequest(
                    user_id=row.user_id,
                    action=action,
                    resource_type=resource_type,
                    target=AdministrationScope(administration_id),
                )
            )
            if not decision.allowed:
                allowed = False
                break
        if allowed:
            found.append(
                Recipient(
                    user_id=row.user_id,
                    email=str(row.email),
                    language=parse_language(row.language) or Language.NL,
                )
            )
    return found


async def _record_count(session: AsyncSession, due: Due, *, count: int, now: datetime) -> None:
    await session.execute(
        text(
            "INSERT INTO chase_recipient_count "
            "  (administration_id, organization_id, recipient_count, counted_at) "
            "VALUES (:admin, :org, :n, :at) "
            "ON CONFLICT (administration_id) DO UPDATE "
            "  SET recipient_count = EXCLUDED.recipient_count, counted_at = EXCLUDED.counted_at"
        ),
        {
            "admin": str(due.administration_id),
            "org": str(due.organization_id),
            "n": count,
            "at": now,
        },
    )


async def _cadence(session: AsyncSession, administration_id: uuid.UUID) -> tuple[bool, Cadence]:
    row = (
        await session.execute(
            text(
                "SELECT enabled, cadence FROM chase_setting WHERE administration_id = :admin "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"admin": str(administration_id)},
        )
    ).first()
    if row is None:
        return False, Cadence.WEEKLY
    return bool(row.enabled), Cadence(row.cadence)


async def _sent_since(session: AsyncSession, administration_id: uuid.UUID, since: datetime) -> bool:
    result = await session.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM chase_send "
            "WHERE administration_id = :admin AND sent_at > :since)"
        ),
        {"admin": str(administration_id), "since": since},
    )
    return bool(result.scalar_one())


async def _window_used(session: AsyncSession, administration_id: uuid.UUID, key: str) -> bool:
    result = await session.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM chase_send "
            "WHERE administration_id = :admin AND window_key = :key)"
        ),
        {"admin": str(administration_id), "key": key},
    )
    return bool(result.scalar_one())


async def _request_sent(session: AsyncSession, request_id: uuid.UUID) -> bool:
    result = await session.execute(
        text("SELECT EXISTS (SELECT 1 FROM chase_send WHERE request_id = :id)"),
        {"id": str(request_id)},
    )
    return bool(result.scalar_one())


async def _administration_name(session: AsyncSession, administration_id: uuid.UUID) -> str:
    result = await session.execute(
        text("SELECT COALESCE(trade_name, legal_name) FROM administration WHERE id = :admin"),
        {"admin": str(administration_id)},
    )
    return str(result.scalar_one())


class _NothingToCommit(Exception):
    """Raised inside a client's transaction to roll it back (dry run, every mail refused)."""


async def _chase(
    session: AsyncSession,
    sender: EmailSender,
    due: Due,
    *,
    now: datetime,
    dry_run: bool,
    report: SweepReport,
) -> None:
    admin = due.administration_id
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('chase:' || :admin))"), {"admin": str(admin)}
    )
    people = await recipients(session, admin)
    if due.recount or due.request is not None or due.scheduled:
        await _record_count(session, due, count=len(people), now=now)
        report.recounted += 1

    kind: SendKind | None = None
    enabled, cadence = await _cadence(session, admin)
    if due.request is not None and not await _request_sent(session, due.request.request_id):
        kind = SendKind.MANUAL
    elif due.scheduled and enabled:
        kind = SendKind.SCHEDULED
    if kind is None:
        return

    key = window_key(cadence, now)
    label = f"administration {admin} ({kind.value})"
    if await _sent_since(session, admin, now - MANUAL_GAP):
        report.skipped += 1
        report.details.append(f"skipped {label}: chased within 24 hours")
        return
    if kind is SendKind.SCHEDULED and await _window_used(session, admin, key):
        report.skipped += 1
        report.details.append(f"skipped {label}: already chased in {key}")
        return
    missing = await missing_count(session, admin)
    if missing <= 0:
        report.skipped += 1
        report.details.append(f"skipped {label}: nothing missing")
        return
    if not people:
        report.skipped += 1
        report.details.append(f"skipped {label}: no recipient")
        return

    if dry_run:
        report.sent += 1
        report.mails += len(people)
        report.details.append(f"would send {label}: {missing} missing, {len(people)} recipients")
        raise _NothingToCommit

    name = await _administration_name(session, admin)
    accepted = 0
    for person in people:
        try:
            outcome = await sender.send(
                build_message(
                    recipient_email=person.email,
                    language=person.language,
                    administration_id=admin,
                    administration_name=name,
                    missing_count=missing,
                )
            )
        except (UnsendableMessage, OSError) as exc:
            report.details.append(f"failed {label} -> {person.email}: {exc}")
            continue
        if outcome.accepted:
            accepted += 1
        else:
            report.details.append(
                f"failed {label} -> {person.email}: {outcome.detail or outcome.provider}"
            )
    if accepted == 0:
        report.failed += 1
        raise _NothingToCommit

    request = due.request if kind is SendKind.MANUAL else None
    send_id = (
        await session.execute(
            text(
                "INSERT INTO chase_send (organization_id, administration_id, kind, missing_count, "
                "  recipient_count, requested_by_user_id, request_id, window_key, sent_at) "
                "VALUES (:org, :admin, :kind, :missing, :recipients, :by, :request, :key, :at) "
                "RETURNING id"
            ),
            {
                "org": str(due.organization_id),
                "admin": str(admin),
                "kind": kind.value,
                "missing": missing,
                "recipients": accepted,
                "by": str(request.requested_by_user_id) if request else None,
                "request": str(request.request_id) if request else None,
                "key": key,
                "at": now,
            },
        )
    ).scalar_one()
    detail: dict[str, object] = {
        "kind": kind.value,
        "missing_count": missing,
        "recipient_count": accepted,
        "window_key": key,
    }
    if request is not None:
        detail["request_id"] = str(request.request_id)
        detail["requested_by_user_id"] = str(request.requested_by_user_id)
    await AuditLog(SqlAuditRepository(session)).record(
        AuditEvent(
            organization_id=due.organization_id,
            administration_id=admin,
            category=AuditCategory.CONFIGURATION,
            action="send_receipt_chase",
            resource_type="receipt_chase",
            resource_id=send_id,
            outcome=AuditOutcome.SUCCESS,
            actor_type=ActorType.SYSTEM,
            occurred_at=now,
            detail=detail,
        )
    )
    report.sent += 1
    report.mails += accepted
    report.details.append(f"sent {label}: {missing} missing, {accepted} recipients")


async def run_chase_sweep(
    *,
    ops_session: AsyncSession,
    app_engine: AsyncEngine,
    sender: EmailSender,
    now: datetime | None = None,
    dry_run: bool = False,
    only: Sequence[uuid.UUID] | None = None,
) -> SweepReport:
    now = now or datetime.now(UTC)
    report = SweepReport()
    sessions = async_sessionmaker(app_engine, expire_on_commit=False)
    for due in await discover(ops_session, now=now, only=only):
        try:
            async with sessions() as session, session.begin():
                # The request path's own tenant scoping (api.db.get_db_session), as the OWNER.
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :org, true)"),
                    {"org": str(due.organization_id)},
                )
                await _chase(session, sender, due, now=now, dry_run=dry_run, report=report)
                if dry_run:  # not even the recount
                    raise _NothingToCommit
        except _NothingToCommit:
            continue
        except Exception as exc:  # one client's failure must not stop the others
            report.failed += 1
            report.details.append(
                f"failed: administration {due.administration_id}: {type(exc).__name__}"
            )
    return report


async def _main() -> int:  # pragma: no cover - exercised by hand and by the cron service
    from api.db import engine, get_ops_engine
    from api.mail.outbox import get_email_sender

    parser = argparse.ArgumentParser(description="Chase clients for missing receipts (ADR-114).")
    parser.add_argument("--dry-run", action="store_true", help="report, send and write nothing")
    args = parser.parse_args()
    try:
        ops = get_ops_engine()
    except RuntimeError as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2
    async with ops.connect() as conn:
        ops_session = AsyncSession(bind=conn)
        report = await run_chase_sweep(
            ops_session=ops_session,
            app_engine=engine,
            sender=get_email_sender(),
            dry_run=args.dry_run,
        )
        await ops_session.rollback()
    await ops.dispose()
    await engine.dispose()
    for line in report.details:
        print(line)
    print(
        f"sent {report.sent} chases ({report.mails} mails), skipped {report.skipped}, "
        f"recounted {report.recounted}, failed {report.failed}"
        f"{' (dry run)' if args.dry_run else ''}"
    )
    return 1 if report.failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(asyncio.run(_main()))
