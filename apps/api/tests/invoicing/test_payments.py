"""api.invoicing.payments - the receivable's other half (ADR-070).

What is under test is the ENTRY the service builds and the rules it refuses on:
which account each amount lands on and on which side, that the receivable names
its debtor, that a payment never exceeds what is owed, and who may record or
undo one. `tests/integration/test_sales_invoice_payment.py` runs the same thing
against Postgres, where the triggers live.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal

import pytest

from api.audit.log import AuditLog
from api.authz.service import AuthorizationService
from api.invoicing.model import InvoiceNotFound, NotAuthorizedToInvoice
from api.invoicing.payments import (
    BalanceState,
    InvalidPaymentAmount,
    InvoiceBalance,
    InvoiceNotPayable,
    InvoicePayment,
    NoPaymentJournal,
    PayableInvoice,
    PaymentAlreadyVoided,
    PaymentExceedsOutstanding,
    PaymentMethod,
    PaymentNotFound,
    PaymentPostingUnavailable,
    SalesPaymentService,
    normalise_amount,
)
from api.invoicing.posting import NoOpenPeriod
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository

pytestmark = pytest.mark.anyio

INVOICE = uuid.uuid4()
BANK = uuid.uuid4()
AR = uuid.uuid4()
PARTY = uuid.uuid4()
BANK_JOURNAL = uuid.uuid4()
CASH_JOURNAL = uuid.uuid4()
PERIOD = uuid.uuid4()
PAID_ON = date(2026, 9, 15)


# --- fakes -------------------------------------------------------------------


@dataclass
class RecordingLedger:
    """What was handed to `LedgerService.post` / `.reverse`. The real ledger is
    covered by tests/ledger; what matters here is the entry this module builds."""

    entries: list[object] = field(default_factory=list)
    reversals: list[dict[str, object]] = field(default_factory=list)

    async def post(self, entry, *, actor_user_id=None, correlation_id=None):  # type: ignore[no-untyped-def]
        # The invariant the real ledger enforces at COMMIT, asserted here so a
        # malformed entry fails in the test that built it.
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
class FakePaymentRepository:
    administration_id: uuid.UUID
    organization_id: uuid.UUID
    gross: Decimal = Decimal("1210.00")
    credited: Decimal = Decimal("0.00")
    issued: bool = True
    credit_note: bool = False
    exists: bool = True
    bank_journal: uuid.UUID | None = BANK_JOURNAL
    cash_journal: uuid.UUID | None = CASH_JOURNAL
    period: uuid.UUID | None = PERIOD
    receivable: uuid.UUID | None = AR
    party: uuid.UUID | None = PARTY
    payments: list[InvoicePayment] = field(default_factory=list)
    journal_types_asked: list[str] = field(default_factory=list)

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.organization_id

    async def invoice(self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID):  # type: ignore[no-untyped-def]
        if not self.exists or administration_id != self.administration_id:
            return None
        return PayableInvoice(
            id=invoice_id,
            administration_id=administration_id,
            is_issued=self.issued,
            is_credit_note=self.credit_note,
            invoice_reference="2026-7",
            customer_name="De Vries Holding B.V.",
        )

    async def balance_of(self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID):  # type: ignore[no-untyped-def]
        # `invoicing.invoice_balances`: unvoided payments only.
        paid = sum((p.amount for p in self.payments if not p.is_voided), Decimal("0.00"))
        return InvoiceBalance(
            invoice_id=invoice_id, gross=self.gross, credited=self.credited, paid=paid
        )

    async def journal_of_type(self, *, administration_id: uuid.UUID, journal_type: str):  # type: ignore[no-untyped-def]
        self.journal_types_asked.append(journal_type)
        return self.cash_journal if journal_type == "cash" else self.bank_journal

    async def open_period_for(self, *, administration_id: uuid.UUID, on: date):  # type: ignore[no-untyped-def]
        return self.period

    async def receivable_control_account(self, *, administration_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self.receivable

    async def invoice_party(self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self.party

    async def insert_payment(self, payment: InvoicePayment) -> InvoicePayment:
        self.payments.append(payment)
        return payment

    async def get_payment(self, *, administration_id: uuid.UUID, payment_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return next((p for p in self.payments if p.id == payment_id), None)

    async def payments_for(self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return list(self.payments)

    async def mark_voided(  # type: ignore[no-untyped-def]
        self, *, administration_id, payment_id, user_id, void_journal_entry_id
    ):
        original = next(p for p in self.payments if p.id == payment_id)
        voided = replace(
            original,
            voided_at=datetime.now().astimezone(),
            void_journal_entry_id=void_journal_entry_id,
        )
        self.payments[self.payments.index(original)] = voided
        return voided


@dataclass
class Setup:
    service: SalesPaymentService
    repository: FakePaymentRepository
    ledger: RecordingLedger
    audit: InMemoryAuditRepository
    user: uuid.UUID
    administration: uuid.UUID

    async def record(self, amount: str = "500.00", **overrides: object) -> InvoicePayment:
        params: dict[str, object] = dict(
            administration_id=self.administration,
            invoice_id=INVOICE,
            actor_user_id=self.user,
            amount=Decimal(amount),
            paid_on=PAID_ON,
            method=PaymentMethod.BANK_TRANSFER,
            bank_account_id=BANK,
        )
        params.update(overrides)
        return await self.service.record(**params)  # type: ignore[arg-type]


def _setup(*, role: str = "Accountant", **repository_overrides: object) -> Setup:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)
    repository = FakePaymentRepository(
        administration_id=world.acme_books, organization_id=world.acme
    )
    for name, value in repository_overrides.items():
        setattr(repository, name, value)
    ledger = RecordingLedger()
    audit = InMemoryAuditRepository()
    service = SalesPaymentService(
        repository=repository,  # type: ignore[arg-type]
        ledger=ledger,  # type: ignore[arg-type]
        authorization=AuthorizationService(world.repository),
        audit_log=AuditLog(audit),
    )
    return Setup(service, repository, ledger, audit, world.user, world.acme_books)


# --- the amount ---------------------------------------------------------------


def test_a_valid_amount_is_normalised_to_two_places() -> None:
    assert normalise_amount(Decimal("100")) == Decimal("100.00")
    assert str(normalise_amount(Decimal("100.5"))) == "100.50"


@pytest.mark.parametrize(
    "bad",
    [Decimal("0"), Decimal("-5.00"), Decimal("10.001"), Decimal("NaN"), Decimal("Infinity")],
)
def test_a_bad_amount_is_refused_not_rounded(bad: Decimal) -> None:
    with pytest.raises(InvalidPaymentAmount):
        normalise_amount(bad)


def test_a_float_is_refused_outright() -> None:
    """NFR-031: a float has already lost the value before it gets here."""
    with pytest.raises(InvalidPaymentAmount):
        normalise_amount(100.0)  # type: ignore[arg-type]


# --- the balance ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("gross", "credited", "paid", "outstanding", "state"),
    [
        ("100.00", "0.00", "0.00", "100.00", BalanceState.OPEN),
        ("100.00", "0.00", "40.00", "60.00", BalanceState.PARTIALLY_PAID),
        ("100.00", "0.00", "100.00", "0.00", BalanceState.PAID),
        ("100.00", "100.00", "0.00", "0.00", BalanceState.CREDITED),
    ],
)
def test_the_balance_and_its_state(
    gross: str, credited: str, paid: str, outstanding: str, state: BalanceState
) -> None:
    balance = InvoiceBalance(INVOICE, Decimal(gross), Decimal(credited), Decimal(paid))
    assert balance.outstanding == Decimal(outstanding)
    assert balance.state is state


# --- recording ------------------------------------------------------------------


async def test_a_payment_debits_the_bank_and_credits_the_debtor() -> None:
    setup = _setup()
    payment = await setup.record("500.00")

    (entry,) = setup.ledger.entries
    bank, receivable = entry.lines  # type: ignore[attr-defined]
    assert (bank.account_id, bank.debit, bank.credit) == (BANK, Decimal("500.00"), Decimal("0"))
    assert (receivable.account_id, receivable.debit, receivable.credit) == (
        AR,
        Decimal("0"),
        Decimal("500.00"),
    )
    # FR-GL-006: mandatory on a control account, and only there.
    assert receivable.subledger_party_id == PARTY
    assert bank.subledger_party_id is None
    assert entry.journal_id == BANK_JOURNAL  # type: ignore[attr-defined]
    assert entry.entry_date == PAID_ON  # type: ignore[attr-defined]
    assert entry.document_reference == "2026-7"  # type: ignore[attr-defined]
    assert entry.posted_by_user_id == setup.user  # type: ignore[attr-defined]
    assert payment.amount == Decimal("500.00")
    assert payment.journal_entry_id is not None


async def test_the_entry_is_idempotent_per_payment() -> None:
    """NFR-032: keyed on the payment's own id, so a retry cannot double-post."""
    setup = _setup()
    payment = await setup.record()

    (entry,) = setup.ledger.entries
    assert entry.idempotency_key == f"sales_invoice_payment:{payment.id}"  # type: ignore[attr-defined]


