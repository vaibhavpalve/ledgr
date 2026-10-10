"""The firm home's reads and its two writes (ADR-109, migration 0078).

Every read here takes the WHOLE portfolio as one `uuid[]` and answers for all of it at once -
GROUP BY administration_id, DISTINCT ON, LATERAL - so the number of queries a page costs is fixed
(about eight) whatever the number of clients. A per-client loop of queries is the one shape this
module must never take: 200 clients would be 1,600 round trips.

The id list is always the caller's authorized portfolio (`api.firm.worklist_access`), and every
table read is under RLS in the request's tenant session, so a stray id could only ever read rows
the firm is engaged on - the predicate narrows, it never widens.

Reads `booking_proposal` (0079) and `question_thread` / `question_message` (0080) by the
contract's column names (docs/firm-home/contract.md), and `booking_rule` /
`booking_proposal.rule_id` (0082) and `chase_send` (0083) by contract-wave2.md's.

`users` carries no RLS (users are global, 0003): every statement on it names the verified
token's own user id, or reads only the users of the caller's organization for the staff list.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.chasing.missing import NO_CANDIDATE_RECEIPT, PENDING_HIGH_PROPOSAL
from api.firm.switcher import SwitcherEntry
from api.firm.worklist_model import (
    ActivityKind,
    BrokenFeed,
    ClientFacts,
    VatPeriodFacts,
)

# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------

# Contract decision 5, per unmatched line: a pending HIGH proposal, else (for money out) whether
# any receipt of exactly that amount exists that no other line has settled. The two predicates are
# `api.chasing.missing`'s - the one definition shared with the client's list and the chase e-mail.
_BANK_BUCKETS = f"""
    SELECT t.administration_id,
           count(*) FILTER (WHERE x.high)                          AS auto_bookings,
           count(*) FILTER (WHERE NOT x.high AND x.no_candidate)   AS missing_receipts,
           count(*) FILTER (WHERE NOT x.high AND NOT x.no_candidate) AS bank_to_match,
           min(t.booking_date) FILTER (WHERE NOT x.high AND x.no_candidate) AS oldest_missing,
           min(t.booking_date)                                     AS oldest_unmatched
      FROM bank_transaction t
      CROSS JOIN LATERAL (
          SELECT {PENDING_HIGH_PROPOSAL} AS high,
                 {NO_CANDIDATE_RECEIPT} AS no_candidate
      ) x
     WHERE t.administration_id = ANY(CAST(:ids AS uuid[]))
       AND t.status = 'unmatched'
     GROUP BY t.administration_id
"""

# The newest line per bank account through bank_transaction_account_idx (bank_account_id,
# booking_date desc): one index probe per account rather than a scan of seven years of lines.
_LAST_TRANSACTION = """
    SELECT ba.administration_id, max(l.booking_date) AS last_date
      FROM bank_account ba
      CROSS JOIN LATERAL (
          SELECT t.booking_date FROM bank_transaction t
           WHERE t.bank_account_id = ba.id
           ORDER BY t.booking_date DESC
           LIMIT 1
      ) l
     WHERE ba.administration_id = ANY(CAST(:ids AS uuid[]))
     GROUP BY ba.administration_id
"""

_TO_BOOK = """
    SELECT e.administration_id, count(*) AS to_book
      FROM expense e
      JOIN capture_item ci ON ci.id = e.capture_item_id
     WHERE e.administration_id = ANY(CAST(:ids AS uuid[]))
       AND e.status IN ('draft', 'ready')
       AND ci.discarded_at IS NULL
       AND NOT EXISTS (SELECT 1 FROM booking_proposal p
                        WHERE p.document_kind = 'expense' AND p.document_id = e.id
                          AND p.status = 'pending')
     GROUP BY e.administration_id
"""

_QUESTIONS = """
    SELECT administration_id,
           count(*)                                          AS open_questions,
           count(*) FILTER (WHERE awaiting = 'client')       AS awaiting_client,
           count(*) FILTER (WHERE awaiting = 'firm')         AS awaiting_firm,
           min(last_message_at) FILTER (WHERE awaiting = 'client') AS oldest_awaiting_client
      FROM question_thread
     WHERE administration_id = ANY(CAST(:ids AS uuid[]))
       AND status = 'open'
     GROUP BY administration_id
