"""Saved views, next client and the worklist's wave-2 additions as tables (ADR-115). No database."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date

import pytest
from pydantic import ValidationError

from api.audit.log import AuditCategory
from api.authz.dependencies import declared_requirements
from api.firm.views_model import (
    SavedViewQuery,
    clean_name,
    parse_stored_query,
)
from api.firm.worklist_model import (
    ActivityKind,
    AssignedFilter,
    Chip,
    ClientFacts,
    SortKey,
    VatFrequency,
    VatPeriodFacts,
    WorklistQuery,
    WorklistRow,
    activity_lines,
    build_row,
    count_rows,
    next_client,
    select_rows,
    worklist_page,
)
from api.main import app

TODAY = date(2026, 10, 8)


def _client(name: str, **changes: object) -> ClientFacts:
    base = ClientFacts(
        administration_id=uuid.uuid4(),
        display_name=name,
        legal_name=name,
        kvk_number="12345678",
        initials=name[:2].upper(),
        colour="teal",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def _vat(scheme: str) -> VatPeriodFacts:
    start, end = (
        (date(2026, 7, 1), date(2026, 9, 30))
        if scheme == "quarterly"
        else (date(2026, 9, 1), date(2026, 9, 30))
    )
    return VatPeriodFacts(
        administration_id=uuid.uuid4(),
        period_id=uuid.uuid4(),
        start_date=start,
        end_date=end,
        period_status="open",
        period_scheme=scheme,
        filed=False,
        has_postings=True,
        unmatched_in_period=False,
        unposted_in_period=False,
    )


def _row(name: str, **changes: object) -> WorklistRow:
    return build_row(_client(name, **changes), today=TODAY)


def _names(rows: list[WorklistRow]) -> list[str]:
    return [r.facts.display_name for r in rows]


def _by_name() -> list[WorklistRow]:
    """Four clients with work, one snoozed, one up to date - sorted by name for readability."""
    return [
        _row("Alfa", to_book=1),
        _row("Bravo", to_book=1, snoozed_until=date(2026, 12, 1)),
        _row("Charlie", to_book=1),
        _row("Delta", to_book=1),
        _row("Echo"),
    ]


_BY_NAME = WorklistQuery(chip=Chip.MY_MOVE, sort=SortKey.NAME)


# --- vat_frequency -------------------------------------------------------------------


def test_vat_frequency_filters_rows_and_chip_counts() -> None:
    rows = [
        _row("Maand", to_book=1, vat=_vat("monthly")),
        _row("Kwartaal", to_book=1, vat=_vat("quarterly")),
        _row("Geen btw", to_book=1),
    ]
    page = worklist_page(
        rows,
        chip=Chip.ALL,
        query=None,
        assigned=AssignedFilter(),
        sort=SortKey.NAME,
        descending=False,
        page=1,
        page_size=50,
        vat_frequency=VatFrequency.MONTHLY,
    )
    assert _names(page.rows) == ["Maand"]
    assert page.total == 1
    assert page.chip_counts[Chip.ALL] == 1
    yearly = WorklistQuery(chip=Chip.ALL, vat_frequency=VatFrequency.YEARLY)
    assert count_rows(rows, yearly) == 0  # no yearly scheme exists (0029)


def test_wave_two_facts_reach_the_row() -> None:
    row = _row("Alfa", rules_count=3)
    assert row.facts.rules_count == 3
    assert row.facts.last_chased_at is None


# --- one query definition: page total == view count == select_rows ---------------------


@pytest.mark.parametrize(
    "query",
    [
        WorklistQuery(),
        WorklistQuery(chip=Chip.ALL),
        WorklistQuery(chip=Chip.SNOOZED),
        WorklistQuery(chip=Chip.UP_TO_DATE, sort=SortKey.NAME, descending=True),
        WorklistQuery(chip=Chip.ALL, q="char"),
    ],
)
def test_a_views_count_is_what_opening_it_shows(query: WorklistQuery) -> None:
    rows = _by_name()
    page = worklist_page(
        rows,
        chip=query.chip,
        query=query.q,
        assigned=query.assigned,
        sort=query.sort,
        descending=query.descending,
        page=1,
        page_size=1000,
        vat_frequency=query.vat_frequency,
    )
    assert count_rows(rows, query) == page.total == len(select_rows(rows, query))
    assert page.rows == select_rows(rows, query)


# --- next client (decision 7) ----------------------------------------------------------


def test_next_without_after_is_the_first_and_counts_the_rest() -> None:
    result = next_client(_by_name(), _BY_NAME, after=None)
    assert result.row is not None
    assert result.row.facts.display_name == "Alfa"
    assert result.remaining == 3  # Alfa, Charlie, Delta - Bravo is snoozed, Echo has no work


def test_next_follows_the_sort_and_skips_snoozed() -> None:
    rows = _by_name()
    alfa = rows[0].facts.administration_id
    result = next_client(rows, _BY_NAME, after=alfa)
    assert result.row is not None
    assert result.row.facts.display_name == "Charlie"
    assert result.remaining == 2


def test_next_skips_snoozed_even_where_the_chip_shows_them() -> None:
    """Chip "all" lists the snoozed Bravo; next client with work still steps over it."""
    rows = _by_name()
    alfa = rows[0].facts.administration_id
    query = replace(_BY_NAME, chip=Chip.ALL)
    assert "Bravo" in _names(select_rows(rows, query))
    result = next_client(rows, query, after=alfa)
    assert result.row is not None
    assert result.row.facts.display_name == "Charlie"
    assert result.remaining == 3  # Charlie, Delta, Echo


def test_next_respects_the_direction() -> None:
    rows = _by_name()
    delta = rows[3].facts.administration_id
    result = next_client(rows, replace(_BY_NAME, descending=True), after=delta)
    assert result.row is not None
    assert result.row.facts.display_name == "Charlie"
    assert result.remaining == 2  # Charlie, Alfa


def test_next_respects_the_filters() -> None:
    rows = _by_name()
    alfa = rows[0].facts.administration_id
    result = next_client(rows, replace(_BY_NAME, q="delta"), after=alfa)
    assert result.row is not None
    assert result.row.facts.display_name == "Delta"
    assert result.remaining == 1


def test_next_is_null_at_the_end() -> None:
    rows = _by_name()
    delta = rows[3].facts.administration_id
    result = next_client(rows, _BY_NAME, after=delta)
    assert result.row is None
    assert result.remaining == 0


def test_a_client_whose_work_is_done_keeps_its_place() -> None:
    """Finishing Charlie's work takes Charlie off My move; next is still Delta, not Alfa."""
    rows = _by_name()
    charlie = rows[2].facts.administration_id
    rows[2] = build_row(replace(rows[2].facts, to_book=0), today=TODAY)
    assert Chip.MY_MOVE not in rows[2].chips
    result = next_client(rows, _BY_NAME, after=charlie)
    assert result.row is not None
    assert result.row.facts.display_name == "Delta"
    assert result.remaining == 1


