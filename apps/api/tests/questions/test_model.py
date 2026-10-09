"""ADR-111: the pure rules of a question thread."""

from __future__ import annotations

import uuid

from api.questions.model import Side, awaiting_after, excerpt, side_for

OWNER = uuid.uuid4()
FIRM = uuid.uuid4()


def test_the_owning_organizations_session_is_the_client() -> None:
    assert side_for(session_organization_id=OWNER, owning_organization_id=OWNER) is Side.CLIENT


def test_any_other_session_that_reaches_the_books_is_the_firm() -> None:
    assert side_for(session_organization_id=FIRM, owning_organization_id=OWNER) is Side.FIRM


def test_the_move_passes_to_the_side_that_did_not_write() -> None:
    assert awaiting_after(Side.FIRM) is Side.CLIENT
    assert awaiting_after(Side.CLIENT) is Side.FIRM


def test_the_excerpt_is_the_first_120_characters_on_one_line() -> None:
    body = "Waar is deze\nbetaling\tvoor?  " + "x" * 200
    short = excerpt(body)
    assert short.startswith("Waar is deze betaling voor? ")
    assert len(short) == 120
    assert excerpt("kort") == "kort"
