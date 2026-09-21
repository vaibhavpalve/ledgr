"""The capture-time category list - FR-EXP-001b.

The server owns the list and the picker in `packages/shared-types` displays it.
Two copies of one list is exactly how a category the picker offers gets refused
by the API, or one the API accepts never gets offered - so the copies are
compared here rather than trusted to stay in step.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from api.expenses.categories import (
    EXPENSE_CATEGORIES,
    UnknownExpenseCategory,
    category_for_key,
)
from api.expenses.model import VatTreatment

_SHARED_TYPES = Path(__file__).parents[4] / "packages" / "shared-types" / "src" / "index.ts"

_ENTRY = re.compile(
    r'\{ key: "(?P<key>[a-z_]+)", rgsCode: "(?P<rgs>\d+)", vatTreatment: "(?P<vat>[a-z_0-9]+)" \}'
)


def test_the_picker_offers_exactly_what_the_api_accepts() -> None:
    source = _SHARED_TYPES.read_text(encoding="utf-8")
    offered = [(m["key"], m["rgs"], m["vat"]) for m in _ENTRY.finditer(source)]

    assert offered, "the EXPENSE_CATEGORIES list could not be read from shared-types"
    assert offered == [(c.key, c.rgs_code, c.vat_treatment.value) for c in EXPENSE_CATEGORIES]


def test_keys_and_labels_are_unique() -> None:
    # The label is what is stored and what a ledger mapping is matched on
    # (case-insensitively), so two categories may not share one.
    assert len({c.key for c in EXPENSE_CATEGORIES}) == len(EXPENSE_CATEGORIES)
    assert len({c.label.strip().lower() for c in EXPENSE_CATEGORIES}) == len(EXPENSE_CATEGORIES)


def test_every_category_carries_a_real_vat_treatment() -> None:
    assert all(isinstance(c.vat_treatment, VatTreatment) for c in EXPENSE_CATEGORIES)
    # btw_marge is not in the enum at all; this guards the shape that matters.
    assert all(c.vat_treatment.value != "btw_marge" for c in EXPENSE_CATEGORIES)


def test_a_key_resolves_to_its_category() -> None:
    assert category_for_key("office_supplies").label == "Office supplies"
    assert category_for_key("  lunch ").label == "Lunch"


@pytest.mark.parametrize("key", ["", "Office supplies", "office-supplies", "nope"])
def test_an_unknown_key_is_refused_not_guessed(key: str) -> None:
    with pytest.raises(UnknownExpenseCategory):
        category_for_key(key)
