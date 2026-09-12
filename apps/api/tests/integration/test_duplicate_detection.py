"""FR-EXP-001g against a real Postgres.

Runs tests/expenses/duplicate_cases.py - the same table
tests/expenses/test_duplicates.py runs against the pure Python - through
migration 0036's SQL, and asserts the two agree row for row.

What only a database can establish is the rest: that pg_trgm's similarity
catches the near-misses exact matching cannot, that the search is confined by
RLS, and that there is no constraint anywhere stopping a legitimate duplicate
from being stored.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from api.db import engine as app_engine
from api.expenses.duplicates import SIMILARITY_THRESHOLD, normalise_supplier
from tests.expenses.duplicate_cases import MATCH_CASES, NORMALISATION_CASES, MatchCase
from tests.support.seed import SeededTenants

DAY = date(2025, 6, 1)
AMOUNT = Decimal("12.50")


async def _seed_claim(
    administration_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    supplier: str,
    on: date = DAY,
    gross: Decimal = AMOUNT,
) -> uuid.UUID:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        session_id = (
            await conn.execute(
                text(
                    "INSERT INTO capture_session ("
                    "  organization_id, administration_id, opened_by_user_id"
                    ") VALUES (:org, :admin, :user) RETURNING id"
                ),
                {
                    "org": str(organization_id),
                    "admin": str(administration_id),
                    "user": str(user_id),
                },
            )
        ).scalar_one()
        item_id = (
            await conn.execute(
                text(
                    "INSERT INTO capture_item ("
                    "  organization_id, administration_id, session_id, position"
                    ") VALUES (:org, :admin, :session, "
                    "  (SELECT coalesce(max(position), 0) + 1 FROM capture_item "
                    "   WHERE session_id = :session)) RETURNING id"
                ),
                {
                    "org": str(organization_id),
                    "admin": str(administration_id),
                    "session": str(session_id),
                },
            )
        ).scalar_one()
        return (
            await conn.execute(
                text(
                    "INSERT INTO expense ("
                    "  organization_id, administration_id, capture_item_id,"
                    "  submitted_by_user_id, supplier, expense_date, gross_amount"
                    ") VALUES (:org, :admin, :item, :user, :supplier, :on, :gross) "
                    "RETURNING id"
                ),
                {
                    "org": str(organization_id),
                    "admin": str(administration_id),
                    "item": str(item_id),
                    "user": str(user_id),
                    "supplier": supplier,
                    "on": on,
                    "gross": gross,
                },
            )
        ).scalar_one()


async def _candidates(
    administration_id: uuid.UUID,
    organization_id: uuid.UUID,
    expense_id: uuid.UUID,
    *,
    supplier: str,
    on: date = DAY,
    gross: Decimal = AMOUNT,
) -> list[tuple[uuid.UUID, str, float | None]]:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(organization_id)},
        )
        result = await conn.execute(
            text(
                "SELECT expense_id, strength, similarity "
                "FROM expenses.duplicate_candidates("
                "    :admin, :expense, :supplier, :on, :gross, :floor)"
            ),
            {
                "admin": str(administration_id),
                "expense": str(expense_id),
                "supplier": supplier,
                "on": on,
                "gross": gross,
                "floor": SIMILARITY_THRESHOLD,
            },
        )
        return [(row.expense_id, row.strength, row.similarity) for row in result]


# ===========================================================================
# The shared tables, through the SQL
# ===========================================================================


@pytest.mark.parametrize(("raw", "expected", "why"), NORMALISATION_CASES, ids=lambda v: str(v)[:40])
async def test_sql_normalisation_matches_the_shared_table(
    raw: str, expected: str, why: str, two_organizations: SeededTenants
) -> None:
    """The rule exists twice and the copies are compared, not trusted."""
    async with app_engine.begin() as conn:
        stored = (
            await conn.execute(
                text("SELECT expenses.normalise_supplier(:raw) AS normalised"),
                {"raw": raw},
            )
        ).scalar_one()

    assert stored == expected, why
    assert stored == normalise_supplier(raw), (
        f"SQL gave {stored!r} and Python gave {normalise_supplier(raw)!r} for {raw!r}"
    )


@pytest.mark.parametrize("case", MATCH_CASES, ids=lambda c: f"{c.left_supplier}~{c.right_supplier}")
async def test_sql_exact_matching_matches_the_shared_table(
    case: MatchCase, two_organizations: SeededTenants
) -> None:
    existing = await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier=case.right_supplier,
        on=DAY if case.same_date else date(2025, 6, 2),
        gross=AMOUNT if case.same_amount else Decimal("12.51"),
    )
    subject = await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier=case.left_supplier,
    )

    found = await _candidates(
        two_organizations.admin_a,
        two_organizations.org_a,
        subject,
        supplier=case.left_supplier,
    )
    exact = [expense_id for expense_id, strength, _ in found if strength == "exact"]

    assert (existing in exact) is case.matches, case.why


# ===========================================================================
# What only pg_trgm can do
# ===========================================================================


async def test_a_similar_supplier_is_a_probable_match(
    two_organizations: SeededTenants,
) -> None:
    """The half that is deliberately NOT reimplemented in Python.

    "ALBERT HEIJN 1043" is what a till prints; "Albert Heijn" is what a person
    types. Exact matching cannot see they are the same shop.
    """
    existing = await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="ALBERT HEIJN 1043",
    )
    subject = await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="Albert Heijn",
    )

    found = await _candidates(
        two_organizations.admin_a,
        two_organizations.org_a,
        subject,
        supplier="Albert Heijn",
    )

    match = next((row for row in found if row[0] == existing), None)
    assert match is not None, "the till's store number should not hide a duplicate"
    assert match[1] == "probable"
    assert match[2] is not None and match[2] >= SIMILARITY_THRESHOLD


async def test_an_unrelated_supplier_is_not_a_match(
    two_organizations: SeededTenants,
) -> None:
    """Same day, same amount, different shop - two people buying lunch for the
    same price is unremarkable, and a warning here would be noise.
    """
    await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="Jumbo",
    )
    subject = await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="Albert Heijn",
    )

    found = await _candidates(
        two_organizations.admin_a,
        two_organizations.org_a,
        subject,
        supplier="Albert Heijn",
    )

    assert found == []


async def test_exact_matches_are_ordered_first(
    two_organizations: SeededTenants,
) -> None:
    """A person reads the top of this list and stops."""
    similar = await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="ALBERT HEIJN 1043",
    )
    exact = await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="albert heijn",
    )
    subject = await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="Albert Heijn",
    )

    found = await _candidates(
        two_organizations.admin_a,
        two_organizations.org_a,
        subject,
        supplier="Albert Heijn",
    )

    assert [row[0] for row in found] == [exact, similar]


# ===========================================================================
# Warn, never block - the schema has no opinion either
# ===========================================================================


async def test_nothing_stops_a_legitimate_duplicate_being_stored(
    two_organizations: SeededTenants,
) -> None:
    """Two identical coffees on one morning. There is deliberately no unique
    index on (supplier, date, amount), and this is what says so.
    """
    first = await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="Albert Heijn",
    )
    second = await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="Albert Heijn",
    )

    assert first != second, "both claims exist; the duplicate is only a warning"

    found = await _candidates(
        two_organizations.admin_a,
        two_organizations.org_a,
        second,
        supplier="Albert Heijn",
    )
    assert [row[0] for row in found] == [first]


async def test_the_search_never_crosses_a_tenant(
    two_organizations: SeededTenants,
) -> None:
    """`expenses.duplicate_candidates` is not SECURITY DEFINER, so RLS confines
    it to what the calling request may see.
    """
    await _seed_claim(
        two_organizations.admin_b,
        two_organizations.org_b,
        two_organizations.owner_b,
        supplier="Albert Heijn",
    )
    subject = await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="Albert Heijn",
    )

    # Org A asks, naming its own administration: it must not see org B's claim.
    assert (
        await _candidates(
            two_organizations.admin_a,
            two_organizations.org_a,
            subject,
            supplier="Albert Heijn",
        )
        == []
    )
    # And asking about org B's administration from org A's context returns
    # nothing either - RLS filters the rows, not the argument.
    assert (
        await _candidates(
            two_organizations.admin_b,
            two_organizations.org_a,
            subject,
            supplier="Albert Heijn",
        )
        == []
    )


async def test_a_junk_supplier_matches_nothing(
    two_organizations: SeededTenants,
) -> None:
    """Found by a property test in the Python suite, asserted here too: without
    this rule every claim with a punctuation-only supplier would warn about
    every other one, and 0033's CHECK does not stop one being entered.
    """
    await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier=":",
    )
    subject = await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="...",
    )

    assert (
        await _candidates(
            two_organizations.admin_a, two_organizations.org_a, subject, supplier="..."
        )
        == []
    )


async def test_a_claim_does_not_match_itself(
    two_organizations: SeededTenants,
) -> None:
    subject = await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        two_organizations.owner_a,
        supplier="Albert Heijn",
    )

    found = await _candidates(
        two_organizations.admin_a,
        two_organizations.org_a,
        subject,
        supplier="Albert Heijn",
    )

    assert subject not in [row[0] for row in found]


async def test_a_colleagues_claim_is_found_but_not_attributed(
    two_organizations: SeededTenants,
) -> None:
    """The case worth catching most - a shared lunch, one bill, two
    photographs - and the line the response does not cross.
    """
    colleague = two_organizations.owner_a
    theirs = await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        colleague,
        supplier="Albert Heijn",
    )
    subject = await _seed_claim(
        two_organizations.admin_a,
        two_organizations.org_a,
        colleague,
        supplier="Albert Heijn",
    )

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(two_organizations.org_a)},
        )
        columns = (
            await conn.execute(
                text(
                    "SELECT * FROM expenses.duplicate_candidates("
                    "    :admin, :expense, 'Albert Heijn', :on, :gross, :floor) LIMIT 1"
                ),
                {
                    "admin": str(two_organizations.admin_a),
                    "expense": str(subject),
                    "on": DAY,
                    "gross": AMOUNT,
                    "floor": SIMILARITY_THRESHOLD,
                },
            )
        ).keys()

    assert theirs is not None
    assert "same_submitter" in columns
    assert "submitted_by_user_id" not in columns, (
        "a duplicate check is a poor place to learn what one's colleagues have been spending"
    )
