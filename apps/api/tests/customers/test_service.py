"""api.customers.service - FR-AR-006 end to end, without a database.

The real `AuthorizationService` over the authz test world, the real VIES
adapters, the real Peppol stub, and `InMemoryCustomerRepository` standing in
for migration 0039 (constraints included). What is faked is the SQL and
nothing else, so a rule that lives in the service is actually exercised here
rather than mocked away - the same posture `tests/expenses/test_posting.py`
takes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

import httpx
import pytest

from api.audit.log import AuditLog, AuditOutcome
from api.authz.matrix import ROLES
from api.authz.service import AuthorizationService
from api.customers.model import (
    CustomerAddressIncomplete,
    CustomerIsArchived,
    CustomerNotFound,
    DeliveryChannel,
    InvalidCustomerField,
    NotAuthorizedToManageCustomers,
)
from api.customers.peppol import DiscoveryStatus, UnconfiguredPeppolDirectory
from api.customers.service import CustomerDetails, CustomerService
from api.customers.vies import RestViesValidator, SyntaxOnlyViesValidator, ViesStatus
from api.i18n.language import Language
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository
from tests.support.fake_customer_repository import InMemoryCustomerRepository

DETAILS = CustomerDetails(
    name="De Vries Holding B.V.",
    address_line1="Damrak 70",
    postal_code="1012 LM",
    city="Amsterdam",
    country="NL",
    kvk_number="12345678",
    vat_number="NL123456789B01",
    invoice_email="facturen@devries.example",
)


@dataclass
class Harness:
    service: CustomerService
    repository: InMemoryCustomerRepository
    audit: InMemoryAuditRepository
    administration: uuid.UUID
    organization: uuid.UUID
    user: uuid.UUID


def harness(
    *,
    role: str = "Bookkeeper",
    vies_responds: object | None = None,
    vies_status: int = 200,
) -> Harness:
    world = build_world()
    # Scope taken from the role's own declaration rather than hard-coded: Owner
    # is organization-scoped and the other three are administration-scoped, and
    # a test that assumed one shape would silently stop granting anything when
    # given a role of the other.
    scope_type = next(r.scope_type for r in ROLES if r.name == role)
    world.repository.assign(
        user_id=world.user,
        role=role,
        scope_id=world.acme if scope_type == "organization" else world.acme_books,
    )

    repository = InMemoryCustomerRepository()
    repository.register_administration(world.acme_books, world.acme)
    audit = InMemoryAuditRepository()

    if vies_responds is None:
        validator: object = SyntaxOnlyViesValidator()
    else:

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(vies_status, json=vies_responds)

        validator = RestViesValidator(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )

    return Harness(
        service=CustomerService(
            repository=repository,  # type: ignore[arg-type]
            authorization=AuthorizationService(world.repository),
            audit_log=AuditLog(audit),  # type: ignore[arg-type]
            vies=validator,  # type: ignore[arg-type]
            peppol=UnconfiguredPeppolDirectory(),
        ),
        repository=repository,
        audit=audit,
        administration=world.acme_books,
        organization=world.acme,
        user=world.user,
    )


async def _create(h: Harness, details: CustomerDetails = DETAILS, **kwargs: object):  # type: ignore[no-untyped-def]
    return await h.service.create(
        administration_id=h.administration,
        actor_user_id=h.user,
        details=details,
        **kwargs,  # type: ignore[arg-type]
    )


# --- FR-AR-006's fields, stored as the requirement names them ---------------


async def test_a_customer_carries_every_field_fr_ar_006_names() -> None:
    h = harness()
    customer = await _create(
        h,
        replace(
            DETAILS,
            payment_terms_days=14,
            credit_limit=Decimal("50000.00"),
            delivery_channel=DeliveryChannel.POST,
            language=Language.EN,
        ),
    )

    assert customer.kvk_number == "12345678"
    assert customer.vat_number == "NL123456789B01"
    assert customer.payment_terms_days == 14
    assert customer.credit_limit == Decimal("50000.00")
    assert customer.delivery_channel is DeliveryChannel.POST
    assert customer.language is Language.EN


async def test_the_credit_limit_stays_a_decimal() -> None:
    """NFR-031, on the one monetary field this table has. A float here would
    be the rule undone in one cast.
    """
    h = harness()
    customer = await _create(h, replace(DETAILS, credit_limit=Decimal("12345.67")))

    assert isinstance(customer.credit_limit, Decimal)
    assert customer.credit_limit == Decimal("12345.67")


async def test_no_credit_limit_is_not_a_limit_of_zero() -> None:
    """Zero is a customer who may have no credit at all. None is a customer
    nobody has assessed. A screen showing them alike would put a trading halt
    on everybody in the second group.
    """
    h = harness()
    unassessed = await _create(h, replace(DETAILS, credit_limit=None))
    refused = await _create(h, replace(DETAILS, credit_limit=Decimal("0.00")))

    assert unassessed.credit_limit is None
    assert refused.credit_limit == Decimal(0)


async def test_the_vat_number_is_normalised_before_it_is_stored() -> None:
    """One number, one spelling. Two spellings would become two customers and
    two different VIES answers.
    """
    h = harness()
    customer = await _create(h, replace(DETAILS, vat_number="nl 1234.56.789.b01"))

    assert customer.vat_number == "NL123456789B01"


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    ("details", "field"),
    [
        (replace(DETAILS, name="   "), "name"),
        (replace(DETAILS, kvk_number="1234567"), "kvk_number"),
        (replace(DETAILS, kvk_number="123456789"), "kvk_number"),
        (replace(DETAILS, kvk_number="1234567X"), "kvk_number"),
        (replace(DETAILS, country="NLD"), "country"),
        (replace(DETAILS, country="12"), "country"),
        (replace(DETAILS, payment_terms_days=-1), "payment_terms_days"),
        (replace(DETAILS, payment_terms_days=400), "payment_terms_days"),
        (replace(DETAILS, credit_limit=Decimal("-1.00")), "credit_limit"),
        (
            replace(DETAILS, delivery_channel=DeliveryChannel.EMAIL, invoice_email=None),
            "invoice_email",
        ),
    ],
)
async def test_a_refused_value_names_the_field(details: CustomerDetails, field: str) -> None:
    """The database would refuse each of these too. Refused here so a person
    gets a sentence about `kvk_number` rather than a constraint name.
    """
    h = harness()
    with pytest.raises(InvalidCustomerField) as caught:
        await _create(h, details)

    assert caught.value.field == field


async def test_a_malformed_vat_number_is_recorded_rather_than_refused() -> None:
    """Deliberately unlike the fields above. A half-typed VAT number in a form
    somebody is still filling in should not lose them the rest of the record -
    it gets a SYNTAX_INVALID verdict instead.
    """
    h = harness()
    customer = await _create(h, replace(DETAILS, vat_number="NL12345B01"))

    assert customer.vat_number_status is ViesStatus.SYNTAX_INVALID
    assert not customer.vat_number_status.permits_zero_rating


async def test_a_customer_needs_no_address_to_be_saved() -> None:
    """FR-AR-006 does not require one, and a half-known customer is a
    legitimate record. The refusal lands where the document is made.
    """
    h = harness()
    customer = await _create(h, replace(DETAILS, address_line1=None, postal_code=None, city=None))

    assert not customer.address.is_complete


# --- FR-ONB-003: VIES -------------------------------------------------------


async def test_a_confirmed_number_is_recorded_with_its_date_and_name() -> None:
    h = harness(vies_responds={"isValid": True, "name": "De Vries Holding B.V."})
    customer = await _create(h)

    assert customer.vat_number_status is ViesStatus.VALID
    assert customer.has_confirmed_vat_number
    assert customer.vat_number_checked_at is not None
    assert customer.vat_number_checked_name == "De Vries Holding B.V."


async def test_a_vies_outage_does_not_fail_the_save() -> None:
    """NFR-026, and the decision the whole VIES module is built around. The
    customer is saved; the outage is a field on it.
    """
    h = harness(vies_responds={"isValid": True}, vies_status=503)
    customer = await _create(h)

    assert customer.vat_number_status is ViesStatus.UNAVAILABLE
    assert customer.name == "De Vries Holding B.V."
    assert not customer.has_confirmed_vat_number


async def test_a_denied_number_does_not_fail_the_save_either() -> None:
    """The customer may still be traded with - they simply cannot be
    zero-rated for an intra-Community supply.
    """
    h = harness(vies_responds={"isValid": False})
    customer = await _create(h)

    assert customer.vat_number_status is ViesStatus.INVALID
    assert not customer.has_confirmed_vat_number


async def test_a_customer_with_no_vat_number_is_unchecked() -> None:
    h = harness(vies_responds={"isValid": False})
    customer = await _create(h, replace(DETAILS, vat_number=None))

    assert customer.vat_number_status is ViesStatus.UNCHECKED
    assert customer.vat_number_checked_at is None


async def test_a_bulk_import_can_skip_the_consultation() -> None:
    """One VIES call per row against a free public service would be throttled
    into failure. The flag defaults to True so the safe behaviour is what a
    caller gets by not thinking about it.
    """
    h = harness(vies_responds={"isValid": True})
    customer = await _create(h, validate_vat_number=False)

    assert customer.vat_number_status is ViesStatus.UNCHECKED


async def test_revalidation_is_its_own_act() -> None:
    """An UNAVAILABLE verdict has to be retryable without editing the
    customer - otherwise a person clears and retypes a correct number to
    trigger a re-check, which teaches exactly the wrong habit.
    """
    h = harness(vies_responds={"isValid": True}, vies_status=503)
    customer = await _create(h)
    assert customer.vat_number_status is ViesStatus.UNAVAILABLE

    # The register comes back.
    h.service._vies = RestViesValidator(  # type: ignore[attr-defined]
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"isValid": True})
            )
        )
    )
    rechecked = await h.service.validate_vat_number(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
    )

    assert rechecked.vat_number_status is ViesStatus.VALID


async def test_changing_the_vat_number_clears_the_old_verdict() -> None:
    """A verdict about the previous number is not evidence about this one.
    Migration 0039's CASE ladder, and the service re-checks straight after.
    """
    h = harness(vies_responds={"isValid": True})
    customer = await _create(h)
    assert customer.vat_number_status is ViesStatus.VALID

    updated = await h.service.update(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
        details=replace(DETAILS, vat_number="DE123456789"),
    )

    assert updated.vat_number == "DE123456789"
    # Re-consulted, because the number changed.
    assert updated.vat_number_status is ViesStatus.VALID
    assert updated.vat_number_checked_at is not None


async def test_clearing_the_vat_number_clears_the_verdict_with_it() -> None:
    h = harness(vies_responds={"isValid": True})
    customer = await _create(h)

    updated = await h.service.update(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
        details=replace(DETAILS, vat_number=None),
    )

    assert updated.vat_number is None
    assert updated.vat_number_status is ViesStatus.UNCHECKED
    assert updated.vat_number_checked_at is None


async def test_an_unrelated_edit_does_not_re_consult_vies() -> None:
    """A call per save would be an outbound request against a rate-limited
    public service on every keystroke-batch - and the first failure would
    overwrite a good verdict with UNAVAILABLE.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"isValid": True})

    h = harness()
    h.service._vies = RestViesValidator(  # type: ignore[attr-defined]
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    customer = await _create(h)
    assert calls == 1

    await h.service.update(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
        details=replace(DETAILS, trade_name="De Vries"),
    )

    assert calls == 1, "an unrelated edit re-consulted VIES"


# --- FR-AR-006: Peppol discovery (P2 stub) ----------------------------------


async def test_discovery_reports_not_configured_and_writes_nothing() -> None:
    h = harness()
    customer = await _create(h)

    after, result = await h.service.discover_peppol_participant(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
    )

    assert result.status is DiscoveryStatus.NOT_CONFIGURED
    assert after.peppol_participant_id is None
    # Nothing looked, so nothing is recorded as having been looked at.
    assert after.peppol_checked_at is None


async def test_a_participant_id_can_still_be_entered_by_hand() -> None:
    """Discovery is not the only way an ID arrives - a customer can simply say
    what theirs is. The stub limits DISCOVERY, not the field.
    """
    h = harness()
    customer = await _create(h, replace(DETAILS, peppol_participant_id="0106:12345678"))

    assert customer.peppol_participant_id == "0106:12345678"
    assert customer.is_deliverable_over_peppol


async def test_preferring_peppol_without_an_id_is_not_deliverable() -> None:
    """The ordinary state today, and legible on the customer rather than
    discovered at delivery time.
    """
    h = harness()
    customer = await _create(
        h, replace(DETAILS, delivery_channel=DeliveryChannel.PEPPOL, invoice_email=None)
    )

    assert customer.delivery_channel is DeliveryChannel.PEPPOL
    assert not customer.is_deliverable_over_peppol


# --- archiving --------------------------------------------------------------


async def test_an_archived_customer_is_still_readable() -> None:
    """Every invoice ever raised for them points at this row, and CMP-001
    keeps those for seven years.
    """
    h = harness()
    customer = await _create(h)

    await h.service.set_archived(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
        archived=True,
    )
    read_back = await h.service.get(
        administration_id=h.administration, customer_id=customer.id, actor_user_id=h.user
    )

    assert read_back.is_archived


async def test_an_archived_customer_cannot_be_edited_or_invoiced() -> None:
    h = harness()
    customer = await _create(h)
    await h.service.set_archived(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
        archived=True,
    )

    with pytest.raises(CustomerIsArchived):
        await h.service.update(
            administration_id=h.administration,
            customer_id=customer.id,
            actor_user_id=h.user,
            details=DETAILS,
        )
    with pytest.raises(CustomerIsArchived):
        await h.service.snapshot_for_invoice(
            administration_id=h.administration,
            customer_id=customer.id,
            actor_user_id=h.user,
            invoice_date=date(2026, 9, 9),
        )


async def test_archiving_twice_keeps_the_original_date() -> None:
    """When trading stopped is a fact; re-archiving is not a new one."""
    h = harness()
    customer = await _create(h)

    first = await h.service.set_archived(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
        archived=True,
    )
    again = await h.service.set_archived(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
        archived=True,
    )

    assert again.archived_at == first.archived_at


async def test_an_archived_customer_is_out_of_the_picker_but_findable() -> None:
    h = harness()
    customer = await _create(h)
    await h.service.set_archived(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
        archived=True,
    )

    default = await h.service.search(administration_id=h.administration, actor_user_id=h.user)
    including = await h.service.search(
        administration_id=h.administration, actor_user_id=h.user, include_archived=True
    )

    assert [c.id for c in default] == []
    assert [c.id for c in including] == [customer.id]


async def test_restoring_brings_a_customer_back() -> None:
    h = harness()
    customer = await _create(h)
    await h.service.set_archived(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
        archived=True,
    )

    restored = await h.service.set_archived(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
        archived=False,
    )

    assert not restored.is_archived


# --- populating an invoice --------------------------------------------------


async def test_the_snapshot_carries_what_0037_freezes() -> None:
    h = harness(vies_responds={"isValid": True})
    customer = await _create(h, replace(DETAILS, payment_terms_days=14, language=Language.EN))

    snapshot = await h.service.snapshot_for_invoice(
        administration_id=h.administration,
        customer_id=customer.id,
        actor_user_id=h.user,
        invoice_date=date(2026, 9, 9),
    )

    assert snapshot.customer_id == customer.id
    assert snapshot.name == "De Vries Holding B.V."
    assert snapshot.address == "Damrak 70\n1012 LM  Amsterdam"
    assert snapshot.country == "NL"
    assert snapshot.vat_number == "NL123456789B01"
    # FR-TPL-013: the RECIPIENT's language.
    assert snapshot.language is Language.EN
    # FR-AR-006's payment terms, turned into the date the document carries.
    assert snapshot.due_date == date(2026, 9, 23)


@pytest.mark.parametrize(
    ("terms", "expected"),
    [
        (0, date(2026, 9, 9)),
        (14, date(2026, 9, 23)),
        (30, date(2026, 10, 9)),
        (60, date(2026, 11, 8)),
    ],
)
async def test_payment_terms_are_calendar_days(terms: int, expected: date) -> None:
    """What "30 dagen netto" means, and what BW art. 6:119a counts. Business
    days would need a holiday calendar per jurisdiction and no Dutch payment
    term is expressed that way.
    """
    h = harness()
    customer = await _create(h, replace(DETAILS, payment_terms_days=terms))

    assert customer.due_date_for(date(2026, 9, 9)) == expected


async def test_an_incomplete_address_refuses_at_the_document_not_at_the_save() -> None:
    h = harness()
    customer = await _create(h, replace(DETAILS, city=None))

    with pytest.raises(CustomerAddressIncomplete) as caught:
        await h.service.snapshot_for_invoice(
            administration_id=h.administration,
            customer_id=customer.id,
            actor_user_id=h.user,
            invoice_date=date(2026, 9, 9),
        )

    assert "city" in caught.value.missing


# --- authorization and tenancy ----------------------------------------------


async def test_a_role_without_manage_customer_is_refused() -> None:
    """§8.4's Expense Submitter cannot maintain customers. Default deny
    (IAM-030): the absence of a grant is a denial.
    """
    h = harness(role="Expense Submitter")

    with pytest.raises(NotAuthorizedToManageCustomers):
        await _create(h)


@pytest.mark.parametrize("role", ["Owner", "Accountant", "Bookkeeper", "Invoicer"])
async def test_the_roles_that_manage_customers_can(role: str) -> None:
    """IAM-101's "Invoice and capture" profile and §8.4's Invoicer both name
    this capability. ADR-012: reused from the matrix, not invented here.
    """
    h = harness(role=role)
    customer = await _create(h)

    assert customer.name == "De Vries Holding B.V."


async def test_reads_need_the_permission_too() -> None:
    """There is no `view customer` in Appendix A and none is invented. The
    consequence - a Viewer cannot list customers - is real, and recorded in
    ADR-038 rather than papered over.
    """
    h = harness(role="Viewer")

    with pytest.raises(NotAuthorizedToManageCustomers):
        await h.service.search(administration_id=h.administration, actor_user_id=h.user)


async def test_another_administrations_customer_is_not_found() -> None:
    """Indistinguishable from "does not exist" - RLS filters it out either way
    and the service must not become the oracle that tells them apart.
    """
    h = harness()
    customer = await _create(h)

    with pytest.raises(CustomerNotFound):
        await h.service.get(
            administration_id=uuid.uuid4(),
            customer_id=customer.id,
            actor_user_id=h.user,
        )


# --- IAM-090 ----------------------------------------------------------------


async def test_creating_a_customer_is_audited_without_the_vat_number() -> None:
    """The verdict, not the number. A VAT number is a tax identifier and the
    audit log is exportable (IAM-094); the resource id is enough to find the
    row.
    """
    h = harness(vies_responds={"isValid": True})
    customer = await _create(h)

    entries = await h.audit.search(organization_id=h.organization)
    created = [e for e in entries if e.action == "create_customer"]
    assert len(created) == 1
    assert created[0].resource_type == "customer"
    assert created[0].resource_id == customer.id
    assert created[0].detail["vat_number_status"] == "valid"
    assert "NL123456789B01" not in str(created[0].detail)


async def test_a_vies_consultation_is_audited_whatever_the_answer() -> None:
    """An INVALID verdict on a customer being zero-rated is exactly the
    sequence an auditor reconstructs afterwards, and UNAVAILABLE explains why
    a number was never confirmed.
    """
    h = harness(vies_responds={"isValid": True}, vies_status=503)
    await _create(h)

    entries = await h.audit.search(organization_id=h.organization)
    checks = [e for e in entries if e.action == "validate_customer_vat_number"]
    assert len(checks) == 1
    assert checks[0].outcome is AuditOutcome.FAILURE
    assert checks[0].detail["vat_number_status"] == "unavailable"


async def test_a_denial_is_audited_as_denied() -> None:
    h = harness(role="Expense Submitter")

    with pytest.raises(NotAuthorizedToManageCustomers):
        await _create(h)

    entries = await h.audit.search(organization_id=h.organization)
    denials = [e for e in entries if e.outcome is AuditOutcome.DENIED]
    assert denials, "a refused attempt left no audit trail (IAM-090)"


async def test_archiving_and_restoring_are_different_audit_actions() -> None:
    """An auditor reading the log needs to see resuming trade with a
    counterparty as its own act, not as a second copy of "archived".
    """
    h = harness()
    customer = await _create(h)

    for archived in (True, False):
        await h.service.set_archived(
            administration_id=h.administration,
            customer_id=customer.id,
            actor_user_id=h.user,
            archived=archived,
        )

    actions = [e.action for e in await h.audit.search(organization_id=h.organization)]
    assert "archive_customer" in actions
    assert "restore_customer" in actions


# --- search -----------------------------------------------------------------


async def test_search_ranks_exact_then_prefix_then_substring() -> None:
    """FR-FRM-000's ordering. Type-then-Enter only works if position 1 is
    predictable.
    """
    h = harness()
    for name in ("Bakker Bouw B.V.", "Bakker", "De Bakkerij"):
        await _create(h, replace(DETAILS, name=name))

    found = await h.service.search(
        administration_id=h.administration, actor_user_id=h.user, query="Bakker"
    )

    assert [c.name for c in found] == ["Bakker", "Bakker Bouw B.V.", "De Bakkerij"]


async def test_search_by_kvk_number() -> None:
    h = harness()
    await _create(h, replace(DETAILS, name="Alpha", kvk_number="11111111"))
    await _create(h, replace(DETAILS, name="Beta", kvk_number="22222222"))

    found = await h.service.search(
        administration_id=h.administration, actor_user_id=h.user, query="2222"
    )

    assert [c.name for c in found] == ["Beta"]
