"""SQLAlchemy-backed LedgerRepository over migration 0020.

This is the ONLY module in the codebase that names a posting table, and
`LedgerService` is the only thing that may import it. That is a code-level
statement of CLAUDE.md's first architectural non-negotiable, and
tests/ledger/test_bounded_context.py fails the build if another module
imports it.

But the boundary is not enforced HERE, and that is the point. `ledgr_app` -
the role every request runs as - holds SELECT on the posting tables and no
INSERT, UPDATE, DELETE or TRUNCATE at all. Application code that ignored the
import rule and wrote its own SQL would get `permission denied for table
journal_entry`. The import test catches the mistake early with a good
message; the missing grant is what makes it impossible.

Note what is absent, for the same reason as api/audit/repository.py: there is
no update() and no delete(). Postings are immutable (FR-GL-003, CMP-009), no
role has the privilege, and a trigger would reject it anyway. A repository
method for a statement the database refuses would be a lie about what these
tables can do. Corrections go through reverse().
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.ledger.model import (
    Account,
    AccountStatus,
    AccountType,
    ControlKind,
    EntryCursor,
    EntryInput,
    Journal,
    JournalEntryPage,
    JournalEntrySummary,
    JournalType,
    Party,
    PartyKind,
    PostedEntry,
    PostedLine,
    ReconciliationRow,
    SubledgerRow,
    TrialBalanceRow,
)

_ENTRY_COLUMNS = """
    id, organization_id, administration_id, fiscal_year_id, period_id,
    journal_id, entry_number, entry_date, description, document_reference,
    posted_by_user_id, source_system, posted_at, reverses_entry_id,
    idempotency_key, suppletie_id
"""


def _entry(row: Any, lines: Sequence[PostedLine] = ()) -> PostedEntry:
    return PostedEntry(
        id=row.id,
        organization_id=row.organization_id,
        administration_id=row.administration_id,
        fiscal_year_id=row.fiscal_year_id,
        period_id=row.period_id,
        journal_id=row.journal_id,
        entry_number=int(row.entry_number),
        entry_date=row.entry_date,
        description=row.description,
        document_reference=row.document_reference,
        posted_by_user_id=row.posted_by_user_id,
        source_system=row.source_system,
        posted_at=row.posted_at,
        reverses_entry_id=row.reverses_entry_id,
        idempotency_key=row.idempotency_key,
        suppletie_id=row.suppletie_id,
        lines=tuple(lines),
    )


def _line(row: Any) -> PostedLine:
    return PostedLine(
        id=row.id,
        line_number=int(row.line_number),
        account_id=row.account_id,
        # Postgres numeric arrives as Decimal through asyncpg. Wrapping it
        # again is a no-op for a Decimal and a loud failure if a driver ever
        # starts handing back a float - which would silently violate NFR-031
        # everywhere downstream.
        debit=Decimal(row.debit),
        credit=Decimal(row.credit),
        subledger_party_id=row.subledger_party_id,
        cost_centre_id=row.cost_centre_id,
        description=row.description,
        account_code=row.account_code,
        account_name=row.account_name,
    )


# The journal as a list reads it (FR-GL-004's header fields plus totals), in
# keyset order. The year filter and the cursor are both optional and both
# typed with an explicit cast: asyncpg binds a None parameter with no type of
# its own, and `:x IS NULL` on an untyped bind is what the driver refuses.
_ENTRY_PAGE_SQL = """
    SELECT je.id, je.administration_id, je.fiscal_year_id, je.period_id, je.journal_id,
           j.code AS journal_code, j.name AS journal_name,
           je.entry_number, je.entry_date, je.description, je.document_reference,
           je.posted_by_user_id, je.source_system, je.posted_at, je.reverses_entry_id,
           coalesce(sum(jl.debit), 0)::numeric(19, 2)  AS total_debit,
           coalesce(sum(jl.credit), 0)::numeric(19, 2) AS total_credit,
           count(jl.id)                                AS line_count
    FROM journal_entry je
    JOIN ledger_journal j ON j.id = je.journal_id
    LEFT JOIN journal_line jl ON jl.journal_entry_id = je.id
    WHERE je.administration_id = :administration_id
      AND (cast(:fiscal_year_id as uuid) IS NULL
           OR je.fiscal_year_id = cast(:fiscal_year_id as uuid))
      AND (cast(:after_posted_at as timestamptz) IS NULL
           OR (je.posted_at, je.id)
              < (cast(:after_posted_at as timestamptz), cast(:after_id as uuid)))
    GROUP BY je.id, j.code, j.name
    ORDER BY je.posted_at DESC, je.id DESC
    LIMIT :limit