async def test_a_cash_payment_goes_to_the_cash_journal() -> None:
    setup = _setup()
    await setup.record(method=PaymentMethod.CASH)

    assert setup.repository.journal_types_asked == ["cash"]
    assert setup.ledger.entries[0].journal_id == CASH_JOURNAL  # type: ignore[attr-defined]


@pytest.mark.parametrize("method", [PaymentMethod.BANK_TRANSFER, PaymentMethod.CARD])
async def test_other_methods_go_to_the_bank_journal(method: PaymentMethod) -> None:
    setup = _setup()
    await setup.record(method=method)

    assert setup.repository.journal_types_asked == ["bank"]


async def test_payments_accumulate_until_the_invoice_is_paid() -> None:
    setup = _setup()
    await setup.record("1000.00")
    await setup.record("210.00")

    balance = await setup.service.balance(
        administration_id=setup.administration, invoice_id=INVOICE, actor_user_id=setup.user
    )
    assert balance.outstanding == Decimal("0.00")
    assert balance.state is BalanceState.PAID


async def test_a_payment_above_the_outstanding_balance_is_refused() -> None:
    setup = _setup()
    await setup.record("1000.00")

    with pytest.raises(PaymentExceedsOutstanding) as caught:
        await setup.record("210.01")

    assert caught.value.outstanding == Decimal("210.00")
    assert len(setup.ledger.entries) == 1  # nothing was posted for the refused one


