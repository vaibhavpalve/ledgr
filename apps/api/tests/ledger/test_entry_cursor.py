"""The journal's page cursor round-trips and refuses what it did not make."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from api.ledger import EntryCursor


def test_a_cursor_round_trips() -> None:
    cursor = EntryCursor(
        posted_at=datetime(2026, 9, 14, 10, 30, 15, 123456, tzinfo=UTC),
        entry_id=uuid.uuid4(),
    )

    assert EntryCursor.decode(cursor.encode()) == cursor


def test_a_cursor_is_url_safe() -> None:
    token = EntryCursor(posted_at=datetime.now(UTC), entry_id=uuid.uuid4()).encode()

    assert "=" not in token
    assert "+" not in token
    assert "/" not in token


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-cursor",
        "MjAyNi0wOS0xNA",  # "2026-09-14": no separator
        "MjAyNi0wOS0xNHxub3QtYS11dWlk",  # "2026-09-14|not-a-uuid"
        "bm90LWEtZGF0ZXwwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDA",  # bad date
    ],
)
def test_anything_else_is_refused(token: str) -> None:
    with pytest.raises(ValueError):
        EntryCursor.decode(token)


def test_a_naive_instant_is_refused() -> None:
    """Keyset order is on a timestamptz; a cursor with no zone would compare
    against it in whatever zone the driver assumed."""
    naive = EntryCursor(posted_at=datetime(2026, 1, 1), entry_id=uuid.uuid4()).encode()

    with pytest.raises(ValueError):
        EntryCursor.decode(naive)
