"""FR-FRM-000 and FR-FRM-000a: the client switcher, and making the active
client unmistakable.

    FR-FRM-000   Client switcher: searchable by client name, KvK number or
                 trade name, keyboard-reachable, showing only granted
                 administrations. Switching preserves the current screen type
                 where it exists for the target client.
    FR-FRM-000a  The active client is unmistakable at all times - persistent
                 name and colour marker in the header on every screen, on web
                 and mobile. Posting to the wrong client is the single worst
                 usability failure in this product.

--- Treating FR-FRM-000a as correctness ---

The requirement names the harm ("posting to the wrong client") rather than
the widget, so the answer is not only a coloured badge. Three things carry
it, in descending order of how much they actually protect:

  1. api.authz.dependencies refuses a MUTATING request that names a
     different administration from the one the session is in. If the header
     says Bakker and the request posts to De Vries, one of them is wrong and
     the write does not happen. This is the only mechanism here that
     prevents the failure rather than making it less likely.
  2. The badge shown in the header is derived from the SESSION's active
     administration (IAM-110's active_administration_id), never from a
     parameter the screen passes in. A header that took its name from the
     page's own state could drift from what the page posts to; one derived
     from the session cannot.
  3. The colour marker, which makes a wrong client noticeable at a glance
     before anything is submitted.

Colour is deliberately the weakest of the three and is never the only
signal: ClientBadge always carries the name, and carries `initials` so a
surface too small for a full name still says something a colour cannot be
confused with. Two clients in one switcher CAN share a colour - the palette
has ten entries and a firm may have hundreds of clients - so the switcher
reports `colour_is_ambiguous` rather than pretending otherwise, and the UI
is expected to lean on name and initials there.

--- Search ---

Three keys, and they are not searched the same way. A KvK number is eight
digits and is matched by PREFIX: typing "1234" should find 12345678, and
trigram similarity over digit strings returns noise. Names are matched by
substring, because a bookkeeper types the fragment they remember rather than
the opening of the legal name.

Ordering puts exact and prefix matches before substring ones, then falls
back to name, so the first keyboard-selectable result is the one a person
typing a specific client's name is reaching for. That ordering is part of
what makes the switcher keyboard-usable: the interaction is type, then
Enter.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol

# Mirrors the client_colour table in migration 0018. Ordering matters - it is
# the tie-break the allocation trigger uses.
PALETTE: tuple[str, ...] = (
    "indigo",
    "amber",
    "teal",
    "rose",
    "lime",
    "violet",
    "cyan",
    "orange",
    "emerald",
    "fuchsia",
)

# [0-9] rather than \d: in Unicode mode \d also matches Arabic-Indic and
# other digit forms, and a KvK number is eight ASCII digits. A query in
# another numeral system is a name query, not a company-number one.
_DIGITS = re.compile(r"^[0-9]+$")

# [^\W_] is "word character except underscore", Unicode-aware. An ASCII-only
# class silently mangles the accented names that are ordinary in Dutch and
# every other market this ships to - "ç Ünïcode B.V." would initial as NC.
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def initials_for(name: str) -> str:
    """Two characters that are not a colour. Used where a surface is too
    small for a full name - a mobile header, a dense list - so the client is
    still identified by something a colour-blind or colour-clipped rendering
    cannot lose.
    """
    words: list[str] = _WORD.findall(name)
    if not words:
        return "??"
    if len(words) == 1:
        return words[0][:2].upper()
    return f"{words[0][0]}{words[1][0]}".upper()


class Screen(Enum):
    """FR-FRM-000's "current screen type". A closed set, because
    "preserves the screen where it exists for the target" needs to know
    which screens can fail to exist - an open string could not answer that.

    Only VAT screens are conditional today: an administration with no VAT
    registration has no VAT return to show. The rest exist for every client,
    and saying so explicitly is what lets the fallback be a rule rather than
    a guess.
    """

    DASHBOARD = "dashboard"
    DOCUMENTS = "documents"
    LEDGER = "ledger"
    BANK = "bank"
    SALES_INVOICES = "sales_invoices"
    PURCHASE_INVOICES = "purchase_invoices"
    EXPENSES = "expenses"
    REPORTS = "reports"
    VAT = "vat"
    SETTINGS = "settings"


# Screens that do not exist for every administration, with what they need.
_CONDITIONAL_SCREENS: dict[Screen, str] = {Screen.VAT: "vat_registered"}

FALLBACK_SCREEN = Screen.DASHBOARD


@dataclass(frozen=True, slots=True)
class ClientBadge:
    """What every screen's header renders. One type, one producer, so web and
    mobile cannot disagree about what identifies a client.
    """

    administration_id: uuid.UUID
    name: str
    colour_token: str
    initials: str
    trade_name: str | None = None
    kvk_number: str | None = None

    @property
    def display_name(self) -> str:
        """The trade name when there is one - it is what the client calls
        itself and what a bookkeeper recognizes - with the legal name still
        available on the badge for the screens that need it.
        """
        return self.trade_name or self.name


@dataclass(frozen=True, slots=True)
class SwitcherEntry:
    badge: ClientBadge
    role_name: str
    expires_at: datetime | None = None
    vat_registered: bool = False
    # FR-LOC-001: whether `role_name` is one of PRD §8.4's twelve system roles
    # (LEDGR's own vocabulary, and translated) or a custom role composed by an
    # organization (ADR-013), whose name is that organization's own words and
    # is shown verbatim - the same rule a client's legal name follows.
    #
    # Only the database can answer this: an organization is free to name a
    # custom role "Bookkeeper", and a client deciding by whether the name
    # happens to be in the catalogue would translate it to "Boekhouder" and
    # show a role that organization does not have.
    #
    # Defaults False, which is the safe direction: an unknown provenance
    # renders the name as given rather than substituting a different word for
    # it.
    role_is_system: bool = False
    # True when another entry in the SAME switcher carries this colour. The
    # palette is small and a firm's portfolio is not, so this is a fact to
    # report rather than a case to pretend away.
    colour_is_ambiguous: bool = False

    def supports(self, screen: Screen) -> bool:
        requirement = _CONDITIONAL_SCREENS.get(screen)
        if requirement is None:
            return True
        return bool(getattr(self, requirement, False))


@dataclass(frozen=True, slots=True)
class SwitchOutcome:
    """FR-FRM-000's "switching preserves the current screen type where it
    exists for the target client" - as a value, so the caller does not have
    to re-derive it and web and mobile land in the same place.
    """

    entry: SwitcherEntry
    screen: Screen
    requested_screen: Screen | None = None

    @property
    def screen_was_preserved(self) -> bool:
        return self.requested_screen is None or self.screen is self.requested_screen


class SwitcherRepository(Protocol):
    async def granted_administrations(
        self, *, user_id: uuid.UUID, query: str | None, now: datetime
    ) -> Sequence[SwitcherEntry]:
        """Administrations this user holds a live grant on, optionally
        filtered. Filtering belongs in the query rather than in Python: a
        firm with hundreds of clients should not transfer all of them to
        find one, and FR-FRM-000's search keys have indexes (0018) built for
        exactly these predicates.
        """
        ...


class ClientSwitcher:
    def __init__(
        self,
        repository: SwitcherRepository,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._repository = repository
        self._clock = clock

    async def list(self, user_id: uuid.UUID, *, query: str | None = None) -> list[SwitcherEntry]:
        """FR-FRM-000. "Showing only granted administrations" is the
        repository's predicate, not a filter applied afterwards - which
        matters because a switcher that fetched everything and hid some rows
        would leak client names to anyone who could read a response body.
        """
        entries = list(
            await self._repository.granted_administrations(
                user_id=user_id, query=_normalize(query), now=self._clock()
            )
        )
        return _flag_colour_collisions(entries)

    async def find(self, user_id: uuid.UUID, administration_id: uuid.UUID) -> SwitcherEntry | None:
        for entry in await self.list(user_id):
            if entry.badge.administration_id == administration_id:
                return entry
        return None

    async def resolve_switch(
        self,
        *,
        user_id: uuid.UUID,
        administration_id: uuid.UUID,
        current_screen: Screen | None = None,
    ) -> SwitchOutcome | None:
        """Where switching to this client should land.

        Returns None when the target is not in the user's switcher at all -
        the caller turns that into a refusal. Screen preservation is
        deliberately computed here rather than in the route, so the web and
        mobile clients cannot land in different places from the same switch.
        """
        entry = await self.find(user_id, administration_id)
        if entry is None:
            return None

        screen = current_screen or FALLBACK_SCREEN
        if not entry.supports(screen):
            screen = FALLBACK_SCREEN

        return SwitchOutcome(entry=entry, screen=screen, requested_screen=current_screen)


def _normalize(query: str | None) -> str | None:
    if query is None:
        return None
    stripped = query.strip()
    return stripped or None


def is_kvk_query(query: str) -> bool:
    """A KvK number is eight digits. An all-digit query is matched against
    kvk_number by prefix; anything else is a name.
    """
    return bool(_DIGITS.match(query))


def _flag_colour_collisions(entries: list[SwitcherEntry]) -> list[SwitcherEntry]:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.badge.colour_token] = counts.get(entry.badge.colour_token, 0) + 1

    return [
        SwitcherEntry(
            badge=entry.badge,
            role_name=entry.role_name,
            role_is_system=entry.role_is_system,
            expires_at=entry.expires_at,
            vat_registered=entry.vat_registered,
            colour_is_ambiguous=counts[entry.badge.colour_token] > 1,
        )
        for entry in entries
    ]
