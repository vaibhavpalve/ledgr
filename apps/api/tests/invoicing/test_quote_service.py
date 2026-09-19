"""api.invoicing.quote_service - SI-08's orchestration (ADR-075).

The rules are in test_quotes.py. What is under test here is the wiring: that only a
draft is editable, that an expired offer cannot be accepted, that a conversion copies
the lines exactly and yields a DRAFT, that converting twice yields one invoice, and
that a failed conversion leaves the quote accepted.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from api.audit.log import AuditLog
from api.authz.service import AuthorizationService
from api.customers.model import CustomerAddressIncomplete, CustomerIsArchived, CustomerNotFound
from api.invoicing.model import NotAuthorizedToInvoice
from api.invoicing.quote_service import (
    Quote,
    QuoteConversionFailed,
    QuoteDraft,
    QuoteExpired,
    QuoteNotEditable,
    QuoteNotFound,
    QuoteService,
    QuoteTransitionInvalid,
)
from api.invoicing.quotes import QuoteInvalid, QuoteKind, QuoteLine, QuoteStatus
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository

pytestmark = pytest.mark.anyio

D = Decimal
TODAY = date(2026, 9, 19)
CUSTOMER = uuid.uuid4()
YEAR = uuid.uuid4()


def draft(**overrides: object) -> QuoteDraft:
    defaults: dict[str, object] = dict(
        kind=QuoteKind.QUOTE,
        customer_id=CUSTOMER,
        subject="Nieuwe website",
        valid_until=TODAY + timedelta(days=30),
        notes="Betaling in twee termijnen",
        lines=(
            QuoteLine(
                description="Ontwerp",
                quantity=D("10"),
                unit_price=D("95.0000"),
                vat_treatment="btw_21",
                discount_percent=D("5"),
            ),
            QuoteLine(
                description="Hosting",
                quantity=D("1"),
                unit_price=D("0.0350"),
                vat_treatment="btw_9",
            ),
        ),
    )
    defaults.update(overrides)
    return QuoteDraft(**defaults)  # type: ignore[arg-type]


# --- fakes --------------------
@dataclass
class FakeInvoicing:
    drafts: list[dict[str, object]] = field(default_factory=list)
    raises: Exception | None = None

    async def create_draft(self, **kwargs: object) -> SimpleNamespace:
        if self.raises is not None:
            raise self.raises
        self.drafts.append(kwargs)
        return SimpleNamespace(invoice=SimpleNamespace(id=uuid.uuid4()))


@dataclass
class FakeRepository:
    organization_id: uuid.UUID
    quotes: dict[uuid.UUID, Quote] = field(default_factory=dict)
    customers: set[uuid.UUID] = field(default_factory=lambda: {CUSTOMER})
    fiscal_years: bool = True
    counters: dict[QuoteKind, int] = field(default_factory=dict)
    lose_conversion_race: bool = False
    rolled_back: int = 0

    async def organization_of(self, *, administration_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return self.organization_id

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        try:
            yield
        except Exception:
            self.rolled_back += 1
            raise

    async def customer_exists(self, *, administration_id, customer_id) -> bool:  # type: ignore[no-untyped-def]
        return customer_id in self.customers

    async def create(self, *, administration_id, user_id, draft):  # type: ignore[no-untyped-def]
        number = self.counters.get(draft.kind, 0) + 1
        self.counters[draft.kind] = number
        prefix = "OF-" if draft.kind is QuoteKind.QUOTE else "OB-"
        quote = Quote(
            id=uuid.uuid4(),
            administration_id=administration_id,
            kind=draft.kind,
            quote_number=number,
            reference=f"{prefix}{number:04d}",
            customer_id=draft.customer_id,
            subject=draft.subject,
            valid_until=draft.valid_until,
            notes=draft.notes,
            status=QuoteStatus.DRAFT,
            lines=draft.lines,
        )
        self.quotes[quote.id] = quote
        return quote

    async def replace(self, *, administration_id, quote_id, draft):  # type: ignore[no-untyped-def]
        updated = replace(
            self.quotes[quote_id],
            customer_id=draft.customer_id,
            subject=draft.subject,
            valid_until=draft.valid_until,
            notes=draft.notes,
            lines=draft.lines,
        )
        self.quotes[quote_id] = updated
        return updated

    async def get(self, *, administration_id, quote_id):  # type: ignore[no-untyped-def]
        return self.quotes.get(quote_id)

    async def list(self, *, administration_id, status):  # type: ignore[no-untyped-def]
        return [q for q in self.quotes.values() if status is None or q.status is status]

    async def transition(  # type: ignore[no-untyped-def]
        self,
        *,
        administration_id,
        quote_id,
        expected,
        to,
        user_id,
        accepted_by_name=None,
        acceptance_reference=None,
        decline_reason=None,
    ):
        current = self.quotes[quote_id]
        if current.status not in expected:
            return False
        now = datetime.now(UTC)
        self.quotes[quote_id] = replace(
            current,
            status=to,
            sent_at=now if to is QuoteStatus.SENT else current.sent_at,
            accepted_at=now if to is QuoteStatus.ACCEPTED else current.accepted_at,
            accepted_by_name=accepted_by_name or current.accepted_by_name,
            acceptance_reference=acceptance_reference or current.acceptance_reference,
            declined_at=now if to is QuoteStatus.DECLINED else current.declined_at,
            decline_reason=decline_reason or current.decline_reason,
            cancelled_at=now if to is QuoteStatus.CANCELLED else current.cancelled_at,
        )
        return True

    async def extend_validity(self, *, administration_id, quote_id, valid_until):  # type: ignore[no-untyped-def]
        self.quotes[quote_id] = replace(self.quotes[quote_id], valid_until=valid_until)

    async def fiscal_year_for(self, *, administration_id, on):  # type: ignore[no-untyped-def]
        return YEAR if self.fiscal_years else None

    async def mark_converted(self, *, administration_id, quote_id, invoice_id):  # type: ignore[no-untyped-def]
        current = self.quotes[quote_id]
        if self.lose_conversion_race or current.status is not QuoteStatus.ACCEPTED:
            return False
        self.quotes[quote_id] = replace(
            current,
            status=QuoteStatus.CONVERTED,
            converted_invoice_id=invoice_id,
            converted_at=datetime.now(UTC),
        )
        return True


@dataclass
class Setup:
    service: QuoteService
    repository: FakeRepository
    invoicing: FakeInvoicing
    audit: InMemoryAuditRepository
    user: uuid.UUID
    administration: uuid.UUID

    async def create(self, **overrides: object) -> Quote:
        return await self.service.create(
            administration_id=self.administration,
            actor_user_id=self.user,
            draft=draft(**overrides),
            today=TODAY,
        )

    async def accepted(self, **overrides: object) -> Quote:
        quote = await self.create(**overrides)
        await self.service.accept(
            administration_id=self.administration,
            quote_id=quote.id,
            actor_user_id=self.user,
            today=TODAY,
        )
        return quote

    async def call(self, method: str, quote_id: uuid.UUID, today: date = TODAY, **kw: object):  # type: ignore[no-untyped-def]
        return await getattr(self.service, method)(
            administration_id=self.administration,
            quote_id=quote_id,
            actor_user_id=self.user,
            today=today,
            **kw,
        )


def _setup(role: str = "Accountant") -> Setup:
    world = build_world()
    world.repository.assign(user_id=world.user, role=role, scope_id=world.acme_books)
    repository = FakeRepository(organization_id=world.acme)
    invoicing = FakeInvoicing()
    audit = InMemoryAuditRepository()
    service = QuoteService(
        repository=repository,  # type: ignore[arg-type]
        invoicing=invoicing,  # type: ignore[arg-type]
        authorization=AuthorizationService(world.repository),
        audit_log=AuditLog(audit),
    )
    return Setup(service, repository, invoicing, audit, world.user, world.acme_books)


def _last_audit(setup: Setup):  # type: ignore[no-untyped-def]
    return setup.audit._entries[-1]


# --- defining --------------------
async def test_a_quote_is_created_as_a_numbered_draft() -> None:
    setup = _setup()
    quote = await setup.create()

    assert quote.status is QuoteStatus.DRAFT
    assert quote.reference == "OF-0001"
    assert _last_audit(setup).action == "create_quote"


async def test_quotes_and_order_confirmations_have_their_own_numbering() -> None:
    setup = _setup()
    first = await setup.create()
    second = await setup.create()
    confirmation = await setup.create(kind=QuoteKind.ORDER_CONFIRMATION)

    assert [first.reference, second.reference, confirmation.reference] == [
        "OF-0001",
        "OF-0002",
        "OB-0001",
    ]


async def test_an_unusable_quote_is_refused_and_a_stranger_is_a_clean_not_found() -> None:
    setup = _setup()
    with pytest.raises(QuoteInvalid):
        await setup.create(lines=())
    with pytest.raises(QuoteInvalid):
        await setup.create(valid_until=TODAY - timedelta(days=1))
    with pytest.raises(CustomerNotFound):
        await setup.create(customer_id=uuid.uuid4())
    assert setup.repository.quotes == {}


async def test_a_role_without_create_sales_invoice_cannot_touch_quotes() -> None:
    setup = _setup("Expense Submitter")
    with pytest.raises(NotAuthorizedToInvoice):
        await setup.create()


async def test_an_unknown_quote_is_not_found() -> None:
    setup = _setup()
    with pytest.raises(QuoteNotFound):
        await setup.service.get(
            administration_id=setup.administration, quote_id=uuid.uuid4(), actor_user_id=setup.user
        )


# --- editing --------------------
async def test_a_draft_can_be_replaced_and_keeps_its_kind_and_number() -> None:
    setup = _setup()
    quote = await setup.create()

    replaced = await setup.service.update(
        administration_id=setup.administration,
        quote_id=quote.id,
        actor_user_id=setup.user,
        # Asking for a different kind on an edit is ignored: the kind is the
        # document's identity ('OF-' or 'OB-').
        draft=draft(subject="Nieuw onderwerp", kind=QuoteKind.ORDER_CONFIRMATION),
        today=TODAY,
    )

    assert replaced.subject == "Nieuw onderwerp"
    assert replaced.kind is QuoteKind.QUOTE and replaced.reference == "OF-0001"


@pytest.mark.parametrize("move", ["mark_sent", "accept"])
async def test_a_quote_that_has_gone_out_can_no_longer_be_edited(move: str) -> None:
    """Once it has gone to the customer it is what they were told."""
    setup = _setup()
    quote = await setup.create()
    await setup.call(move, quote.id)

    with pytest.raises(QuoteNotEditable):
        await setup.service.update(
            administration_id=setup.administration,
            quote_id=quote.id,
            actor_user_id=setup.user,
            draft=draft(subject="Stiekem aangepast"),
            today=TODAY,
        )


# --- the lifecycle --------------------
async def test_sending_then_accepting_records_who_and_what() -> None:
    setup = _setup()
    quote = await setup.create()

    sent = await setup.call("mark_sent", quote.id)
    accepted = await setup.call(
        "accept", quote.id, accepted_by_name="  J. de Vries ", acceptance_reference="PO-4471"
    )

    assert sent.status is QuoteStatus.SENT and sent.sent_at is not None
    assert accepted.status is QuoteStatus.ACCEPTED and accepted.accepted_at is not None
    assert accepted.accepted_by_name == "J. de Vries"  # trimmed
    assert accepted.acceptance_reference == "PO-4471"


async def test_the_audit_says_details_were_given_but_not_what_they_were() -> None:
    """A customer's own words and PO number are not something to accumulate under the
    audit log's own retention."""
    setup = _setup()
    quote = await setup.create()
    await setup.call(
        "accept", quote.id, accepted_by_name="J. de Vries", acceptance_reference="PO-1"
    )

    detail = _last_audit(setup).detail
    assert detail["acceptance_details_given"] is True
    assert "J. de Vries" not in str(detail) and "PO-1" not in str(detail)