async def test_a_credit_reduces_what_can_be_paid() -> None:
    setup = _setup(credited=Decimal("200.00"))

    with pytest.raises(PaymentExceedsOutstanding):
        await setup.record("1100.00")
    await setup.record("1010.00")


async def test_a_voided_payment_frees_its_amount_again() -> None:
    setup = _setup()
    first = await setup.record("1210.00")
    await setup.service.void(
        administration_id=setup.administration,
        invoice_id=INVOICE,
        payment_id=first.id,
        actor_user_id=setup.user,
        void_date=date(2026, 9, 20),
    )

    await setup.record("1210.00")  # owed again, so this fits


@pytest.mark.parametrize("override", [{"issued": False}, {"credit_note": True}])
async def test_only_an_issued_invoice_that_is_not_a_credit_note_is_payable(
    override: dict[str, bool],
) -> None:
    setup = _setup(**override)  # type: ignore[arg-type]

    with pytest.raises(InvoiceNotPayable):
        await setup.record()
    assert setup.ledger.entries == []


async def test_an_unknown_invoice_is_not_found() -> None:
    setup = _setup(exists=False)
    with pytest.raises(InvoiceNotFound):
        await setup.record()


async def test_no_journal_is_refused_before_anything_is_posted() -> None:
    setup = _setup(bank_journal=None)
    with pytest.raises(NoPaymentJournal) as caught:
        await setup.record()
    assert caught.value.journal_type == "bank"
    assert setup.ledger.entries == []


async def test_a_date_in_no_open_period_is_refused() -> None:
    setup = _setup(period=None)
    with pytest.raises(NoOpenPeriod):
        await setup.record()
    assert setup.ledger.entries == []


