"""IAM-090: every path that exists actually emits an audit event.

test_audit_coverage.py enforces that a mutating ROUTE declares a category.
This file checks the other half - that the service-layer paths which have no
route yet still record, and that the categories which record nothing do so
because the feature does not exist rather than because someone forgot.

The distinction matters: a category with no producer is either a gap or a
feature that has not been built, and only one of those is acceptable. WIRING
below states which is which, and the tests hold it to it.
"""

from __future__ import annotations

import uuid

import pytest

from api.audit.log import AuditCategory, AuditLog, AuditOutcome
from api.audit.trail import AuditTrail
from api.authz.engagement_revocation import EngagementRevocationService
from api.authz.firm_staff import FirmStaffAccessService, NoActiveEngagementError
from api.authz.profiles import BUILTIN_PROFILES, ClientAccessProfileService
from api.authz.service import (
    AuthorizationService,
    ClientRightsFloorError,
    PrivilegeEscalationError,
)
from api.authz.sod import SegregationOfDutiesService, SodRule
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository
from tests.support.fake_firm_access_repository import InMemoryFirmAccessRepository
from tests.support.fake_firm_staff_repository import InMemoryFirmStaffRepository
from tests.support.fake_profile_repository import InMemoryProfileRepository
from tests.support.fake_sod_repository import InMemorySodRepository

STAFF = uuid.uuid4()


# IAM-090's nine categories, and where each one is produced today. A category
# whose producer is None must name the requirement that will build it - that
# is what turns "nothing writes this yet" from an oversight into a decision
# somebody can check.
WIRING: dict[AuditCategory, str | None] = {
    AuditCategory.AUTHENTICATION: "api.mfa_middleware + api.audit.middleware",
    AuditCategory.PERMISSION_CHANGE: "api.authz.service, firm_staff, engagement_revocation",
    AuditCategory.FINANCIAL_READ: "api.main GET /v1/administrations/{id}",
    AuditCategory.CONFIGURATION: "api.authz.profiles, api.authz.sod, api.main switcher",
    AuditCategory.POSTING: "api.ledger.service",
    # api.ledger.periods, and only the ledger half: marking a period filed
    # (which applies FR-GL-007's hard lock) and the suppletie flow that
    # corrects one. The RETURN itself - rubriek values, the Digipoort
    # submission, the receipt - is FR-VAT and is not built, so this category
    # is wired but not yet complete.
    AuditCategory.FILING: "api.ledger.periods",
    # No feature to produce these yet. Each names the requirement that will.
    AuditCategory.EXPORT: None,  # FR-RPT export, not built
    AuditCategory.APPROVAL: None,  # FR-AP, not built
    AuditCategory.SUPPORT_ACCESS: None,  # no support tooling path exists
}


def _trail() -> tuple[AuditTrail, InMemoryAuditRepository]:
    repository = InMemoryAuditRepository()
    return AuditTrail(AuditLog(repository)), repository


def test_every_category_is_accounted_for() -> None:
    """A category missing from WIRING is one nobody decided about."""
    assert set(WIRING) == set(AuditCategory)


def test_the_unwired_categories_are_exactly_the_unbuilt_features() -> None:
    """Pinned so that building FR-GL without wiring its audit event is a
    failing test rather than a silent gap - the entry here has to change
    when the feature lands.
    """
    assert {c for c, producer in WIRING.items() if producer is None} == {
        AuditCategory.EXPORT,
        AuditCategory.APPROVAL,
        AuditCategory.SUPPORT_ACCESS,
    }


# ===========================================================================
# posting (FR-GL-001 - FR-GL-007)
# ===========================================================================


async def test_posting_to_the_ledger_is_recorded() -> None:
    """The registry entry above, backed by the service actually recording.

    Without this the WIRING table could name `api.ledger.service` after the
    call had been deleted, and the "unwired categories" test would keep
    passing - a registry that says a producer exists is only worth as much as
    the assertion behind it.
    """
    from api.audit.log import AuditLog
    from api.ledger import LedgerService
    from tests.ledger.cases import entry
    from tests.ledger.fake_world import build_fake_world

    repository = InMemoryAuditRepository()
    ledger_repository, world = await build_fake_world()
    service = LedgerService(ledger_repository, AuditLog(repository))

    posted = await service.post(entry(world))

    recorded = await repository.search(
        organization_id=posted.organization_id,
        categories=[AuditCategory.POSTING],
    )
    assert [e.action for e in recorded] == ["post_journal_entry"]
    assert recorded[0].resource_id == posted.id
    assert recorded[0].administration_id == posted.administration_id


# ===========================================================================
# permission_change
# ===========================================================================


