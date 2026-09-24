"""api.invoicing.receivables_service and the JSON it becomes - SI-06 (ADR-072).

The arithmetic is tested in test_receivables.py. What is under test here is the
wiring: who may read a report, that a statement for an unknown customer is a
refusal, that a bad period is refused before any data is read, and the wire shape
`packages/shared-types` promises - driven through the real JSON builders.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import pytest

from api.audit.log import AuditLog
from api.authz.service import AuthorizationService
from api.i18n.language import Language
from api.invoicing.model import NotAuthorizedToInvoice
from api.invoicing.receivables import MovementKind, OpenItem, StatementMovement
from api.invoicing.receivables_service import (
    InvalidStatementPeriod,
    ReceivablesService,
    StatementCustomerNotFound,
)
from api.invoicing.routes import _ageing_json, _statement_json
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository

D = Decimal
AS_OF = date(2026, 6, 30)
CUSTOMER = uuid.uuid4()


@dataclass
class FakeRepository:
    organization_id: uuid.UUID
    items: list[OpenItem] = field(default_factory=list)
    movement_rows: list[StatementMovement] = field(default_factory=list)
    known: dict[uuid.UUID, str] = field(default_factory=lambda: {CUSTOMER: "De Vries B.V."})
    reads: list[str] = field(default_factory=list)

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.organization_id

    async def open_items(self, *, administration_id: uuid.UUID, as_of: date):  # type: ignore[no-untyped-def]
        self.reads.append("items")
        return list(self.items)

    async def customer_name(self, *, administration_id: uuid.UUID, customer_id: uuid.UUID):  # type: ignore[no-untyped-def]
        self.reads.append("name")
        return self.known.get(customer_id)

    async def movements(self, *, administration_id: uuid.UUID, customer_id: uuid.UUID):  # type: ignore[no-untyped-def]
        self.reads.append("movements")
        return list(self.movement_rows)


@dataclass
class Setup:
    service: ReceivablesService
    repository: FakeRepository
    audit: InMemoryAuditRepository
    user: uuid.UUID
    administration: uuid.UUID


def _setup(role: str = "Accountant") -> Setup:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)
    repository = FakeRepository(organization_id=world.acme)
    audit = InMemoryAuditRepository()
    service = ReceivablesService(
        repository=repository,  # type: ignore[arg-type]
        authorization=AuthorizationService(world.repository),
        audit_log=AuditLog(audit),
    )
    return Setup(service, repository, audit, world.user, world.acme_books)


def _item(days_late: int | None, amount: str, **kw: object) -> OpenItem:
    defaults: dict[str, object] = dict(
        invoice_id=uuid.uuid4(),
        invoice_reference="2026-1",
        invoice_date=date(2026, 1, 1),
        due_date=None if days_late is None else AS_OF - timedelta(days=days_late),
        customer_id=CUSTOMER,
        customer_name="De Vries B.V.",
        outstanding=D(amount),
    )
    defaults.update(kw)
    return OpenItem(**defaults)  # type: ignore[arg-type]


# --- who may -------------------------------------------------------------------------


# Owner is granted at the ORGANIZATION, not the administration, so it is not assigned here.
@pytest.mark.parametrize("role", ["Accountant", "Bookkeeper", "Viewer"])
async def test_roles_with_view_reports_can_read_the_ageing(role: str) -> None:
    setup = _setup(role)
    report = await setup.service.ageing(
        administration_id=setup.administration, actor_user_id=setup.user, as_of=AS_OF
    )
    assert report.grand_total == D("0.00")


@pytest.mark.parametrize("role", ["Invoicer", "Expense Submitter"])
async def test_a_role_without_view_reports_is_refused_and_nothing_is_read(role: str) -> None:
    """The Invoicer drafts and sends invoices but must not read every customer's
    debt and payment history for having done so."""
    setup = _setup(role)

    with pytest.raises(NotAuthorizedToInvoice):
        await setup.service.ageing(
            administration_id=setup.administration, actor_user_id=setup.user, as_of=AS_OF
        )
    with pytest.raises(NotAuthorizedToInvoice):
        await setup.service.statement(
            administration_id=setup.administration,
            customer_id=CUSTOMER,
            actor_user_id=setup.user,
            date_from=date(2026, 1, 1),
            date_to=AS_OF,
        )
    assert setup.repository.reads == []
    assert setup.audit._entries[-1].outcome.value == "denied"


# --- the ageing ------------------------------------------------------------------------


async def test_the_ageing_buckets_what_the_repository_returns() -> None:
    setup = _setup()
    setup.repository.items = [_item(10, "100.00"), _item(100, "250.00")]

    report = await setup.service.ageing(
        administration_id=setup.administration, actor_user_id=setup.user, as_of=AS_OF
    )

    assert report.grand_total == D("350.00")
    (customer,) = report.customers
    assert customer.total == D("350.00")


# --- the statement ------------------------------------------------------------------------


async def test_a_statement_is_built_from_the_movements() -> None:
    setup = _setup()
    setup.repository.movement_rows = [
        StatementMovement(
            date(2026, 2, 1), MovementKind.INVOICE, "2026-1", uuid.uuid4(), D("1210.00"), D("0.00")
        ),
        StatementMovement(
            date(2026, 3, 1), MovementKind.PAYMENT, "2026-1", None, D("0.00"), D("400.00")
        ),
    ]
    result = await setup.service.statement(
        administration_id=setup.administration,
        customer_id=CUSTOMER,
        actor_user_id=setup.user,
        date_from=date(2026, 1, 1),
        date_to=AS_OF,
    )

    assert result.customer_name == "De Vries B.V."
    assert result.statement.closing_balance == D("810.00")


async def test_a_statement_for_an_unknown_customer_is_refused() -> None:
    setup = _setup()
    with pytest.raises(StatementCustomerNotFound):
        await setup.service.statement(
            administration_id=setup.administration,
            customer_id=uuid.uuid4(),
            actor_user_id=setup.user,
            date_from=date(2026, 1, 1),
            date_to=AS_OF,
        )
    # Refused before any movement was read.
    assert "movements" not in setup.repository.reads


async def test_an_inverted_period_is_refused_before_anything_is_read() -> None:
    setup = _setup()
    with pytest.raises(InvalidStatementPeriod):
        await setup.service.statement(
            administration_id=setup.administration,
            customer_id=CUSTOMER,
            actor_user_id=setup.user,
            date_from=date(2026, 12, 31),
            date_to=date(2026, 1, 1),
        )
    assert setup.repository.reads == []


# --- the wire shape -------------------------------------------------------------------------


async def test_the_ageing_json_has_labelled_buckets_totals_and_a_drill_down() -> None:
    setup = _setup()
    late = _item(45, "300.00")
    setup.repository.items = [late, _item(None, "60.00")]
    report = await setup.service.ageing(
        administration_id=setup.administration, actor_user_id=setup.user, as_of=AS_OF
    )

    body = _ageing_json(report, Language.EN)

    assert body["as_of"] == "2026-06-30"
    assert body["grand_total"] == "360.00"
    buckets = {b["bucket"]: b for b in body["buckets"]}  # type: ignore[attr-defined,union-attr]
    assert buckets["days_31_60"] == {
        "bucket": "days_31_60",
        "label": "31–60 days overdue",
        "amount": "300.00",
    }
    assert buckets["no_due_date"]["amount"] == "60.00"
    assert [b["bucket"] for b in body["buckets"]][0] == "current"  # type: ignore[attr-defined,union-attr]

    (customer,) = body["customers"]  # type: ignore[misc]
    assert customer["total"] == "360.00"
    invoice = next(i for i in customer["invoices"] if i["invoice_id"] == str(late.invoice_id))
    assert invoice["days_late"] == 45 and invoice["bucket"] == "days_31_60"
    assert invoice["outstanding_amount"] == "300.00"


async def test_the_ageing_labels_follow_the_readers_language() -> None:
    setup = _setup()
    setup.repository.items = [_item(45, "10.00")]
    report = await setup.service.ageing(
        administration_id=setup.administration, actor_user_id=setup.user, as_of=AS_OF
    )
    dutch = {b["bucket"]: b["label"] for b in _ageing_json(report, Language.NL)["buckets"]}  # type: ignore[attr-defined,union-attr]
    assert dutch["days_31_60"] == "31–60 dagen te laat"
    assert dutch["current"] == "Nog niet vervallen"


async def test_the_statement_json_carries_running_balances_and_kind_labels() -> None:
    setup = _setup()
    invoice = uuid.uuid4()
    setup.repository.movement_rows = [
        StatementMovement(
            date(2026, 2, 1), MovementKind.INVOICE, "2026-1", invoice, D("1210.00"), D("0.00")
        ),
        StatementMovement(
            date(2026, 3, 1), MovementKind.PAYMENT, "2026-1", invoice, D("0.00"), D("400.00")
        ),
    ]
    result = await setup.service.statement(
        administration_id=setup.administration,
        customer_id=CUSTOMER,
        actor_user_id=setup.user,
        date_from=date(2026, 3, 1),
        date_to=AS_OF,
    )

    body = _statement_json(result, Language.NL)

    assert body["opening_balance"] == "1210.00"  # the February invoice, brought forward
    (line,) = body["lines"]  # type: ignore[misc]
    assert line["kind"] == "payment" and line["label"] == "Betaling"
    assert (line["debit"], line["credit"], line["balance"]) == ("0.00", "400.00", "810.00")
    assert line["invoice_id"] == str(invoice)
    assert body["closing_balance"] == "810.00"
    assert body["total_credit"] == "400.00"
