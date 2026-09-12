"""FR-GL-007: period locking, the unlock authority, and the hard lock.

    FR-GL-007  Period locking per fiscal period with a defined unlock
               authority; VAT-filed periods are hard-locked and require a
               suppletie flow to change.

Three claims, tested separately because they fail separately:

  1. **Locking closes a period to postings.** Already covered by the ledger
     suites; here it is only setup for the rest.
  2. **The unlock authority is defined and enforced.** Appendix A gives
     "Lock / unlock periods" to Owner and Accountant and to nobody else. The
     tests below drive EVERY standard role at it rather than the two that
     should pass - a permission bug does not usually take an authority away,
     it hands one out, and only an exhaustive check sees that.
  3. **VAT-filed is terminal.** No role, no flag, no method reopens it. The
     route is a suppletie.

The `current_user <> 'ledgr_ledger'` gate that makes the authority
unskippable is not testable here - it is about database privileges - and is
asserted in tests/integration/test_period_locking.py.
"""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from datetime import date

import pytest

from api.audit.log import AuditCategory, AuditLog, AuditOutcome
from api.authz.matrix import ROLES, permissions_for_role
from api.authz.service import AuthorizationService
from api.ledger.periods import (
    FILE_VAT_RETURN,
    LOCK_PERIOD,
    PREPARE_VAT_RETURN,
    HardLocked,
    NotAuthorized,
    Period,
    PeriodError,
    PeriodService,
    PeriodStatus,
    SuppletieStatus,
)
from tests.authz.helpers import build_world
from tests.support.fake_audit_repository import InMemoryAuditRepository
from tests.support.fake_period_repository import InMemoryPeriodRepository

ROLE_NAMES = sorted(role.name for role in ROLES)


def _roles_holding(permission: tuple[str, str]) -> set[str]:
    """Read the expectation out of the matrix rather than restating it.

    The parametrized tests below then check BEHAVIOUR against the matrix, and
    the pinning tests check the MATRIX against Appendix A's prose. Two
    separate failures: a service that ignores a permission, and a matrix that
    has drifted from the PRD.
    """
    return {role.name for role in ROLES if permission in set(permissions_for_role(role))}


def test_appendix_a_gives_the_unlock_authority_to_exactly_two_roles() -> None:
    """PRD §8.4: Accountant does "period lock/unlock"; Bookkeeper explicitly
    "cannot: Unlock closed periods". Appendix A's row is
    (F, N, N, N, F, N, N, N, N, N).

    Pinned here as well as in the Appendix A conformance test because THIS is
    the requirement FR-GL-007 calls "a defined unlock authority" - if it ever
    widens, the failure should name period locking, not just a matrix cell.
    """
    assert _roles_holding(LOCK_PERIOD) == {"Owner", "Accountant"}


def test_filing_and_preparing_are_different_authorities() -> None:
    """Appendix A grades "Prepare VAT return" and "File VAT return"
    differently, and a Bookkeeper sits in the gap: they may prepare a
    correction and may not file it. open_suppletie and close_suppletie check
    the two different permissions for exactly that reason.
    """
    assert _roles_holding(FILE_VAT_RETURN) == {"Owner", "Accountant"}
    assert _roles_holding(PREPARE_VAT_RETURN) == {"Owner", "Accountant", "Bookkeeper"}
    assert "Bookkeeper" in _roles_holding(PREPARE_VAT_RETURN) - _roles_holding(FILE_VAT_RETURN)


@dataclass(frozen=True, slots=True)
class Fixture:
    service: PeriodService
    repository: InMemoryPeriodRepository
    period: Period
    user: uuid.UUID
    organization_id: uuid.UUID
    audit: AuditLog


async def _world(role: str = "Owner", *, status: PeriodStatus = PeriodStatus.OPEN) -> Fixture:
    authz = build_world()
    role_definition = next(r for r in ROLES if r.name == role)
    # The fake infers scope_type from the role, so scope_id has to match it:
    # an organization id for an organization-scoped role, an administration id
    # for an administration-scoped one.
    authz.repository.assign(
        user_id=authz.user,
        role=role,
        scope_id=(authz.acme if role_definition.scope_type == "organization" else authz.acme_books),
    )

    repository = InMemoryPeriodRepository(organization_id=authz.acme)
    period = repository.add_period(
        administration_id=authz.acme_books,
        fiscal_year_id=uuid.uuid4(),
        period_number=1,
        status=status,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
    )
    audit = AuditLog(InMemoryAuditRepository())
    return Fixture(
        service=PeriodService(repository, AuthorizationService(authz.repository), audit),
        repository=repository,
        period=period,
        user=authz.user,
        organization_id=authz.acme,
        audit=audit,
    )


