"""SQL behind `CaptureRepository` - FR-EXP-001, FR-EXP-001a.

Every statement runs on the request's own tenant-scoped session, so RLS
(ADR-003) is what confines it. The `administration_id` predicates here narrow
WITHIN the tenant; they are not the tenant boundary, which is the division
`api.main.list_administrations` documents.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.expenses.duplicates import (
    SIMILARITY_THRESHOLD,
    DuplicateStrength,
    DuplicateWarning,
    ExpenseTriple,
)
from api.expenses.model import (
    CaptureSession,
    Expense,
    ExpenseStatus,
    PaymentMethod,
    ReviewEntry,
    VatTreatment,
)

_EXPENSE_COLUMNS = """
    id, administration_id, capture_item_id, status, submitted_by_user_id,
    expense_date, supplier, gross_amount, vat_treatment, vat_rate, vat_amount,
    net_amount, category, payment_method, journal_entry_id, posted_at
"""

#: Columns the form may write. Named as a frozenset rather than interpolating
#: whatever a caller passes, so a `changes` dict that picked up a key from a
#: request body cannot reach the UPDATE - `status` and `net_amount` most of
#: all, the first being the release gate and the second being generated.
_WRITABLE_FIELDS = frozenset(
    {
        "expense_date",
        "supplier",
        "gross_amount",
        "vat_treatment",
        "vat_rate",
        "vat_amount",
        "category",
        "payment_method",
    }
)


def _to_expense(row: object) -> Expense:
    method = row.payment_method  # type: ignore[attr-defined]
    treatment = row.vat_treatment  # type: ignore[attr-defined]
    return Expense(
        id=row.id,  # type: ignore[attr-defined]
        administration_id=row.administration_id,  # type: ignore[attr-defined]
        capture_item_id=row.capture_item_id,  # type: ignore[attr-defined]
        status=ExpenseStatus(row.status),  # type: ignore[attr-defined]
        submitted_by_user_id=row.submitted_by_user_id,  # type: ignore[attr-defined]
        expense_date=row.expense_date,  # type: ignore[attr-defined]
        supplier=row.supplier,  # type: ignore[attr-defined]
        # asyncpg returns numeric as Decimal, which is the point (NFR-031).
        gross_amount=row.gross_amount,  # type: ignore[attr-defined]
        vat_treatment=VatTreatment(treatment) if treatment else None,
        vat_rate=row.vat_rate,  # type: ignore[attr-defined]
        vat_amount=row.vat_amount,  # type: ignore[attr-defined]
        net_amount=row.net_amount,  # type: ignore[attr-defined]
        category=row.category,  # type: ignore[attr-defined]
        payment_method=PaymentMethod(method) if method else None,
        journal_entry_id=row.journal_entry_id,  # type: ignore[attr-defined]
        posted_at=row.posted_at,  # type: ignore[attr-defined]
    )


def _to_session(row: object) -> CaptureSession:
    return CaptureSession(
        id=row.id,  # type: ignore[attr-defined]
        administration_id=row.administration_id,  # type: ignore[attr-defined]
        opened_by_user_id=row.opened_by_user_id,  # type: ignore[attr-defined]
        opened_at=row.opened_at,  # type: ignore[attr-defined]
        finalised_at=row.finalised_at,  # type: ignore[attr-defined]
    )


class SqlCaptureRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def open_session(
        self, *, organization_id: uuid.UUID, administration_id: uuid.UUID, user_id: uuid.UUID
    ) -> CaptureSession:
        result = await self._session.execute(
            text(
                """
                INSERT INTO capture_session (
                    organization_id, administration_id, opened_by_user_id
                ) VALUES (:org, :admin, :user)
                RETURNING id, administration_id, opened_by_user_id, opened_at, finalised_at
                """
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "user": str(user_id),
            },
        )
        return _to_session(result.one())

    async def get_session(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID
    ) -> CaptureSession | None:
        result = await self._session.execute(
            text(
                "SELECT id, administration_id, opened_by_user_id, opened_at, finalised_at "
                "FROM capture_session "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(session_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _to_session(row)

    async def review_list(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID
    ) -> Sequence[ReviewEntry]:
        """FR-EXP-001a's list, from the SQL function that derives it.

        Derived in one query rather than assembled per item: a review screen
        for a shoebox is a list, and N+1 lookups over pages and expenses would
        make the batch case the slow one.
        """
        result = await self._session.execute(
            text(
                "SELECT item_id, position, expense_id, page_count, content_types, "
                "       total_bytes, discarded, duplicate_of "
                "FROM expenses.review_list(:session)"
            ),
            {"session": str(session_id)},
        )
        return [
            ReviewEntry(
                item_id=row.item_id,
                position=row.position,
                expense_id=row.expense_id,
                page_count=row.page_count,
                content_types=tuple(row.content_types or ()),
                total_bytes=row.total_bytes,
                discarded=row.discarded,
                duplicate_of=row.duplicate_of,
            )
            for row in result
        ]

    async def add_item(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> tuple[uuid.UUID, Expense]:
        """A receipt and its expense, in one statement.

        FR-EXP-001a's "each becoming a separate expense" must have no window in
        which an item exists without one: an item with no expense is a receipt
        that silently never became a claim.

        `position` is allocated as `max + 1` and guarded by the UNIQUE index in
        0032. Two captures racing within one session lose one to a unique
        violation, which the client's idempotency key (NFR-032) makes safe to
        retry - preferable to a sequence, which would leave gaps in a list a
        person is checking against a pile of paper.
        """
        result = await self._session.execute(
            text(
                """
                WITH new_item AS (
                    INSERT INTO capture_item (
                        organization_id, administration_id, session_id, position
                    )
                    SELECT :org, :admin, :session,
                           coalesce(max(position), 0) + 1
                      FROM capture_item WHERE session_id = :session
                    RETURNING id, administration_id
                )
                INSERT INTO expense (
                    organization_id, administration_id, capture_item_id,
                    submitted_by_user_id
                )
                SELECT :org, new_item.administration_id, new_item.id, :user
                  FROM new_item
                RETURNING """
                + _EXPENSE_COLUMNS
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "session": str(session_id),
                "user": str(user_id),
            },
        )
        expense = _to_expense(result.one())
        return expense.capture_item_id, expense

    async def add_page(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        item_id: uuid.UUID,
        document_id: uuid.UUID,
        source: str,
    ) -> int:
        result = await self._session.execute(
            text(
                """
                INSERT INTO capture_page (
                    organization_id, administration_id, item_id, document_id,
                    page_number, source
                )
                SELECT :org, :admin, :item, :document,
                       coalesce(max(page_number), 0) + 1, :source
                  FROM capture_page WHERE item_id = :item
                RETURNING page_number
                """
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "item": str(item_id),
                "document": str(document_id),
                "source": source,
            },
        )
        return int(result.scalar_one())

    async def item_belongs_to_session(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID, item_id: uuid.UUID
    ) -> bool:
        result = await self._session.execute(
            text(
                "SELECT 1 FROM capture_item "
                "WHERE id = :item AND session_id = :session "
                "  AND administration_id = :admin AND discarded_at IS NULL"
            ),
            {
                "item": str(item_id),
                "session": str(session_id),
                "admin": str(administration_id),
            },
        )
        return result.first() is not None

    async def items_without_pages(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID
    ) -> Sequence[uuid.UUID]:
        result = await self._session.execute(
            text("SELECT * FROM expenses.items_without_pages(:session)"),
            {"session": str(session_id)},
        )
        return [row[0] for row in result]

    async def discard_item(
        self,
        *,
        administration_id: uuid.UUID,
        item_id: uuid.UUID,
        user_id: uuid.UUID,
        reason: str,
    ) -> None:
        await self._session.execute(
            text(
                "UPDATE capture_item "
                "SET discarded_at = now(), discarded_by_user_id = :user, "
                "    discarded_reason = :reason "
                "WHERE id = :item AND administration_id = :admin "
                "  AND discarded_at IS NULL"
            ),
            {
                "item": str(item_id),
                "admin": str(administration_id),
                "user": str(user_id),
                "reason": reason,
            },
        )

    async def finalise(
        self, *, administration_id: uuid.UUID, session_id: uuid.UUID, user_id: uuid.UUID
    ) -> Sequence[Expense]:
        """Close the session and return the expenses it produced.

        It does NOT mark them ready, and that is a correction to how this
        worked before the expense form existed. `ready` now means FR-EXP-001b's
        minimum has been given (migration 0033's `expense_ready_is_complete`),
        so readying a freshly captured expense would be releasing a claim with
        no amount and no payment method - which FR-EXP-002 cannot approve and
        FR-EXP-003 cannot pay.

        Finalising closes the sitting; each expense becomes ready on its own
        when its form is complete. See
        `api.expenses.capture.status_after_finalisation`.

        Discarded items are skipped: FR-EXP-001a's review list is where a
        receipt is dropped, and a dropped one never becomes a claim.
        """
        await self._session.execute(
            text(
                "UPDATE capture_session "
                "SET finalised_at = now(), finalised_by_user_id = :user "
                "WHERE id = :session AND administration_id = :admin "
                "  AND finalised_at IS NULL"
            ),
            {
                "session": str(session_id),
                "admin": str(administration_id),
                "user": str(user_id),
            },
        )
        result = await self._session.execute(
            text(
                f"""
                SELECT {_EXPENSE_COLUMNS}
                  FROM expense
                 WHERE administration_id = :admin
                   AND capture_item_id IN (
                       SELECT id FROM capture_item
                        WHERE session_id = :session AND discarded_at IS NULL
                   )
                 ORDER BY created_at
                """
            ),
            {"session": str(session_id), "admin": str(administration_id)},
        )
        return [_to_expense(row) for row in result]

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id

    # -- FR-EXP-001b / FR-EXP-001e: the form ------------------------------

    async def get(self, *, administration_id: uuid.UUID, expense_id: uuid.UUID) -> Expense | None:
        result = await self._session.execute(
            text(
                f"SELECT {_EXPENSE_COLUMNS} FROM expense "
                f"WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(expense_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _to_expense(row)

    async def update(
        self,
        *,
        administration_id: uuid.UUID,
        expense_id: uuid.UUID,
        changes: dict[str, object],
    ) -> Expense:
        """Writes only the fields the form owns.

        Column names come from `_WRITABLE_FIELDS`, never from the caller's
        keys, so a `changes` dict that picked something up from a request body
        cannot reach the statement. `status` is absent from that set on
        purpose: releasing a claim goes through `set_status`, where the
        completeness check lives.
        """
        unknown = set(changes) - _WRITABLE_FIELDS
        if unknown:
            raise ValueError(f"not writable through the expense form: {sorted(unknown)}")
        if not changes:
            existing = await self.get(administration_id=administration_id, expense_id=expense_id)
            if existing is None:
                raise ValueError(f"expense {expense_id} not found")
            return existing

        assignments = ", ".join(f"{name} = :{name}" for name in sorted(changes))
        result = await self._session.execute(
            text(
                f"UPDATE expense SET {assignments}, updated_at = now() "
                f"WHERE id = :expense_id AND administration_id = :admin "
                f"RETURNING {_EXPENSE_COLUMNS}"
            ),
            {
                **{name: changes[name] for name in changes},
                "expense_id": str(expense_id),
                "admin": str(administration_id),
            },
        )
        return _to_expense(result.one())

    async def set_status(
        self, *, administration_id: uuid.UUID, expense_id: uuid.UUID, status: str
    ) -> Expense:
        result = await self._session.execute(
            text(
                f"UPDATE expense SET status = :status, updated_at = now() "
                f"WHERE id = :id AND administration_id = :admin "
                f"RETURNING {_EXPENSE_COLUMNS}"
            ),
            {"status": status, "id": str(expense_id), "admin": str(administration_id)},
        )
        return _to_expense(result.one())

    async def rate_on(self, *, treatment: str, on: date) -> Decimal | None:
        """CMP-014's effective-dated rate, from migration 0028.

        Called with the expense's OWN date rather than today's, which is the
        whole point: `btw_21` is 19% before 2012-10-01 and 21% after, and a
        receipt is filed under the rate that applied when it was issued.
        """
        result = await self._session.execute(
            text("SELECT vat.rate_on(:treatment, :on) AS rate"),
            {"treatment": treatment, "on": on},
        )
        return result.scalar_one_or_none()

    async def duplicate_candidates(
        self,
        *,
        administration_id: uuid.UUID,
        expense_id: uuid.UUID,
        triple: ExpenseTriple,
    ) -> Sequence[DuplicateWarning]:
        """FR-EXP-001g, from the SQL function that derives it.

        The similarity floor is passed from Python so the number has one home
        (`api.expenses.duplicates.SIMILARITY_THRESHOLD`) rather than one in the
        function's default and another in a constant nobody reconciles.
        """
        result = await self._session.execute(
            text(
                "SELECT expense_id, strength, supplier, expense_date, gross_amount, "
                "       status, same_submitter, similarity "
                "FROM expenses.duplicate_candidates("
                "    :admin, :expense, :supplier, :on, :gross, :floor)"
            ),
            {
                "admin": str(administration_id),
                "expense": str(expense_id),
                "supplier": triple.supplier,
                "on": triple.on,
                "gross": triple.gross_amount,
                "floor": SIMILARITY_THRESHOLD,
            },
        )
        return [
            DuplicateWarning(
                expense_id=row.expense_id,
                strength=DuplicateStrength(row.strength),
                supplier=row.supplier,
                on=row.expense_date,
                gross_amount=row.gross_amount,
                status=row.status,
                same_submitter=row.same_submitter,
                similarity=float(row.similarity) if row.similarity is not None else None,
            )
            for row in result
        ]

    # -- FR-EXP-001d / FR-EXP-001e: posting ------------------------------

    async def account_for(
        self, *, administration_id: uuid.UUID, purpose: str, category: str | None = None
    ) -> uuid.UUID | None:
        """The mapped account, or None.

        For `expense_category` the exact match wins and the administration's
        default row (`category_key IS NULL`) is the fallback - ordered so that
        a mapped category beats the default rather than depending on which row
        the planner reached first.
        """
        if purpose != "expense_category":
            result = await self._session.execute(
                text(
                    "SELECT account_id FROM expense_posting_account "
                    "WHERE administration_id = :admin AND purpose = :purpose"
                ),
                {"admin": str(administration_id), "purpose": purpose},
            )
            return result.scalar_one_or_none()

        result = await self._session.execute(
            text(
                """
                SELECT account_id
                  FROM expense_posting_account
                 WHERE administration_id = :admin
                   AND purpose = 'expense_category'
                   AND (
                       category_key IS NULL
                       OR lower(btrim(category_key)) = lower(btrim(:category))
                   )
                 ORDER BY category_key IS NULL
                 LIMIT 1
                """
            ),
            {"admin": str(administration_id), "category": category},
        )
        return result.scalar_one_or_none()

    async def open_period_for(self, *, administration_id: uuid.UUID, on: date) -> uuid.UUID | None:
        """The OPEN period containing `on`.

        Returns None for "no period exists" and "the period is locked or filed"
        alike, and the caller refuses either way. Distinguishing them would
        invite a caller to post into a different period, which is how a cost
        ends up in the wrong month to avoid an inconvenience (FR-GL-007).
        """
        result = await self._session.execute(
            text(
                "SELECT id FROM period "
                "WHERE administration_id = :admin "
                "  AND :on BETWEEN start_date AND end_date "
                "  AND status = 'open'"
            ),
            {"admin": str(administration_id), "on": on},
        )
        return result.scalar_one_or_none()

    async def purchase_journal(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        """The administration's single active purchase journal.

        None when there is none OR when there are several: an ambiguous answer
        would make which journal a claim lands in depend on row order, and
        FR-GL-013's numbering is per journal - so the choice shows up in the
        entry numbers forever.
        """
        result = await self._session.execute(
            text(
                # `journal_type`, not `type`: migration 0020 names the column
                # that way and never renamed it. This read `type` until
                # 2026-09-09 and would have raised `column "type" does not
                # exist` on every attempt to post an expense - undetected
                # because the DB-backed suite is skipped without Postgres and
                # tests/expenses/test_posting.py drives a fake repository.
                "SELECT id FROM ledger_journal "
                "WHERE administration_id = :admin AND journal_type = 'purchase' "
                "  AND status = 'active'"
            ),
            {"admin": str(administration_id)},
        )
        rows = result.fetchall()
        return rows[0][0] if len(rows) == 1 else None

    async def mark_posted(
        self, *, administration_id: uuid.UUID, expense_id: uuid.UUID, entry_id: uuid.UUID
    ) -> Expense:
        result = await self._session.execute(
            text(
                f"UPDATE expense "
                f"SET status = 'posted', journal_entry_id = :entry, "
                f"    posted_at = now(), updated_at = now() "
                f"WHERE id = :id AND administration_id = :admin "
                f"RETURNING {_EXPENSE_COLUMNS}"
            ),
            {
                "entry": str(entry_id),
                "id": str(expense_id),
                "admin": str(administration_id),
            },
        )
        return _to_expense(result.one())

    async def link_documents_to_entry(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        expense_id: uuid.UUID,
        entry_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> int:
        """FR-EXP-001d's bidirectional link, completed.

        Every original captured for this claim now points at the entry it
        produced, through migration 0031's `document_posting_link` - which is
        the same table a directly-uploaded document uses, so FR-DOC-003's
        completeness report sees expense evidence without knowing what an
        expense is.

        `ON CONFLICT DO NOTHING` for the reason the document service gives:
        linking the same pair twice is a retry, not a conflict.
        """
        result = await self._session.execute(
            text(
                """
                INSERT INTO document_posting_link (
                    organization_id, administration_id, document_id,
                    journal_entry_id, linked_by_user_id
                )
                SELECT :org, :admin, p.document_id, :entry, :user
                  FROM capture_page p
                  JOIN capture_item i ON i.id = p.item_id
                  JOIN expense e      ON e.capture_item_id = i.id
                 WHERE e.id = :expense
                   AND e.administration_id = :admin
                ON CONFLICT (document_id, journal_entry_id)
                    WHERE detached_at IS NULL
                DO NOTHING
                RETURNING id
                """
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "entry": str(entry_id),
                "user": str(user_id),
                "expense": str(expense_id),
            },
        )
        # Counted from RETURNING rather than `rowcount`: with ON CONFLICT DO
        # NOTHING the two differ on a retry - rowcount reports the rows the
        # statement touched, and only the rows that came back are links this
        # call actually made.
        return len(result.fetchall())

    async def suggest_category(
        self, *, administration_id: uuid.UUID, user_id: uuid.UUID, supplier: str | None
    ) -> str | None:
        result = await self._session.execute(
            text("SELECT expenses.suggest_category(:admin, :user, :supplier) AS category"),
            {
                "admin": str(administration_id),
                "user": str(user_id),
                "supplier": supplier,
            },
        )
        return result.scalar_one_or_none()

    async def list_by_status(
        self, *, administration_id: uuid.UUID, status: str | None, limit: int
    ) -> Sequence[Expense]:
        """MOB-004's Approve/View tabs: newest first, bounded, no cursor.

        `administration_id` is a defence-in-depth predicate, same as every
        other query here (ADR-003) - RLS is the actual tenant boundary.
        """
        if status is not None:
            result = await self._session.execute(
                text(
                    f"SELECT {_EXPENSE_COLUMNS} FROM expense "
                    f"WHERE administration_id = :admin AND status = :status "
                    f"ORDER BY created_at DESC LIMIT :limit"
                ),
                {"admin": str(administration_id), "status": status, "limit": limit},
            )
        else:
            result = await self._session.execute(
                text(
                    f"SELECT {_EXPENSE_COLUMNS} FROM expense "
                    f"WHERE administration_id = :admin "
                    f"ORDER BY created_at DESC LIMIT :limit"
                ),
                {"admin": str(administration_id), "limit": limit},
            )
        return [_to_expense(row) for row in result]
