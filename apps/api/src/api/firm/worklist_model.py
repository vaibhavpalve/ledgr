"""The firm home's work queue, decided in one place: FR-FRM-001, FR-FRM-002 (ADR-109).

Pure: no database, no clock. `api.firm.worklist_repository` reads the facts for every granted
administration in a handful of set-based queries and hands them here; everything that turns a
fact into a bucket, a date, a chip or a sort position is in this file and tested as a table.

--- Buckets are mutually exclusive (contract decision 5) ---

An unmatched bank line lands in exactly one of three counts, so no line is counted twice and the
three add up to "unmatched lines":

    a pending HIGH proposal for it        -> auto_bookings
    else a debit with no candidate receipt -> missing_receipts   (waiting on the client)
    else                                   -> bank_to_match      (my move)

The repository does the classification in SQL (it is per line); this module only reads the three
counts. `to_book` is receipts captured but not posted, minus any a pending proposal already
covers.

--- booked_until is capped by the feed (contract decision 4) ---

"Booked until" is the day before the oldest unmatched bank line (every line on or before it is
reconciled), or the last line's date when none is unmatched. A broken or lagging feed caps it at
the feed's last sync, because lines the bank has not delivered yet cannot be unmatched - an
expired consent would otherwise make the books look complete.

--- Risk (contract decision 7) ---

    risk = days to the next unfiled VAT deadline        (NO_DEADLINE_DAYS when there is none)
         - ceil(my-move items / 10)
         - 3 when a bank feed is broken
         - 5 when the books are more than one month behind

Lower is more urgent; the default sort is ascending risk.
"""

from __future__ import annotations

import enum
import math
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from api.vat_returns.service import due_date as vat_due_date

#: Risk's stand-in for "days to the next deadline" when a client has no unfiled VAT deadline (not
#: VAT-registered, no periods yet, or everything filed): far enough away not to outrank anyone
#: with a real deadline, near enough that a pile of work still surfaces them.
NO_DEADLINE_DAYS = 90

#: The summary's "since" when the person has never marked it read and has no previous login.
DEFAULT_SINCE = timedelta(days=7)

#: How many clients each activity line names (the rest are a count).
ACTIVITY_CLIENTS_SHOWN = 3


class Chip(enum.Enum):
    MY_MOVE = "my_move"
    WAITING_ON_CLIENT = "waiting_on_client"
    VAT_NOT_FILED = "vat_not_filed"
    BOOKS_BEHIND = "books_behind"
    UP_TO_DATE = "up_to_date"
    SNOOZED = "snoozed"
    ALL = "all"


class SortKey(enum.Enum):
    RISK = "risk"
    NAME = "name"
    BOOKED_UNTIL = "booked_until"
    TO_BOOK = "to_book"
    MISSING_RECEIPTS = "missing_receipts"
    BANK_TO_MATCH = "bank_to_match"
    OPEN_QUESTIONS = "open_questions"
    VAT_DUE = "vat_due"