async def test_a_blank_acceptance_field_is_none() -> None:
    setup = _setup()
    quote = await setup.create()
    accepted = await setup.call("accept", quote.id, accepted_by_name="   ", acceptance_reference="")
    assert accepted.accepted_by_name is None and accepted.acceptance_reference is None


async def test_declining_is_only_possible_for_a_quote_that_is_out() -> None:
    setup = _setup()
    draft_quote = await setup.create()
    with pytest.raises(QuoteTransitionInvalid):
        await setup.call("decline", draft_quote.id)

    sent = await setup.create()
    await setup.call("mark_sent", sent.id)
    declined = await setup.call("decline", sent.id, reason="Te duur")
    assert declined.status is QuoteStatus.DECLINED and declined.decline_reason == "Te duur"


async def test_a_terminal_quote_cannot_move_again() -> None:
    setup = _setup()
    quote = await setup.create()
    await setup.call("cancel", quote.id)

    for move in ("mark_sent", "accept", "decline", "cancel"):
        with pytest.raises(QuoteTransitionInvalid):
            await setup.call(move, quote.id)


async def test_an_accepted_quote_can_still_be_cancelled_if_the_customer_withdraws() -> None:
    setup = _setup()
    quote = await setup.accepted()
    cancelled = await setup.call("cancel", quote.id)
    assert cancelled.status is QuoteStatus.CANCELLED