# ===========================================================================
# The defined unlock authority
# ===========================================================================


@pytest.mark.parametrize("role", ROLE_NAMES)
async def test_only_the_defined_authority_can_unlock(role: str) -> None:
    w = await _world(role, status=PeriodStatus.LOCKED)

    if role in _roles_holding(LOCK_PERIOD):
        unlocked = await w.service.unlock(
            period_id=w.period.id, user_id=w.user, reason="reopened to correct coding"
        )
        assert unlocked.status is PeriodStatus.OPEN
    else:
        with pytest.raises(NotAuthorized) as raised:
            await w.service.unlock(period_id=w.period.id, user_id=w.user, reason="should not work")
        assert (raised.value.action, raised.value.resource_type) == LOCK_PERIOD
        assert w.repository.periods[w.period.id].status is PeriodStatus.LOCKED


@pytest.mark.parametrize("role", ROLE_NAMES)
async def test_only_the_defined_authority_can_lock(role: str) -> None:
    w = await _world(role)

    if role in _roles_holding(LOCK_PERIOD):
        locked = await w.service.lock(period_id=w.period.id, user_id=w.user)
        assert locked.status is PeriodStatus.LOCKED
    else:
        with pytest.raises(NotAuthorized):
            await w.service.lock(period_id=w.period.id, user_id=w.user)
        assert w.repository.periods[w.period.id].status is PeriodStatus.OPEN


@pytest.mark.parametrize("role", ROLE_NAMES)
async def test_only_the_filing_authority_can_hard_lock(role: str) -> None:
    w = await _world(role)

    if role in _roles_holding(FILE_VAT_RETURN):
        filed = await w.service.mark_filed(period_id=w.period.id, user_id=w.user)
        assert filed.status is PeriodStatus.VAT_FILED
    else:
        with pytest.raises(NotAuthorized) as raised:
            await w.service.mark_filed(period_id=w.period.id, user_id=w.user)
        assert (raised.value.action, raised.value.resource_type) == FILE_VAT_RETURN


@pytest.mark.parametrize("role", ROLE_NAMES)
async def test_preparing_a_suppletie_takes_the_preparing_authority(role: str) -> None:
    w = await _world(role, status=PeriodStatus.VAT_FILED)

    if role in _roles_holding(PREPARE_VAT_RETURN):
        suppletie = await w.service.open_suppletie(
            period_id=w.period.id, user_id=w.user, reason="omzet understated"
        )
        assert suppletie.status is SuppletieStatus.OPEN
    else:
        with pytest.raises(NotAuthorized) as raised:
            await w.service.open_suppletie(
                period_id=w.period.id, user_id=w.user, reason="omzet understated"
            )
        assert (raised.value.action, raised.value.resource_type) == PREPARE_VAT_RETURN


async def test_a_bookkeeper_can_prepare_a_suppletie_but_not_close_it() -> None:
    """The division above, end to end, because it is the point of splitting
    the two permissions and reads as an accident otherwise.
    """
    w = await _world("Bookkeeper", status=PeriodStatus.VAT_FILED)

    suppletie = await w.service.open_suppletie(
        period_id=w.period.id, user_id=w.user, reason="omzet understated"
    )

    with pytest.raises(NotAuthorized) as raised:
        await w.service.close_suppletie(
            suppletie_id=suppletie.id, user_id=w.user, status=SuppletieStatus.FILED
        )
    assert (raised.value.action, raised.value.resource_type) == FILE_VAT_RETURN


async def test_an_unlock_requires_a_stated_reason() -> None:
    """Reopening a closed period is the most consequential thing this service
    does - figures someone has already relied on become editable again. The
    reason goes into the audit log, which is append-only and hash-chained.
    """
    w = await _world(status=PeriodStatus.LOCKED)

    for blank in ("", "   ", "\n"):
        with pytest.raises(PeriodError) as raised:
            await w.service.unlock(period_id=w.period.id, user_id=w.user, reason=blank)
        assert "reason" in str(raised.value)


async def test_unlocking_records_who_held_the_lock() -> None:
    """The transition clears locked_by_user_id, so unless the audit entry
    captures it first, who had locked the period is lost.
    """
    w = await _world()
    locked = await w.service.lock(period_id=w.period.id, user_id=w.user)
    assert locked.locked_by_user_id == w.user

    await w.service.unlock(
        period_id=w.period.id, user_id=w.user, reason="correcting the VAT coding"
    )

    entries = await w.audit.search(
        organization_id=w.organization_id, categories=[AuditCategory.CONFIGURATION]
    )
    unlock_entry = next(e for e in entries if e.action == "unlock_period")
    assert unlock_entry.detail["previously_locked_by"] == str(w.user)
    assert unlock_entry.detail["reason"] == "correcting the VAT coding"