"""

# The CURRENT connection per active bank account (rows are never deleted - an expired consent
# followed by a new one leaves both). A linked consent past its end date is expired whether or
# not the daily sync has noticed yet.
_FEEDS = """
    WITH latest AS (
        SELECT DISTINCT ON (f.bank_account_id)
               f.administration_id, f.last_synced_at, f.updated_at, f.consent_expires_at,
               coalesce(f.institution_name, ba.name) AS bank_name,
               CASE
                   WHEN f.status = 'linked' AND f.consent_expires_at IS NOT NULL
                        AND f.consent_expires_at <= now() THEN 'expired'
                   ELSE f.status
               END AS effective
          FROM bank_feed_connection f
          JOIN bank_account ba ON ba.id = f.bank_account_id
         WHERE f.administration_id = ANY(CAST(:ids AS uuid[]))
           AND ba.status = 'active'
         ORDER BY f.bank_account_id, f.created_at DESC
    )
    SELECT administration_id, effective, bank_name, last_synced_at,
           CASE WHEN effective = 'expired' AND consent_expires_at IS NOT NULL
                THEN least(consent_expires_at, updated_at) ELSE updated_at END AS broken_since
      FROM latest
     WHERE effective IN ('linked', 'expired', 'failed')
"""

_ASSIGNMENTS = """
    SELECT DISTINCT ON (ca.administration_id)
           ca.administration_id, ca.assigned_user_id, u.email AS assigned_email
      FROM client_assignment ca
      LEFT JOIN users u ON u.id = ca.assigned_user_id
     WHERE ca.administration_id = ANY(CAST(:ids AS uuid[]))
     ORDER BY ca.administration_id, ca.created_at DESC, ca.id DESC
"""

_SNOOZES = """
    SELECT DISTINCT ON (s.administration_id)
           s.administration_id, s.snoozed_until, s.reason
      FROM client_snooze s
     WHERE s.administration_id = ANY(CAST(:ids AS uuid[]))
     ORDER BY s.administration_id, s.created_at DESC, s.id DESC
"""

# Receipt chasing (0083, contract-wave2): the latest send per client. chase_send is append-only.
_LAST_CHASED = """
    SELECT administration_id, max(sent_at) AS last_chased_at
      FROM chase_send
     WHERE administration_id = ANY(CAST(:ids AS uuid[]))
     GROUP BY administration_id
"""

# Approval rules (0082, contract-wave2): active ones only - suspended and retired rules approve
# nothing.
_RULES = """
    SELECT administration_id, count(*) AS rules_count
      FROM booking_rule
     WHERE administration_id = ANY(CAST(:ids AS uuid[]))
       AND status = 'active'
     GROUP BY administration_id
"""

# A VAT period's facts - shared by the queue (one period per client) and the deadlines view
# (every period due in a range). `filed` reads both the stored return and the period lock, so a
# return filed before 0068 existed still counts.
_PERIOD_FACTS = """
    EXISTS (SELECT 1 FROM journal_entry je WHERE je.period_id = pp.id) AS has_postings,
    EXISTS (SELECT 1 FROM bank_transaction b
             WHERE b.administration_id = pp.administration_id AND b.status = 'unmatched'
               AND b.booking_date BETWEEN pp.start_date AND pp.end_date) AS unmatched_in_period,
    EXISTS (SELECT 1 FROM expense x
             WHERE x.administration_id = pp.administration_id
               AND x.status IN ('draft', 'ready')
               AND (x.expense_date IS NULL
                    OR x.expense_date BETWEEN pp.start_date AND pp.end_date)
               AND NOT EXISTS (SELECT 1 FROM capture_item xci
                                WHERE xci.id = x.capture_item_id
                                  AND xci.discarded_at IS NOT NULL)) AS unposted_in_period