@pytest.mark.parametrize(
    ("override", "missing"),
    [({"receivable": None}, "accounts_receivable"), ({"party": None}, "debtor")],
)
async def test_an_unready_ledger_names_what_is_missing(
    override: dict[str, None], missing: str
) -> None:
    setup = _setup(**override)  # type: ignore[arg-type]
    with pytest.raises(PaymentPostingUnavailable) as caught:
        await setup.record()
    assert caught.value.missing == missing
    assert setup.ledger.entries == []


async def test_a_recorded_payment_is_audited() -> None:
    setup = _setup()
    payment = await setup.record("500.00")

    event = _last_audit(setup)
    assert event.action == "record_sales_invoice_payment"
    assert event.resource_id == payment.id
    assert event.detail["amount"] == "500.00"


def _last_audit(setup: Setup):  # type: ignore[no-untyped-def]
    """The newest audit entry. The fake exposes only _entries; reading it here
    beats widening a shared fake for one file."""
    return setup.audit._entries[-1]


# --- who may ---------------------------------------------------------------------


async def test_an_invoicer_cannot_record_a_payment() -> None:
    """Separation of duties: the person who raises invoices may not also record
    that cash arrived against them - the lapping opportunity."""
    setup = _setup(role="Invoicer")

    with pytest.raises(NotAuthorizedToInvoice):
        await setup.record()
    assert setup.ledger.entries == []
    assert _last_audit(setup).outcome.value == "denied"


async def test_an_invoicer_cannot_void_one_either() -> None:
    setup = _setup()
    payment = await setup.record()
    invoicer = _setup(role="Invoicer")
    invoicer.repository.payments.append(payment)

    with pytest.raises(NotAuthorizedToInvoice):
        await invoicer.service.void(
            administration_id=invoicer.administration,
            invoice_id=INVOICE,
            payment_id=payment.id,
            actor_user_id=invoicer.user,
            void_date=date(2026, 9, 20),
        )
    assert invoicer.ledger.reversals == []


# --- voiding ---------------------------------------------------------------------


async def test_voiding_reverses_the_receipt_and_never_edits_it() -> None:
    setup = _setup()
    payment = await setup.record("500.00")

    voided = await setup.service.void(
        administration_id=setup.administration,
        invoice_id=INVOICE,
        payment_id=payment.id,
        actor_user_id=setup.user,
        void_date=date(2026, 9, 20),
    )

    (reversal,) = setup.ledger.reversals
    assert reversal["entry_id"] == payment.journal_entry_id
    # Dated when the mistake is fixed, not when the payment was made.
    assert reversal["entry_date"] == date(2026, 9, 20)
    assert reversal["idempotency_key"] == f"sales_invoice_payment_void:{payment.id}"
    assert voided.is_voided
    assert voided.void_journal_entry_id is not None
    assert len(setup.ledger.entries) == 1  # the original was not posted again


async def test_a_payment_can_only_be_voided_once() -> None:
    setup = _setup()
    payment = await setup.record()
    args = dict(
        administration_id=setup.administration,
        invoice_id=INVOICE,
        payment_id=payment.id,
        actor_user_id=setup.user,
        void_date=date(2026, 9, 20),
    )
    await setup.service.void(**args)  # type: ignore[arg-type]

    with pytest.raises(PaymentAlreadyVoided):
        await setup.service.void(**args)  # type: ignore[arg-type]
    assert len(setup.ledger.reversals) == 1


async def test_a_payment_is_not_found_on_another_invoice() -> None:
    """A payment id only addresses a payment through ITS invoice."""
    setup = _setup()
    payment = await setup.record()

    with pytest.raises(PaymentNotFound):
        await setup.service.void(
            administration_id=setup.administration,
            invoice_id=uuid.uuid4(),
            payment_id=payment.id,
            actor_user_id=setup.user,
            void_date=date(2026, 9, 20),
        )


async def test_voiding_in_a_closed_period_is_refused() -> None:
    setup = _setup()
    payment = await setup.record()
    setup.repository.period = None

    with pytest.raises(NoOpenPeriod):
        await setup.service.void(
            administration_id=setup.administration,
            invoice_id=INVOICE,
            payment_id=payment.id,
            actor_user_id=setup.user,
            void_date=date(2026, 9, 20),
        )
    assert setup.ledger.reversals == []