async def test_somebody_else_moving_it_first_is_reported_not_overwritten() -> None:
    setup = _setup()
    quote = await setup.create()
    # Somebody else moved it between our read and our write.
    setup.repository.quotes[quote.id] = replace(quote, status=QuoteStatus.CANCELLED)
    original_get = setup.repository.get
    stale = quote

    async def stale_then_fresh(*, administration_id, quote_id):  # type: ignore[no-untyped-def]
        setup.repository.get = original_get  # type: ignore[method-assign]
        return stale

    setup.repository.get = stale_then_fresh  # type: ignore[method-assign]

    with pytest.raises(QuoteTransitionInvalid) as caught:
        await setup.call("mark_sent", quote.id)
    assert caught.value.current is QuoteStatus.CANCELLED


# --- expiry --------------------
async def test_an_expired_offer_cannot_be_sent_or_accepted() -> None:
    setup = _setup()
    quote = await setup.create(valid_until=TODAY)
    later = TODAY + timedelta(days=1)

    with pytest.raises(QuoteExpired):
        await setup.call("mark_sent", quote.id, today=later)
    with pytest.raises(QuoteExpired):
        await setup.call("accept", quote.id, today=later)


async def test_the_last_valid_day_can_still_be_accepted() -> None:
    setup = _setup()
    quote = await setup.create(valid_until=TODAY)
    accepted = await setup.call("accept", quote.id, today=TODAY)
    assert accepted.status is QuoteStatus.ACCEPTED