"""

_PERIODS = """
    SELECT p.id, p.administration_id, p.start_date, p.end_date, p.status, fy.period_scheme,
           (p.status = 'vat_filed'
            OR EXISTS (SELECT 1 FROM vat_return v WHERE v.period_id = p.id)) AS filed
      FROM period p
      JOIN fiscal_year fy ON fy.id = p.fiscal_year_id
     WHERE p.administration_id = ANY(CAST(:ids AS uuid[]))
"""

# One period per client - "the next unfiled deadline":
#   0  the earliest ENDED, unfiled period after the last filed one; with nothing filed in
#      Boeklite yet, the most recent ended one (a client onboarded in September filed January
#      somewhere else, and must not show as nine months overdue);
#   1  else the period running today;
#   2  else the most recently filed one, shown as filed.
_VAT_TARGET = f"""
    WITH p AS ({_PERIODS}),
    lf AS (
        SELECT administration_id, max(end_date) AS last_filed_end FROM p WHERE filed
         GROUP BY administration_id
    ),
    ranked AS (
        SELECT p.*,
               CASE
                   WHEN NOT p.filed AND p.end_date < CAST(:today AS date)
                        AND (lf.last_filed_end IS NULL OR p.end_date > lf.last_filed_end) THEN 0
                   WHEN NOT p.filed AND p.start_date <= CAST(:today AS date)
                        AND p.end_date >= CAST(:today AS date) THEN 1
                   WHEN p.filed THEN 2
                   ELSE 3
               END AS priority,
               CASE
                   WHEN lf.last_filed_end IS NULL THEN CAST(:today AS date) - p.end_date
                   ELSE p.end_date - CAST(:today AS date)
               END AS nearest_first
          FROM p LEFT JOIN lf USING (administration_id)
    ),
    target AS (
        SELECT DISTINCT ON (administration_id) *
          FROM ranked
         WHERE priority < 3
         ORDER BY administration_id, priority,
                  CASE priority
                      WHEN 0 THEN nearest_first
                      WHEN 2 THEN CAST(:today AS date) - end_date
                      ELSE 0
                  END
    )
    SELECT pp.*, {_PERIOD_FACTS}
      FROM target pp
"""

_DEADLINE_PERIODS = f"""
    WITH pp AS (
        SELECT * FROM ({_PERIODS}) p
         WHERE (date_trunc('month', p.end_date) + interval '2 months' - interval '1 day')::date
               BETWEEN CAST(:from_date AS date) AND CAST(:to_date AS date)
    )
    SELECT pp.*, {_PERIOD_FACTS}
      FROM pp
