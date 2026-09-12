"""The chart of accounts against the in-memory chart (FR-GL-005, FR-ONB-004,
FR-ONB-005, CMP-003).

Runs tests/ledger/chart_cases.py - the same table
tests/integration/test_chart_of_accounts.py runs against Postgres - and does it
over the RGS dataset that actually ships, so a mistake in the file fails here
rather than in production's first onboarding.

The authorization tests drive EVERY standard role at each method rather than
the ones that should pass. A permission bug does not usually take an authority
away, it hands one out, and only an exhaustive check sees that.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from api.audit.log import AuditCategory, AuditLog, AuditOutcome
from api.authz.matrix import ROLES, permissions_for_role
from api.authz.service import AuthorizationService
from api.ledger.chart import (
    MANAGE_CHART,
    VIEW_CHART,
    ChartError,
    ChartNotAuthorized,
    ChartOfAccountsService,
    LegalForm,
    RgsMappingViolation,
    RgsSource,
    UnknownLegalForm,
    UpgradeBlocked,
    UpgradeResolution,
    VatTreatment,
)
from api.ledger.model import AccountStatus, AccountType, ControlKind
from tests.authz.helpers import build_world
from tests.ledger.chart_cases import (
    COMMON_ACCOUNTS,
    DISTINCTIVE_ACCOUNTS,
    EXCLUSIVE_ACCOUNTS,
    MAPPING_CASES,
    MappingCase,
    successor,
)
from tests.support.fake_audit_repository import InMemoryAuditRepository
from tests.support.fake_chart_repository import (
    InMemoryChartRepository,
    canonical_legal_form,
    load_document,
)

ROLE_NAMES = sorted(role.name for role in ROLES)

LEGAL_FORMS = ("eenmanszaak", "vof", "bv", "stichting", "vereniging")


def _roles_holding(permission: tuple[str, str]) -> set[str]:
    """Read the expectation out of the matrix rather than restating it."""
    return {role.name for role in ROLES if permission in set(permissions_for_role(role))}


@dataclass(frozen=True, slots=True)
class Fixture:
    service: ChartOfAccountsService
    repository: InMemoryChartRepository
    administration_id: uuid.UUID
    organization_id: uuid.UUID
    user: uuid.UUID
    audit: AuditLog
    authz: Any

    def as_role(self, role: str) -> tuple[ChartOfAccountsService, uuid.UUID]:
        """A second service over the SAME chart, acting as a second user who
        holds `role`.

        The same repository on purpose: the only difference between this
        caller and the fixture's own is the grant, so a test comparing them is
        testing authorization rather than two unrelated worlds.
        """
        user = uuid.uuid4()
        role_definition = next(r for r in ROLES if r.name == role)
        self.authz.repository.assign(
            user_id=user,
            role=role,
            scope_id=(
                self.authz.acme
                if role_definition.scope_type == "organization"
                else self.authz.acme_books
            ),
        )
        service = ChartOfAccountsService(
            self.repository,
            AuthorizationService(self.authz.repository),
            self.audit,
        )
        return service, user


def _world(legal_form: str = "bv", role: str = "Owner") -> Fixture:
    authz = build_world()
    role_definition = next(r for r in ROLES if r.name == role)
    authz.repository.assign(
        user_id=authz.user,
        role=role,
        scope_id=(authz.acme if role_definition.scope_type == "organization" else authz.acme_books),
    )

    repository = InMemoryChartRepository()
    repository.administrations[authz.acme_books] = _administration(
        authz.acme_books, authz.acme, legal_form
    )
    repository.load(load_document())

    audit = AuditLog(InMemoryAuditRepository())
    return Fixture(
        service=ChartOfAccountsService(repository, AuthorizationService(authz.repository), audit),
        repository=repository,
        administration_id=authz.acme_books,
        organization_id=authz.acme,
        user=authz.user,
        audit=audit,
        authz=authz,
    )


def _administration(
    administration_id: uuid.UUID, organization_id: uuid.UUID, legal_form: str
) -> Any:
    from tests.support.fake_chart_repository import FakeAdministration

    return FakeAdministration(
        id=administration_id, organization_id=organization_id, legal_form=legal_form
    )


async def _seeded(legal_form: str = "bv", role: str = "Owner") -> Fixture:
    fixture = _world(legal_form, role)
    await fixture.service.seed(
        administration_id=fixture.administration_id, actor_user_id=fixture.user
    )
    return fixture


# ===========================================================================
# FR-ONB-005: seeding from the profile matching the legal form
# ===========================================================================


@pytest.mark.parametrize("legal_form", LEGAL_FORMS)
async def test_seeding_produces_a_usable_chart(legal_form: str) -> None:
    fixture = _world(legal_form)

    result = await fixture.service.seed(
        administration_id=fixture.administration_id, actor_user_id=fixture.user
    )

    assert result.legal_form is LegalForm(legal_form)
    assert result.seeded_count > 0
    assert result.skipped_count == 0

    chart = await fixture.service.chart(
        administration_id=fixture.administration_id, actor_user_id=fixture.user
    )
    codes = {account.code for account in chart}

    missing_common = set(COMMON_ACCOUNTS) - codes
    assert not missing_common, f"{legal_form} is missing common accounts {missing_common}"

    missing = set(DISTINCTIVE_ACCOUNTS[legal_form]) - codes
    assert not missing, f"{legal_form} is missing {missing}"


@pytest.mark.parametrize("legal_form", LEGAL_FORMS)
async def test_every_seeded_account_is_mapped_and_agrees_with_its_element(
    legal_form: str,
) -> None:
    """FR-GL-005's four fields, on every account the seed creates.

    The type agreement is the one worth asserting explicitly: it is what
    FR-ONB-005's "mapping integrity" means, and a profile row whose account
    type disagreed with its RGS element would be an integrity violation
    shipped as data.
    """
    fixture = await _seeded(legal_form)

    chart = await fixture.service.chart(
        administration_id=fixture.administration_id, actor_user_id=fixture.user
    )

    assert chart
    for account in chart:
        assert account.is_mapped, f"{account.code} was seeded without an RGS code"
        assert account.rgs_description_nl
        assert account.rgs_version
        assert account.is_seeded
        assert account.status is AccountStatus.ACTIVE

        options = await fixture.service.rgs_options(
            administration_id=fixture.administration_id,
            actor_user_id=fixture.user,
            account_type=account.account_type,
        )
        assert account.rgs_code in {element.code for element in options}, (
            f"{account.code} is {account.account_type.value} but its RGS code "
            f"{account.rgs_code} is not offered for that type"
        )


@pytest.mark.parametrize("legal_form", LEGAL_FORMS)
async def test_the_seed_creates_both_control_accounts(legal_form: str) -> None:
    """FR-GL-006 needs somewhere for the sub-ledgers to reconcile to, from the
    first minute of the administration's life. A profile that seeded neither
    would leave the first sales invoice with nowhere to post.
    """
    fixture = await _seeded(legal_form)

    chart = await fixture.service.chart(
        administration_id=fixture.administration_id, actor_user_id=fixture.user
    )
    controls = {a.control_kind: a.code for a in chart if a.control_kind is not None}

    assert controls.keys() == {
        ControlKind.ACCOUNTS_RECEIVABLE,
        ControlKind.ACCOUNTS_PAYABLE,
    }
    assert controls[ControlKind.ACCOUNTS_RECEIVABLE] == "1300"
    assert controls[ControlKind.ACCOUNTS_PAYABLE] == "1600"


async def test_the_legal_form_actually_drives_the_chart() -> None:
    """FR-ONB-004, asserted in both directions.

    A profile table that returned the same chart for all five forms would pass
    every other test here. What makes the requirement real is that a BV gets
    share capital and corporate income tax and an eenmanszaak does not.
    """
    charts: dict[str, set[str]] = {}
    for legal_form in LEGAL_FORMS:
        fixture = await _seeded(legal_form)
        charts[legal_form] = {
            account.code
            for account in await fixture.service.chart(
                administration_id=fixture.administration_id,
                actor_user_id=fixture.user,
            )
        }

    for owner, exclusive in EXCLUSIVE_ACCOUNTS.items():
        for code in exclusive:
            for legal_form, codes in charts.items():
                if legal_form == owner:
                    assert code in codes
                else:
                    assert code not in codes, (
                        f"{code} is meant to be specific to {owner} but {legal_form} has it too"
                    )

    # And no two forms produce an identical chart, which is the same claim
    # stated without naming any account.
    distinct = {frozenset(codes) for codes in charts.values()}
    assert len(distinct) == len(charts)


async def test_seeding_twice_adds_nothing_the_second_time() -> None:
    """FR-ONB-010 makes onboarding resumable, so this WILL be called twice."""
    fixture = _world("bv")

    first = await fixture.service.seed(
        administration_id=fixture.administration_id, actor_user_id=fixture.user
    )
    second = await fixture.service.seed(
        administration_id=fixture.administration_id, actor_user_id=fixture.user
    )

    assert second.seeded_count == 0
    assert second.skipped_count == first.seeded_count
    assert second.total == first.total


async def test_seeding_never_overwrites_an_account_somebody_created() -> None:
    """The idempotence is by account CODE, and the seed adds rather than
    overwrites: a user who has already made 1000 mean something else keeps
    their decision, and their postings keep their meaning.
    """
    fixture = _world("bv")
    await fixture.service.seed(
        administration_id=fixture.administration_id, actor_user_id=fixture.user
    )
    account = next(
        a
        for a in await fixture.service.chart(
            administration_id=fixture.administration_id, actor_user_id=fixture.user
        )
        if a.code == "1000"
    )
    await fixture.service.remap_account(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        account_id=account.account_id,
        rgs_code=None,
    )

    await fixture.service.seed(
        administration_id=fixture.administration_id, actor_user_id=fixture.user
    )

    after = next(
        a
        for a in await fixture.service.chart(
            administration_id=fixture.administration_id, actor_user_id=fixture.user
        )
        if a.code == "1000"
    )
    assert after.rgs_code is None, "the second seed overwrote a user's account"


@pytest.mark.parametrize("captured", ["BV", "b.v.", "Besloten Vennootschap", "bv"])
async def test_the_legal_form_is_resolved_through_its_aliases(captured: str) -> None:
    """FR-ONB-002 fills legal_form from the KvK API, in the KvK's vocabulary.
    Constraining that column would have been the wrong fix; mapping it is the
    right one.
    """
    fixture = _world(captured)

    result = await fixture.service.seed(
        administration_id=fixture.administration_id, actor_user_id=fixture.user
    )

    assert result.legal_form is LegalForm.BV


async def test_a_legal_form_nobody_recognises_refuses_rather_than_guesses() -> None:
    """Seeding the wrong chart is worse than refusing to seed one: a BV's
    equity accounts in an eenmanszaak's books are wrong in a way that is
    invisible until the year-end.
    """
    fixture = _world("Naamloze Vennootschap")

    with pytest.raises(UnknownLegalForm) as raised:
        await fixture.service.seed(
            administration_id=fixture.administration_id, actor_user_id=fixture.user
        )

    assert "Naamloze Vennootschap" in str(raised.value)
    assert "FR-ONB-004" in str(raised.value)


def test_the_alias_table_covers_every_form_the_prd_names() -> None:
    for form in LEGAL_FORMS:
        assert canonical_legal_form(form) == form
        assert canonical_legal_form(form.upper()) == form


# ===========================================================================
# FR-ONB-005: may extend, may not break mapping integrity
# ===========================================================================


@pytest.mark.parametrize("case", MAPPING_CASES, ids=lambda c: c.name)
async def test_mapping_integrity(case: MappingCase) -> None:
    fixture = await _seeded("bv")

    async def add() -> Any:
        return await fixture.service.add_account(
            administration_id=fixture.administration_id,
            actor_user_id=fixture.user,
            code=case.account_code,
            name="Test",
            account_type=case.account_type,
            rgs_code=case.rgs_code,
        )

    if case.is_accepted:
        account = await add()
        assert account.code == case.account_code
        assert account.rgs_code == case.rgs_code
        assert not account.is_seeded, "a user's account is not part of the profile"
        return

    with pytest.raises(RgsMappingViolation) as raised:
        await add()

    assert case.expect is not None
    assert case.expect in str(raised.value), (
        f"{case.name} ({case.requirement}): refused, but not for the reason "
        f"under test. Expected {case.expect!r}, got: {raised.value}"
    )


async def test_an_extended_account_is_distinguishable_from_a_seeded_one() -> None:
    """FR-ONB-005 has two populations and a user has to be able to tell them
    apart - "did I add this, or did the profile" is the first question when a
    chart looks wrong.
    """
    fixture = await _seeded("bv")

    await fixture.service.add_account(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        code="4700",
        name="Advieskosten",
        account_type=AccountType.EXPENSE,
        rgs_code="WBedAlg",
        default_vat_code=VatTreatment.STANDARD,
    )

    chart = await fixture.service.chart(
        administration_id=fixture.administration_id, actor_user_id=fixture.user
    )
    added = next(a for a in chart if a.code == "4700")
    seeded = next(a for a in chart if a.code == "1000")

    assert not added.is_seeded
    assert seeded.is_seeded
    assert added.default_vat_code is VatTreatment.STANDARD


async def test_a_mapping_can_be_corrected_within_its_type() -> None:
    """The "may extend" half again: an account mapped to the wrong element of
    the RIGHT type is a correction, and refusing it would leave the user with
    no way to fix their own chart.
    """
    fixture = await _seeded("bv")
    account = next(
        a
        for a in await fixture.service.chart(
            administration_id=fixture.administration_id, actor_user_id=fixture.user
        )
        if a.code == "1000"
    )

    remapped = await fixture.service.remap_account(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        account_id=account.account_id,
        rgs_code="BLimBan",
    )

    assert remapped.rgs_code == "BLimBan"


async def test_a_mapping_cannot_be_corrected_across_types() -> None:
    fixture = await _seeded("bv")
    account = next(
        a
        for a in await fixture.service.chart(
            administration_id=fixture.administration_id, actor_user_id=fixture.user
        )
        if a.code == "1000"
    )

    with pytest.raises(RgsMappingViolation) as raised:
        await fixture.service.remap_account(
            administration_id=fixture.administration_id,
            actor_user_id=fixture.user,
            account_id=account.account_id,
            rgs_code="WOmzNeoBin",
        )

    assert "remapped across types" in str(raised.value)


async def test_the_options_offered_are_exactly_the_ones_that_would_be_accepted() -> None:
    """PRD design principle D5, no dead ends: a picker populated from a
    different rule than the one the database enforces offers choices that fail
    on save.
    """
    fixture = await _seeded("bv")

    for account_type in AccountType:
        options = await fixture.service.rgs_options(
            administration_id=fixture.administration_id,
            actor_user_id=fixture.user,
            account_type=account_type,
        )
        for element in options:
            assert element.is_postable
            assert element.account_type is account_type

    # And the converse: a heading is offered for no type at all.
    every_option = {
        element.code
        for account_type in AccountType
        for element in await fixture.service.rgs_options(
            administration_id=fixture.administration_id,
            actor_user_id=fixture.user,
            account_type=account_type,
        )
    }
    assert "BLim" not in every_option
    assert "B" not in every_option


# ===========================================================================
# CMP-003: the versioned upgrade path
# ===========================================================================


async def test_an_upgrade_with_a_clean_mapping_applies() -> None:
    fixture = await _seeded("bv")
    fixture.repository.load(
        successor(load_document(), changes={"WBedKan": ("WBedKanKan", "renamed")}),
        checksum="v2",
    )

    plan = await fixture.service.plan_upgrade(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        to_version="4.0-test",
    )
    assert plan.can_apply, [step.detail for step in plan.blocked]
    assert plan.from_version == "3.8-provisional"
    assert plan.to_version == "4.0-test"

    result = await fixture.service.apply_upgrade(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        to_version="4.0-test",
    )

    assert result.rgs_version == "4.0-test"
    assert result.remapped_count > 0

    chart = await fixture.service.chart(
        administration_id=fixture.administration_id, actor_user_id=fixture.user
    )
    office = next(a for a in chart if a.code == "4100")
    assert office.rgs_code == "WBedKanKan"
    assert office.rgs_version == "4.0-test"
    assert not await fixture.service.mapping_deviations(administration_id=fixture.administration_id)


async def test_a_withdrawn_code_blocks_the_upgrade_until_a_human_decides() -> None:
    """CMP-003's upgrade path has to be able to say no. A half-upgraded chart
    files some accounts under the new taxonomy and some under the old, with
    nothing on the screen saying which.
    """
    fixture = await _seeded("bv")
    fixture.repository.load(
        successor(load_document(), changes={"WBedKan": (None, "withdrawn")}),
        checksum="v2",
    )

    plan = await fixture.service.plan_upgrade(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        to_version="4.0-test",
    )

    assert not plan.can_apply
    blocked = plan.blocked
    assert {step.account_code for step in blocked} == {"4100"}
    assert blocked[0].resolution is UpgradeResolution.NEEDS_A_DECISION
    assert "withdrawn" in blocked[0].detail

    with pytest.raises(UpgradeBlocked) as raised:
        await fixture.service.apply_upgrade(
            administration_id=fixture.administration_id,
            actor_user_id=fixture.user,
            to_version="4.0-test",
        )
    assert raised.value.steps

    # And nothing moved.
    assert (
        await fixture.repository.pinned_version(administration_id=fixture.administration_id)
    ).version == "3.8-provisional"


async def test_a_blocked_upgrade_proceeds_once_the_account_is_remapped() -> None:
    """The blocked case is not a dead end (D5). Remapping the account onto a
    code that survives is the resolution, and the plan then clears.
    """
    fixture = await _seeded("bv")
    fixture.repository.load(
        successor(load_document(), changes={"WBedKan": (None, "withdrawn")}),
        checksum="v2",
    )
    account = next(
        a
        for a in await fixture.service.chart(
            administration_id=fixture.administration_id, actor_user_id=fixture.user
        )
        if a.code == "4100"
    )

    await fixture.service.remap_account(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        account_id=account.account_id,
        rgs_code="WBedAlg",
    )

    plan = await fixture.service.plan_upgrade(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        to_version="4.0-test",
    )
    assert plan.can_apply

    result = await fixture.service.apply_upgrade(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        to_version="4.0-test",
    )
    assert result.rgs_version == "4.0-test"


async def test_an_unmapped_account_does_not_block_an_upgrade() -> None:
    """An account with no RGS code has nothing to migrate. Treating it as
    unresolved would make FR-ONB-005's "may extend" incompatible with CMP-003's
    upgrade path - a user with one unmapped account could never move version.
    """
    fixture = await _seeded("bv")
    await fixture.service.add_account(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        code="4800",
        name="Iets eigens",
        account_type=AccountType.EXPENSE,
        rgs_code=None,
    )
    fixture.repository.load(successor(load_document()), checksum="v2")

    plan = await fixture.service.plan_upgrade(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        to_version="4.0-test",
    )

    step = next(s for s in plan.steps if s.account_code == "4800")
    assert step.resolution is UpgradeResolution.NOTHING_TO_DO
    assert plan.can_apply

    result = await fixture.service.apply_upgrade(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        to_version="4.0-test",
    )
    assert result.unmapped_count == 1


async def test_upgrading_onto_a_version_nobody_loaded_is_refused() -> None:
    fixture = await _seeded("bv")

    with pytest.raises(ChartError) as raised:
        await fixture.service.plan_upgrade(
            administration_id=fixture.administration_id,
            actor_user_id=fixture.user,
            to_version="9.9",
        )

    assert "not loaded" in str(raised.value)
    assert "load_rgs_version" in str(raised.value)


async def test_a_second_document_cannot_be_loaded_under_a_loaded_version_name() -> None:
    """A published RGS release is frozen. Re-loading the same file is a no-op;
    loading a different one under the same name would silently change what
    every account's code means.
    """
    fixture = _world("bv")
    document = load_document()

    again = fixture.repository.load(document, checksum="test")
    assert again.version == document["rgs_version"]

    with pytest.raises(ChartError) as raised:
        fixture.repository.load(document, checksum="different")
    assert "already loaded" in str(raised.value)


# ===========================================================================
# CMP-003 / CMP-013: readiness
# ===========================================================================


async def test_readiness_reports_the_provisional_dataset() -> None:
    """The shipped dataset is a starter subset, not the official publication.
    An administration seeded from it must be reportable, because CMP-002's XAF
    export and CMP-004's SBR filing both carry these codes outside the system.
    """
    fixture = await _seeded("bv")

    rows = await fixture.service.readiness(administration_id=fixture.administration_id)

    assert len(rows) == 1
    readiness = rows[0]
    assert readiness.version_source is RgsSource.PROVISIONAL
    assert readiness.is_provisional
    assert not readiness.is_behind_current
    assert readiness.accounts_unmapped == 0
    assert not readiness.ready_to_file, (
        "a provisional dataset is not a filing basis (CMP-002, CMP-004)"
    )


async def test_readiness_counts_unmapped_accounts() -> None:
    fixture = await _seeded("bv")
    await fixture.service.add_account(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        code="4800",
        name="Iets eigens",
        account_type=AccountType.EXPENSE,
        rgs_code=None,
    )

    readiness = (await fixture.service.readiness(administration_id=fixture.administration_id))[0]

    assert readiness.accounts_unmapped == 1
    assert not readiness.ready_to_file


async def test_readiness_notices_an_administration_left_behind() -> None:
    """CMP-013's regulatory watch needs to know who has not moved yet."""
    fixture = await _seeded("bv")
    fixture.repository.load(successor(load_document()), checksum="v2")

    readiness = (await fixture.service.readiness(administration_id=fixture.administration_id))[0]

    assert readiness.is_behind_current
    assert readiness.rgs_version == "3.8-provisional"


