"""Client access profiles: IAM-100 through IAM-104 (PRD §8.6).

One section per requirement, with IAM-102 - "a firm cannot grant a client's
users a permission the firm itself does not hold on that administration" -
carrying the most weight, including the version-edit route into it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from api.authz.model import AdministrationScope, AuthorizationRequest, ResourceAttributes
from api.authz.profiles import (
    BUILTIN_PROFILES,
    CLIENT_RIGHTS_FLOOR,
    RESTRICTION_KEYS,
    ClientAccessProfileService,
    ProfileCeilingError,
    ProfileNotAvailableError,
    ProfileRestrictions,
    ProfileVersion,
    UnknownRestrictionError,
    builtin_profile_names,
    describe_change,
    resolve,
)
from api.authz.service import AuthorizationService
from tests.authz.helpers import build_world
from tests.support.fake_profile_repository import InMemoryProfileRepository


class _RecordingNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.changes: list[object] = []
        self._fail = fail

    async def notify_client_owner(self, change: object) -> None:
        if self._fail:
            raise RuntimeError("notification channel unavailable")
        self.changes.append(change)


FIRM = uuid.uuid4()
ADMIN = uuid.uuid4()
STAFF = uuid.uuid4()

# Everything the three built-ins can contain, so a firm with full access is
# never the thing under test unless a test makes it so.
FULL_FIRM_ACCESS = set().union(*(permissions for _, _, permissions, _ in BUILTIN_PROFILES))


def _service(
    *, firm_holds: set[tuple[str, str]] | None = None, notifier: _RecordingNotifier | None = None
) -> tuple[ClientAccessProfileService, InMemoryProfileRepository, _RecordingNotifier]:
    repository = InMemoryProfileRepository()
    repository.set_firm_holdings(
        firm_organization_id=FIRM,
        administration_id=ADMIN,
        permissions=set(FULL_FIRM_ACCESS if firm_holds is None else firm_holds),
    )
    sink = notifier or _RecordingNotifier()
    return ClientAccessProfileService(repository, sink), repository, sink


# ===========================================================================
# IAM-101: the three built-in profiles
# ===========================================================================


def test_the_three_builtin_profiles_ship_with_the_names_the_prd_gives() -> None:
    assert builtin_profile_names() == (
        "Capture only",
        "Invoice and capture",
        "Full self-service",
    )


def test_each_builtin_profile_contains_what_iam_101_says_it_does() -> None:
    by_name = {name: perms for name, _, perms, _ in BUILTIN_PROFILES}

    # "Capture only (upload documents, submit expenses, view own submissions)"
    capture = by_name["Capture only"]
    assert ("upload", "document") in capture
    assert ("submit", "expense") in capture
    assert ("view", "document") in capture
    assert ("create", "sales_invoice") not in capture

    # "Invoice and capture (adds sales invoicing, customers, and viewing own
    # reports)"
    invoice = by_name["Invoice and capture"]
    assert capture <= invoice, "each profile adds to the one before it"
    assert ("create", "sales_invoice") in invoice
    assert ("manage", "customer") in invoice
    assert ("view", "report") in invoice

    # "Full self-service (adds coding, bank reconciliation and VAT
    # preparation, while the firm retains filing and period control)"
    full = by_name["Full self-service"]
    assert invoice <= full
    assert ("code", "purchase_invoice") in full
    assert ("reconcile", "bank_transaction") in full
    assert ("prepare", "vat_return") in full
    assert ("file", "vat_return") not in full, "the firm retains filing"
    assert ("lock", "period") not in full, "the firm retains period control"
    assert ("close", "fiscal_year") not in full


def test_no_builtin_profile_grants_the_firms_own_ledger_authority() -> None:
    """§8.6's profiles govern what a CLIENT may do. None of the three should
    hand the client posting or payment-release authority, which stays with
    the firm in Model A.
    """
    for name, _, permissions, _ in BUILTIN_PROFILES:
        for withheld in (
            ("post", "journal_entry"),
            ("reverse", "journal_entry"),
            ("release", "payment_batch"),
            ("approve", "purchase_invoice"),
        ):
            assert withheld not in permissions, f"{name} should not contain {withheld}"


def test_every_builtin_profile_contains_only_administration_scope_permissions() -> None:
    """A profile governs access inside one administration; an
    organization-scope permission in one would be meaningless, and 0013's
    permission guard refuses to store it.
    """
    from api.authz.matrix import permission_catalogue

    scopes = {p.key: scope for p, scope in permission_catalogue()}
    for name, _, permissions, _ in BUILTIN_PROFILES:
        for permission in permissions:
            assert scopes[permission] == "administration", f"{name}: {permission}"


async def test_a_builtin_profile_cannot_be_edited() -> None:
    service, repository, _ = _service()

    with pytest.raises(ProfileNotAvailableError, match="built-in"):
        await service.publish_version(
            firm_organization_id=FIRM,
            profile_id=repository.profile_id("Capture only"),
            summary="Adding posting rights to a built-in.",
            permissions=frozenset({("post", "journal_entry")}),
            published_by_user_id=STAFF,
        )


# ===========================================================================
# IAM-102: the firm cannot grant what it does not hold
# ===========================================================================


async def test_a_firm_cannot_assign_a_profile_exceeding_its_own_access() -> None:
    """The headline constraint. The firm holds only capture-level access on
    this administration, so it cannot hand the client full self-service.
    """
    service, repository, _ = _service(
        firm_holds={
            ("view", "administration"),
            ("upload", "document"),
            ("view", "document"),
            ("submit", "expense"),
        }
    )

    with pytest.raises(ProfileCeilingError) as raised:
        await service.assign_profile(
            firm_organization_id=FIRM,
            administration_id=ADMIN,
            profile_id=repository.profile_id("Full self-service"),
            assigned_by_user_id=STAFF,
        )

    assert ("reconcile", "bank_transaction") in raised.value.missing
    assert raised.value.administration_id == ADMIN


async def test_a_firm_may_assign_a_profile_within_its_access() -> None:
    service, repository, _ = _service(
        firm_holds={
            ("view", "administration"),
            ("upload", "document"),
            ("view", "document"),
            ("submit", "expense"),
        }
    )

    change = await service.assign_profile(
        firm_organization_id=FIRM,
        administration_id=ADMIN,
        profile_id=repository.profile_id("Capture only"),
        assigned_by_user_id=STAFF,
    )

    assert change.profile_name == "Capture only"


async def test_the_ceiling_is_per_administration_not_per_firm() -> None:
    """IAM-102 says "on that administration". A firm with deep access to one
    client cannot use it to over-provision another.
    """
    other_admin = uuid.uuid4()
    service, repository, _ = _service()
    repository.set_firm_holdings(
        firm_organization_id=FIRM,
        administration_id=other_admin,
        permissions={("view", "administration")},
    )

    await service.assign_profile(
        firm_organization_id=FIRM,
        administration_id=ADMIN,
        profile_id=repository.profile_id("Full self-service"),
        assigned_by_user_id=STAFF,
    )

    with pytest.raises(ProfileCeilingError):
        await service.assign_profile(
            firm_organization_id=FIRM,
            administration_id=other_admin,
            profile_id=repository.profile_id("Full self-service"),
            assigned_by_user_id=STAFF,
        )


async def test_a_firm_cannot_edit_a_profile_upward_past_its_access() -> None:
    """The version-edit route into IAM-102, and the one a check only at
    assignment time would miss: assign a modest profile, then edit it to
    contain more than the firm holds.
    """
    service, repository, _ = _service(
        firm_holds={
            ("view", "administration"),
            ("upload", "document"),
            ("view", "document"),
            ("submit", "expense"),
        }
    )
    profile_id = await service.create_profile(
        firm_organization_id=FIRM,
        name="Client Basic",
        description="",
        created_by_user_id=STAFF,
    )
    await service.publish_version(
        firm_organization_id=FIRM,
        profile_id=profile_id,
        summary="Initial: capture only.",
        permissions=frozenset({("view", "administration"), ("upload", "document")}),
        published_by_user_id=STAFF,
    )
    await service.assign_profile(
        firm_organization_id=FIRM,
        administration_id=ADMIN,
        profile_id=profile_id,
        assigned_by_user_id=STAFF,
    )

    with pytest.raises(ProfileCeilingError) as raised:
        await service.publish_version(
            firm_organization_id=FIRM,
            profile_id=profile_id,
            summary="Quietly adding bank reconciliation.",
            permissions=frozenset(
                {
                    ("view", "administration"),
                    ("upload", "document"),
                    ("reconcile", "bank_transaction"),
                }
            ),
            published_by_user_id=STAFF,
        )

    assert ("reconcile", "bank_transaction") in raised.value.missing


async def test_editing_a_profile_no_administration_uses_is_unconstrained() -> None:
    """The ceiling is about what a firm can GIVE a client. A profile sitting
    unassigned gives nobody anything, so composing one ahead of gaining
    access is fine - it is checked again the moment it is assigned.
    """
    service, _, _ = _service(firm_holds={("view", "administration")})
    profile_id = await service.create_profile(
        firm_organization_id=FIRM, name="Draft", description="", created_by_user_id=STAFF
    )

    version_id = await service.publish_version(
        firm_organization_id=FIRM,
        profile_id=profile_id,
        summary="Drafted for a future client.",
        permissions=frozenset({("reconcile", "bank_transaction")}),
        published_by_user_id=STAFF,
    )

    assert version_id is not None


async def test_the_ceiling_measures_what_the_client_actually_receives() -> None:
    """The ceiling is checked against the RESOLVED set - what the client
    ends up with after IAM-104 restrictions - not the raw permission list.

    A profile that lists `view bank_transaction` but switches bank detail
    off gives the client nothing of the sort, so a firm without that
    permission may still publish it. Checking the raw list instead would be
    stricter than the requirement (it could never let something through that
    the client receives), but it would refuse a profile that is in fact
    within the firm's reach.
    """
    service, _, _ = _service(firm_holds={("view", "administration"), ("submit", "expense")})
    profile_id = await service.create_profile(
        firm_organization_id=FIRM, name="Bank Hidden", description="", created_by_user_id=STAFF
    )
    await service.publish_version(
        firm_organization_id=FIRM,
        profile_id=profile_id,
        summary="Initial.",
        permissions=frozenset({("view", "administration")}),
        published_by_user_id=STAFF,
    )
    # Assigned FIRST, so the publish below goes through the ceiling check
    # for an administration already using this profile - the path that
    # actually measures the resolved set.
    await service.assign_profile(
        firm_organization_id=FIRM,
        administration_id=ADMIN,
        profile_id=profile_id,
        assigned_by_user_id=STAFF,
    )

    await service.publish_version(
        firm_organization_id=FIRM,
        profile_id=profile_id,
        summary="Bank detail switched off.",
        permissions=frozenset({("view", "administration"), ("view", "bank_transaction")}),
        restrictions=ProfileRestrictions(bank_detail_visible=False),
        published_by_user_id=STAFF,
    )

    access = await service.effective_access(ADMIN)
    assert access is not None
    assert ("view", "bank_transaction") not in access.permissions


async def test_a_restriction_cannot_be_used_to_smuggle_a_permission_past_the_ceiling() -> None:
    """The other half of the rule above: what the client DOES receive is
    still measured. Switching bank detail off does not license everything
    else in the profile.
    """
    service, _, _ = _service(firm_holds={("view", "administration")})
    profile_id = await service.create_profile(
        firm_organization_id=FIRM, name="Sneaky", description="", created_by_user_id=STAFF
    )
    await service.publish_version(
        firm_organization_id=FIRM,
        profile_id=profile_id,
        summary="Bank hidden, but coding added.",
        permissions=frozenset(
            {
                ("view", "administration"),
                ("view", "bank_transaction"),
                ("code", "purchase_invoice"),
            }
        ),
        restrictions=ProfileRestrictions(bank_detail_visible=False),
        published_by_user_id=STAFF,
    )

    with pytest.raises(ProfileCeilingError) as raised:
        await service.assign_profile(
            firm_organization_id=FIRM,
            administration_id=ADMIN,
            profile_id=profile_id,
            assigned_by_user_id=STAFF,
        )

    assert ("code", "purchase_invoice") in raised.value.missing
    assert ("view", "bank_transaction") not in raised.value.missing


async def test_another_firms_profile_cannot_be_assigned_or_probed() -> None:
    service, repository, _ = _service()
    other_firm = uuid.uuid4()
    foreign = await service.create_profile(
        firm_organization_id=other_firm,
        name="Rival Standard",
        description="",
        created_by_user_id=uuid.uuid4(),
    )

    with pytest.raises(ProfileNotAvailableError) as raised:
        await service.assign_profile(
            firm_organization_id=FIRM,
            administration_id=ADMIN,
            profile_id=foreign,
            assigned_by_user_id=STAFF,
        )

    # Worded identically to "no such profile" - a firm must not learn that
    # another firm's profile exists.
    assert "is not available" in str(raised.value)
    with pytest.raises(ProfileNotAvailableError, match="is not available"):
        await service.assign_profile(
            firm_organization_id=FIRM,
            administration_id=ADMIN,
            profile_id=uuid.uuid4(),
            assigned_by_user_id=STAFF,
        )


async def test_the_client_rights_floor_is_exempt_from_the_ceiling() -> None:
    """IAM-105's floor is the client's own, not the firm's to confer. A firm
    that cannot itself read the client's audit log does not thereby stop the
    client's Owner reading it, so the floor is excluded from the IAM-102
    subset check.
    """
    service, repository, _ = _service(
        firm_holds={("view", "administration"), ("upload", "document"), ("submit", "expense")}
    )

    await service.assign_profile(
        firm_organization_id=FIRM,
        administration_id=ADMIN,
        profile_id=repository.profile_id("Capture only"),
        assigned_by_user_id=STAFF,
    )

    access = await service.effective_access(ADMIN, is_client_owner=True)
    assert access is not None
    assert access.permissions >= CLIENT_RIGHTS_FLOOR


# ===========================================================================
# IAM-100: versioned, reusable
# ===========================================================================


async def test_publishing_creates_a_new_version_rather_than_editing_one() -> None:
    service, repository, _ = _service()
    profile_id = await service.create_profile(
        firm_organization_id=FIRM, name="House Style", description="", created_by_user_id=STAFF
    )

    await service.publish_version(
        firm_organization_id=FIRM,
        profile_id=profile_id,
        summary="Initial.",
        permissions=frozenset({("view", "administration")}),
        published_by_user_id=STAFF,
    )
    await service.publish_version(
        firm_organization_id=FIRM,
        profile_id=profile_id,
        summary="Added expenses.",
        permissions=frozenset({("view", "administration"), ("submit", "expense")}),
        published_by_user_id=STAFF,
    )

    current = await repository.current_version(profile_id)
    assert current is not None
    assert current.version == 2
    assert ("submit", "expense") in current.permissions


async def test_a_new_version_takes_effect_for_administrations_already_using_it() -> None:
    """IAM-100's "reusable across clients" and IAM-103's "changes take
    effect within 60 seconds" together: an assignment points at the profile,
    so a new version applies without reassigning anything.
    """
    service, repository, _ = _service()
    profile_id = await service.create_profile(
        firm_organization_id=FIRM, name="House Style", description="", created_by_user_id=STAFF
    )
    await service.publish_version(
        firm_organization_id=FIRM,
        profile_id=profile_id,
        summary="Initial.",
        permissions=frozenset({("view", "administration")}),
        published_by_user_id=STAFF,
    )
    await service.assign_profile(
        firm_organization_id=FIRM,
        administration_id=ADMIN,
        profile_id=profile_id,
        assigned_by_user_id=STAFF,
    )

    await service.publish_version(
        firm_organization_id=FIRM,
        profile_id=profile_id,
        summary="Added expense submission.",
        permissions=frozenset({("view", "administration"), ("submit", "expense")}),
        published_by_user_id=STAFF,
    )

    access = await service.effective_access(ADMIN)
    assert access is not None
    assert ("submit", "expense") in access.permissions
    assert access.profile_version == 2


async def test_one_profile_serves_several_clients() -> None:
    second_admin = uuid.uuid4()
    service, repository, _ = _service()
    repository.set_firm_holdings(
        firm_organization_id=FIRM,
        administration_id=second_admin,
        permissions=set(FULL_FIRM_ACCESS),
    )
    profile_id = repository.profile_id("Invoice and capture")

    for administration in (ADMIN, second_admin):
        await service.assign_profile(
            firm_organization_id=FIRM,
            administration_id=administration,
            profile_id=profile_id,
            assigned_by_user_id=STAFF,
        )

    assert set(await repository.administrations_using(profile_id)) == {ADMIN, second_admin}


async def test_assigning_a_second_profile_supersedes_the_first() -> None:
    service, repository, _ = _service()

    await service.assign_profile(
        firm_organization_id=FIRM,
        administration_id=ADMIN,
        profile_id=repository.profile_id("Capture only"),
        assigned_by_user_id=STAFF,
    )
    await service.assign_profile(
        firm_organization_id=FIRM,
        administration_id=ADMIN,
        profile_id=repository.profile_id("Full self-service"),
        assigned_by_user_id=STAFF,
    )

    access = await service.effective_access(ADMIN)
    assert access is not None
    assert access.profile_name == "Full self-service"
    assert await repository.administrations_using(repository.profile_id("Capture only")) == []


# ===========================================================================
# IAM-103: effect, logging, notification, no silent reduction
# ===========================================================================


async def test_the_client_owner_is_notified_with_a_plain_language_summary() -> None:
    service, repository, notifier = _service()

    change = await service.assign_profile(
        firm_organization_id=FIRM,
        administration_id=ADMIN,
        profile_id=repository.profile_id("Invoice and capture"),
        assigned_by_user_id=STAFF,
    )

    assert notifier.changes == [change]
    assert "Invoice and capture" in change.summary
    assert "You can now" in change.summary
    # Plain language: no permission triples leak into what the client reads.
    assert "sales_invoice" not in change.summary
    assert "create sales invoices" in change.summary


async def test_a_reduction_is_described_explicitly() -> None:
    """IAM-103: "silent reduction of a client's access is prohibited." The
    summary must say what was taken away, in words.
    """
    service, repository, notifier = _service()
    await service.assign_profile(
        firm_organization_id=FIRM,
        administration_id=ADMIN,
        profile_id=repository.profile_id("Full self-service"),
        assigned_by_user_id=STAFF,
    )

    change = await service.assign_profile(
        firm_organization_id=FIRM,
        administration_id=ADMIN,
        profile_id=repository.profile_id("Capture only"),
        assigned_by_user_id=STAFF,
    )

    assert change.is_reduction
    assert ("reconcile", "bank_transaction") in change.removed
    assert "You can no longer" in change.summary
    assert "reconcile the bank" in change.summary
    assert len(notifier.changes) == 2


async def test_a_failed_notification_aborts_the_change_rather_than_going_silent() -> None:
    """The teeth behind "silent reduction is prohibited": if the client
    cannot be told, the change does not quietly happen anyway. Deliberately
    unlike the geolocation resolver (ADR-010), which degrades to None -
    that one is enrichment, this one is the requirement.
    """
    service, repository, _ = _service(notifier=_RecordingNotifier(fail=True))

    with pytest.raises(RuntimeError, match="notification channel unavailable"):
        await service.assign_profile(
            firm_organization_id=FIRM,
            administration_id=ADMIN,
            profile_id=repository.profile_id("Capture only"),
            assigned_by_user_id=STAFF,
        )


async def test_publishing_a_version_notifies_every_affected_client() -> None:
    second_admin = uuid.uuid4()
    service, repository, notifier = _service()
    repository.set_firm_holdings(
        firm_organization_id=FIRM,
        administration_id=second_admin,
        permissions=set(FULL_FIRM_ACCESS),
    )
    profile_id = await service.create_profile(
        firm_organization_id=FIRM, name="House Style", description="", created_by_user_id=STAFF
    )
    await service.publish_version(
        firm_organization_id=FIRM,
        profile_id=profile_id,
        summary="Initial.",
        permissions=frozenset({("view", "administration"), ("submit", "expense")}),
        published_by_user_id=STAFF,
    )
    for administration in (ADMIN, second_admin):
        await service.assign_profile(
            firm_organization_id=FIRM,
            administration_id=administration,
            profile_id=profile_id,
            assigned_by_user_id=STAFF,
        )
    notifier.changes.clear()

    await service.publish_version(
        firm_organization_id=FIRM,
        profile_id=profile_id,
        summary="Removed expense submission.",
        permissions=frozenset({("view", "administration")}),
        published_by_user_id=STAFF,
    )

    assert len(notifier.changes) == 2
    assert all(c.is_reduction for c in notifier.changes)  # type: ignore[attr-defined]


async def test_a_version_requires_a_summary() -> None:
    service, _, _ = _service()
    profile_id = await service.create_profile(
        firm_organization_id=FIRM, name="Nameless", description="", created_by_user_id=STAFF
    )

    with pytest.raises(ValueError, match="plain-language summary"):
        await service.publish_version(
            firm_organization_id=FIRM,
            profile_id=profile_id,
            summary="   ",
            permissions=frozenset({("view", "administration")}),
            published_by_user_id=STAFF,
        )


def test_a_change_with_no_effect_says_so_rather_than_being_blank() -> None:
    change = describe_change(
        administration_id=ADMIN,
        profile_name="Capture only",
        before=frozenset({("view", "administration")}),
        after=frozenset({("view", "administration")}),
    )

    assert not change.is_reduction
    assert "Nothing about what you can do has changed." in change.summary


# ===========================================================================
# IAM-104: per-profile restrictions
# ===========================================================================


def test_restrictions_default_to_unrestricted() -> None:
    restrictions = ProfileRestrictions()

    assert restrictions.bank_detail_visible
    assert restrictions.reports_visible
    assert restrictions.periods_editable
    assert restrictions.visible_journal_ids is None
    assert restrictions.approval_amount_ceiling is None


def test_hiding_bank_detail_withholds_the_bank_permission() -> None:
    version = ProfileVersion(
        id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        version=1,
        summary="",
        permissions=frozenset({("view", "bank_transaction"), ("view", "report")}),
        restrictions=ProfileRestrictions(bank_detail_visible=False),
    )

    resolved = resolve(version)

    assert ("view", "bank_transaction") not in resolved.permissions
    assert ("view", "report") in resolved.permissions


def test_hiding_reports_withholds_viewing_and_exporting_them() -> None:
    version = ProfileVersion(
        id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        version=1,
        summary="",
        permissions=frozenset({("view", "report"), ("export", "report_data")}),
        restrictions=ProfileRestrictions(reports_visible=False),
    )

    resolved = resolve(version)

    assert ("view", "report") not in resolved.permissions
    assert ("export", "report_data") not in resolved.permissions


def test_locking_periods_withholds_period_and_year_end_control() -> None:
    version = ProfileVersion(
        id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        version=1,
        summary="",
        permissions=frozenset({("lock", "period"), ("close", "fiscal_year")}),
        restrictions=ProfileRestrictions(periods_editable=False),
    )

    assert resolve(version).permissions == frozenset()


def test_journal_and_ceiling_restrictions_become_iam_033_conditions() -> None:
    """IAM-104's journal and amount limits compile down to the SAME
    condition mechanism grants use, so there is no second enforcement path
    to keep in step.
    """
    journal = uuid.uuid4()
    version = ProfileVersion(
        id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        version=1,
        summary="",
        permissions=frozenset({("code", "purchase_invoice")}),
        restrictions=ProfileRestrictions(
            visible_journal_ids=(journal,), approval_amount_ceiling=Decimal("2500.00")
        ),
    )

    resolved = resolve(version)

    assert resolved.conditions["journal_ids"] == [str(journal)]
    # A decimal STRING, matching how every other ceiling is stored (NFR-031).
    assert resolved.conditions["amount_ceiling"] == "2500.00"
    assert isinstance(resolved.conditions["amount_ceiling"], str)


def test_an_unknown_restriction_is_refused() -> None:
    with pytest.raises(UnknownRestrictionError):
        ProfileRestrictions.from_mapping({"bank_detail_visable": False})


def test_a_float_ceiling_is_refused_rather_than_coerced() -> None:
    with pytest.raises(UnknownRestrictionError, match="decimal string"):
        ProfileRestrictions.from_mapping({"approval_amount_ceiling": 2500.00})


def test_the_restriction_keys_match_iam_104s_list() -> None:
    assert {
        "visible_journal_ids",
        "bank_detail_visible",
        "reports_visible",
        "periods_editable",
        "approval_amount_ceiling",
    } == RESTRICTION_KEYS


def test_restrictions_survive_a_round_trip_through_storage() -> None:
    journal = uuid.uuid4()
    original = ProfileRestrictions(
        visible_journal_ids=(journal,),
        bank_detail_visible=False,
        reports_visible=True,
        periods_editable=False,
        approval_amount_ceiling=Decimal("100.50"),
    )

    assert ProfileRestrictions.from_mapping(original.to_mapping()) == original


def test_the_floor_is_restored_after_restrictions_are_applied() -> None:
    """Order matters: a firm switching reports off must not thereby remove
    the client Owner's export of their own data (IAM-105).
    """
    version = ProfileVersion(
        id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        version=1,
        summary="",
        permissions=frozenset({("export", "report_data"), ("view", "report")}),
        restrictions=ProfileRestrictions(reports_visible=False),
    )

    for_staff = resolve(version)
    for_owner = resolve(version, is_client_owner=True)

    assert ("export", "report_data") not in for_staff.permissions
    assert ("export", "report_data") in for_owner.permissions


# ===========================================================================
# The cap applied through authorize()
# ===========================================================================


async def _capped_service(profile_name: str) -> tuple[AuthorizationService, object]:
    """A client Bookkeeper on an administration the firm has capped."""
    world = build_world()
    profiles = InMemoryProfileRepository()
    profiles.set_firm_holdings(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        permissions=set(FULL_FIRM_ACCESS),
    )
    profile_service = ClientAccessProfileService(profiles, _RecordingNotifier())
    await profile_service.assign_profile(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        profile_id=profiles.profile_id(profile_name),
        assigned_by_user_id=STAFF,
    )
    # The client's own user: an organization-scoped grant at acme.
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    return AuthorizationService(world.repository, profiles=profile_service), world


async def test_a_profile_caps_what_the_clients_own_user_may_do() -> None:
    service, world = await _capped_service("Capture only")

    submits = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,  # type: ignore[attr-defined]
            action="submit",
            resource_type="expense",
            target=AdministrationScope(world.acme_books),  # type: ignore[attr-defined]
        )
    )
    posts = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,  # type: ignore[attr-defined]
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_books),  # type: ignore[attr-defined]
        )
    )

    assert submits.allowed
    # The Owner role carries posting rights; the profile does not.
    assert posts.denied
    assert posts.reason == "capped_by_client_access_profile"
    assert "Capture only" in (posts.detail or "")


async def test_a_profile_never_grants_what_the_role_lacks() -> None:
    """A profile is a cap, not a grant. A user with no role at all gains
    nothing from a permissive profile.
    """
    world = build_world()
    profiles = InMemoryProfileRepository()
    profiles.set_firm_holdings(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        permissions=set(FULL_FIRM_ACCESS),
    )
    profile_service = ClientAccessProfileService(profiles, _RecordingNotifier())
    await profile_service.assign_profile(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        profile_id=profiles.profile_id("Full self-service"),
        assigned_by_user_id=STAFF,
    )
    service = AuthorizationService(world.repository, profiles=profile_service)

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="submit",
            resource_type="expense",
            target=AdministrationScope(world.acme_books),
        )
    )

    assert decision.denied
    assert decision.reason == "no_matching_grant"


async def test_firm_staff_are_not_capped_by_the_profile_they_set() -> None:
    """§8.6: a profile governs "what the client's OWN users may do". Firm
    staff reach the administration through an engagement, and capping them
    with the cap they themselves set would be incoherent.
    """
    world = build_world()
    profiles = InMemoryProfileRepository()
    profiles.set_firm_holdings(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        permissions=set(FULL_FIRM_ACCESS),
    )
    profile_service = ClientAccessProfileService(profiles, _RecordingNotifier())
    await profile_service.assign_profile(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        profile_id=profiles.profile_id("Capture only"),
        assigned_by_user_id=STAFF,
    )
    service = AuthorizationService(world.repository, profiles=profile_service)

    # Firm staff: an administration-scoped Accountant grant on the client's
    # books, made FROM the firm's tenant context under its engagement
    # (IAM-107). That provenance is what marks the holder as firm staff -
    # the grant's scope alone cannot, since the client's own bookkeeper
    # holds the identically-shaped row.
    world.repository.assign(user_id=world.user, role="Firm Manager", scope_id=world.klaver)
    world.repository.assign(
        user_id=world.user,
        role="Accountant",
        scope_id=world.acme_books,
        granted_by_organization_id=world.klaver,
    )

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_books),
        )
    )

    assert decision.allowed


async def test_an_administration_with_no_profile_is_not_capped() -> None:
    """No profile means no cap - never no access. Self-managed
    administrations must be unaffected by this whole mechanism.
    """
    world = build_world()
    profiles = InMemoryProfileRepository()
    service = AuthorizationService(
        world.repository, profiles=ClientAccessProfileService(profiles, _RecordingNotifier())
    )
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_books),
        )
    )

    assert decision.allowed


async def test_a_profiles_journal_restriction_is_enforced_on_a_real_request() -> None:
    """IAM-104 end to end: the restriction compiles into an IAM-033
    condition, and authorize() applies it with the same evaluator a grant's
    own conditions use.
    """
    world = build_world()
    permitted_journal, other_journal = uuid.uuid4(), uuid.uuid4()
    profiles = InMemoryProfileRepository()
    profiles.set_firm_holdings(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        permissions=set(FULL_FIRM_ACCESS) | {("post", "journal_entry")},
    )
    profile_service = ClientAccessProfileService(profiles, _RecordingNotifier())
    profile_id = await profile_service.create_profile(
        firm_organization_id=world.klaver,
        name="Sales journal only",
        description="",
        created_by_user_id=STAFF,
    )
    await profile_service.publish_version(
        firm_organization_id=world.klaver,
        profile_id=profile_id,
        summary="Posting limited to the sales journal.",
        permissions=frozenset({("view", "administration"), ("post", "journal_entry")}),
        restrictions=ProfileRestrictions(visible_journal_ids=(permitted_journal,)),
        published_by_user_id=STAFF,
    )
    await profile_service.assign_profile(
        firm_organization_id=world.klaver,
        administration_id=world.acme_books,
        profile_id=profile_id,
        assigned_by_user_id=STAFF,
    )
    world.repository.assign(user_id=world.user, role="Owner", scope_id=world.acme)
    service = AuthorizationService(world.repository, profiles=profile_service)

    def post(journal: uuid.UUID) -> AuthorizationRequest:
        return AuthorizationRequest(
            user_id=world.user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_books),
            attributes=ResourceAttributes(journal_id=journal),
        )

    assert (await service.authorize(post(permitted_journal))).allowed
    denied = await service.authorize(post(other_journal))
    assert denied.denied
    assert denied.reason == "capped_by_client_access_profile"
    assert "journal_ids" in (denied.detail or "")


async def test_the_clients_owner_keeps_the_floor_through_authorize() -> None:
    """IAM-105's floor, applied where it actually matters: the client's
    Owner can still export their own data under the most restrictive
    profile, which explicitly switches reports off.
    """
    service, world = await _capped_service("Capture only")

    decision = await service.authorize(
        AuthorizationRequest(
            user_id=world.user,  # type: ignore[attr-defined]
            action="export",
            resource_type="report_data",
            target=AdministrationScope(world.acme_books),  # type: ignore[attr-defined]
        )
    )

    assert decision.allowed


def test_the_builtin_profiles_are_defined_once() -> None:
    """The fake seeds from BUILTIN_PROFILES, and so does the migration's
    companion seeding - so the fake and production cannot describe different
    profiles.
    """
    repository = InMemoryProfileRepository()

    for name, _, _, _ in BUILTIN_PROFILES:
        assert repository.profile_id(name) is not None


async def test_an_archived_profile_cannot_be_assigned() -> None:
    service, repository, _ = _service()
    profile_id = repository.profile_id("Capture only")
    repository.archive_profile(profile_id, datetime(2026, 1, 1, tzinfo=UTC))

    with pytest.raises(ProfileNotAvailableError):
        await service.assign_profile(
            firm_organization_id=FIRM,
            administration_id=ADMIN,
            profile_id=profile_id,
            assigned_by_user_id=STAFF,
        )