"""

# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------

# One row per (kind, administration) with anything since `since`. possible_duplicates is the
# EXACT half of FR-EXP-001g (same normalised supplier, date and amount, through
# expense_duplicate_exact_idx); the trigram "probable" half is per receipt and stays on the
# receipt's own screen.
_ACTIVITY = """
    SELECT 'receipts_uploaded' AS kind, e.administration_id, count(*) AS n
      FROM expense e
     WHERE e.administration_id = ANY(CAST(:ids AS uuid[])) AND e.created_at > :since
       AND NOT EXISTS (SELECT 1 FROM capture_item eci
                        WHERE eci.id = e.capture_item_id AND eci.discarded_at IS NOT NULL)
     GROUP BY e.administration_id
    UNION ALL
    SELECT 'auto_bookings_ready', p.administration_id, count(*)
      FROM booking_proposal p
     WHERE p.administration_id = ANY(CAST(:ids AS uuid[])) AND p.status = 'pending'
       AND p.confidence = 'high' AND p.created_at > :since
     GROUP BY p.administration_id
    UNION ALL
    SELECT 'client_replies', m.administration_id, count(*)
      FROM question_message m
     WHERE m.administration_id = ANY(CAST(:ids AS uuid[])) AND m.author_side = 'client'
       AND m.created_at > :since
     GROUP BY m.administration_id
    UNION ALL
    SELECT 'bank_feeds_broken', f.administration_id, count(*)
      FROM bank_feed_connection f
     WHERE f.administration_id = ANY(CAST(:ids AS uuid[]))
       AND f.status IN ('expired', 'failed') AND f.updated_at > :since
       AND NOT EXISTS (SELECT 1 FROM bank_feed_connection newer
                        WHERE newer.bank_account_id = f.bank_account_id
                          AND newer.created_at > f.created_at)
     GROUP BY f.administration_id
    UNION ALL
    SELECT 'possible_duplicates', e.administration_id, count(*)
      FROM expense e
     WHERE e.administration_id = ANY(CAST(:ids AS uuid[])) AND e.created_at > :since
       AND e.supplier IS NOT NULL AND e.expense_date IS NOT NULL AND e.gross_amount IS NOT NULL
       AND expenses.normalise_supplier(e.supplier) <> ''
       AND NOT EXISTS (SELECT 1 FROM capture_item eci
                        WHERE eci.id = e.capture_item_id AND eci.discarded_at IS NOT NULL)
       AND EXISTS (
           SELECT 1 FROM expense o
            WHERE o.administration_id = e.administration_id AND o.id <> e.id
              AND NOT EXISTS (SELECT 1 FROM capture_item oci
                               WHERE oci.id = o.capture_item_id AND oci.discarded_at IS NOT NULL)
              AND o.supplier IS NOT NULL AND o.expense_date IS NOT NULL
              AND o.gross_amount IS NOT NULL
              AND expenses.normalise_supplier(o.supplier) = expenses.normalise_supplier(e.supplier)
              AND o.expense_date = e.expense_date
              AND o.gross_amount = e.gross_amount
       )
     GROUP BY e.administration_id
    UNION ALL
    SELECT 'rule_postings', p.administration_id, count(*)
      FROM booking_proposal p
     WHERE p.administration_id = ANY(CAST(:ids AS uuid[])) AND p.rule_id IS NOT NULL
       AND p.status = 'approved' AND p.decided_at > :since
     GROUP BY p.administration_id
"""

# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------

# "Users of the caller's organization": anyone holding a live grant AT the organization, or one
# the organization made (IAM-107's firm staff grants on clients carry the firm as
# granted_by_organization_id, 0016). A client's own users are neither.
_STAFF = """
    SELECT DISTINCT u.id, u.email
      FROM role_assignment ra
      JOIN users u ON u.id = ra.user_id
     WHERE ra.revoked_at IS NULL
       AND (ra.expires_at IS NULL OR ra.expires_at > now())
       AND ((ra.scope_type = 'organization' AND ra.scope_id = :org)
            OR ra.granted_by_organization_id = :org)
       AND u.status = 'active'
     ORDER BY u.email