async def test_a_healthy_chart_reports_no_mapping_deviations() -> None:
    fixture = await _seeded("bv")

    assert not await fixture.service.mapping_deviations(administration_id=fixture.administration_id)


# ===========================================================================
# Authorization (CLAUDE.md rule 3) and audit (IAM-090)
# ===========================================================================


def test_appendix_a_places_the_chart_permissions() -> None:
    """Pinned here as well as in the Appendix A conformance test, because
    these are the grants FR-ONB-005's "user may extend" actually rests on.
    """
    assert _roles_holding(MANAGE_CHART) == {"Owner", "Accountant", "Bookkeeper"}
    assert _roles_holding(VIEW_CHART) == {
        "Owner",
        "Accountant",
        "Bookkeeper",
        "Approver",
        "Invoicer",
        "Viewer",
    }


@pytest.mark.parametrize("role", ROLE_NAMES)
async def test_only_roles_holding_manage_may_seed(role: str) -> None:
    fixture = _world("bv", role)
    allowed = role in _roles_holding(MANAGE_CHART)

    if allowed:
        result = await fixture.service.seed(
            administration_id=fixture.administration_id, actor_user_id=fixture.user
        )
        assert result.seeded_count > 0
    else:
        with pytest.raises(ChartNotAuthorized) as raised:
            await fixture.service.seed(
                administration_id=fixture.administration_id,
                actor_user_id=fixture.user,
            )
        assert raised.value.action == "manage"
        assert raised.value.resource_type == "chart_of_accounts"