async def test_an_expired_quote_can_still_be_declined_or_cancelled() -> None:
    setup = _setup()
    quote = await setup.create(valid_until=TODAY)
    cancelled = await setup.call("cancel", quote.id, today=TODAY + timedelta(days=5))
    assert cancelled.status is QuoteStatus.CANCELLED


async def test_extending_the_validity_revives_an_expired_quote() -> None:
    setup = _setup()
    quote = await setup.create(valid_until=TODAY)
    later = TODAY + timedelta(days=10)
    assert quote.is_expired(later)

    extended = await setup.service.extend_validity(
        administration_id=setup.administration,
        quote_id=quote.id,
        actor_user_id=setup.user,
        valid_until=later + timedelta(days=20),
        today=later,
    )

    assert not extended.is_expired(later)
    assert (await setup.call("accept", quote.id, today=later)).status is QuoteStatus.ACCEPTED


async def test_validity_can_only_be_extended_never_shortened_and_only_while_out() -> None:
    setup = _setup()
    quote = await setup.create(valid_until=TODAY + timedelta(days=30))

    with pytest.raises(QuoteInvalid):
        await setup.service.extend_validity(
            administration_id=setup.administration,
            quote_id=quote.id,
            actor_user_id=setup.user,
            valid_until=TODAY + timedelta(days=10),  # earlier than the current date
            today=TODAY,
        )
    await setup.call("accept", quote.id)
    with pytest.raises(QuoteNotEditable):
        await setup.service.extend_validity(
            administration_id=setup.administration,
            quote_id=quote.id,
            actor_user_id=setup.user,
            valid_until=TODAY + timedelta(days=90),
            today=TODAY,
        )


async def test_an_accepted_quote_is_never_reported_expired() -> None:
    """It was accepted in time; the date passing afterwards does not un-accept it."""
    setup = _setup()
    quote = await setup.accepted(valid_until=TODAY)
    current = setup.repository.quotes[quote.id]
    assert not current.is_expired(TODAY + timedelta(days=365))


# --- converting --------------------
async def test_only_an_accepted_quote_can_be_converted() -> None:
    setup = _setup()
    quote = await setup.create()
    with pytest.raises(QuoteTransitionInvalid):
        await setup.call("convert", quote.id)

    await setup.call("mark_sent", quote.id)
    with pytest.raises(QuoteTransitionInvalid):
        await setup.call("convert", quote.id)
    assert setup.invoicing.drafts == []