"""


def name_from_email(email: str) -> str:
    """`users` has no name column (0003); the address's local part is the closest thing to one
    a person will recognise in a dropdown."""
    local, _, _ = email.partition("@")
    return local or email


@dataclass(frozen=True, slots=True)
class StaffMember:
    user_id: uuid.UUID
    email: str

    @property
    def name(self) -> str:
        return name_from_email(self.email)


@dataclass(frozen=True, slots=True)
class LoginWindow:
    previous_login_at: datetime | None
    firm_activity_seen_at: datetime | None


def _ids(ids: Sequence[uuid.UUID]) -> list[str]:
    return [str(i) for i in ids]


def _period(row: Any) -> VatPeriodFacts:
    return VatPeriodFacts(
        administration_id=row.administration_id,
        period_id=row.id,
        start_date=row.start_date,
        end_date=row.end_date,
        period_status=row.status,
        period_scheme=row.period_scheme,
        filed=bool(row.filed),
        has_postings=bool(row.has_postings),
        unmatched_in_period=bool(row.unmatched_in_period),
        unposted_in_period=bool(row.unposted_in_period),
    )


def _as_date(value: datetime | date | None) -> date | None:
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


class SqlWorklistRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- the queue ------------------------------------------------------------

    async def client_facts(
        self, entries: Sequence[SwitcherEntry], *, today: date
    ) -> list[ClientFacts]:
        """Every fact the queue needs, for the whole portfolio, in ten queries."""
        if not entries:
            return []
        ids = [e.badge.administration_id for e in entries]
        params = {"ids": _ids(ids)}
        facts: dict[uuid.UUID, ClientFacts] = {
            e.badge.administration_id: ClientFacts(
                administration_id=e.badge.administration_id,
                display_name=e.badge.display_name,
                legal_name=e.badge.name,
                kvk_number=e.badge.kvk_number,
                initials=e.badge.initials,
                colour=e.badge.colour_token,
            )
            for e in entries
        }

        def update(administration_id: uuid.UUID, **changes: Any) -> None:
            current = facts.get(administration_id)
            if current is not None:
                facts[administration_id] = replace(current, **changes)

        for row in await self._session.execute(text(_BANK_BUCKETS), params):
            update(
                row.administration_id,
                auto_bookings=int(row.auto_bookings),
                missing_receipts=int(row.missing_receipts),
                bank_to_match=int(row.bank_to_match),
                oldest_missing_receipt=row.oldest_missing,
                oldest_unmatched=row.oldest_unmatched,
            )
        for row in await self._session.execute(text(_LAST_TRANSACTION), params):
            update(row.administration_id, last_transaction=row.last_date)
        for row in await self._session.execute(text(_TO_BOOK), params):
            update(row.administration_id, to_book=int(row.to_book))
        for row in await self._session.execute(text(_QUESTIONS), params):
            update(
                row.administration_id,
                open_questions=int(row.open_questions),
                questions_awaiting_client=int(row.awaiting_client),
                questions_awaiting_firm=int(row.awaiting_firm),
                oldest_awaiting_client=_as_date(row.oldest_awaiting_client),
            )

        synced: dict[uuid.UUID, datetime] = {}
        broken: dict[uuid.UUID, tuple[datetime, BrokenFeed]] = {}
        for row in await self._session.execute(text(_FEEDS), params):
            if row.last_synced_at is not None:
                prior = synced.get(row.administration_id)
                if prior is None or row.last_synced_at < prior:
                    synced[row.administration_id] = row.last_synced_at
            if row.effective in ("expired", "failed"):
                since = row.broken_since
                current = broken.get(row.administration_id)
                if current is None or since < current[0]:
                    broken[row.administration_id] = (
                        since,
                        BrokenFeed(bank_name=row.bank_name, status=row.effective),
                    )
        for administration_id, at in synced.items():
            update(administration_id, feed_synced_until=at)
        for administration_id, (since, feed) in broken.items():
            update(administration_id, broken_feed=feed, broken_feed_since=_as_date(since))

        for row in await self._session.execute(text(_ASSIGNMENTS), params):
            update(
                row.administration_id,
                assigned_user_id=row.assigned_user_id,
                assigned_name=(
                    name_from_email(row.assigned_email) if row.assigned_email is not None else None
                ),
            )
        for row in await self._session.execute(text(_SNOOZES), params):
            update(row.administration_id, snoozed_until=row.snoozed_until, snooze_reason=row.reason)
        for row in await self._session.execute(text(_LAST_CHASED), params):
            update(row.administration_id, last_chased_at=row.last_chased_at)
        for row in await self._session.execute(text(_RULES), params):
            update(row.administration_id, rules_count=int(row.rules_count))

        vat_ids = [e.badge.administration_id for e in entries if e.vat_registered]
        if vat_ids:
            result = await self._session.execute(
                text(_VAT_TARGET), {"ids": _ids(vat_ids), "today": today}
            )
            for row in result:
                update(row.administration_id, vat=_period(row))

        return [facts[e.badge.administration_id] for e in entries]

    async def deadline_periods(
        self, entries: Sequence[SwitcherEntry], *, from_date: date, to_date: date
    ) -> list[VatPeriodFacts]:
        vat_ids = [e.badge.administration_id for e in entries if e.vat_registered]
        if not vat_ids:
            return []
        result = await self._session.execute(
            text(_DEADLINE_PERIODS),
            {"ids": _ids(vat_ids), "from_date": from_date, "to_date": to_date},
        )
        return [_period(row) for row in result]

    # -- the summary ------------------------------------------------------------

    async def activity(
        self, administration_ids: Sequence[uuid.UUID], *, since: datetime
    ) -> list[tuple[ActivityKind, uuid.UUID, int]]:
        if not administration_ids:
            return []
        result = await self._session.execute(
            text(_ACTIVITY), {"ids": _ids(administration_ids), "since": since}
        )
        return [(ActivityKind(row.kind), row.administration_id, int(row.n)) for row in result]

    async def broken_feed_count(self, administration_ids: Sequence[uuid.UUID]) -> int:
        if not administration_ids:
            return 0
        result = await self._session.execute(
            text(f"SELECT count(*) FROM ({_FEEDS}) f WHERE f.effective IN ('expired', 'failed')"),
            {"ids": _ids(administration_ids)},
        )
        return int(result.scalar_one())

    async def login_window(self, user_id: uuid.UUID) -> LoginWindow:
        result = await self._session.execute(
            text("SELECT previous_login_at, firm_activity_seen_at FROM users WHERE id = :id"),
            {"id": str(user_id)},
        )
        row = result.first()
        if row is None:
            return LoginWindow(previous_login_at=None, firm_activity_seen_at=None)
        return LoginWindow(
            previous_login_at=row.previous_login_at,
            firm_activity_seen_at=row.firm_activity_seen_at,
        )

    async def mark_activity_seen(self, user_id: uuid.UUID, *, at: datetime) -> None:
        await self._session.execute(
            text("UPDATE users SET firm_activity_seen_at = :at WHERE id = :id"),
            {"id": str(user_id), "at": at},
        )

    # -- assignment and snooze ------------------------------------------------------

    async def staff(self, organization_id: uuid.UUID) -> list[StaffMember]:
        result = await self._session.execute(text(_STAFF), {"org": str(organization_id)})
        return [StaffMember(user_id=row.id, email=str(row.email)) for row in result]

    async def visible_active_administrations(
        self, administration_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Under RLS: the firm's engagements (or the business's own administrations)."""
        if not administration_ids:
            return set()
        result = await self._session.execute(
            text(
                "SELECT id FROM administration "
                "WHERE id = ANY(CAST(:ids AS uuid[])) AND status = 'active'"
            ),
            {"ids": _ids(administration_ids)},
        )
        return {row.id for row in result}

    async def record_assignments(
        self,
        administration_ids: Sequence[uuid.UUID],
        *,
        assigned_user_id: uuid.UUID | None,
        assigned_by_user_id: uuid.UUID,
    ) -> int:
        """One INSERT for the whole batch. organization_id is the administration's own, read
        in the same statement, like every other insert into a tenant table here."""
        if not administration_ids:
            return 0
        result = await self._session.execute(
            text(
                "INSERT INTO client_assignment "
                "  (organization_id, administration_id, assigned_user_id, assigned_by_user_id) "
                "SELECT a.organization_id, a.id, CAST(:assignee AS uuid), CAST(:by AS uuid) "
                "  FROM administration a WHERE a.id = ANY(CAST(:ids AS uuid[])) "
                "RETURNING id"
            ),
            {
                "ids": _ids(administration_ids),
                "assignee": str(assigned_user_id) if assigned_user_id is not None else None,
                "by": str(assigned_by_user_id),
            },
        )
        return len(result.all())

    async def record_snooze(
        self,
        administration_id: uuid.UUID,
        *,
        until: date | None,
        reason: str | None,
        user_id: uuid.UUID,
    ) -> bool:
        result = await self._session.execute(
            text(
                "INSERT INTO client_snooze "
                "  (organization_id, administration_id, snoozed_until, reason, created_by_user_id) "
                "SELECT a.organization_id, a.id, CAST(:until AS date), CAST(:reason AS text), "
                "       CAST(:user AS uuid) "
                "  FROM administration a WHERE a.id = :admin "
                "RETURNING id"
            ),
            {
                "admin": str(administration_id),
                "until": until,
                "reason": reason,
                "user": str(user_id),
            },
        )
        return result.first() is not None
