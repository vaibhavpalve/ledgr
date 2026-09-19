"""SI-12 - warn when a draft invoice looks like one already raised.

The property asserted at every layer that could take it away is that a warning
does not stop anything - ADR-034's commitment, carried over. "Warn, don't block"
quietly becomes "block" the first time somebody treats a warning as an error.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from api.audit.log import AuditLog
from api.authz.service import AuthorizationService
from api.i18n.language import Language
from api.invoicing.duplicates import (
    MAX_WARNINGS,
    WINDOW_DAYS,
    DuplicateCandidate,
    InvoiceDuplicateStrength,
    InvoiceDuplicateWarning,
    InvoiceFingerprint,
    LineFingerprint,
    find_duplicates,
    message_for,
    normalise_name,
    strength_of,
)
from api.invoicing.model import (
    InvoiceLine,
    InvoiceStatus,
    InvoiceView,
    SalesInvoice,
)
from api.invoicing.service import InvoicingService
from api.vat.rules import EffectiveRules, TreatmentRole
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository
from tests.support.fake_vat_rules_repository import InMemoryVatRulesRepository, load_document

DAY = date(2026, 9, 9)
CUSTOMER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def line(
    description: str = "Consultancy",
    quantity: str = "10",
    unit_price: str = "95.00",
    discount: str = "0",
) -> LineFingerprint:
    return LineFingerprint(
        description=description,
        quantity=Decimal(quantity),
        unit_price=Decimal(unit_price),
        discount_percent=Decimal(discount),
    )


def fingerprint(
    *,
    customer_id: uuid.UUID | None = CUSTOMER,
    name: str = "De Vries Holding B.V.",
    on: date = DAY,
    lines: tuple[LineFingerprint, ...] | None = None,
    net: str = "950.00",
) -> InvoiceFingerprint:
    return InvoiceFingerprint(
        customer_id=customer_id,
        customer_name=name,
        invoice_date=on,
        lines=(line(),) if lines is None else lines,
        net_total=Decimal(net),
    )


# ===========================================================================
# The rule
# ===========================================================================


def test_the_same_lines_for_the_same_customer_are_identical() -> None:
    assert strength_of(fingerprint(), fingerprint()) is InvoiceDuplicateStrength.IDENTICAL


def test_line_order_and_description_case_and_spacing_do_not_matter() -> None:
    a = fingerprint(lines=(line("Consultancy"), line("Travel", "1", "40.00")), net="990.00")
    b = fingerprint(
        lines=(line("  travel ", "1", "40.00"), line("CONSULTANCY")),
        net="990.00",
    )
    assert strength_of(a, b) is InvoiceDuplicateStrength.IDENTICAL


def test_different_lines_with_the_same_net_total_are_the_weaker_match() -> None:
    other = fingerprint(lines=(line("Consulting", "5", "190.00"),), net="950.00")
    assert strength_of(fingerprint(), other) is InvoiceDuplicateStrength.SAME_TOTAL


def test_a_different_net_total_and_different_lines_do_not_match() -> None:
    other = fingerprint(lines=(line("Consulting", "5", "191.00"),), net="955.00")
    assert strength_of(fingerprint(), other) is None


def test_decimal_exponents_do_not_defeat_a_match() -> None:
    """Decimal('950.0') == Decimal('950.00') - a total read back from
    Postgres and one computed in Python differ in exponent, not in value."""
    other = fingerprint(net="950.0", lines=(line("Other", "1", "950"),))
    assert strength_of(fingerprint(), other) is InvoiceDuplicateStrength.SAME_TOTAL


def test_a_different_customer_never_matches() -> None:
    other = fingerprint(customer_id=uuid.uuid4(), name="Jansen Bouw")
    assert strength_of(fingerprint(), other) is None


def test_the_same_customer_master_record_matches_under_a_different_name() -> None:
    """The snapshot name is frozen on each invoice, so a customer renamed
    between two invoices is still one customer."""
    other = fingerprint(name="De Vries Beheer B.V.")
    assert strength_of(fingerprint(), other) is InvoiceDuplicateStrength.IDENTICAL


def test_a_one_off_customer_matches_by_normalised_name() -> None:
    a = fingerprint(customer_id=None, name="Jansen Bouw")
    b = fingerprint(customer_id=None, name="  jansen   bouw. ")
    assert strength_of(a, b) is InvoiceDuplicateStrength.IDENTICAL


def test_a_typed_out_invoice_matches_the_same_customer_added_to_the_master_later() -> None:
    a = fingerprint(customer_id=None, name="Jansen Bouw")
    b = fingerprint(customer_id=CUSTOMER, name="Jansen Bouw")
    assert strength_of(a, b) is InvoiceDuplicateStrength.IDENTICAL


def test_a_name_that_normalises_to_nothing_matches_nothing() -> None:
    """ADR-034 section 5: two punctuation-only names would otherwise match
    every other invoice with a punctuation-only name."""
    a = fingerprint(customer_id=None, name="...")
    b = fingerprint(customer_id=None, name=":")
    assert strength_of(a, b) is None


def test_an_invoice_with_no_lines_matches_nothing() -> None:
    """Two empty drafts are two forms, not two claims."""
    empty = fingerprint(lines=(), net="0.00")
    assert strength_of(empty, empty) is None
    assert strength_of(fingerprint(), empty) is None


@pytest.mark.parametrize(
    ("days_apart", "matches"),
    [(0, True), (1, True), (WINDOW_DAYS, True), (WINDOW_DAYS + 1, False), (400, False)],
)
def test_the_window_is_inclusive_and_symmetric(days_apart: int, matches: bool) -> None:
    for direction in (1, -1):
        other = fingerprint(on=DAY + timedelta(days=direction * days_apart))
        assert (strength_of(fingerprint(), other) is not None) is matches


@given(
    left_days=st.integers(min_value=-90, max_value=90),
    right_days=st.integers(min_value=-90, max_value=90),
    left_qty=st.integers(min_value=1, max_value=5),
    right_qty=st.integers(min_value=1, max_value=5),
    same_customer=st.booleans(),
)
@settings(max_examples=200)
def test_strength_is_symmetric(
    left_days: int, right_days: int, left_qty: int, right_qty: int, same_customer: bool
) -> None:
    a = fingerprint(
        on=DAY + timedelta(days=left_days),
        lines=(line(quantity=str(left_qty)),),
        net=str(left_qty * 95),
    )
    b = fingerprint(
        customer_id=CUSTOMER if same_customer else uuid.uuid4(),
        name="De Vries Holding B.V." if same_customer else "Someone Else",
        on=DAY + timedelta(days=right_days),
        lines=(line(quantity=str(right_qty)),),
        net=str(right_qty * 95),
    )
    assert strength_of(a, b) == strength_of(b, a)


def test_normalise_name_is_adr_034s_rule() -> None:
    assert normalise_name("  De   Vries B.V. ") == "de vries b.v"
    assert normalise_name("...") == ""
    assert normalise_name("Jan's Cafe") == "jan's cafe"


# ===========================================================================
# find_duplicates
# ===========================================================================


def candidate(
    *,
    on: date = DAY,
    status: str = "issued",
    reference: str | None = "2026-1",
    **kwargs: object,
) -> DuplicateCandidate:
    return DuplicateCandidate(
        invoice_id=uuid.uuid4(),
        status=status,
        invoice_reference=reference,
        fingerprint=fingerprint(on=on, **kwargs),  # type: ignore[arg-type]
    )


def test_an_invoice_is_never_a_duplicate_of_itself() -> None:
    me = candidate()
    assert find_duplicates(fingerprint(), [me], subject_id=me.invoice_id) == []


def test_the_strongest_and_nearest_come_first() -> None:
    weak_near = candidate(on=DAY, lines=(line("Other", "1", "950"),))
    strong_far = candidate(on=DAY - timedelta(days=20))
    strong_near = candidate(on=DAY - timedelta(days=2))

    result = find_duplicates(fingerprint(), [weak_near, strong_far, strong_near])

    assert [w.invoice_id for w in result] == [
        strong_near.invoice_id,
        strong_far.invoice_id,
        weak_near.invoice_id,
    ]


def test_the_list_is_capped() -> None:
    many = [candidate(on=DAY - timedelta(days=i % 20)) for i in range(MAX_WARNINGS + 4)]
    assert len(find_duplicates(fingerprint(), many)) == MAX_WARNINGS


def test_a_warning_carries_the_other_invoices_own_facts() -> None:
    other = candidate(status="draft", reference=None, on=DAY - timedelta(days=3))
    (warning,) = find_duplicates(fingerprint(), [other])
    assert warning.invoice_id == other.invoice_id
    assert warning.status == "draft"
    assert warning.invoice_reference is None
    assert warning.invoice_date == DAY - timedelta(days=3)
    assert warning.net_total == Decimal("950.00")


# ===========================================================================
# The sentences - FR-LOC-001: both languages, every key
# ===========================================================================


def _warning(strength: InvoiceDuplicateStrength, status: str) -> InvoiceDuplicateWarning:
    return InvoiceDuplicateWarning(
        invoice_id=uuid.uuid4(),
        strength=strength,
        status=status,
        invoice_reference="2026-7" if status == "issued" else None,
        invoice_date=date(2026, 9, 2),
        net_total=Decimal("1234.50"),
    )


@pytest.mark.parametrize("language", list(Language))
@pytest.mark.parametrize("strength", list(InvoiceDuplicateStrength))
@pytest.mark.parametrize("status", ["issued", "draft"])
def test_every_warning_has_a_sentence_in_every_language(
    language: Language, strength: InvoiceDuplicateStrength, status: str
) -> None:
    message = message_for(_warning(strength, status), language)
    assert "1.234,50" in message
    assert "{" not in message


def test_an_issued_invoice_is_named_by_its_number_and_a_draft_is_not() -> None:
    issued = message_for(_warning(InvoiceDuplicateStrength.IDENTICAL, "issued"), Language.EN)
    draft = message_for(_warning(InvoiceDuplicateStrength.IDENTICAL, "draft"), Language.EN)
    assert "2026-7" in issued
    assert "draft" in draft.lower()


# ===========================================================================
# The service: warn, never block, and never cross a tenant
# ===========================================================================


def _invoice(
    administration_id: uuid.UUID, organization_id: uuid.UUID, **overrides: object
) -> SalesInvoice:
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        organization_id=organization_id,
        administration_id=administration_id,
        fiscal_year_id=uuid.uuid4(),
        status=InvoiceStatus.DRAFT,
        invoice_number=None,
        number_prefix=None,
        invoice_reference=None,
        invoice_date=DAY,
        supply_date=None,
        due_date=None,
        customer_name="De Vries Holding B.V.",
        customer_address="Damrak 70",
        customer_country="NL",
        customer_vat_number=None,
        customer_id=CUSTOMER,
        customer_language=Language.NL,
        credits_invoice_id=None,
        notes=None,
        issued_at=None,
        lines=(
            InvoiceLine(
                id=uuid.uuid4(),
                position=1,
                description="Consultancy",
                quantity=Decimal("10"),
                unit_price=Decimal("95.00"),
                discount_percent=Decimal("0"),
                vat_treatment="btw_21",
                role=TreatmentRole.STANDARD,
                line_net=Decimal("950.00"),
            ),
        ),
    )
    defaults.update(overrides)
    return SalesInvoice(**defaults)  # type: ignore[arg-type]


@dataclass
class FakeRepository:
    invoice: SalesInvoice
    pool: list[DuplicateCandidate] = field(default_factory=list)
    organization: uuid.UUID | None = None
    #: Every call to `duplicate_candidates`, so a test can assert what the
    #: service asked for.
    asked: list[dict[str, object]] = field(default_factory=list)

    async def get(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> SalesInvoice | None:
        if invoice_id == self.invoice.id and administration_id == self.invoice.administration_id:
            return self.invoice
        return None

    async def rates_on(self, *, treatments: object, on_date: date) -> dict[str, Decimal | None]:
        return {"btw_21": Decimal("21.00")}

    async def supplier(self, *, administration_id: uuid.UUID) -> None:
        return None

    async def effective_rules_on(self, *, on_date: date) -> EffectiveRules:
        vat_rules = InMemoryVatRulesRepository()
        vat_rules.load(load_document())
        return await vat_rules.rules_on(on_date=on_date)

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        return self.organization

    async def duplicate_candidates(self, **kwargs: object) -> list[DuplicateCandidate]:
        self.asked.append(kwargs)
        return list(self.pool)


@dataclass
class Setup:
    service: InvoicingService
    repository: FakeRepository
    user: uuid.UUID
    invoice: SalesInvoice

    async def view(self) -> InvoiceView:
        return await self.service.view(
            administration_id=self.invoice.administration_id,
            invoice_id=self.invoice.id,
            actor_user_id=self.user,
        )


def _setup(pool: list[DuplicateCandidate], **invoice_overrides: object) -> Setup:
    """An invoice in a world where the user may create invoices, and a
    repository whose duplicate pool is `pool`."""
    world = build_world()
    world.repository.assign(user_id=world.user, role="Bookkeeper", scope_id=world.acme_books)
    invoice = _invoice(world.acme_books, world.acme, **invoice_overrides)
    repository = FakeRepository(invoice=invoice, pool=pool, organization=world.acme)
    service = InvoicingService(
        repository=repository,  # type: ignore[arg-type]
        authorization=AuthorizationService(world.repository),
        audit_log=AuditLog(InMemoryAuditRepository()),
        customers=None,  # type: ignore[arg-type]
        posting=None,  # type: ignore[arg-type]
    )
    return Setup(service=service, repository=repository, user=world.user, invoice=invoice)


async def test_a_draft_with_a_matching_recent_invoice_carries_a_warning() -> None:
    other = candidate(on=DAY - timedelta(days=5))
    view = await _setup([other]).view()

    assert [w.invoice_id for w in view.duplicate_warnings] == [other.invoice_id]
    assert view.duplicate_warnings[0].strength is InvoiceDuplicateStrength.IDENTICAL


async def test_a_warning_does_not_change_whether_the_invoice_can_be_issued() -> None:
    """SI-12 warns and never blocks - the property that quietly becomes
    'blocks' the first time somebody adds `and not warnings` to a gate."""
    with_warning = await _setup([candidate()]).view()
    without = await _setup([]).view()

    assert with_warning.duplicate_warnings and not without.duplicate_warnings
    assert with_warning.statutory_failures == without.statutory_failures
    assert with_warning.can_be_issued == without.can_be_issued


def test_can_be_issued_ignores_warnings_by_construction() -> None:
    invoice = _invoice(uuid.uuid4(), uuid.uuid4())
    warning = _warning(InvoiceDuplicateStrength.IDENTICAL, "issued")
    view = InvoiceView(
        invoice=invoice,
        groups=(),
        net=Decimal("950.00"),
        vat=Decimal("199.50"),
        gross=Decimal("1149.50"),
        duplicate_warnings=(warning,),
    )
    assert view.can_be_issued is True


async def test_the_lookup_is_scoped_to_this_administration_and_customer_and_window() -> None:
    """The tenant predicate is passed to the repository, not applied after: an
    over-broad pool would be a cross-tenant read even if it were filtered."""
    setup = _setup([])
    await setup.view()

    (asked,) = setup.repository.asked
    assert asked["administration_id"] == setup.invoice.administration_id
    assert asked["exclude_invoice_id"] == setup.invoice.id
    assert asked["customer_id"] == CUSTOMER
    assert asked["customer_name"] == "De Vries Holding B.V."
    assert asked["since"] == DAY - timedelta(days=WINDOW_DAYS)
    assert asked["until"] == DAY + timedelta(days=WINDOW_DAYS)


async def test_an_issued_invoice_is_not_checked() -> None:
    setup = _setup([candidate()], status=InvoiceStatus.ISSUED, invoice_number=3)
    view = await setup.view()

    assert view.duplicate_warnings == ()
    assert setup.repository.asked == []


async def test_a_credit_note_is_not_checked() -> None:
    setup = _setup([candidate()], credits_invoice_id=uuid.uuid4())
    view = await setup.view()

    assert view.duplicate_warnings == ()
    assert setup.repository.asked == []


async def test_a_draft_with_no_lines_is_not_checked() -> None:
    """A form somebody is still filling in: no query, no warning."""
    setup = _setup([candidate()], lines=())
    view = await setup.view()

    assert view.duplicate_warnings == ()
    assert setup.repository.asked == []


async def test_a_candidate_that_only_the_over_approximate_query_would_fetch_is_dropped() -> None:
    """The repository's customer match is deliberately looser than the exact
    rule; `find_duplicates` is what decides."""
    stranger = candidate(customer_id=uuid.uuid4(), name="Jansen Bouw")
    view = await _setup([stranger]).view()

    assert view.duplicate_warnings == ()