# ===========================================================================
# The hard lock
# ===========================================================================


async def test_a_vat_filed_period_cannot_be_unlocked_by_anyone() -> None:
    """Not "by anyone without permission" - by ANYONE. The Owner holds every
    permission the system defines and still cannot do this.
    """
    w = await _world(status=PeriodStatus.VAT_FILED)

    with pytest.raises(HardLocked) as raised:
        await w.service.unlock(period_id=w.period.id, user_id=w.user, reason="we need to fix it")

    assert "suppletie" in str(raised.value)
    assert "FR-GL-007" in str(raised.value)
    assert w.repository.periods[w.period.id].status is PeriodStatus.VAT_FILED


async def test_the_hard_lock_is_reported_as_its_own_failure_not_as_a_denial() -> None:
    """HardLocked rather than NotAuthorized, deliberately.

    A permission error tells the caller to find someone with a bigger role.
    Nobody has one, so that answer sends them on a search that cannot succeed.
    The type says what the actual next step is.
    """
    w = await _world(status=PeriodStatus.VAT_FILED)

    with pytest.raises(HardLocked):
        await w.service.unlock(period_id=w.period.id, user_id=w.user, reason="x")

    assert not issubclass(HardLocked, NotAuthorized)


async def test_the_hard_lock_is_checked_before_the_permission() -> None:
    """Order matters for the message. A user with no authority hitting a filed
    period should be told about the suppletie, not about their role - the role
    is not the obstacle and never can be.
    """
    w = await _world("Viewer", status=PeriodStatus.VAT_FILED)

    with pytest.raises(HardLocked):
        await w.service.unlock(period_id=w.period.id, user_id=w.user, reason="x")


async def test_the_service_offers_no_way_to_reopen_a_filed_period() -> None:
    """Asserted by construction. There is no `force`, no `reopen`, no
    `set_status`, so there is no argument a caller can pass to get past the
    check above.
    """
    surface = {name for name in dir(PeriodService) if not name.startswith("_")}
    assert not (surface & {"reopen", "force_unlock", "set_status", "unfile"}), surface

    parameters = set(inspect.signature(PeriodService.unlock).parameters)
    assert parameters == {"self", "period_id", "user_id", "reason", "correlation_id"}


async def test_a_filed_period_shows_as_locked_not_as_never_locked() -> None:
    """Filing from `open` skips the locked state. Without carrying a lock
    timestamp across, a filed period would report locked_at = None and read as
    though it had never been closed - the opposite of the truth.
    """
    w = await _world()

    filed = await w.service.mark_filed(
        period_id=w.period.id, user_id=w.user, filing_reference="OB-2026-01"
    )

    assert filed.is_hard_locked
    assert filed.locked_at is not None
    assert filed.filed_at is not None
    assert filed.filing_reference == "OB-2026-01"


# ===========================================================================
# The suppletie flow
# ===========================================================================


async def test_a_suppletie_can_only_be_opened_against_a_filed_period() -> None:
    """Otherwise it would correct nothing - and would give a caller a way to
    dress ordinary postings as statutory corrections.
    """
    for status in (PeriodStatus.OPEN, PeriodStatus.LOCKED):
        w = await _world(status=status)
        with pytest.raises(PeriodError) as raised:
            await w.service.open_suppletie(period_id=w.period.id, user_id=w.user, reason="x")
        assert "filed return to correct" in str(raised.value)


async def test_a_suppletie_requires_a_stated_reason() -> None:
    w = await _world(status=PeriodStatus.VAT_FILED)

    with pytest.raises(PeriodError) as raised:
        await w.service.open_suppletie(period_id=w.period.id, user_id=w.user, reason="  ")
    assert "reason" in str(raised.value)


async def test_only_one_suppletie_is_open_against_a_period_at_a_time() -> None:
    """Two concurrent open corrections would make "the net difference to
    report" ambiguous, and being unambiguous is the whole value of the link.
    """
    w = await _world(status=PeriodStatus.VAT_FILED)

    first = await w.service.open_suppletie(
        period_id=w.period.id, user_id=w.user, reason="omzet understated"
    )
    with pytest.raises(PeriodError):
        await w.service.open_suppletie(period_id=w.period.id, user_id=w.user, reason="another one")

    assert await w.service.open_suppletie_for(w.period.id) == first


