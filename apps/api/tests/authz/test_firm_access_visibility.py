"""IAM-109 and IAM-110 (PRD §8.6): what the client can see about the firm's
access, and what happens when they end it.

    IAM-109  The client sees, at any time and without asking, which firm
             users hold access to their administration, with what role,
             since when, and when each last accessed it.
    IAM-110  On revocation of firm access, firm sessions for that
             administration terminate immediately, the administration
             disappears from the firm switcher, and the client retains all
             data including entries the firm made.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from api.authz.engagement_revocation import (
    EngagementRevocationService,
    NoSessionError,
    NoSuchEngagementError,
    NotPermittedToRevokeError,
)
from api.authz.firm_access_register import ACCESS_RECORD_WINDOW, FirmAccessRegister
from api.authz.model import AdministrationScope, AuthorizationRequest
from api.authz.service import AuthorizationService
from tests.authz.helpers import World, build_world
from tests.support.fake_firm_access_repository import InMemoryFirmAccessRepository


class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def _setup() -> tuple[
    World,
    InMemoryFirmAccessRepository,
    FirmAccessRegister,
    EngagementRevocationService,
    AuthorizationService,
    _FakeClock,
]:
    """klaver (a firm) engaged on acme_books, with one accountant granted
    access and the client's own bookkeeper alongside them.
    """
    world = build_world()
    clock = _FakeClock()
    repository = InMemoryFirmAccessRepository(world.repository)
    repository.name_organization(world.klaver, "Klaver & Partners")
    repository.name_administration(world.acme_books, "Bakker Consultancy B.V.")
    repository.name_administration(world.klaver_books, "Klaver & Partners B.V.")
    return (
        world,
        repository,
        FirmAccessRegister(repository, clock=clock),
        EngagementRevocationService(repository, clock=clock),
        AuthorizationService(world.repository, clock=clock),
        clock,
    )


def _grant_firm_accountant(
    world: World,
    repository: InMemoryFirmAccessRepository,
    *,
    email: str,
    at: datetime | None = None,
) -> uuid.UUID:
    staff = uuid.uuid4()
    repository.add_user(staff, email)
    world.repository.assign(
        user_id=staff,
        role="Accountant",
        scope_id=world.acme_books,
        granted_by_organization_id=world.klaver,
        granted_by_user_id=staff,
        created_at=at,
    )
    return staff


# ===========================================================================
# IAM-109: the client's register
# ===========================================================================


async def test_the_register_names_the_firm_user_role_and_when_access_began() -> None:
    world, repository, register, _, _, clock = _setup()
    granted_at = clock.now
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl", at=granted_at)

    entries = await register.list_firm_access(world.acme_books)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.user_id == staff
    assert entry.email == "jan@klaver.nl"
    assert entry.firm_name == "Klaver & Partners"
    assert entry.role_name == "Accountant"
    # IAM-109's "since when".
    assert entry.granted_at == granted_at


async def test_the_register_shows_when_each_user_last_accessed() -> None:
    """The one column of IAM-109 that had nowhere to live before 0017."""
    world, repository, register, _, _, clock = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")

    clock.advance(hours=3)
    await register.record_access(user_id=staff, administration_id=world.acme_books)
    accessed_at = clock.now

    entries = await register.list_firm_access(world.acme_books)

    assert entries[0].last_accessed_at == accessed_at
    assert entries[0].has_ever_accessed


async def test_a_firm_user_who_has_never_looked_still_appears() -> None:
    """The row a client most needs to see: someone who holds access to their
    books and has never opened them. An INNER JOIN on the access table would
    drop exactly this person.
    """
    world, repository, register, _, _, _ = _setup()
    _grant_firm_accountant(world, repository, email="never@klaver.nl")

    entries = await register.list_firm_access(world.acme_books)

    assert len(entries) == 1
    assert entries[0].last_accessed_at is None
    assert not entries[0].has_ever_accessed
    assert entries[0].access_count == 0


async def test_the_register_excludes_the_clients_own_users() -> None:
    """ "Which FIRM users hold access". The client's own bookkeeper holds an
    identically-shaped administration-scoped grant on the same
    administration; only provenance separates them.
    """
    world, repository, register, _, _, _ = _setup()
    firm_user = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    client_user = uuid.uuid4()
    repository.add_user(client_user, "bookkeeper@bakker.nl")
    world.repository.assign(user_id=client_user, role="Bookkeeper", scope_id=world.acme_books)

    entries = await register.list_firm_access(world.acme_books)

    assert [e.user_id for e in entries] == [firm_user]


async def test_the_register_shows_only_live_access() -> None:
    """Present tense: "which firm users HOLD access". A client scanning the
    list to decide whether to revoke must not have to work out which rows
    are still in force.
    """
    world, repository, register, _, _, clock = _setup()
    seasonal = uuid.uuid4()
    repository.add_user(seasonal, "interim@klaver.nl")
    world.repository.assign(
        user_id=seasonal,
        role="Bookkeeper",
        scope_id=world.acme_books,
        granted_by_organization_id=world.klaver,
        expires_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    assert len(await register.list_firm_access(world.acme_books)) == 1

    clock.advance(days=60)

    assert await register.list_firm_access(world.acme_books) == []


async def test_the_register_surfaces_a_seasonal_grants_expiry() -> None:
    """IAM-108's expiry is part of what a client is entitled to see - "holds
    access until September" is a materially different fact from "holds
    access".
    """
    world, repository, register, _, _, _ = _setup()
    expiry = datetime(2026, 9, 1, tzinfo=UTC)
    seasonal = uuid.uuid4()
    repository.add_user(seasonal, "interim@klaver.nl")
    world.repository.assign(
        user_id=seasonal,
        role="Bookkeeper",
        scope_id=world.acme_books,
        granted_by_organization_id=world.klaver,
        expires_at=expiry,
    )

    entries = await register.list_firm_access(world.acme_books)

    assert entries[0].expires_at == expiry


async def test_the_most_recently_active_appear_first() -> None:
    """People who have actually been in the books before people who merely
    could be.
    """
    world, repository, register, _, _, clock = _setup()
    dormant = _grant_firm_accountant(world, repository, email="dormant@klaver.nl")
    active = _grant_firm_accountant(world, repository, email="active@klaver.nl")

    clock.advance(hours=1)
    await register.record_access(user_id=active, administration_id=world.acme_books)

    entries = await register.list_firm_access(world.acme_books)

    assert [e.user_id for e in entries] == [active, dormant]


# --- access recording -------------------------------------------------------


async def test_repeated_accesses_inside_the_window_collapse() -> None:
    """A write per request would put a write on the read path of every page
    a firm user opens.
    """
    world, repository, register, _, _, clock = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")

    assert await register.record_access(user_id=staff, administration_id=world.acme_books)
    clock.advance(seconds=30)
    assert not await register.record_access(user_id=staff, administration_id=world.acme_books)

    entries = await register.list_firm_access(world.acme_books)
    assert entries[0].access_count == 1


async def test_an_access_after_the_window_is_recorded() -> None:
    world, repository, register, _, _, clock = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    await register.record_access(user_id=staff, administration_id=world.acme_books)

    clock.advance(seconds=int(ACCESS_RECORD_WINDOW.total_seconds()) + 60)
    assert await register.record_access(user_id=staff, administration_id=world.acme_books)

    entries = await register.list_firm_access(world.acme_books)
    assert entries[0].last_accessed_at == clock.now
    assert entries[0].access_count == 2


async def test_a_straggler_request_cannot_move_last_access_backwards() -> None:
    """A retried request carrying an older timestamp must not make a firm
    user's last access appear EARLIER than it was - the direction that would
    mislead a client checking who has been in their books.

    On the ordinary path the throttle already absorbs this: an `at` older
    than the stored value never clears the cutoff, so the write is skipped.
    Asserted here so that remains true rather than becoming an accident.
    """
    world, repository, register, _, _, clock = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    await register.record_access(user_id=staff, administration_id=world.acme_books)

    clock.advance(hours=2)
    await register.record_access(user_id=staff, administration_id=world.acme_books)
    latest = clock.now

    clock.now = latest - timedelta(hours=1)
    assert not await register.record_access(user_id=staff, administration_id=world.acme_books)

    entries = await register.list_firm_access(world.acme_books)
    assert entries[0].last_accessed_at == latest


async def test_a_direct_write_cannot_move_last_access_backwards() -> None:
    """administration_access_monotonic_trg, on the only path that can reach
    it: a raw UPDATE that skips the throttle - a backfill, an ops script, or
    a future service writing the row directly.
    """
    world, repository, register, _, _, clock = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    clock.advance(hours=4)
    await register.record_access(user_id=staff, administration_id=world.acme_books)
    latest = clock.now

    repository.force_access_timestamp(
        user_id=staff,
        administration_id=world.acme_books,
        at=latest - timedelta(days=30),
    )

    entries = await register.list_firm_access(world.acme_books)
    assert entries[0].last_accessed_at == latest


async def test_first_access_is_preserved_across_later_ones() -> None:
    world, repository, register, _, _, clock = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    await register.record_access(user_id=staff, administration_id=world.acme_books)
    first = clock.now

    clock.advance(days=5)
    await register.record_access(user_id=staff, administration_id=world.acme_books)

    entries = await register.list_firm_access(world.acme_books)
    assert entries[0].first_accessed_at == first
    assert entries[0].last_accessed_at == clock.now


# ===========================================================================
# IAM-110: revocation
# ===========================================================================


async def test_revocation_stops_the_firms_access_on_the_next_request() -> None:
    """ "Terminate immediately". Authorization is evaluated per request
    against live grants (IAM-034), so there is no cache to invalidate and no
    window to close.
    """
    world, repository, register, revocation, authz, _ = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    request = AuthorizationRequest(
        user_id=staff,
        action="post",
        resource_type="journal_entry",
        target=AdministrationScope(world.acme_books),
    )
    assert (await authz.authorize(request)).allowed

    await revocation.revoke_firm_access(
        administration_id=world.acme_books,
        firm_organization_id=world.klaver,
        revoked_by_user_id=world.user,
        acting_organization_id=world.acme,
    )

    assert (await authz.authorize(request)).denied


async def test_revocation_clears_the_session_context_for_that_administration() -> None:
    world, repository, _, revocation, _, _ = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    session = repository.open_session(user_id=staff, active_administration_id=world.acme_books)

    receipt = await revocation.revoke_firm_access(
        administration_id=world.acme_books,
        firm_organization_id=world.klaver,
        revoked_by_user_id=world.user,
        acting_organization_id=world.acme,
    )

    assert receipt.sessions_cleared == 1
    assert repository.session_context(session) is None


async def test_revocation_does_not_sign_the_firm_user_out_of_ledgr() -> None:
    """Losing one client puts a firm employee back at the switcher, not out
    of the product. Revoking the session would also cut them off from every
    other client they work on.
    """
    world, repository, _, revocation, _, _ = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    session = repository.open_session(user_id=staff, active_administration_id=world.acme_books)

    await revocation.revoke_firm_access(
        administration_id=world.acme_books,
        firm_organization_id=world.klaver,
        revoked_by_user_id=world.user,
        acting_organization_id=world.acme,
    )

    assert not repository.session_is_revoked(session)


async def test_a_session_inside_another_client_is_untouched() -> None:
    world, repository, _, revocation, _, _ = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    world.repository.assign(user_id=staff, role="Accountant", scope_id=world.klaver_books)
    elsewhere = repository.open_session(user_id=staff, active_administration_id=world.klaver_books)

    await revocation.revoke_firm_access(
        administration_id=world.acme_books,
        firm_organization_id=world.klaver,
        revoked_by_user_id=world.user,
        acting_organization_id=world.acme,
    )

    assert repository.session_context(elsewhere) == world.klaver_books


async def test_the_administration_disappears_from_the_firm_switcher() -> None:
    world, repository, _, revocation, _, _ = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    world.repository.assign(user_id=staff, role="Accountant", scope_id=world.klaver_books)

    before = await revocation.switchable_administrations(staff)
    assert {a.administration_id for a in before} == {world.acme_books, world.klaver_books}

    await revocation.revoke_firm_access(
        administration_id=world.acme_books,
        firm_organization_id=world.klaver,
        revoked_by_user_id=world.user,
        acting_organization_id=world.acme,
    )

    after = await revocation.switchable_administrations(staff)
    assert {a.administration_id for a in after} == {world.klaver_books}


async def test_the_switcher_is_derived_not_stored() -> None:
    """An entry disappears because the grant that put it there is gone, not
    because revocation remembered to remove it from a list. Demonstrated by
    a grant expiring, which no revocation code path touches at all.
    """
    world, repository, _, revocation, _, clock = _setup()
    seasonal = uuid.uuid4()
    world.repository.assign(
        user_id=seasonal,
        role="Bookkeeper",
        scope_id=world.acme_books,
        granted_by_organization_id=world.klaver,
        expires_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert len(await revocation.switchable_administrations(seasonal)) == 1

    clock.advance(days=60)

    assert await revocation.switchable_administrations(seasonal) == []


async def test_the_switcher_names_the_administration_and_role() -> None:
    world, repository, _, revocation, _, _ = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")

    entries = await revocation.switchable_administrations(staff)

    assert entries[0].legal_name == "Bakker Consultancy B.V."
    assert entries[0].role_name == "Accountant"


async def test_revocation_leaves_the_clients_own_users_untouched() -> None:
    """The firm's grants are revoked, not everyone's. The client's bookkeeper
    holds an identically-shaped row on the same administration.
    """
    world, repository, _, revocation, authz, _ = _setup()
    _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    client_user = uuid.uuid4()
    world.repository.assign(user_id=client_user, role="Bookkeeper", scope_id=world.acme_books)

    await revocation.revoke_firm_access(
        administration_id=world.acme_books,
        firm_organization_id=world.klaver,
        revoked_by_user_id=world.user,
        acting_organization_id=world.acme,
    )

    decision = await authz.authorize(
        AuthorizationRequest(
            user_id=client_user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_books),
        )
    )
    assert decision.allowed


async def test_the_receipt_states_what_was_retained() -> None:
    """IAM-110's third clause is a non-action, so the receipt says it
    positively rather than leaving the absence of deletion to be inferred.
    """
    world, repository, _, revocation, _, _ = _setup()
    _grant_firm_accountant(world, repository, email="jan@klaver.nl")

    receipt = await revocation.revoke_firm_access(
        administration_id=world.acme_books,
        firm_organization_id=world.klaver,
        revoked_by_user_id=world.user,
        acting_organization_id=world.acme,
    )

    assert receipt.grants_revoked == 1
    assert "retained" in receipt.retained
    assert "removes nothing" in receipt.retained


async def test_the_access_history_survives_revocation() -> None:
    """ "The client retains all data" includes the record of who was in their
    books. A firm losing access must not thereby erase the evidence of what
    it did - administration_access has no DELETE grant.
    """
    world, repository, register, revocation, _, clock = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    clock.advance(hours=2)
    await register.record_access(user_id=staff, administration_id=world.acme_books)
    accessed_at = clock.now

    await revocation.revoke_firm_access(
        administration_id=world.acme_books,
        firm_organization_id=world.klaver,
        revoked_by_user_id=world.user,
        acting_organization_id=world.acme,
    )

    # The register lists LIVE access, so the entry is gone from it - but the
    # underlying record is not, and re-granting shows the old history.
    assert await register.list_firm_access(world.acme_books) == []
    world.repository.engage_firm(
        firm_organization_id=world.klaver, administration_id=world.acme_books
    )
    world.repository.assign(
        user_id=staff,
        role="Accountant",
        scope_id=world.acme_books,
        granted_by_organization_id=world.klaver,
    )
    entries = await register.list_firm_access(world.acme_books)
    assert entries[0].last_accessed_at == accessed_at


# --- entering an administration (the write revocation clears) ---------------


async def test_switching_sets_the_sessions_active_administration() -> None:
    """The write that makes IAM-110's "sessions for that administration"
    name specific rows.
    """
    world, repository, _, revocation, _, _ = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    session = repository.open_session(user_id=staff, active_administration_id=None)

    entry = await revocation.switch_to(
        session_id=session, user_id=staff, administration_id=world.acme_books
    )

    assert entry.administration_id == world.acme_books
    assert repository.session_context(session) == world.acme_books


async def test_leaving_returns_the_session_to_the_switcher() -> None:
    world, repository, _, revocation, _, _ = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    session = repository.open_session(user_id=staff, active_administration_id=world.acme_books)

    assert await revocation.leave_administration(session_id=session, user_id=staff)

    assert repository.session_context(session) is None


async def test_switching_into_an_administration_not_in_the_switcher_is_refused() -> None:
    """Holding `view administration` and appearing in the switcher are not
    the same set, so switch_to re-checks rather than trusting the route's
    permission check alone. Writing a context the switcher would not offer
    would put a session somewhere the UI cannot represent.
    """
    world, repository, _, revocation, _, _ = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    session = repository.open_session(user_id=staff, active_administration_id=None)

    with pytest.raises(NoSuchEngagementError):
        await revocation.switch_to(
            session_id=session, user_id=staff, administration_id=world.acme_holding
        )

    assert repository.session_context(session) is None


async def test_switching_without_a_session_identifier_is_refused() -> None:
    """Writing to every session the user holds would move devices they are
    not holding.

    Asserts the REASON, not just the refusal. Without the early guard the
    repository lookup would refuse this anyway (no row has a null id), so a
    bare `pytest.raises` would pass with the guard deleted - and the caller
    would get "session ... is not a live session for this user", which is
    both wrong and unactionable for a client whose token simply lacks a
    `sid`.
    """
    world, repository, _, revocation, _, _ = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")

    with pytest.raises(NoSessionError, match="no session identifier"):
        await revocation.switch_to(
            session_id=None, user_id=staff, administration_id=world.acme_books
        )

    with pytest.raises(NoSessionError, match="no session identifier"):
        await revocation.leave_administration(session_id=None, user_id=staff)


async def test_one_user_cannot_move_another_users_session() -> None:
    """The session identifier alone is not authority - the write is scoped
    to the holder too.
    """
    world, repository, _, revocation, _, _ = _setup()
    owner = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    intruder = _grant_firm_accountant(world, repository, email="mal@klaver.nl")
    session = repository.open_session(user_id=owner, active_administration_id=None)

    with pytest.raises(NoSessionError):
        await revocation.switch_to(
            session_id=session, user_id=intruder, administration_id=world.acme_books
        )

    assert repository.session_context(session) is None


async def test_a_revoked_grant_removes_the_administration_from_the_switcher_target() -> None:
    """After revocation the administration is no longer switchable, so a
    session cannot be moved back into it - the switcher and the write agree.
    """
    world, repository, _, revocation, _, _ = _setup()
    staff = _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    session = repository.open_session(user_id=staff, active_administration_id=None)
    await revocation.revoke_firm_access(
        administration_id=world.acme_books,
        firm_organization_id=world.klaver,
        revoked_by_user_id=world.user,
        acting_organization_id=world.acme,
    )

    with pytest.raises(NoSuchEngagementError):
        await revocation.switch_to(
            session_id=session, user_id=staff, administration_id=world.acme_books
        )


# --- who may revoke ---------------------------------------------------------


async def test_a_firm_cannot_revoke_its_own_engagement() -> None:
    """IAM-105 puts "the ability to revoke the firm's access" in the client
    rights floor. Letting the firm do it would make the audit trail lie
    about who ended the relationship.
    """
    world, repository, _, revocation, _, _ = _setup()
    _grant_firm_accountant(world, repository, email="jan@klaver.nl")

    with pytest.raises(NotPermittedToRevokeError):
        await revocation.revoke_firm_access(
            administration_id=world.acme_books,
            firm_organization_id=world.klaver,
            revoked_by_user_id=world.user,
            acting_organization_id=world.klaver,
        )


async def test_an_unrelated_organization_cannot_revoke() -> None:
    world, repository, _, revocation, _, _ = _setup()
    _grant_firm_accountant(world, repository, email="jan@klaver.nl")

    with pytest.raises(NotPermittedToRevokeError):
        await revocation.revoke_firm_access(
            administration_id=world.acme_books,
            firm_organization_id=world.klaver,
            revoked_by_user_id=world.user,
            acting_organization_id=uuid.uuid4(),
        )


async def test_revoking_an_engagement_that_is_not_active_is_refused() -> None:
    world, _, _, revocation, _, _ = _setup()

    with pytest.raises(NoSuchEngagementError):
        await revocation.revoke_firm_access(
            administration_id=world.acme_holding,
            firm_organization_id=world.klaver,
            revoked_by_user_id=world.user,
            acting_organization_id=world.acme,
        )


async def test_revoking_twice_is_refused_rather_than_silently_repeated() -> None:
    world, repository, _, revocation, _, _ = _setup()
    _grant_firm_accountant(world, repository, email="jan@klaver.nl")
    await revocation.revoke_firm_access(
        administration_id=world.acme_books,
        firm_organization_id=world.klaver,
        revoked_by_user_id=world.user,
        acting_organization_id=world.acme,
    )

    with pytest.raises(NoSuchEngagementError):
        await revocation.revoke_firm_access(
            administration_id=world.acme_books,
            firm_organization_id=world.klaver,
            revoked_by_user_id=world.user,
            acting_organization_id=world.acme,
        )