@pytest.mark.parametrize("role", ROLE_NAMES)
async def test_only_roles_holding_view_may_read_the_chart(role: str) -> None:
    fixture = await _seeded("bv")
    service, reader = fixture.as_role(role)

    if role in _roles_holding(VIEW_CHART):
        assert await service.chart(
            administration_id=fixture.administration_id, actor_user_id=reader
        )
    else:
        with pytest.raises(ChartNotAuthorized):
            await service.chart(administration_id=fixture.administration_id, actor_user_id=reader)


async def test_a_reader_cannot_extend_the_chart() -> None:
    """The gap Appendix A draws between R and F on the same capability."""
    fixture = _world("bv", "Viewer")

    with pytest.raises(ChartNotAuthorized):
        await fixture.service.add_account(
            administration_id=fixture.administration_id,
            actor_user_id=fixture.user,
            code="4700",
            name="Advieskosten",
            account_type=AccountType.EXPENSE,
        )


async def test_seeding_is_audited() -> None:
    """IAM-090. The chart of accounts is the configuration every later number
    is expressed in, so a change to it with nobody's name on it is exactly
    what an audit log is for.
    """
    fixture = _world("bv")

    await fixture.service.seed(
        administration_id=fixture.administration_id, actor_user_id=fixture.user
    )

    entries = await fixture.audit.search(organization_id=fixture.organization_id)
    seeded = next(e for e in entries if e.action == "seed_chart_of_accounts")
    assert seeded.category is AuditCategory.CONFIGURATION
    assert seeded.outcome is AuditOutcome.SUCCESS
    assert seeded.actor_user_id == fixture.user
    assert seeded.administration_id == fixture.administration_id
    assert seeded.detail["legal_form"] == "bv"
    assert seeded.detail["rgs_version"] == "3.8-provisional"