class VatStatus(enum.Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    READY_TO_REVIEW = "ready_to_review"
    READY_TO_FILE = "ready_to_file"
    FILED = "filed"


#: The order a deadline's buckets are reported in.
VAT_STATUSES: tuple[VatStatus, ...] = (
    VatStatus.FILED,
    VatStatus.READY_TO_FILE,
    VatStatus.READY_TO_REVIEW,
    VatStatus.IN_PROGRESS,
    VatStatus.NOT_STARTED,
)


# ---------------------------------------------------------------------------
# Facts (what the repository reads)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VatPeriodFacts:
    """One VAT period of one administration, with what its status is decided from.

    Only a FILED return is stored (0068); every other status is derived here from the period's
    own state and the work left in it - the same facts FR-VAT-002's checks read:

        filed            a vat_return exists, or the period is vat_filed
        ready_to_file    the period is locked: reviewed, waiting only for the filing
        not_started      nothing posted in the period yet
        in_progress      the period is still running, or has unmatched bank lines or unposted
                         receipts in it
        ready_to_review  ended, posted, and nothing left to book in it
    """

    administration_id: uuid.UUID
    period_id: uuid.UUID
    start_date: date
    end_date: date
    period_status: str
    period_scheme: str
    filed: bool
    has_postings: bool
    unmatched_in_period: bool
    unposted_in_period: bool

    def status(self, *, today: date) -> VatStatus:
        if self.filed:
            return VatStatus.FILED
        if self.period_status == "locked":
            return VatStatus.READY_TO_FILE
        if not self.has_postings:
            return VatStatus.NOT_STARTED
        if self.end_date >= today or self.unmatched_in_period or self.unposted_in_period:
            return VatStatus.IN_PROGRESS
        return VatStatus.READY_TO_REVIEW

    @property
    def due_date(self) -> date:
        return vat_due_date(self.end_date)

    @property
    def label(self) -> str:
        return period_label(self.period_scheme, self.start_date, self.end_date)

    @property
    def frequency(self) -> str:
        return self.period_scheme


def period_label(scheme: str, start: date, end: date) -> str:
    """`2026-09` for a month, `2026-Q3` for a quarter - named by the period's end."""
    if scheme == "monthly":
        return f"{end.year}-{end.month:02d}"
    if scheme == "quarterly":
        return f"{end.year}-Q{(end.month - 1) // 3 + 1}"
    return f"{start.isoformat()}/{end.isoformat()}"


@dataclass(frozen=True, slots=True)
class BrokenFeed:
    bank_name: str
    status: str


@dataclass(frozen=True, slots=True)
class ClientFacts:
    """Everything the queue knows about one administration, read in bulk."""

    administration_id: uuid.UUID
    display_name: str
    legal_name: str
    kvk_number: str | None
    initials: str
    colour: str
    auto_bookings: int = 0
    to_book: int = 0
    missing_receipts: int = 0
    bank_to_match: int = 0
    open_questions: int = 0
    questions_awaiting_client: int = 0
    #: Open threads where the client replied last (awaiting = 'firm'): my move.
    questions_awaiting_firm: int = 0
    oldest_awaiting_client: date | None = None
    oldest_missing_receipt: date | None = None
    oldest_unmatched: date | None = None
    last_transaction: date | None = None
    #: The least recent sync over the client's live and broken feeds (pending/revoked excluded).
    feed_synced_until: datetime | None = None
    broken_feed: BrokenFeed | None = None
    broken_feed_since: date | None = None
    vat: VatPeriodFacts | None = None
    assigned_user_id: uuid.UUID | None = None
    assigned_name: str | None = None
    snoozed_until: date | None = None
    snooze_reason: str | None = None


# ---------------------------------------------------------------------------
# Derived figures
# ---------------------------------------------------------------------------


def booked_until(facts: ClientFacts) -> tuple[date | None, bool]:
    """(booked_until, capped_by_feed). None when the client has no bank lines at all."""
    if facts.last_transaction is None:
        return None, False
    if facts.oldest_unmatched is None:
        by_lines = facts.last_transaction
    else:
        by_lines = facts.oldest_unmatched - timedelta(days=1)
    if facts.feed_synced_until is not None:
        cap = facts.feed_synced_until.date()
        if cap < by_lines:
            return cap, True
    return by_lines, False


def months_behind(booked: date | None, *, today: date) -> int | None:
    """Whole calendar months missing between `booked` and the last completed month.

    Booked to 30 June on 8 October is 3 (July, August, September). A date inside a month leaves
    that month unfinished, so it counts too. None when nothing is booked at all.
    """
    if booked is None:
        return None
    last_complete = today.year * 12 + today.month - 1  # the month before today's, as an index
    next_day = booked + timedelta(days=1)
    booked_month = booked.year * 12 + booked.month
    if next_day.month == booked.month:  # not the month's last day: that month is unfinished
        booked_month -= 1
    return max(0, last_complete - booked_month)


def is_snoozed(facts: ClientFacts, *, today: date) -> bool:
    return facts.snoozed_until is not None and facts.snoozed_until > today


@dataclass(frozen=True, slots=True)
class VatView:
    period_label: str
    frequency: str
    status: VatStatus
    due_date: date
    days_to_due: int
    period_end: date


@dataclass(frozen=True, slots=True)
class WorklistRow:
    facts: ClientFacts
    booked_until: date | None
    booked_until_capped_by_feed: bool
    months_behind: int | None
    vat: VatView | None
    waiting_on_client_since: date | None
    snoozed: bool
    risk: int
    chips: frozenset[Chip] = field(default_factory=frozenset)

    @property
    def my_move_items(self) -> int:
        return self.facts.auto_bookings + self.facts.to_book + self.facts.bank_to_match


def build_row(facts: ClientFacts, *, today: date) -> WorklistRow:
    booked, capped = booked_until(facts)
    behind = months_behind(booked, today=today)
    vat: VatView | None = None
    if facts.vat is not None:
        due = facts.vat.due_date
        vat = VatView(
            period_label=facts.vat.label,
            frequency=facts.vat.frequency,
            status=facts.vat.status(today=today),
            due_date=due,
            days_to_due=(due - today).days,
            period_end=facts.vat.end_date,
        )

    mine = facts.auto_bookings + facts.to_book + facts.bank_to_match
    deadline = vat.days_to_due if vat is not None and vat.status is not VatStatus.FILED else None
    risk = (deadline if deadline is not None else NO_DEADLINE_DAYS) - math.ceil(mine / 10)
    if facts.broken_feed is not None:
        risk -= 3
    if behind is not None and behind > 1:
        risk -= 5

    waiting_since = _earliest(
        facts.oldest_missing_receipt if facts.missing_receipts else None,
        facts.oldest_awaiting_client if facts.questions_awaiting_client else None,
        facts.broken_feed_since if facts.broken_feed is not None else None,
    )
    snoozed = is_snoozed(facts, today=today)

    # Contract decision 3: a client's reply (an open thread awaiting the firm) is my move.
    my_move = (
        mine > 0
        or facts.questions_awaiting_firm > 0
        or (vat is not None and vat.status is VatStatus.READY_TO_REVIEW)
    )
    waiting = (
        facts.missing_receipts > 0
        or facts.questions_awaiting_client > 0
        or facts.broken_feed is not None
    )
    vat_not_filed = vat is not None and vat.status is not VatStatus.FILED and vat.period_end < today
    books_behind = behind is not None and behind > 1

    chips = {Chip.ALL}
    if snoozed:
        chips.add(Chip.SNOOZED)
    else:
        if my_move:
            chips.add(Chip.MY_MOVE)
        if waiting:
            chips.add(Chip.WAITING_ON_CLIENT)
    if vat_not_filed:
        chips.add(Chip.VAT_NOT_FILED)
    if books_behind:
        chips.add(Chip.BOOKS_BEHIND)
    if not (snoozed or my_move or waiting or vat_not_filed or books_behind):
        chips.add(Chip.UP_TO_DATE)

    return WorklistRow(
        facts=facts,
        booked_until=booked,
        booked_until_capped_by_feed=capped,
        months_behind=behind,
        vat=vat,
        waiting_on_client_since=waiting_since,
        snoozed=snoozed,
        risk=risk,
        chips=frozenset(chips),
    )


def _earliest(*dates: date | None) -> date | None:
    present = [d for d in dates if d is not None]
    return min(present) if present else None


# ---------------------------------------------------------------------------
# Filtering, sorting, paging
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssignedFilter:
    """`assigned=me|any|<user_id>`. `user_id` None with `any` False never happens."""

    any: bool = True
    user_id: uuid.UUID | None = None

    def admits(self, row: WorklistRow) -> bool:
        return self.any or row.facts.assigned_user_id == self.user_id


def matches_query(row: WorklistRow, query: str | None) -> bool:
    """FR-FRM-000's search keys: KvK by prefix (all digits), names by substring."""
    if query is None or not query.strip():
        return True
    needle = query.strip()
    if needle.isdigit() and needle.isascii():
        return (row.facts.kvk_number or "").startswith(needle)
    lowered = needle.casefold()
    return (
        lowered in row.facts.display_name.casefold() or lowered in row.facts.legal_name.casefold()
    )


def _numeric_sort_value(key: SortKey, row: WorklistRow) -> int | None:
    """The value a numeric sort orders by; None sorts last in either direction."""
    if key is SortKey.RISK:
        return row.risk
    if key is SortKey.BOOKED_UNTIL:
        return None if row.booked_until is None else row.booked_until.toordinal()
    if key is SortKey.TO_BOOK:
        return row.facts.to_book
    if key is SortKey.MISSING_RECEIPTS:
        return row.facts.missing_receipts
    if key is SortKey.BANK_TO_MATCH:
        return row.facts.bank_to_match
    if key is SortKey.OPEN_QUESTIONS:
        return row.facts.open_questions
    # VAT_DUE: only an unfiled deadline counts.
    if row.vat is None or row.vat.status is VatStatus.FILED:
        return None
    return row.vat.days_to_due


def sort_rows(rows: Iterable[WorklistRow], key: SortKey, *, descending: bool) -> list[WorklistRow]:
    """Stable: ties fall back to name, then id, so paging never shows a row twice."""
    by_name = sorted(
        rows, key=lambda r: (r.facts.display_name.casefold(), str(r.facts.administration_id))
    )
    if key is SortKey.NAME:
        return list(reversed(by_name)) if descending else by_name
    present: list[tuple[int, WorklistRow]] = []
    missing: list[WorklistRow] = []
    for row in by_name:
        value = _numeric_sort_value(key, row)
        if value is None:
            missing.append(row)
        else:
            present.append((value, row))
    # sorted() is stable, so equal values keep the name order above.
    present.sort(key=lambda pair: -pair[0] if descending else pair[0])
    return [row for _, row in present] + missing


@dataclass(frozen=True, slots=True)
class WorklistPage:
    rows: list[WorklistRow]
    total: int
    page: int
    page_size: int
    chip_counts: dict[Chip, int]


def worklist_page(
    rows: Sequence[WorklistRow],
    *,
    chip: Chip,
    query: str | None,
    assigned: AssignedFilter,
    sort: SortKey,
    descending: bool,
    page: int,
    page_size: int,
) -> WorklistPage:
    """Chip counts are over the search and assignee filters, so the numbers on the chips are what
    clicking each would show."""
    filtered = [r for r in rows if matches_query(r, query) and assigned.admits(r)]
    counts = {c: sum(1 for r in filtered if c in r.chips) for c in Chip}
    in_chip = [r for r in filtered if chip in r.chips]
    ordered = sort_rows(in_chip, sort, descending=descending)
    start = (page - 1) * page_size
    return WorklistPage(
        rows=ordered[start : start + page_size],
        total=len(in_chip),
        page=page,
        page_size=page_size,
        chip_counts=counts,
    )


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------


def resolve_since(
    *,
    requested: datetime | None,
    seen_at: datetime | None,
    previous_login_at: datetime | None,
    now: datetime,
) -> datetime:
    """Contract decision 6: an explicit `since`, else when they last marked it read, else their
    previous login, else a week."""
    for candidate in (requested, seen_at, previous_login_at):
        if candidate is not None:
            return candidate if candidate.tzinfo is not None else candidate.replace(tzinfo=UTC)
    return now - DEFAULT_SINCE


class ActivityKind(enum.Enum):
    RECEIPTS_UPLOADED = "receipts_uploaded"
    AUTO_BOOKINGS_READY = "auto_bookings_ready"
    CLIENT_REPLIES = "client_replies"
    BANK_FEEDS_BROKEN = "bank_feeds_broken"
    POSSIBLE_DUPLICATES = "possible_duplicates"


@dataclass(frozen=True, slots=True)
class ActivityLine:
    kind: ActivityKind
    count: int
    client_count: int
    clients: list[tuple[uuid.UUID, str]]


def activity_lines(
    per_client: Iterable[tuple[ActivityKind, uuid.UUID, int]],
    names: dict[uuid.UUID, str],
) -> list[ActivityLine]:
    """One line per kind that has anything, in ActivityKind's order. Each names the clients with
    the most, up to ACTIVITY_CLIENTS_SHOWN; only administrations in `names` (the caller's
    authorized portfolio) are ever counted or named."""
    by_kind: dict[ActivityKind, dict[uuid.UUID, int]] = {}
    for kind, administration_id, count in per_client:
        if administration_id not in names or count <= 0:
            continue
        bucket = by_kind.setdefault(kind, {})
        bucket[administration_id] = bucket.get(administration_id, 0) + count
    lines: list[ActivityLine] = []
    for kind in ActivityKind:
        per_admin = by_kind.get(kind)
        if not per_admin:
            continue
        top = sorted(per_admin.items(), key=lambda item: (-item[1], names[item[0]].casefold()))
        lines.append(
            ActivityLine(
                kind=kind,
                count=sum(per_admin.values()),
                client_count=len(per_admin),
                clients=[(aid, names[aid]) for aid, _ in top[:ACTIVITY_CLIENTS_SHOWN]],
            )
        )
    return lines


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Deadline:
    kind: str
    period_label: str
    due_date: date
    days_to_due: int
    client_count: int
    buckets: dict[VatStatus, int]


def deadlines(periods: Iterable[VatPeriodFacts], *, today: date) -> list[Deadline]:
    """One row per (period label, due date): a monthly September and a Q3 are both due on 31
    October and are two rows, because they are two different returns."""
    groups: dict[tuple[str, date], dict[VatStatus, set[uuid.UUID]]] = {}
    for period in periods:
        key = (period.label, period.due_date)
        group = groups.setdefault(key, {status: set() for status in VAT_STATUSES})
        group[period.status(today=today)].add(period.administration_id)
    result = [
        Deadline(
            kind="vat",
            period_label=label,
            due_date=due,
            days_to_due=(due - today).days,
            client_count=len(set().union(*buckets.values())),
            buckets={status: len(ids) for status, ids in buckets.items()},
        )
        for (label, due), buckets in groups.items()
    ]
    result.sort(key=lambda d: (d.due_date, d.period_label))
    return result
