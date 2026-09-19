"""api.invoicing.write_off_service - what it posts and what it refuses (ADR-076).

Under test is the ENTRIES the service builds (which account, which side, which VAT
treatment), the order of refusals, and who may do it. `tests/integration/test_write_offs.py`
runs the same against Postgres, where the triggers live.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal

import pytest

from api.audit.log import AuditLog
from api.authz.service import AuthorizationService
from api.invoicing.bad_debt import VatGroupTotal
from api.invoicing.model import InvoiceNotFound, NotAuthorizedToInvoice
from api.invoicing.payments import BalanceState, InvoiceBalance
from api.invoicing.write_off_service import (
    ExpenseAccountInvalid,
    InvoiceWriteOff,
    NoOpenPeriod,
    NothingOutstanding,
    NothingToReclaim,
    NoWriteOffJournal,
    VatAlreadyReclaimed,
    VatReclaimNotYetAllowed,
    WriteOffAlreadyVoided,
    WriteOffDateInFuture,
    WriteOffInvoice,
    WriteOffNotAllowed,
    WriteOffNotFound,
    WriteOffPostingUnavailable,
    WriteOffReasonMissing,
    WriteOffService,
)
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository

pytestmark = pytest.mark.anyio

INVOICE = uuid.uuid4()
EXPENSE = uuid.uuid4()
AR = uuid.uuid4()
PARTY = uuid.uuid4()
MEMORIAL = uuid.uuid4()
PERIOD = uuid.uuid4()
VAT_21 = uuid.uuid4()
TODAY = date(2026, 9, 20)
DUE = date(2025, 8, 1)  # over a year ago: the VAT wait has passed


@dataclass
class RecordingLedger:
    entries: list[object] = field(default_factory=list)
    reversals: list[dict[str, object]] = field(default_factory=list)

    async def post(self, entry, *, actor_user_id=None, correlation_id=None):  # type: ignore[no-untyped-def]
        entry.assert_balanced()
        self.entries.append(entry)
        return _Posted(id=uuid.uuid4())

    async def reverse(self, **kwargs):  # type: ignore[no-untyped-def]
        self.reversals.append(kwargs)
        return _Posted(id=uuid.uuid4())


@dataclass
class _Posted:
    id: uuid.UUID


@dataclass
class FakeRepository:
    administration_id: uuid.UUID
    organization_id: uuid.UUID
    gross: Decimal = Decimal("1210.00")
    paid: Decimal = Decimal("0.00")
    groups: list[VatGroupTotal] = field(
        default_factory=lambda: [VatGroupTotal("btw_21", Decimal("1000.00"), Decimal("210.00"))]
    )
    issued: bool = True
    credit_note: bool = False
    exists: bool = True
    due_date: date | None = DUE
    expense_ok: bool = True
    journal: uuid.UUID | None = MEMORIAL
    period: uuid.UUID | None = PERIOD
    receivable: uuid.UUID | None = AR
    party: uuid.UUID | None = PARTY
    vat_accounts: dict[str, uuid.UUID] = field(default_factory=lambda: {"btw_21": VAT_21})
    write_offs: list[InvoiceWriteOff] = field(default_factory=list)

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.organization_id

    async def invoice(self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID):  # type: ignore[no-untyped-def]
        if not self.exists or administration_id != self.administration_id:
            return None
        return WriteOffInvoice(
            id=invoice_id,
            administration_id=administration_id,
            is_issued=self.issued,
            is_credit_note=self.credit_note,
            invoice_reference="2025-7",
            customer_name="De Vries Holding B.V.",
            invoice_date=date(2025, 7, 1),
            due_date=self.due_date,
        )

    async def balance_of(self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID):  # type: ignore[no-untyped-def]
        written = sum((w.amount for w in self.write_offs if not w.is_voided), Decimal("0.00"))
        return InvoiceBalance(
            invoice_id=invoice_id,
            gross=self.gross,
            credited=Decimal("0.00"),
            paid=self.paid,
            written_off=written,
        )

    async def vat_groups(self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return list(self.groups)

    async def is_expense_account(self, *, administration_id: uuid.UUID, account_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self.expense_ok

    async def journal_of_type(self, *, administration_id: uuid.UUID, journal_type: str):  # type: ignore[no-untyped-def]
        assert journal_type == "memorial"
        return self.journal

    async def open_period_for(self, *, administration_id: uuid.UUID, on: date):  # type: ignore[no-untyped-def]
        return self.period

    async def receivable_control_account(self, *, administration_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self.receivable

    async def invoice_party(self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self.party

    async def vat_output_account(self, *, administration_id: uuid.UUID, vat_treatment: str):  # type: ignore[no-untyped-def]
        return self.vat_accounts.get(vat_treatment)

    async def insert_write_off(self, write_off: InvoiceWriteOff) -> InvoiceWriteOff:
        self.write_offs.append(write_off)
        return write_off

    async def get_write_off(self, *, administration_id: uuid.UUID, write_off_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return next((w for w in self.write_offs if w.id == write_off_id), None)

    async def write_offs_for(self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return list(self.write_offs)

    async def mark_vat_reclaimed(  # type: ignore[no-untyped-def]
        self, *, administration_id, write_off_id, user_id, reclaimed_on, journal_entry_id
    ):
        original = next(w for w in self.write_offs if w.id == write_off_id)
        updated = replace(
            original, vat_reclaimed_on=reclaimed_on, vat_reclaim_journal_entry_id=journal_entry_id
        )
        self.write_offs[self.write_offs.index(original)] = updated
        return updated

    async def mark_voided(  # type: ignore[no-untyped-def]
        self,
        *,
        administration_id,
        write_off_id,
        user_id,
        void_journal_entry_id,
        void_reclaim_journal_entry_id,
    ):
        original = next(w for w in self.write_offs if w.id == write_off_id)
        voided = replace(
            original,
            voided_at=datetime.now().astimezone(),
            void_journal_entry_id=void_journal_entry_id,
            void_reclaim_journal_entry_id=void_reclaim_journal_entry_id,
        )
        self.write_offs[self.write_offs.index(original)] = voided
        return voided


@dataclass
class Setup:
    service: WriteOffService
    repository: FakeRepository
    ledger: RecordingLedger
    audit: InMemoryAuditRepository
    user: uuid.UUID
    administration: uuid.UUID

    async def write_off(self, **overrides: object) -> InvoiceWriteOff:
        params: dict[str, object] = dict(
            administration_id=self.administration,
            invoice_id=INVOICE,
            actor_user_id=self.user,
            expense_account_id=EXPENSE,
            written_off_on=TODAY,
            today=TODAY,
            reason="Klant is failliet verklaard",
        )
        params.update(overrides)
        return await self.service.write_off(**params)  # type: ignore[arg-type]


def _setup(*, role: str = "Accountant", **repository_overrides: object) -> Setup:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)
    repository = FakeRepository(administration_id=world.acme_books, organization_id=world.acme)
    for name, value in repository_overrides.items():
        setattr(repository, name, value)
    ledger = RecordingLedger()
    audit = InMemoryAuditRepository()
    service = WriteOffService(
        repository=repository,  # type: ignore[arg-type]
        ledger=ledger,  # type: ignore[arg-type]
        authorization=AuthorizationService(world.repository),
        audit_log=AuditLog(audit),
    )
    return Setup(service, repository, ledger, audit, world.user, world.acme_books)


def _lines(entry: object) -> list[tuple[uuid.UUID, Decimal, Decimal]]:
    return [(line.account_id, line.debit, line.credit) for line in entry.lines]  # type: ignore[attr-defined]


# --- the write-off entry ---------------------------------------------------------------------


async def test_a_write_off_debits_the_expense_and_credits_the_debtor_for_the_whole_balance() -> (
    None
):
    setup = _setup()

    written = await setup.write_off()

    (entry,) = setup.ledger.entries
    assert _lines(entry) == [
        (EXPENSE, Decimal("1210.00"), Decimal("0.00")),
        (AR, Decimal("0.00"), Decimal("1210.00")),
    ]
    receivable = entry.lines[1]  # type: ignore[attr-defined]
    assert receivable.subledger_party_id == PARTY  # FR-GL-006: the debtor is named
    assert entry.journal_id == MEMORIAL  # type: ignore[attr-defined]
    assert entry.idempotency_key == f"sales_invoice_write_off:{written.id}"  # type: ignore[attr-defined]
    assert written.amount == Decimal("1210.00")
    assert written.vat_amount == Decimal("210.00")
    assert [(s.treatment, s.vat) for s in written.vat_split] == [("btw_21", Decimal("210.00"))]


async def test_a_partly_paid_invoice_writes_off_only_what_is_left_and_only_its_vat_share() -> None:
    setup = _setup(paid=Decimal("605.00"))

    written = await setup.write_off()

    assert written.amount == Decimal("605.00")
    assert written.vat_amount == Decimal("105.00")
    (entry,) = setup.ledger.entries
    assert _lines(entry)[1][2] == Decimal("605.00")


async def test_the_write_off_does_not_reclaim_vat_unless_asked() -> None:
    setup = _setup()
    written = await setup.write_off()
    assert len(setup.ledger.entries) == 1
    assert not written.is_vat_reclaimed


async def test_the_balance_is_written_off_afterwards() -> None:
    setup = _setup()
    await setup.write_off()

    balance = await setup.service.balance(
        administration_id=setup.administration, invoice_id=INVOICE, actor_user_id=setup.user
    )

    assert balance.outstanding == Decimal("0.00")
    assert balance.state is BalanceState.WRITTEN_OFF


async def test_a_second_write_off_finds_nothing_outstanding() -> None:
    setup = _setup()
    await setup.write_off()
    with pytest.raises(NothingOutstanding):
        await setup.write_off()
    assert len(setup.ledger.entries) == 1


# --- the VAT reclaim ----------------------------------------
async def test_reclaiming_with_the_write_off_posts_a_second_entry_debiting_the_vat_account() -> (
    None
):
    setup = _setup()

    written = await setup.write_off(reclaim_vat=True)

    write_off_entry, reclaim_entry = setup.ledger.entries
    assert _lines(write_off_entry)[0][1] == Decimal("1210.00")
    assert _lines(reclaim_entry) == [
        (VAT_21, Decimal("210.00"), Decimal("0.00")),
        (EXPENSE, Decimal("0.00"), Decimal("210.00")),
    ]
    # The VAT line names its treatment so the return can put it in the right box (FR-VAT-001);
    # the expense line names none: it is not turnover.
    assert reclaim_entry.lines[0].vat_treatment == "btw_21"  # type: ignore[attr-defined]
    assert reclaim_entry.lines[1].vat_treatment is None  # type: ignore[attr-defined]
    assert reclaim_entry.idempotency_key == f"sales_invoice_write_off_vat:{written.id}"  # type: ignore[attr-defined]
    assert written.is_vat_reclaimed and written.vat_reclaimed_on == TODAY


async def test_the_reclaim_is_refused_inside_the_waiting_period_and_nothing_is_written() -> None:
    setup = _setup(due_date=date(2026, 3, 1))

    with pytest.raises(VatReclaimNotYetAllowed) as refused:
        await setup.write_off(reclaim_vat=True)

    assert refused.value.eligible_on == date(2027, 3, 1)
    assert setup.ledger.entries == [] and setup.repository.write_offs == []


async def test_an_insolvent_customer_waives_the_wait() -> None:
    setup = _setup(due_date=date(2026, 8, 1))
    written = await setup.write_off(reclaim_vat=True, customer_insolvent=True)
    assert written.is_vat_reclaimed and written.customer_insolvent


async def test_a_write_off_inside_the_waiting_period_is_allowed_and_the_reclaim_follows() -> None:
    setup = _setup(due_date=date(2026, 3, 1))
    written = await setup.write_off()  # recognises the loss now

    with pytest.raises(VatReclaimNotYetAllowed):
        await setup.service.reclaim_vat(
            administration_id=setup.administration,
            invoice_id=INVOICE,
            write_off_id=written.id,
            actor_user_id=setup.user,
            today=date(2026, 9, 20),
        )

    reclaimed = await setup.service.reclaim_vat(
        administration_id=setup.administration,
        invoice_id=INVOICE,
        write_off_id=written.id,
        actor_user_id=setup.user,
        today=date(2027, 3, 1),  # the first day
    )
    assert reclaimed.vat_reclaimed_on == date(2027, 3, 1)
    assert len(setup.ledger.entries) == 2


async def test_the_reclaim_happens_once() -> None:
    setup = _setup()
    written = await setup.write_off(reclaim_vat=True)
    with pytest.raises(VatAlreadyReclaimed):
        await setup.service.reclaim_vat(
            administration_id=setup.administration,
            invoice_id=INVOICE,
            write_off_id=written.id,
            actor_user_id=setup.user,
            today=TODAY,
        )


async def test_a_balance_without_vat_has_nothing_to_reclaim() -> None:
    setup = _setup(
        gross=Decimal("1000.00"),
        groups=[VatGroupTotal("btw_0", Decimal("1000.00"), Decimal("0.00"))],
    )
    with pytest.raises(NothingToReclaim):
        await setup.write_off(reclaim_vat=True)
    assert setup.ledger.entries == []

    written = await setup.write_off()  # the write-off itself is fine
    assert written.vat_amount == Decimal("0.00") and written.vat_split == ()


async def test_a_missing_output_vat_account_refuses_the_reclaim_before_anything_is_written() -> (
    None
):
    setup = _setup(vat_accounts={})
    with pytest.raises(WriteOffPostingUnavailable) as refused:
        await setup.write_off(reclaim_vat=True)
    assert refused.value.missing == "vat_output"
    assert setup.ledger.entries == [] and setup.repository.write_offs == []


# --- void ----------------------------------------
async def test_voiding_reverses_the_write_off_and_owes_the_invoice_again() -> None:
    setup = _setup()
    written = await setup.write_off()

    voided = await setup.service.void(
        administration_id=setup.administration,
        invoice_id=INVOICE,
        write_off_id=written.id,
        actor_user_id=setup.user,
        void_date=TODAY,
    )

    (reversal,) = setup.ledger.reversals
    assert reversal["entry_id"] == written.journal_entry_id
    assert voided.is_voided
    balance = await setup.service.balance(
        administration_id=setup.administration, invoice_id=INVOICE, actor_user_id=setup.user
    )
    assert balance.outstanding == Decimal("1210.00")


async def test_voiding_also_reverses_the_vat_reclaim() -> None:
    setup = _setup()
    written = await setup.write_off(reclaim_vat=True)

    voided = await setup.service.void(
        administration_id=setup.administration,
        invoice_id=INVOICE,
        write_off_id=written.id,
        actor_user_id=setup.user,
        void_date=TODAY,
    )

    reclaim_reversal, write_off_reversal = setup.ledger.reversals
    assert reclaim_reversal["entry_id"] == written.vat_reclaim_journal_entry_id
    assert write_off_reversal["entry_id"] == written.journal_entry_id
    assert voided.void_reclaim_journal_entry_id is not None


async def test_a_write_off_can_be_voided_once_and_a_voided_one_cannot_be_reclaimed() -> None:
    setup = _setup()
    written = await setup.write_off()
    kwargs = dict(
        administration_id=setup.administration,
        invoice_id=INVOICE,
        write_off_id=written.id,
        actor_user_id=setup.user,
    )
    await setup.service.void(void_date=TODAY, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(WriteOffAlreadyVoided):
        await setup.service.void(void_date=TODAY, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(WriteOffAlreadyVoided):
        await setup.service.reclaim_vat(today=TODAY, **kwargs)  # type: ignore[arg-type]


async def test_a_voided_write_off_lets_the_invoice_be_written_off_again() -> None:
    setup = _setup()
    written = await setup.write_off()
    await setup.service.void(
        administration_id=setup.administration,
        invoice_id=INVOICE,
        write_off_id=written.id,
        actor_user_id=setup.user,
        void_date=TODAY,
    )
    again = await setup.write_off()
    assert again.id != written.id and again.amount == Decimal("1210.00")


async def test_an_unknown_write_off_is_not_found() -> None:
    setup = _setup()
    with pytest.raises(WriteOffNotFound):
        await setup.service.void(
            administration_id=setup.administration,
            invoice_id=INVOICE,
            write_off_id=uuid.uuid4(),
            actor_user_id=setup.user,
            void_date=TODAY,
        )


# --- refusals ----------------------------------------
@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"exists": False}, InvoiceNotFound),
        ({"issued": False}, WriteOffNotAllowed),
        ({"credit_note": True}, WriteOffNotAllowed),
        ({"paid": Decimal("1210.00")}, NothingOutstanding),
        ({"expense_ok": False}, ExpenseAccountInvalid),
        ({"journal": None}, NoWriteOffJournal),
        ({"period": None}, NoOpenPeriod),
        ({"receivable": None}, WriteOffPostingUnavailable),
        ({"party": None}, WriteOffPostingUnavailable),
    ],
)
async def test_a_refusal_writes_nothing(
    overrides: dict[str, object], error: type[Exception]
) -> None:
    setup = _setup(**overrides)
    with pytest.raises(error):
        await setup.write_off()
    assert setup.ledger.entries == [] and setup.repository.write_offs == []


async def test_a_reason_is_required_and_the_date_cannot_be_in_the_future() -> None:
    setup = _setup()
    with pytest.raises(WriteOffReasonMissing):
        await setup.write_off(reason="   ")
    with pytest.raises(WriteOffDateInFuture):
        await setup.write_off(written_off_on=date(2026, 9, 21))
    assert setup.ledger.entries == []


# --- who may ----------------------------------------
async def test_an_invoicer_may_not_write_off_debt_and_the_denial_is_audited() -> None:
    """The person who raises invoices must not be the one who makes a debt vanish."""
    setup = _setup(role="Invoicer")
    with pytest.raises(NotAuthorizedToInvoice):
        await setup.write_off()
    assert setup.ledger.entries == [] and setup.repository.write_offs == []
    assert any(event.outcome.value == "denied" for event in setup.audit._entries)


async def test_voiding_needs_the_reverse_permission() -> None:
    setup = _setup(role="Invoicer")
    with pytest.raises(NotAuthorizedToInvoice):
        await setup.service.void(
            administration_id=setup.administration,
            invoice_id=INVOICE,
            write_off_id=uuid.uuid4(),
            actor_user_id=setup.user,
            void_date=TODAY,
        )


async def test_a_write_off_is_audited_with_its_amounts_but_not_free_text() -> None:
    setup = _setup()
    written = await setup.write_off(reason="secret reason text")
    events = [e for e in setup.audit._entries if e.action == "write_off_sales_invoice"]
    assert len(events) == 1
    assert events[0].resource_id == written.id
    assert events[0].detail["amount"] == "1210.00"
    assert "secret reason text" not in str(events[0].detail)