async def test_granting_a_role_is_recorded() -> None:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    trail, entries = _trail()
    service = AuthorizationService(world.repository, audit=trail)

    await service.assign_role(
        granter_user_id=world.user,
        subject_user_id=uuid.uuid4(),
        role_id=world.repository.role_id("Bookkeeper"),
        scope_type="administration",
        scope_id=world.acme_books,
    )

    logged = await entries.search(organization_id=world.acme)
    assert len(logged) == 1
    assert logged[0].category is AuditCategory.PERMISSION_CHANGE
    assert logged[0].action == "grant"
    assert logged[0].outcome is AuditOutcome.SUCCESS
    assert logged[0].actor_user_id == world.user


async def test_a_refused_grant_is_recorded_as_denied() -> None:
    """The more interesting of the two to a reviewer: someone repeatedly
    trying to grant what they cannot is exactly what a log of only successes
    would hide.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Organization Admin", scope_id=world.acme)
    trail, entries = _trail()
    service = AuthorizationService(world.repository, audit=trail)

    with pytest.raises(PrivilegeEscalationError):
        await service.assign_role(
            granter_user_id=world.user,
            subject_user_id=uuid.uuid4(),
            role_id=world.repository.role_id("Accountant"),
            scope_type="administration",
            scope_id=world.acme_books,
        )

    logged = await entries.search(organization_id=world.acme)
    assert len(logged) == 1
    assert logged[0].outcome is AuditOutcome.DENIED
    assert "ceiling" in str(logged[0].detail["reason"])


async def test_revoking_a_role_is_recorded() -> None:
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    colleague = uuid.uuid4()
    assignment = world.repository.assign(
        user_id=colleague, role="Bookkeeper", scope_id=world.acme_books
    )
    trail, entries = _trail()
    service = AuthorizationService(world.repository, audit=trail)

    await service.revoke_assignment(revoker_user_id=world.user, assignment_id=assignment)

    logged = await entries.search(organization_id=world.acme)
    assert [e.action for e in logged] == ["revoke"]
    assert logged[0].detail["role"] == "Bookkeeper"


async def test_a_refused_last_owner_revocation_is_recorded() -> None:
    """IAM-105 refuses it; IAM-090 still wants to know it was tried."""
    world = build_world()
    assignment = world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    trail, entries = _trail()
    service = AuthorizationService(world.repository, audit=trail)

    with pytest.raises(ClientRightsFloorError):
        await service.revoke_assignment(revoker_user_id=world.user, assignment_id=assignment)

    logged = await entries.search(organization_id=world.acme)
    assert logged[0].outcome is AuditOutcome.DENIED
    assert "last Owner" in str(logged[0].detail["reason"])


async def test_firm_staff_grants_are_recorded_against_the_client() -> None:
    """IAM-094 lets a customer read their own log, and "a firm employee was
    given access to my books" is an event about the client.
    """
    world = build_world()
    manager = uuid.uuid4()
    world.repository.assign(user_id=manager, role="Firm Manager", scope_id=world.klaver)
    trail, entries = _trail()
    service = FirmStaffAccessService(
        InMemoryFirmStaffRepository(world.repository, firm_organization_id=world.klaver),
        audit=trail,
    )

    await service.grant_access(
        firm_organization_id=world.klaver,
        granter_user_id=manager,
        staff_user_id=uuid.uuid4(),
        administration_ids=[world.acme_books],
        role_name="Accountant",
    )

    # acme's log, not klaver's.
    assert len(await entries.search(organization_id=world.acme)) == 1
    assert await entries.search(organization_id=world.klaver) == []


async def test_a_seasonal_grant_records_one_entry_per_client() -> None:
    """Each client's log shows the grant that affected THEM. One row listing
    four other companies would be both confusing and a disclosure.
    """
    world = build_world()
    manager = uuid.uuid4()
    world.repository.assign(user_id=manager, role="Firm Manager", scope_id=world.klaver)
    second = uuid.uuid4()
    other_org = uuid.uuid4()
    world.repository.add_administration(second, owned_by=other_org)
    world.repository.engage_firm(firm_organization_id=world.klaver, administration_id=second)
    trail, entries = _trail()
    service = FirmStaffAccessService(
        InMemoryFirmStaffRepository(world.repository, firm_organization_id=world.klaver),
        audit=trail,
    )

    await service.grant_access(
        firm_organization_id=world.klaver,
        granter_user_id=manager,
        staff_user_id=uuid.uuid4(),
        administration_ids=[world.acme_books, second],
        role_name="Bookkeeper",
    )

    assert len(await entries.search(organization_id=world.acme)) == 1
    assert len(await entries.search(organization_id=other_org)) == 1


async def test_a_refused_client_set_records_nothing() -> None:
    """The set is validated in full before anything is written (ADR-017), so
    a refused batch must not leave audit entries claiming partial grants
    happened.
    """
    world = build_world()
    manager = uuid.uuid4()
    world.repository.assign(user_id=manager, role="Firm Manager", scope_id=world.klaver)
    trail, entries = _trail()
    service = FirmStaffAccessService(
        InMemoryFirmStaffRepository(world.repository, firm_organization_id=world.klaver),
        audit=trail,
    )

    with pytest.raises(NoActiveEngagementError):
        await service.grant_access(
            firm_organization_id=world.klaver,
            granter_user_id=manager,
            staff_user_id=uuid.uuid4(),
            administration_ids=[world.acme_books, world.acme_holding],
            role_name="Bookkeeper",
        )

    assert await entries.search(organization_id=world.acme) == []


async def test_revoking_a_firms_engagement_is_recorded() -> None:
    world = build_world()
    trail, entries = _trail()
    repository = InMemoryFirmAccessRepository(world.repository)
    world.repository.assign(
        user_id=uuid.uuid4(),
        role="Accountant",
        scope_id=world.acme_books,
        granted_by_organization_id=world.klaver,
    )
    service = EngagementRevocationService(repository, audit=trail)

    await service.revoke_firm_access(
        administration_id=world.acme_books,
        firm_organization_id=world.klaver,
        revoked_by_user_id=world.user,
        acting_organization_id=world.acme,
    )

    logged = await entries.search(organization_id=world.acme)
    assert logged[0].category is AuditCategory.PERMISSION_CHANGE
    assert logged[0].resource_type == "firm_engagement"
    assert logged[0].detail["grants_revoked"] == 1


# ===========================================================================
# configuration
# ===========================================================================


async def test_assigning_a_client_access_profile_is_recorded() -> None:
    world = build_world()
    trail, entries = _trail()
    profiles = InMemoryProfileRepository()
    profiles.own_administration(world.acme_books, owner=world.acme)
    profiles.set_firm_holdings(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        permissions={p for _, _, perms, _ in BUILTIN_PROFILES for p in perms},
    )

    class _Silent:
        async def notify_client_owner(self, change: object) -> None:
            return None

    service = ClientAccessProfileService(profiles, _Silent(), audit=trail)
    await service.assign_profile(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        profile_id=profiles.profile_id("Capture only"),
        assigned_by_user_id=STAFF,
    )

    logged = await entries.search(organization_id=world.acme)
    assert logged[0].category is AuditCategory.CONFIGURATION
    assert logged[0].detail["profile"] == "Capture only"


async def test_a_profile_change_that_reduces_access_says_so() -> None:
    """IAM-103 forbids silent reduction. Recording which changes took
    something away makes "was any client quietly restricted" a query rather
    than a diff.
    """
    world = build_world()
    trail, entries = _trail()
    profiles = InMemoryProfileRepository()
    profiles.own_administration(world.acme_books, owner=world.acme)
    profiles.set_firm_holdings(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        permissions={p for _, _, perms, _ in BUILTIN_PROFILES for p in perms},
    )

    class _Silent:
        async def notify_client_owner(self, change: object) -> None:
            return None

    service = ClientAccessProfileService(profiles, _Silent(), audit=trail)
    await service.assign_profile(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        profile_id=profiles.profile_id("Full self-service"),
        assigned_by_user_id=STAFF,
    )
    await service.assign_profile(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        profile_id=profiles.profile_id("Capture only"),
        assigned_by_user_id=STAFF,
    )

    logged = await entries.search(organization_id=world.acme)
    assert logged[0].detail["is_reduction"] is True
    assert logged[1].detail["is_reduction"] is False


async def test_acknowledging_and_revoking_a_sod_deviation_is_recorded() -> None:
    """IAM-064 requires deviations to appear in the audit report. sod_event
    records what SoD DID; this records who changed what it is configured to
    do, in the tamper-evident log.
    """
    org = uuid.uuid4()
    owner = uuid.uuid4()
    sod_repository = InMemorySodRepository()
    sod_repository.set_active_user_count(org, 5)
    sod_repository.make_owner(user_id=owner, organization_id=org)
    trail, entries = _trail()
    service = SegregationOfDutiesService(sod_repository, audit=trail)

    await service.acknowledge_deviation(
        owner_user_id=owner,
        organization_id=org,
        rule=SodRule.SUBMITTER_NOT_APPROVER,
        reason="Two-person finance team.",
    )
    await service.revoke_deviation(
        owner_user_id=owner, organization_id=org, rule=SodRule.SUBMITTER_NOT_APPROVER
    )

    logged = await entries.search(organization_id=org)
    assert [e.action for e in logged] == ["revoke", "acknowledge"]
    assert all(e.category is AuditCategory.CONFIGURATION for e in logged)
    assert logged[1].detail["reason"] == "Two-person finance team."


# ===========================================================================
# Services record nothing when no trail is wired
# ===========================================================================


async def test_a_service_without_a_trail_still_works() -> None:
    """The trail is optional on every service that takes one, so the several
    hundred tests that construct these without a database keep working. A
    required parameter would have meant a null object with the same effect.
    """
    world = build_world()
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    service = AuthorizationService(world.repository)

    assignment = await service.assign_role(
        granter_user_id=world.user,
        subject_user_id=uuid.uuid4(),
        role_id=world.repository.role_id("Bookkeeper"),
        scope_type="administration",
        scope_id=world.acme_books,
    )

    assert assignment is not None
