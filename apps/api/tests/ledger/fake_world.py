"""Builds a World over the in-memory fake.

Its counterpart is build_db_world() in tests/integration/ledger_world.py, which
seeds the same fixtures into a real Postgres. Keeping the two builders apart
but the World shape identical is what lets tests/ledger/cases.py run unchanged
against either.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date

from api.ledger.model import AccountStatus, AccountType, ControlKind, JournalType, PartyKind
from tests.ledger.world import World
from tests.support.fake_ledger_repository import InMemoryLedgerRepository

PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 1, 31)


async def build_fake_world() -> tuple[InMemoryLedgerRepository, World]:
    repo = InMemoryLedgerRepository()
    admin = repo.administration_id
    other_admin = uuid.uuid4()

    year = repo.add_fiscal_year()
    open_period = repo.add_period(
        year, period_number=1, start_date=PERIOD_START, end_date=PERIOD_END
    )
    locked_period = repo.add_period(
        year,
        period_number=2,
        status="locked",
        start_date=date(2026, 2, 1),
        end_date=date(2026, 2, 28),
    )
    filed_period = repo.add_period(
        year,
        period_number=3,
        status="vat_filed",
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 31),
    )

    # The second administration shares the organization, so a case that
    # reaches across it is refused by FR-GL-002 rather than by tenant
    # isolation - which would prove the wrong thing.
    other_year = repo.add_fiscal_year()
    other_year.administration_id = other_admin
    other_period = repo.add_period(other_year, period_number=1)

    journal = await repo.create_journal(
        administration_id=admin, code="MEM", name="Memoriaal", journal_type=JournalType.MEMORIAL
    )
    blocked_journal = await repo.create_journal(
        administration_id=admin, code="OLD", name="Retired", journal_type=JournalType.MEMORIAL
    )
    repo.journals[blocked_journal.id] = replace(blocked_journal, status="blocked")

    other_journal = await repo.create_journal(
        administration_id=other_admin,
        code="MEM",
        name="Memoriaal",
        journal_type=JournalType.MEMORIAL,
    )

    cash = await repo.create_account(
        administration_id=admin, code="1000", name="Kas", account_type=AccountType.ASSET
    )
    revenue = await repo.create_account(
        administration_id=admin, code="8000", name="Omzet", account_type=AccountType.REVENUE
    )
    blocked = await repo.create_account(
        administration_id=admin, code="9999", name="Vervallen", account_type=AccountType.EXPENSE
    )
    await repo.set_account_status(account_id=blocked.id, status=AccountStatus.BLOCKED)

    receivables = await repo.create_account(
        administration_id=admin,
        code="1300",
        name="Debiteuren",
        account_type=AccountType.ASSET,
        control_kind=ControlKind.ACCOUNTS_RECEIVABLE,
    )
    payables = await repo.create_account(
        administration_id=admin,
        code="1600",
        name="Crediteuren",
        account_type=AccountType.LIABILITY,
        control_kind=ControlKind.ACCOUNTS_PAYABLE,
    )
    other_account = await repo.create_account(
        administration_id=other_admin, code="1000", name="Kas", account_type=AccountType.ASSET
    )

    customer = await repo.create_party(
        administration_id=admin, party_kind=PartyKind.CUSTOMER, name="Jansen B.V."
    )
    supplier = await repo.create_party(
        administration_id=admin, party_kind=PartyKind.SUPPLIER, name="De Vries Groothandel"
    )

    world = World(
        administration_id=admin,
        fiscal_year_id=year.id,
        actor_user_id=uuid.uuid4(),
        open_period_id=open_period.id,
        locked_period_id=locked_period.id,
        vat_filed_period_id=filed_period.id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        journal_id=journal.id,
        blocked_journal_id=blocked_journal.id,
        cash_account_id=cash.id,
        revenue_account_id=revenue.id,
        blocked_account_id=blocked.id,
        receivables_control_id=receivables.id,
        payables_control_id=payables.id,
        customer_party_id=customer.id,
        supplier_party_id=supplier.id,
        other_administration_id=other_admin,
        other_journal_id=other_journal.id,
        other_period_id=other_period.id,
        other_account_id=other_account.id,
    )
    return repo, world