async def test_a_conversion_creates_a_draft_with_exactly_the_quoted_lines() -> None:
    setup = _setup()
    quote = await setup.accepted()

    result = await setup.call("convert", quote.id)

    (created,) = setup.invoicing.drafts
    # The same lines, unchanged: quantity, price, discount and treatment.
    assert [
        (x.description, x.quantity, x.unit_price, x.discount_percent, x.vat_treatment)
        for x in created["lines"]  # type: ignore[attr-defined]
    ] == [
        ("Ontwerp", D("10"), D("95.0000"), D("5"), "btw_21"),
        ("Hosting", D("1"), D("0.0350"), D("0"), "btw_9"),
    ]
    assert created["customer_id"] == CUSTOMER
    assert created["notes"] == "Betaling in twee termijnen"
    assert created["invoice_date"] == TODAY and created["fiscal_year_id"] == YEAR
    assert not result.already_converted
    assert result.quote.status is QuoteStatus.CONVERTED
    assert result.quote.converted_invoice_id == result.invoice_id


async def test_the_invoice_date_can_be_chosen() -> None:
    setup = _setup()
    quote = await setup.accepted()
    await setup.call("convert", quote.id, invoice_date=date(2026, 10, 1))
    assert setup.invoicing.drafts[0]["invoice_date"] == date(2026, 10, 1)


async def test_converting_twice_returns_the_same_invoice_and_creates_one() -> None:
    """A retried request must not invoice the customer twice (NFR-032)."""
    setup = _setup()
    quote = await setup.accepted()

    first = await setup.call("convert", quote.id)
    second = await setup.call("convert", quote.id)

    assert second.invoice_id == first.invoice_id
    assert second.already_converted and not first.already_converted
    assert len(setup.invoicing.drafts) == 1


async def test_losing_the_race_returns_the_winners_invoice_and_rolls_back_our_draft() -> None:
    setup = _setup()
    quote = await setup.accepted()
    winner = uuid.uuid4()
    setup.repository.lose_conversion_race = True

    original_get = setup.repository.get
    calls = 0

    async def get_after_race(*, administration_id, quote_id):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        found = await original_get(administration_id=administration_id, quote_id=quote_id)
        assert found is not None
        if calls >= 2:  # after our failed mark, the quote shows the winner's conversion
            return replace(found, status=QuoteStatus.CONVERTED, converted_invoice_id=winner)
        return found

    setup.repository.get = get_after_race  # type: ignore[method-assign]

    result = await setup.call("convert", quote.id)

    assert result.invoice_id == winner and result.already_converted
    assert setup.repository.rolled_back == 1  # our draft went with the savepoint


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (CustomerNotFound("gone"), "customer_not_found"),
        (CustomerIsArchived("archived"), "customer_archived"),
        (CustomerAddressIncomplete(("city",)), "customer_details_incomplete"),
    ],
)
async def test_a_failed_conversion_leaves_the_quote_accepted(error: Exception, code: str) -> None:
    setup = _setup()
    quote = await setup.accepted()
    setup.invoicing.raises = error

    with pytest.raises(QuoteConversionFailed) as caught:
        await setup.call("convert", quote.id)

    assert caught.value.code == code
    assert setup.repository.quotes[quote.id].status is QuoteStatus.ACCEPTED  # retryable

    setup.invoicing.raises = None  # the customer is fixed
    assert (await setup.call("convert", quote.id)).quote.status is QuoteStatus.CONVERTED


async def test_a_date_in_no_fiscal_year_fails_and_leaves_the_quote_accepted() -> None:
    setup = _setup()
    quote = await setup.accepted()
    setup.repository.fiscal_years = False

    with pytest.raises(QuoteConversionFailed) as caught:
        await setup.call("convert", quote.id)

    assert caught.value.code == "no_fiscal_year"
    assert setup.invoicing.drafts == []
    assert setup.repository.quotes[quote.id].status is QuoteStatus.ACCEPTED


async def test_a_conversion_is_audited() -> None:
    setup = _setup()
    quote = await setup.accepted()
    result = await setup.call("convert", quote.id)

    entry = _last_audit(setup)
    assert entry.action == "convert_quote"
    assert entry.detail["invoice_id"] == str(result.invoice_id)
    assert entry.detail["lines"] == 2


async def test_the_service_cannot_issue_send_or_post_anything() -> None:
    """A quote turning into a numbered, posted invoice with no human looking at it is
    the bulk-click problem again. The service is built without any way to."""
    import inspect

    parameters = set(inspect.signature(QuoteService.__init__).parameters)
    assert parameters == {"self", "repository", "invoicing", "authorization", "audit_log"}
    assert not hasattr(FakeInvoicing(), "issue")  # and the fake would fail loudly
