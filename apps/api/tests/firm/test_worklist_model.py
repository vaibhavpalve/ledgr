"""The firm home's decisions as tables (api.firm.worklist_model, ADR-109). No database."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from api.firm.worklist_model import (
    NO_DEADLINE_DAYS,
    ActivityKind,
    AssignedFilter,
    BrokenFeed,
    Chip,
    ClientFacts,
    SortKey,
    VatPeriodFacts,
    VatStatus,
    WorklistPage,
    WorklistRow,
    activity_lines,
    booked_until,
    build_row,
    deadlines,
    months_behind,
    period_label,
    resolve_since,
    sort_rows,
    worklist_page,
)

TODAY = date(2026, 10, 8)


def _client(name: str = "Bakker B.V.", **changes: object) -> ClientFacts:
    base = ClientFacts(
        administration_id=uuid.uuid4(),
        display_name=name,
        legal_name=name,
        kvk_number="12345678",
        initials="BB",
        colour="teal",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def _vat(
    *,
    end: date = date(2026, 9, 30),
    start: date = date(2026, 7, 1),
    scheme: str = "quarterly",
    status: str = "open",
    filed: bool = False,
    postings: bool = True,
    unmatched: bool = False,
    unposted: bool = False,
    administration_id: uuid.UUID | None = None,
) -> VatPeriodFacts:
    return VatPeriodFacts(
        administration_id=administration_id or uuid.uuid4(),
        period_id=uuid.uuid4(),
        start_date=start,
        end_date=end,
        period_status=status,
        period_scheme=scheme,
        filed=filed,
        has_postings=postings,
        unmatched_in_period=unmatched,
        unposted_in_period=unposted,
    )


# --- booked_until (contract decision 4) --------------------------------------


def test_nothing_unmatched_is_booked_to_the_last_line() -> None:
    assert booked_until(_client(last_transaction=date(2026, 9, 30))) == (date(2026, 9, 30), False)


def test_an_unmatched_line_books_until_the_day_before_it() -> None:
    facts = _client(last_transaction=date(2026, 9, 30), oldest_unmatched=date(2026, 7, 1))
    assert booked_until(facts) == (date(2026, 6, 30), False)


def test_a_lagging_feed_caps_booked_until() -> None:
    facts = _client(
        last_transaction=date(2026, 9, 30),
        feed_synced_until=datetime(2026, 8, 15, 6, 0, tzinfo=UTC),
    )
    assert booked_until(facts) == (date(2026, 8, 15), True)


def test_a_feed_ahead_of_the_books_does_not_cap() -> None:
    facts = _client(
        last_transaction=date(2026, 9, 30),
        oldest_unmatched=date(2026, 9, 1),
        feed_synced_until=datetime(2026, 10, 7, tzinfo=UTC),
    )
    assert booked_until(facts) == (date(2026, 8, 31), False)


def test_no_bank_lines_is_not_booked_at_all() -> None:
    assert booked_until(_client()) == (None, False)


# --- months_behind --------------------------------------------------------------


@pytest.mark.parametrize(
    ("booked", "expected"),
    [
        (date(2026, 6, 30), 3),  # the contract's own example
        (date(2026, 9, 30), 0),
        (date(2026, 8, 31), 1),
        (date(2026, 9, 15), 1),  # September unfinished
        (date(2026, 10, 7), 0),
        (None, None),
    ],
)
def test_months_behind(booked: date | None, expected: int | None) -> None:
    assert months_behind(booked, today=TODAY) == expected


# --- VAT status ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        (_vat(filed=True), VatStatus.FILED),
        (_vat(status="locked"), VatStatus.READY_TO_FILE),
        (_vat(postings=False), VatStatus.NOT_STARTED),
        (_vat(unmatched=True), VatStatus.IN_PROGRESS),
        (_vat(unposted=True), VatStatus.IN_PROGRESS),
        (_vat(end=date(2026, 12, 31), start=date(2026, 10, 1)), VatStatus.IN_PROGRESS),
        (_vat(), VatStatus.READY_TO_REVIEW),
    ],
)
def test_vat_status(period: VatPeriodFacts, expected: VatStatus) -> None:
    assert period.status(today=TODAY) is expected


def test_period_labels_and_due_date() -> None:
    assert period_label("quarterly", date(2026, 7, 1), date(2026, 9, 30)) == "2026-Q3"
    assert period_label("monthly", date(2026, 9, 1), date(2026, 9, 30)) == "2026-09"
    assert _vat().due_date == date(2026, 10, 31)


# --- risk and buckets ---------------------------------------------------------------


def test_risk_is_days_to_deadline_minus_work_and_penalties() -> None:
    facts = _client(
        to_book=14,
        bank_to_match=7,
        auto_bookings=4,
        last_transaction=date(2026, 9, 30),
        oldest_unmatched=date(2026, 7, 1),  # booked to 30 June: 3 months behind
        broken_feed=BrokenFeed(bank_name="ING", status="expired"),
        vat=_vat(),
    )
    row = build_row(facts, today=TODAY)
    # 23 days to 31 October, ceil(25 / 10) = 3, broken feed 3, behind 5.
    assert row.vat is not None and row.vat.days_to_due == 23
    assert row.risk == 23 - 3 - 3 - 5


def test_no_unfiled_deadline_uses_the_stand_in() -> None:
    assert build_row(_client(), today=TODAY).risk == NO_DEADLINE_DAYS
    filed = build_row(_client(vat=_vat(filed=True)), today=TODAY)
    assert filed.risk == NO_DEADLINE_DAYS


def test_my_move_and_waiting_on_client_are_separate() -> None:
    mine = build_row(_client(bank_to_match=2), today=TODAY)
    theirs = build_row(
        _client(missing_receipts=2, oldest_missing_receipt=date(2026, 9, 29)), today=TODAY
    )
    assert Chip.MY_MOVE in mine.chips and Chip.WAITING_ON_CLIENT not in mine.chips
    assert Chip.WAITING_ON_CLIENT in theirs.chips and Chip.MY_MOVE not in theirs.chips
    assert theirs.waiting_on_client_since == date(2026, 9, 29)


def test_questions_split_by_who_they_await() -> None:
    replied = build_row(_client(open_questions=1, questions_awaiting_firm=1), today=TODAY)
    asked = build_row(
        _client(
            open_questions=1,
            questions_awaiting_client=1,
            oldest_awaiting_client=date(2026, 10, 1),
        ),
        today=TODAY,
    )
    assert Chip.MY_MOVE in replied.chips and Chip.WAITING_ON_CLIENT not in replied.chips
    assert Chip.WAITING_ON_CLIENT in asked.chips and Chip.MY_MOVE not in asked.chips
    assert asked.waiting_on_client_since == date(2026, 10, 1)
    assert replied.waiting_on_client_since is None


def test_vat_ready_to_review_is_my_move() -> None:
    row = build_row(_client(vat=_vat()), today=TODAY)
    assert Chip.MY_MOVE in row.chips
    assert Chip.VAT_NOT_FILED in row.chips


def test_a_snoozed_client_leaves_my_move_and_waiting() -> None:
    facts = _client(bank_to_match=5, missing_receipts=1, snoozed_until=TODAY + timedelta(days=7))
    row = build_row(facts, today=TODAY)
    assert Chip.SNOOZED in row.chips
    assert Chip.MY_MOVE not in row.chips and Chip.WAITING_ON_CLIENT not in row.chips


def test_a_snooze_ending_today_is_over() -> None:
    row = build_row(_client(bank_to_match=1, snoozed_until=TODAY), today=TODAY)
    assert Chip.SNOOZED not in row.chips and Chip.MY_MOVE in row.chips


def test_up_to_date_has_nothing_else() -> None:
    row = build_row(_client(last_transaction=date(2026, 9, 30)), today=TODAY)
    assert row.chips == {Chip.ALL, Chip.UP_TO_DATE}


def test_one_month_behind_is_normal_two_is_behind() -> None:
    one = build_row(_client(last_transaction=date(2026, 8, 31)), today=TODAY)
    two = build_row(_client(last_transaction=date(2026, 7, 31)), today=TODAY)
    assert Chip.BOOKS_BEHIND not in one.chips
    assert Chip.BOOKS_BEHIND in two.chips


# --- the page -----------------------------------------------------------------------


def _rows() -> list[WorklistRow]:
    return [
        build_row(_client("Alfa", bank_to_match=30, vat=_vat()), today=TODAY),
        build_row(_client("Bravo", to_book=1), today=TODAY),
        build_row(_client("Charlie", kvk_number="87654321"), today=TODAY),
    ]


def test_default_sort_is_ascending_risk() -> None:
    page = worklist_page(
        _rows(),
        chip=Chip.ALL,
        query=None,
        assigned=AssignedFilter(),
        sort=SortKey.RISK,
        descending=False,
        page=1,
        page_size=50,
    )
    assert [r.facts.display_name for r in page.rows][0] == "Alfa"
    assert page.chip_counts[Chip.ALL] == 3
    assert page.chip_counts[Chip.MY_MOVE] == 2
    assert page.chip_counts[Chip.UP_TO_DATE] == 1


def test_query_matches_names_and_kvk_prefix() -> None:
    def names(query: str) -> list[str]:
        page = worklist_page(
            _rows(),
            chip=Chip.ALL,
            query=query,
            assigned=AssignedFilter(),
            sort=SortKey.NAME,
            descending=False,
            page=1,
            page_size=50,
        )
        return [r.facts.display_name for r in page.rows]

    assert names("rav") == ["Bravo"]
    assert names("8765") == ["Charlie"]


def test_assigned_filter_and_paging() -> None:
    me = uuid.uuid4()
    rows = [
        build_row(_client(f"Client {i:02d}", assigned_user_id=me if i % 2 else None), today=TODAY)
        for i in range(10)
    ]
    page = worklist_page(
        rows,
        chip=Chip.ALL,
        query=None,
        assigned=AssignedFilter(any=False, user_id=me),
        sort=SortKey.NAME,
        descending=False,
        page=2,
        page_size=2,
    )
    assert page.total == 5
    assert [r.facts.display_name for r in page.rows] == ["Client 05", "Client 07"]


def _mine_page(rows: list[WorklistRow], me: uuid.UUID, chip: Chip = Chip.ALL) -> WorklistPage:
    return worklist_page(
        rows,
        chip=chip,
        query=None,
        assigned=AssignedFilter.mine(me),
        sort=SortKey.NAME,
        descending=False,
        page=1,
        page_size=50,
    )


def test_mine_includes_unassigned_clients_but_not_a_colleagues() -> None:
    me, colleague = uuid.uuid4(), uuid.uuid4()
    rows = [
        build_row(_client("Mine", assigned_user_id=me, to_book=1), today=TODAY),
        build_row(_client("Nobody's", to_book=2), today=TODAY),
        build_row(_client("Theirs", assigned_user_id=colleague, to_book=3), today=TODAY),
    ]
    page = _mine_page(rows, me)
    assert [r.facts.display_name for r in page.rows] == ["Mine", "Nobody's"]
    assert page.total == 2


def test_a_firm_that_assigned_nobody_sees_everything_under_mine() -> None:
    """The QA case: no assignments yet must not mean an empty "Mine" with every chip at 0."""
    me = uuid.uuid4()
    rows = _rows()  # none assigned
    mine = _mine_page(rows, me, Chip.MY_MOVE)
    everyone = worklist_page(
        rows,
        chip=Chip.MY_MOVE,
        query=None,
        assigned=AssignedFilter(),
        sort=SortKey.NAME,
        descending=False,
        page=1,
        page_size=50,
    )
    assert mine.total == everyone.total == 2
    assert mine.chip_counts == everyone.chip_counts


def test_chip_counts_follow_mine_plus_unassigned() -> None:
    me, colleague = uuid.uuid4(), uuid.uuid4()
    rows = [
        build_row(_client("Mine", assigned_user_id=me, to_book=1), today=TODAY),
        build_row(_client("Nobody's"), today=TODAY),
        build_row(_client("Theirs", assigned_user_id=colleague, to_book=3), today=TODAY),
    ]
    counts = _mine_page(rows, me, Chip.MY_MOVE).chip_counts
    assert counts[Chip.ALL] == 2
    assert counts[Chip.MY_MOVE] == 1  # "Theirs" has work, but is a colleague's
    assert counts[Chip.UP_TO_DATE] == 1


def test_naming_a_colleague_is_exact() -> None:
    me, colleague = uuid.uuid4(), uuid.uuid4()
    rows = [
        build_row(_client("Nobody's"), today=TODAY),
        build_row(_client("Theirs", assigned_user_id=colleague), today=TODAY),
        build_row(_client("Mine", assigned_user_id=me), today=TODAY),
    ]
    page = worklist_page(
        rows,
        chip=Chip.ALL,
        query=None,
        assigned=AssignedFilter(any=False, user_id=colleague),
        sort=SortKey.NAME,
        descending=False,
        page=1,
        page_size=50,
    )
    assert [r.facts.display_name for r in page.rows] == ["Theirs"]


def test_missing_values_sort_last_both_ways() -> None:
    with_date = build_row(_client("A", last_transaction=date(2026, 9, 1)), today=TODAY)
    without = build_row(_client("B"), today=TODAY)
    for descending in (False, True):
        ordered = sort_rows([without, with_date], SortKey.BOOKED_UNTIL, descending=descending)
        assert ordered[-1] is without


def test_descending_counts() -> None:
    rows = _rows()
    ordered = sort_rows(rows, SortKey.BANK_TO_MATCH, descending=True)
    assert ordered[0].facts.display_name == "Alfa"


# --- the summary --------------------------------------------------------------------


NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


def test_since_prefers_explicit_then_seen_then_previous_login_then_a_week() -> None:
    explicit = datetime(2026, 10, 1, tzinfo=UTC)
    seen = datetime(2026, 10, 5, tzinfo=UTC)
    login = datetime(2026, 10, 6, tzinfo=UTC)
    assert resolve_since(requested=explicit, seen_at=seen, previous_login_at=login, now=NOW) == (
        explicit
    )
    assert resolve_since(requested=None, seen_at=seen, previous_login_at=login, now=NOW) == seen
    assert resolve_since(requested=None, seen_at=None, previous_login_at=login, now=NOW) == login
    assert resolve_since(requested=None, seen_at=None, previous_login_at=None, now=NOW) == (
        NOW - timedelta(days=7)
    )


def test_activity_names_the_three_busiest_and_only_the_portfolio() -> None:
    ids = [uuid.uuid4() for _ in range(5)]
    names = {aid: f"Client {i}" for i, aid in enumerate(ids[:4])}
    stranger = ids[4]
    per_client = [
        (ActivityKind.RECEIPTS_UPLOADED, ids[0], 2),
        (ActivityKind.RECEIPTS_UPLOADED, ids[1], 9),
        (ActivityKind.RECEIPTS_UPLOADED, ids[2], 5),
        (ActivityKind.RECEIPTS_UPLOADED, ids[3], 1),
        (ActivityKind.RECEIPTS_UPLOADED, stranger, 100),
        (ActivityKind.CLIENT_REPLIES, ids[0], 1),
    ]
    lines = activity_lines(per_client, names)
    assert [line.kind for line in lines] == [
        ActivityKind.RECEIPTS_UPLOADED,
        ActivityKind.CLIENT_REPLIES,
    ]
    receipts = lines[0]
    assert (receipts.count, receipts.client_count) == (17, 4)
    assert [aid for aid, _ in receipts.clients] == [ids[1], ids[2], ids[0]]
    assert all(aid != stranger for line in lines for aid, _ in line.clients)


# --- deadlines ----------------------------------------------------------------------


def test_deadlines_group_by_return_and_bucket_by_status() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    periods = [
        _vat(administration_id=a, filed=True),
        _vat(administration_id=b),
        _vat(administration_id=c, postings=False),
        _vat(
            administration_id=a,
            scheme="monthly",
            start=date(2026, 9, 1),
            end=date(2026, 9, 30),
        ),
    ]
    rows = deadlines(periods, today=TODAY)
    assert [(d.period_label, d.due_date) for d in rows] == [
        ("2026-09", date(2026, 10, 31)),
        ("2026-Q3", date(2026, 10, 31)),
    ]
    quarter = rows[1]
    assert quarter.client_count == 3
    assert quarter.buckets[VatStatus.FILED] == 1
    assert quarter.buckets[VatStatus.READY_TO_REVIEW] == 1
    assert quarter.buckets[VatStatus.NOT_STARTED] == 1
    assert quarter.days_to_due == 23