async def test_a_period_can_be_corrected_again_after_a_suppletie_closes() -> None:
    """The one-at-a-time rule is about concurrency, not a lifetime limit: a
    period may genuinely need correcting more than once.
    """
    w = await _world(status=PeriodStatus.VAT_FILED)

    first = await w.service.open_suppletie(
        period_id=w.period.id, user_id=w.user, reason="omzet understated"
    )
    await w.service.close_suppletie(
        suppletie_id=first.id, user_id=w.user, status=SuppletieStatus.FILED
    )

    second = await w.service.open_suppletie(
        period_id=w.period.id, user_id=w.user, reason="and again"
    )
    assert second.id != first.id


async def test_the_filed_period_itself_never_changes() -> None:
    """The whole point of the flow. A suppletie is opened, closed, and opened
    again, and the period it corrects is identical throughout.
    """
    w = await _world(status=PeriodStatus.VAT_FILED)
    before = w.repository.periods[w.period.id]

    suppletie = await w.service.open_suppletie(
        period_id=w.period.id, user_id=w.user, reason="omzet understated"
    )
    await w.service.close_suppletie(
        suppletie_id=suppletie.id,
        user_id=w.user,
        status=SuppletieStatus.FILED,
        filing_reference="SUP-2026-01",
    )
    await w.service.open_suppletie(
        period_id=w.period.id, user_id=w.user, reason="another correction"
    )

    assert w.repository.periods[w.period.id] == before


async def test_a_closed_suppletie_cannot_be_closed_again() -> None:
    w = await _world(status=PeriodStatus.VAT_FILED)

    suppletie = await w.service.open_suppletie(
        period_id=w.period.id, user_id=w.user, reason="omzet understated"
    )
    await w.service.close_suppletie(
        suppletie_id=suppletie.id, user_id=w.user, status=SuppletieStatus.SUBMITTED
    )

    with pytest.raises(PeriodError):
        await w.service.close_suppletie(
            suppletie_id=suppletie.id, user_id=w.user, status=SuppletieStatus.FILED
        )


async def test_a_suppletie_cannot_be_closed_as_open() -> None:
    w = await _world(status=PeriodStatus.VAT_FILED)
    suppletie = await w.service.open_suppletie(
        period_id=w.period.id, user_id=w.user, reason="omzet understated"
    )

    with pytest.raises(PeriodError):
        await w.service.close_suppletie(
            suppletie_id=suppletie.id, user_id=w.user, status=SuppletieStatus.OPEN
        )


# ===========================================================================
# IAM-090
# ===========================================================================


async def test_every_transition_is_audited() -> None:
    w = await _world()

    await w.service.lock(period_id=w.period.id, user_id=w.user)
    await w.service.unlock(period_id=w.period.id, user_id=w.user, reason="reopened")
    await w.service.mark_filed(period_id=w.period.id, user_id=w.user, filing_reference="OB-2026-01")

    recorded = list(reversed(await w.audit.search(organization_id=w.organization_id)))
    assert [e.action for e in recorded] == [
        "lock_period",
        "unlock_period",
        "mark_period_filed",
    ]
    assert [e.category for e in recorded] == [
        AuditCategory.CONFIGURATION,
        AuditCategory.CONFIGURATION,
        # Marking a period filed is the ledger half of a statutory filing, so
        # it is recorded under `filing` rather than `configuration`: it is what
        # an inspector looks for, and it is what applies the hard lock.
        AuditCategory.FILING,
    ]
    assert all(e.resource_type == "period" for e in recorded)


async def test_a_refused_transition_is_audited_as_denied() -> None:
    """Someone repeatedly trying to unlock a period they have no authority
    over is exactly what a reviewer is looking for, so a denial that left no
    trace would be the wrong silence.
    """
    w = await _world("Bookkeeper", status=PeriodStatus.LOCKED)

    with pytest.raises(NotAuthorized):
        await w.service.unlock(period_id=w.period.id, user_id=w.user, reason="please")

    recorded = await w.audit.search(organization_id=w.organization_id)
    assert [(e.action, e.outcome) for e in recorded] == [("lock_period", AuditOutcome.DENIED)]


async def test_the_suppletie_flow_is_audited_as_filing() -> None:
    w = await _world(status=PeriodStatus.VAT_FILED)

    suppletie = await w.service.open_suppletie(
        period_id=w.period.id, user_id=w.user, reason="omzet understated"
    )
    await w.service.close_suppletie(
        suppletie_id=suppletie.id, user_id=w.user, status=SuppletieStatus.FILED
    )

    recorded = list(
        reversed(
            await w.audit.search(
                organization_id=w.organization_id, categories=[AuditCategory.FILING]
            )
        )
    )
    assert [e.action for e in recorded] == ["open_suppletie", "close_suppletie"]
    assert recorded[0].detail["reason"] == "omzet understated"
    assert recorded[0].detail["suppletie_id"] == str(suppletie.id)