async def test_a_denied_attempt_is_audited_too() -> None:
    """IAM-090 wants the denial recorded, including on an administration whose
    chart has never been seeded - which is the case that would have raised if
    the audit entry's organization were read from the RGS pin.
    """
    fixture = _world("bv", "Viewer")

    with pytest.raises(ChartNotAuthorized):
        await fixture.service.seed(
            administration_id=fixture.administration_id, actor_user_id=fixture.user
        )

    entries = await fixture.audit.search(organization_id=fixture.organization_id)
    denied = next(e for e in entries if e.outcome is AuditOutcome.DENIED)
    assert denied.action == "manage_chart_of_accounts"
    assert denied.actor_user_id == fixture.user


async def test_an_upgrade_is_audited() -> None:
    fixture = await _seeded("bv")
    fixture.repository.load(successor(load_document()), checksum="v2")

    await fixture.service.apply_upgrade(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        to_version="4.0-test",
    )

    entries = await fixture.audit.search(organization_id=fixture.organization_id)
    upgrade = next(e for e in entries if e.action == "upgrade_rgs_version")
    assert upgrade.detail["to_rgs_version"] == "4.0-test"
    assert upgrade.category is AuditCategory.CONFIGURATION


# ===========================================================================
# FR-GL-005: blocked/active
# ===========================================================================


async def test_an_account_can_be_blocked_and_keeps_its_place_in_the_chart() -> None:
    """A blocked account keeps every posting it already has. A chart that hid
    it would make last year's numbers unreadable, so it stays listed by
    default and is excluded only on request.
    """
    fixture = await _seeded("bv")
    account = next(
        a
        for a in await fixture.service.chart(
            administration_id=fixture.administration_id, actor_user_id=fixture.user
        )
        if a.code == "4300"
    )

    blocked = await fixture.service.set_account_status(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        account_id=account.account_id,
        status=AccountStatus.BLOCKED,
    )
    assert blocked.status is AccountStatus.BLOCKED

    listed = await fixture.service.chart(
        administration_id=fixture.administration_id, actor_user_id=fixture.user
    )
    active_only = await fixture.service.chart(
        administration_id=fixture.administration_id,
        actor_user_id=fixture.user,
        include_blocked=False,
    )

    assert "4300" in {a.code for a in listed}
    assert "4300" not in {a.code for a in active_only}