def test_an_after_outside_the_portfolio_starts_from_the_top() -> None:
    result = next_client(_by_name(), _BY_NAME, after=uuid.uuid4())
    assert result.row is not None
    assert result.row.facts.display_name == "Alfa"
    assert result.remaining == 3


def test_next_only_ever_returns_a_row_it_was_given() -> None:
    rows = _by_name()
    given = {r.facts.administration_id for r in rows}
    after: uuid.UUID | None = None
    seen: list[str] = []
    while True:
        result = next_client(rows, _BY_NAME, after=after)
        if result.row is None:
            break
        assert result.row.facts.administration_id in given
        seen.append(result.row.facts.display_name)
        after = result.row.facts.administration_id
    assert seen == ["Alfa", "Charlie", "Delta"]


# --- saved view queries ------------------------------------------------------------------


def test_a_saved_query_defaults_to_the_worklists_defaults() -> None:
    stored = SavedViewQuery().stored()
    assert stored == {
        "chip": "my_move",
        "q": None,
        "assigned": "any",
        "sort": "risk",
        "dir": "asc",
        "vat_frequency": None,
    }


def test_a_saved_query_resolves_me_for_its_reader() -> None:
    me = uuid.uuid4()
    query = SavedViewQuery.model_validate(
        {"chip": "all", "assigned": "me", "dir": "desc", "vat_frequency": "quarterly"}
    ).to_worklist_query(user_id=me)
    assert query.assigned == AssignedFilter.mine(me)
    assert query.descending is True
    assert query.vat_frequency is VatFrequency.QUARTERLY


@pytest.mark.parametrize(
    "raw",
    [
        {"chip": "nonsense"},
        {"assigned": "somebody"},
        {"dir": "sideways"},
        {"vat_frequency": "weekly"},
        {"page": 2},  # paging is not part of a view
    ],
)
def test_a_bad_saved_query_is_refused(raw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SavedViewQuery.model_validate(raw)
    assert parse_stored_query(raw) is None


def test_blank_search_is_stored_as_none() -> None:
    assert SavedViewQuery.model_validate({"q": "   "}).q is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  Q3 btw ", "Q3 btw"), ("", None), ("   ", None), ("x" * 60, "x" * 60), ("x" * 61, None)],
)
def test_view_names(raw: str, expected: str | None) -> None:
    assert clean_name(raw) == expected


# --- summary: rule_postings ------------------------------------------------------------


def test_rule_postings_is_an_activity_line_last() -> None:
    a = uuid.uuid4()
    lines = activity_lines(
        [(ActivityKind.RULE_POSTINGS, a, 4), (ActivityKind.RECEIPTS_UPLOADED, a, 1)],
        names={a: "Alfa"},
    )
    assert [line.kind.value for line in lines] == ["receipts_uploaded", "rule_postings"]
    assert lines[1].count == 4


# --- route declarations ----------------------------------------------------------------


def _route(path: str, method: str) -> object:
    return next(
        r
        for r in app.routes
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or set())
    )


def test_every_new_route_declares_the_portfolio_permission() -> None:
    for path, method in (
        ("/v1/firm/worklist/next", "GET"),
        ("/v1/firm/views", "GET"),
        ("/v1/firm/views", "POST"),
        ("/v1/firm/views/{view_id}/rename", "POST"),
        ("/v1/firm/views/{view_id}/archive", "POST"),
    ):
        [requirement] = declared_requirements(_route(path, method))
        assert (requirement.action, requirement.resource_type) == ("view", "report")
        assert requirement.scope.kind == "administration"
        assert requirement.scope.path_param is None
        if method == "POST":
            assert requirement.audit_category is AuditCategory.CONFIGURATION
