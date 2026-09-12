"""FR-FRM-000 and FR-FRM-000a: the client switcher, and making the active
client unmistakable.

FR-FRM-000a names a harm rather than a widget - "posting to the wrong client
is the single worst usability failure in this product" - so the tests are
weighted towards the mechanisms that prevent it, not the ones that decorate
it. The active-client guard is tested in tests/test_authz_middleware.py,
where the request path is.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from api.firm.switcher import (
    FALLBACK_SCREEN,
    PALETTE,
    ClientSwitcher,
    Screen,
    initials_for,
    is_kvk_query,
)
from tests.support.fake_switcher_repository import InMemorySwitcherRepository

USER = uuid.uuid4()
BAKKER = uuid.uuid4()
DE_VRIES = uuid.uuid4()
JANSEN = uuid.uuid4()


class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 6, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


def _switcher() -> tuple[ClientSwitcher, InMemorySwitcherRepository, _FakeClock]:
    repository = InMemorySwitcherRepository()
    clock = _FakeClock()
    repository.grant(
        user_id=USER,
        administration_id=BAKKER,
        legal_name="Bakker Consultancy B.V.",
        trade_name="Bakker IT",
        kvk_number="12345678",
        colour_token="indigo",
        vat_registered=True,
    )
    repository.grant(
        user_id=USER,
        administration_id=DE_VRIES,
        legal_name="De Vries Holding B.V.",
        kvk_number="87654321",
        colour_token="amber",
    )
    repository.grant(
        user_id=USER,
        administration_id=JANSEN,
        legal_name="Jansen Tandarts",
        trade_name="Tandartspraktijk Jansen",
        kvk_number="12349999",
        colour_token="teal",
        vat_registered=True,
    )
    return ClientSwitcher(repository, clock=clock), repository, clock


# ===========================================================================
# FR-FRM-000: only granted administrations
# ===========================================================================


async def test_the_switcher_shows_only_granted_administrations() -> None:
    switcher, repository, _ = _switcher()
    stranger = uuid.uuid4()
    repository.grant(
        user_id=stranger,
        administration_id=uuid.uuid4(),
        legal_name="Somebody Else B.V.",
        colour_token="rose",
    )

    entries = await switcher.list(USER)

    assert {e.badge.administration_id for e in entries} == {BAKKER, DE_VRIES, JANSEN}


async def test_an_expired_grant_leaves_the_switcher() -> None:
    switcher, repository, clock = _switcher()
    seasonal = uuid.uuid4()
    repository.grant(
        user_id=USER,
        administration_id=seasonal,
        legal_name="Seasonal B.V.",
        colour_token="lime",
        expires_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert len(await switcher.list(USER)) == 4

    clock.advance(days=60)

    assert seasonal not in {e.badge.administration_id for e in await switcher.list(USER)}


# ===========================================================================
# FR-FRM-000: searchable by name, trade name, KvK
# ===========================================================================


async def test_search_matches_the_legal_name() -> None:
    switcher, _, _ = _switcher()

    entries = await switcher.list(USER, query="Vries")

    assert [e.badge.administration_id for e in entries] == [DE_VRIES]


async def test_search_matches_the_trade_name() -> None:
    """The name on the invoice, not the one in the deed - which is what a
    bookkeeper looking for a client actually types.
    """
    switcher, _, _ = _switcher()

    entries = await switcher.list(USER, query="Bakker IT")

    assert [e.badge.administration_id for e in entries] == [BAKKER]


async def test_search_matches_a_kvk_number_by_prefix() -> None:
    """Typing the first digits should find the client. Trigram similarity
    over digit strings returns noise, so KvK is matched by prefix.
    """
    switcher, _, _ = _switcher()

    entries = await switcher.list(USER, query="8765")

    assert [e.badge.administration_id for e in entries] == [DE_VRIES]


async def test_a_kvk_prefix_can_match_several_clients() -> None:
    switcher, _, _ = _switcher()

    entries = await switcher.list(USER, query="1234")

    assert {e.badge.administration_id for e in entries} == {BAKKER, JANSEN}


async def test_a_digit_query_does_not_match_names() -> None:
    """An all-digit query is a KvK search. Matching it against names too
    would make "1234" return every client with a 1234 anywhere in an address
    line once those exist.
    """
    switcher, repository, _ = _switcher()
    repository.grant(
        user_id=USER,
        administration_id=uuid.uuid4(),
        legal_name="Bedrijf 1234 B.V.",
        colour_token="violet",
        kvk_number="99999999",
    )

    entries = await switcher.list(USER, query="1234")

    assert all(e.badge.kvk_number and e.badge.kvk_number.startswith("1234") for e in entries)


async def test_search_is_case_insensitive() -> None:
    switcher, _, _ = _switcher()

    assert len(await switcher.list(USER, query="bakker")) == 1
    assert len(await switcher.list(USER, query="BAKKER")) == 1


async def test_an_exact_match_is_offered_first() -> None:
    """Keyboard reachability is type-then-Enter, and that only works if
    position one is what the typist meant. Asserted with a query that also
    matches another client by substring.
    """
    switcher, repository, _ = _switcher()
    repository.grant(
        user_id=USER,
        administration_id=uuid.uuid4(),
        legal_name="Bakker Consultancy Holding B.V.",
        colour_token="cyan",
    )

    entries = await switcher.list(USER, query="Bakker IT")

    assert entries[0].badge.administration_id == BAKKER


async def test_a_prefix_match_beats_a_substring_match() -> None:
    switcher, repository, _ = _switcher()
    substring_match = uuid.uuid4()
    repository.grant(
        user_id=USER,
        administration_id=substring_match,
        legal_name="Van Jansen Advies B.V.",
        colour_token="orange",
    )

    entries = await switcher.list(USER, query="Jansen")

    # "Jansen Tandarts" starts with it; "Van Jansen Advies" merely contains it.
    assert entries[0].badge.administration_id == JANSEN
    assert substring_match in {e.badge.administration_id for e in entries}


async def test_an_empty_or_blank_query_lists_everything() -> None:
    switcher, _, _ = _switcher()

    assert len(await switcher.list(USER, query="")) == 3
    assert len(await switcher.list(USER, query="   ")) == 3
    assert len(await switcher.list(USER, query=None)) == 3


async def test_a_query_matching_nothing_returns_nothing() -> None:
    switcher, _, _ = _switcher()

    assert await switcher.list(USER, query="Nonexistent") == []


# ===========================================================================
# FR-FRM-000a: the badge
# ===========================================================================


async def test_every_entry_carries_a_name_and_a_colour() -> None:
    switcher, _, _ = _switcher()

    for entry in await switcher.list(USER):
        assert entry.badge.display_name
        assert entry.badge.colour_token in PALETTE
        assert len(entry.badge.initials) == 2


async def test_the_display_name_prefers_the_trade_name() -> None:
    """What the client calls itself and what a bookkeeper recognizes. The
    legal name stays on the badge for the screens that need it.
    """
    switcher, _, _ = _switcher()

    entry = await switcher.find(USER, BAKKER)

    assert entry is not None
    assert entry.badge.display_name == "Bakker IT"
    assert entry.badge.name == "Bakker Consultancy B.V."


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Bakker Consultancy B.V.", "BC"),
        ("Jansen", "JA"),
        ("De Vries Holding", "DV"),
        ("X", "X"),
        ("", "??"),
        ("...", "??"),
        ("ç Ünïcode B.V.", "ÇÜ"),
    ],
)
def test_initials_never_depend_on_colour(name: str, expected: str) -> None:
    """The accessibility answer, and the correctness one: a surface too small
    for a full name still says something a colour cannot be confused with.
    """
    assert initials_for(name) == expected


async def test_a_colour_shared_by_two_clients_is_flagged() -> None:
    """The palette has ten entries and a firm may have hundreds of clients,
    so collisions are inevitable. Reporting one lets the UI lean on name and
    initials instead of pretending the colour is distinguishing.
    """
    switcher, repository, _ = _switcher()
    repository.grant(
        user_id=USER,
        administration_id=uuid.uuid4(),
        legal_name="Collision B.V.",
        colour_token="indigo",  # same as Bakker
    )

    entries = await switcher.list(USER)
    by_colour = {e.badge.administration_id: e.colour_is_ambiguous for e in entries}

    assert by_colour[BAKKER] is True
    assert by_colour[DE_VRIES] is False


async def test_no_collision_is_flagged_when_colours_are_distinct() -> None:
    switcher, _, _ = _switcher()

    assert all(not e.colour_is_ambiguous for e in await switcher.list(USER))


async def test_ambiguity_is_computed_within_the_visible_set() -> None:
    """A colour shared with a client the user cannot see is not ambiguous to
    them - they will never have both on screen.
    """
    switcher, repository, _ = _switcher()
    repository.grant(
        user_id=uuid.uuid4(),  # somebody else
        administration_id=uuid.uuid4(),
        legal_name="Other Firm's Client",
        colour_token="indigo",
    )

    entries = await switcher.list(USER)

    assert all(not e.colour_is_ambiguous for e in entries)


# ===========================================================================
# FR-FRM-000: switching preserves the current screen type
# ===========================================================================


async def test_switching_preserves_the_current_screen() -> None:
    switcher, _, _ = _switcher()

    outcome = await switcher.resolve_switch(
        user_id=USER, administration_id=DE_VRIES, current_screen=Screen.LEDGER
    )

    assert outcome is not None
    assert outcome.screen is Screen.LEDGER
    assert outcome.screen_was_preserved


async def test_switching_falls_back_when_the_screen_does_not_exist_there() -> None:
    """ "Where it exists for the target client". De Vries has no VAT
    registration, so there is no VAT screen to land on.
    """
    switcher, _, _ = _switcher()

    outcome = await switcher.resolve_switch(
        user_id=USER, administration_id=DE_VRIES, current_screen=Screen.VAT
    )

    assert outcome is not None
    assert outcome.screen is FALLBACK_SCREEN
    assert not outcome.screen_was_preserved


async def test_a_conditional_screen_is_preserved_when_it_does_exist() -> None:
    switcher, _, _ = _switcher()

    outcome = await switcher.resolve_switch(
        user_id=USER, administration_id=JANSEN, current_screen=Screen.VAT
    )

    assert outcome is not None
    assert outcome.screen is Screen.VAT


async def test_switching_with_no_current_screen_lands_on_the_default() -> None:
    switcher, _, _ = _switcher()

    outcome = await switcher.resolve_switch(user_id=USER, administration_id=BAKKER)

    assert outcome is not None
    assert outcome.screen is FALLBACK_SCREEN
    assert outcome.screen_was_preserved, "nothing was requested, so nothing was lost"


async def test_switching_to_an_ungranted_client_resolves_to_nothing() -> None:
    switcher, _, _ = _switcher()

    outcome = await switcher.resolve_switch(
        user_id=USER, administration_id=uuid.uuid4(), current_screen=Screen.LEDGER
    )

    assert outcome is None


async def test_every_screen_is_either_universal_or_declares_what_it_needs() -> None:
    """Guards against adding a Screen member whose availability nobody
    decided - it would silently be treated as existing for every client.
    """
    switcher, _, _ = _switcher()
    de_vries = await switcher.find(USER, DE_VRIES)
    jansen = await switcher.find(USER, JANSEN)
    assert de_vries is not None and jansen is not None

    # Jansen is VAT-registered, De Vries is not; every screen must be
    # supported by at least the VAT-registered one.
    for screen in Screen:
        assert jansen.supports(screen), f"{screen} exists for no client"
    assert not de_vries.supports(Screen.VAT)


def test_the_palette_matches_the_migration() -> None:
    """PALETTE mirrors the client_colour table. A drift would mean the API
    hands clients a token they cannot render.
    """
    assert len(PALETTE) == 10
    assert len(set(PALETTE)) == 10


def test_kvk_detection() -> None:
    assert is_kvk_query("12345678")
    assert is_kvk_query("1")
    assert not is_kvk_query("Bakker")
    assert not is_kvk_query("12ab")
    assert not is_kvk_query("")