"""


def _summary(row: Any) -> JournalEntrySummary:
    return JournalEntrySummary(
        id=row.id,
        administration_id=row.administration_id,
        fiscal_year_id=row.fiscal_year_id,
        period_id=row.period_id,
        journal_id=row.journal_id,
        journal_code=row.journal_code,
        journal_name=row.journal_name,
        entry_number=int(row.entry_number),
        entry_date=row.entry_date,
        description=row.description,
        document_reference=row.document_reference,
        posted_by_user_id=row.posted_by_user_id,
        source_system=row.source_system,
        posted_at=row.posted_at,
        reverses_entry_id=row.reverses_entry_id,
        total_debit=Decimal(row.total_debit),
        total_credit=Decimal(row.total_credit),
        line_count=int(row.line_count),
    )


class LedgerRepository(Protocol):
    async def post(self, entry: EntryInput) -> PostedEntry: ...

    async def reverse(
        self,
        *,
        entry_id: uuid.UUID,
        period_id: uuid.UUID,
        entry_date: date,
        description: str,
        posted_by_user_id: uuid.UUID | None,
        source_system: str,
        idempotency_key: str | None = None,
    ) -> PostedEntry: ...

    async def entry(self, entry_id: uuid.UUID) -> PostedEntry | None: ...

    async def reversal_of(self, entry_id: uuid.UUID) -> PostedEntry | None: ...

    async def entries(
        self,
        *,
        administration_id: uuid.UUID,
        fiscal_year_id: uuid.UUID | None,
        after: EntryCursor | None,
        limit: int,
    ) -> JournalEntryPage: ...

    async def create_account(
        self,
        *,
        administration_id: uuid.UUID,
        code: str,
        name: str,
        account_type: AccountType,
        rgs_code: str | None = None,
        default_vat_code: str | None = None,
        control_kind: ControlKind | None = None,
    ) -> Account: ...

    async def set_account_status(
        self, *, account_id: uuid.UUID, status: AccountStatus
    ) -> Account: ...

    async def create_journal(
        self,
        *,
        administration_id: uuid.UUID,
        code: str,
        name: str,
        journal_type: JournalType,
    ) -> Journal: ...

    async def set_journal_status(self, *, journal_id: uuid.UUID, status: str) -> Journal: ...

    async def create_party(
        self,
        *,
        administration_id: uuid.UUID,
        party_kind: PartyKind,
        name: str,
        external_reference: str | None = None,
    ) -> Party: ...

    async def numbering_gaps(
        self, *, journal_id: uuid.UUID, fiscal_year_id: uuid.UUID
    ) -> Sequence[int]: ...

    async def trial_balance(
        self, *, administration_id: uuid.UUID, fiscal_year_id: uuid.UUID
    ) -> Sequence[TrialBalanceRow]: ...

    async def subledger_balance(
        self, *, administration_id: uuid.UUID, control_kind: ControlKind
    ) -> Sequence[SubledgerRow]: ...

    async def reconciliation(
        self, *, administration_id: uuid.UUID
    ) -> Sequence[ReconciliationRow]: ...


class SqlLedgerRepository:
    """Every write goes through a `ledger.*` function.

    Not as a convention: `ledgr_app` cannot execute an INSERT against these
    tables, so there is no other statement this class could issue. The
    functions are SECURITY DEFINER and run as `ledgr_ledger`, which holds the
    only INSERT privilege in the system.

    Tenant isolation survives that elevation because RLS predicates read
    `app.current_org_id()`, which is session state rather than role state - the
    definer functions see exactly the calling tenant's rows.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- writes -----------------------------------------------------------

    async def post(self, entry: EntryInput) -> PostedEntry:
        # json.dumps over the line payload, whose amounts are already strings
        # (model.amount_to_string). A float anywhere in this structure would
        # serialise to a JSON number and ledger.post_entry rejects those -
        # NFR-031's second line of defence, at the database boundary.
        result = await self._session.execute(
            text(
                f"SELECT {_ENTRY_COLUMNS} FROM ledger.post_entry("
                "  p_administration_id  => :administration_id,"
                "  p_journal_id         => :journal_id,"
                "  p_period_id          => :period_id,"
                "  p_entry_date         => cast(:entry_date as date),"
                "  p_description        => :description,"
                "  p_document_reference => :document_reference,"
                "  p_posted_by_user_id  => :posted_by_user_id,"
                "  p_source_system      => :source_system,"
                "  p_lines              => cast(:lines as jsonb),"
                "  p_reverses_entry_id  => :reverses_entry_id,"
                "  p_idempotency_key    => :idempotency_key,"
                "  p_suppletie_id       => :suppletie_id"
                ")"
            ),
            {
                "administration_id": str(entry.administration_id),
                "journal_id": str(entry.journal_id),
                "period_id": str(entry.period_id),
                "entry_date": entry.entry_date,
                "description": entry.description,
                "document_reference": entry.document_reference,
                "posted_by_user_id": (
                    str(entry.posted_by_user_id) if entry.posted_by_user_id else None
                ),
                "source_system": entry.source_system,
                "lines": json.dumps(entry.as_line_payload()),
                "reverses_entry_id": (
                    str(entry.reverses_entry_id) if entry.reverses_entry_id else None
                ),
                "idempotency_key": entry.idempotency_key,
                "suppletie_id": (str(entry.suppletie_id) if entry.suppletie_id else None),
            },
        )
        posted = _entry(result.one())
        return await self._with_lines(posted)

    async def reverse(
        self,
        *,
        entry_id: uuid.UUID,
        period_id: uuid.UUID,
        entry_date: date,
        description: str,
        posted_by_user_id: uuid.UUID | None,
        source_system: str,
        idempotency_key: str | None = None,
    ) -> PostedEntry:
        result = await self._session.execute(
            text(
                f"SELECT {_ENTRY_COLUMNS} FROM ledger.reverse_entry("
                "  p_entry_id          => :entry_id,"
                "  p_period_id         => :period_id,"
                "  p_entry_date        => cast(:entry_date as date),"
                "  p_description       => :description,"
                "  p_posted_by_user_id => :posted_by_user_id,"
                "  p_source_system     => :source_system,"
                "  p_idempotency_key   => :idempotency_key"
                ")"
            ),
            {
                "entry_id": str(entry_id),
                "period_id": str(period_id),
                "entry_date": entry_date,
                "description": description,
                "posted_by_user_id": (str(posted_by_user_id) if posted_by_user_id else None),
                "source_system": source_system,
                "idempotency_key": idempotency_key,
            },
        )
        return await self._with_lines(_entry(result.one()))

    # -- reads ------------------------------------------------------------

    async def entry(self, entry_id: uuid.UUID) -> PostedEntry | None:
        result = await self._session.execute(
            text(f"SELECT {_ENTRY_COLUMNS} FROM journal_entry WHERE id = :id"),
            {"id": str(entry_id)},
        )
        row = result.first()
        return await self._with_lines(_entry(row)) if row is not None else None

    async def reversal_of(self, entry_id: uuid.UUID) -> PostedEntry | None:
        """FR-GL-003. "Has this been reversed" is a lookup on
        reverses_entry_id, not a flag on the original - a flag would need an
        UPDATE to a committed row, which is exactly what this table forbids.
        """
        result = await self._session.execute(
            text(f"SELECT {_ENTRY_COLUMNS} FROM journal_entry WHERE reverses_entry_id = :id"),
            {"id": str(entry_id)},
        )
        row = result.first()
        return await self._with_lines(_entry(row)) if row is not None else None

    async def entries(
        self,
        *,
        administration_id: uuid.UUID,
        fiscal_year_id: uuid.UUID | None,
        after: EntryCursor | None,
        limit: int,
    ) -> JournalEntryPage:
        """One page of the journal, newest posting first.

        Fetches one row more than asked for: that row's existence is what
        says there is a next page, and it is dropped rather than returned.
        """
        result = await self._session.execute(
            text(_ENTRY_PAGE_SQL),
            {
                "administration_id": str(administration_id),
                "fiscal_year_id": str(fiscal_year_id) if fiscal_year_id else None,
                "after_posted_at": after.posted_at if after else None,
                "after_id": str(after.entry_id) if after else None,
                "limit": limit + 1,
            },
        )
        rows = [_summary(row) for row in result]
        items = tuple(rows[:limit])
        next_cursor = items[-1].cursor if len(rows) > limit and items else None
        return JournalEntryPage(items=items, next_cursor=next_cursor)

    async def _with_lines(self, entry: PostedEntry) -> PostedEntry:
        result = await self._session.execute(
            text(
                "SELECT l.id, l.line_number, l.account_id, l.debit, l.credit, "
                "       l.subledger_party_id, l.cost_centre_id, l.description, "
                "       a.code AS account_code, a.name AS account_name "
                "FROM journal_line l "
                "JOIN ledger_account a ON a.id = l.account_id "
                "WHERE l.journal_entry_id = :id "
                "ORDER BY l.line_number"
            ),
            {"id": str(entry.id)},
        )
        return PostedEntry(
            id=entry.id,
            organization_id=entry.organization_id,
            administration_id=entry.administration_id,
            fiscal_year_id=entry.fiscal_year_id,
            period_id=entry.period_id,
            journal_id=entry.journal_id,
            entry_number=entry.entry_number,
            entry_date=entry.entry_date,
            description=entry.description,
            document_reference=entry.document_reference,
            posted_by_user_id=entry.posted_by_user_id,
            source_system=entry.source_system,
            posted_at=entry.posted_at,
            reverses_entry_id=entry.reverses_entry_id,
            idempotency_key=entry.idempotency_key,
            suppletie_id=entry.suppletie_id,
            lines=tuple(_line(row) for row in result),
        )

    # -- master data ------------------------------------------------------

    async def create_account(
        self,
        *,
        administration_id: uuid.UUID,
        code: str,
        name: str,
        account_type: AccountType,
        rgs_code: str | None = None,
        default_vat_code: str | None = None,
        control_kind: ControlKind | None = None,
    ) -> Account:
        result = await self._session.execute(
            text(
                "SELECT id, administration_id, code, name, account_type, status, "
                "       rgs_code, default_vat_code, control_kind "
                "FROM ledger.create_account("
                "  p_administration_id => :administration_id,"
                "  p_code              => :code,"
                "  p_name              => :name,"
                "  p_account_type      => :account_type,"
                "  p_rgs_code          => :rgs_code,"
                "  p_default_vat_code  => :default_vat_code,"
                "  p_control_kind      => :control_kind"
                ")"
            ),
            {
                "administration_id": str(administration_id),
                "code": code,
                "name": name,
                "account_type": account_type.value,
                "rgs_code": rgs_code,
                "default_vat_code": default_vat_code,
                "control_kind": control_kind.value if control_kind else None,
            },
        )
        return _account(result.one())

    async def set_account_status(self, *, account_id: uuid.UUID, status: AccountStatus) -> Account:
        result = await self._session.execute(
            text(
                "SELECT id, administration_id, code, name, account_type, status, "
                "       rgs_code, default_vat_code, control_kind "
                "FROM ledger.set_account_status(:account_id, :status)"
            ),
            {"account_id": str(account_id), "status": status.value},
        )
        return _account(result.one())

    async def create_journal(
        self,
        *,
        administration_id: uuid.UUID,
        code: str,
        name: str,
        journal_type: JournalType,
    ) -> Journal:
        result = await self._session.execute(
            text(
                "SELECT id, administration_id, code, name, journal_type, status "
                "FROM ledger.create_journal("
                "  :administration_id, :code, :name, :journal_type)"
            ),
            {
                "administration_id": str(administration_id),
                "code": code,
                "name": name,
                "journal_type": journal_type.value,
            },
        )
        row = result.one()
        return Journal(
            id=row.id,
            administration_id=row.administration_id,
            code=row.code,
            name=row.name,
            journal_type=JournalType(row.journal_type),
            status=row.status,
        )

    async def set_journal_status(self, *, journal_id: uuid.UUID, status: str) -> Journal:
        result = await self._session.execute(
            text(
                "SELECT id, administration_id, code, name, journal_type, status "
                "FROM ledger.set_journal_status(:journal_id, :status)"
            ),
            {"journal_id": str(journal_id), "status": status},
        )
        row = result.one()
        return Journal(
            id=row.id,
            administration_id=row.administration_id,
            code=row.code,
            name=row.name,
            journal_type=JournalType(row.journal_type),
            status=row.status,
        )

    async def create_party(
        self,
        *,
        administration_id: uuid.UUID,
        party_kind: PartyKind,
        name: str,
        external_reference: str | None = None,
    ) -> Party:
        result = await self._session.execute(
            text(
                "SELECT id, administration_id, party_kind, name, external_reference "
                "FROM ledger.create_party("
                "  :administration_id, :party_kind, :name, :external_reference)"
            ),
            {
                "administration_id": str(administration_id),
                "party_kind": party_kind.value,
                "name": name,
                "external_reference": external_reference,
            },
        )
        row = result.one()
        return Party(
            id=row.id,
            administration_id=row.administration_id,
            party_kind=PartyKind(row.party_kind),
            name=row.name,
            external_reference=row.external_reference,
        )

    # -- reporting --------------------------------------------------------

    async def numbering_gaps(
        self, *, journal_id: uuid.UUID, fiscal_year_id: uuid.UUID
    ) -> Sequence[int]:
        result = await self._session.execute(
            text(
                "SELECT missing_number FROM ledger.numbering_gaps(  :journal_id, :fiscal_year_id)"
            ),
            {"journal_id": str(journal_id), "fiscal_year_id": str(fiscal_year_id)},
        )
        return [int(row.missing_number) for row in result]

    async def trial_balance(
        self, *, administration_id: uuid.UUID, fiscal_year_id: uuid.UUID
    ) -> Sequence[TrialBalanceRow]:
        result = await self._session.execute(
            text(
                "SELECT account_id, account_code, account_name, account_type, "
                "       total_debit, total_credit, balance "
                "FROM ledger.trial_balance(:administration_id, :fiscal_year_id)"
            ),
            {
                "administration_id": str(administration_id),
                "fiscal_year_id": str(fiscal_year_id),
            },
        )
        return [
            TrialBalanceRow(
                account_id=row.account_id,
                account_code=row.account_code,
                account_name=row.account_name,
                account_type=AccountType(row.account_type),
                total_debit=Decimal(row.total_debit),
                total_credit=Decimal(row.total_credit),
                balance=Decimal(row.balance),
            )
            for row in result
        ]

    async def subledger_balance(
        self, *, administration_id: uuid.UUID, control_kind: ControlKind
    ) -> Sequence[SubledgerRow]:
        result = await self._session.execute(
            text(
                "SELECT party_id, party_name, total_debit, total_credit, balance "
                "FROM ledger.subledger_balance(:administration_id, :control_kind)"
            ),
            {
                "administration_id": str(administration_id),
                "control_kind": control_kind.value,
            },
        )
        return [
            SubledgerRow(
                party_id=row.party_id,
                party_name=row.party_name,
                total_debit=Decimal(row.total_debit),
                total_credit=Decimal(row.total_credit),
                balance=Decimal(row.balance),
            )
            for row in result
        ]

    async def reconciliation(self, *, administration_id: uuid.UUID) -> Sequence[ReconciliationRow]:
        result = await self._session.execute(
            text(
                "SELECT control_kind, control_account_code, control_balance, "
                "       subledger_balance, difference "
                "FROM ledger.control_account_reconciliation(:administration_id)"
            ),
            {"administration_id": str(administration_id)},
        )
        return [
            ReconciliationRow(
                control_kind=ControlKind(row.control_kind),
                control_account_code=row.control_account_code,
                control_balance=Decimal(row.control_balance),
                subledger_balance=Decimal(row.subledger_balance),
                difference=Decimal(row.difference),
            )
            for row in result
        ]


def _account(row: Any) -> Account:
    return Account(
        id=row.id,
        administration_id=row.administration_id,
        code=row.code,
        name=row.name,
        account_type=AccountType(row.account_type),
        status=AccountStatus(row.status),
        rgs_code=row.rgs_code,
        default_vat_code=row.default_vat_code,
        control_kind=ControlKind(row.control_kind) if row.control_kind else None,
    )
