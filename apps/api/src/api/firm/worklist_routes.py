"""The firm home's work queue and deadlines: FR-FRM-001, FR-FRM-002 (ADR-109).

    GET /v1/firm/worklist?chip=&q=&assigned=me|any|<user_id>&sort=&dir=&vat_frequency=
                         &page=&page_size=
    GET /v1/firm/worklist/next?after=<administration_id>&<the same query, no paging>
    GET /v1/firm/deadlines?from=&to=

`next` is contract-wave2 decision 7 (ADR-115): the next client in the same query, after the one
open, skipping snoozed clients - `api.firm.worklist_model.next_client`.

Registered via `register(app)`, not `include_router` - see `api.documents.routes.register`.

Both are portfolio reads: `require_portfolio_permission()` resolves the administrations the
caller holds a live grant on and is authorized for `view report` on, one `authorize()` per
administration (`api.firm.worklist_access`). Shapes are docs/firm-home/contract.md's; dates are
ISO strings and there is no money on these two (NFR-031 has nothing to round here).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Literal

from fastapi import Depends, FastAPI, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import get_db_session
from api.firm.worklist_access import PORTFOLIO_READ, Portfolio
from api.firm.worklist_model import (
    AssignedFilter,
    Chip,
    Deadline,
    SortKey,
    VatFrequency,
    WorklistQuery,
    WorklistRow,
    build_row,
    deadlines,
    next_client,
    worklist_page,
)
from api.firm.worklist_repository import SqlWorklistRepository
from api.i18n.http import problem

#: The deadlines view's default window: from the start of this month to three months out - the
#: returns being worked on now and the next round.
DEFAULT_DEADLINE_DAYS_AHEAD = 92
MAX_DEADLINE_RANGE_DAYS = 366


def register(app: FastAPI) -> None:
    app.add_api_route("/v1/firm/worklist", get_worklist, methods=["GET"], name="get_firm_worklist")
    app.add_api_route(
        "/v1/firm/worklist/next",
        get_next_client,
        methods=["GET"],
        name="get_firm_worklist_next",
    )
    app.add_api_route(
        "/v1/firm/deadlines", get_deadlines, methods=["GET"], name="get_firm_deadlines"
    )


async def get_worklist_repository(
    session: AsyncSession = Depends(get_db_session),
) -> SqlWorklistRepository:
    return SqlWorklistRepository(session)


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def row_json(row: WorklistRow) -> dict[str, object]:
    facts = row.facts
    return {
        "administration_id": str(facts.administration_id),
        "display_name": facts.display_name,
        "legal_name": facts.legal_name,
        "kvk_number": facts.kvk_number,
        "initials": facts.initials,
        "colour": facts.colour,
        "assigned_user_id": None if facts.assigned_user_id is None else str(facts.assigned_user_id),
        "assigned_name": facts.assigned_name,
        "booked_until": _iso(row.booked_until),
        "booked_until_capped_by_feed": row.booked_until_capped_by_feed,
        "months_behind": row.months_behind,
        "counts": {
            "auto_bookings": facts.auto_bookings,
            "to_book": facts.to_book,
            "missing_receipts": facts.missing_receipts,
            "bank_to_match": facts.bank_to_match,
            "open_questions": facts.open_questions,
        },
        "waiting_on_client_since": _iso(row.waiting_on_client_since),
        "broken_feed": (
            None
            if facts.broken_feed is None
            else {"bank_name": facts.broken_feed.bank_name, "status": facts.broken_feed.status}
        ),
        "vat": (
            None
            if row.vat is None
            else {
                "period_label": row.vat.period_label,
                "frequency": row.vat.frequency,
                "status": row.vat.status.value,
                "due_date": row.vat.due_date.isoformat(),
                "days_to_due": row.vat.days_to_due,
            }
        ),
        "snoozed_until": _iso(facts.snoozed_until),
        "snooze_reason": facts.snooze_reason,
        "last_chased_at": (
            None if facts.last_chased_at is None else facts.last_chased_at.isoformat()
        ),
        "rules_count": facts.rules_count,
        "risk": row.risk,
    }


def deadline_json(deadline: Deadline) -> dict[str, object]:
    return {
        "kind": deadline.kind,
        "period_label": deadline.period_label,
        "due_date": deadline.due_date.isoformat(),
        "days_to_due": deadline.days_to_due,
        "client_count": deadline.client_count,
        "buckets": {status.value: count for status, count in deadline.buckets.items()},
    }


def _assigned_filter(request: Request, raw: str, user_id: uuid.UUID) -> AssignedFilter:
    try:
        return AssignedFilter.parse(raw, user_id=user_id)
    except ValueError as exc:
        raise problem(
            request,
            422,
            "errors.firm_assigned_filter_invalid",
            reason="firm_assigned_filter_invalid",
        ) from exc


async def load_rows(
    portfolio: Portfolio, repository: SqlWorklistRepository, *, today: date
) -> list[WorklistRow]:
    """Every portfolio row, from one set-based load - what every worklist query is evaluated
    over (the page, `next`, and every saved view's count)."""
    facts = await repository.client_facts(portfolio.entries, today=today)
    return [build_row(f, today=today) for f in facts]


async def get_worklist(
    request: Request,
    chip: Chip = Chip.MY_MOVE,
    q: str | None = Query(default=None, max_length=200),
    assigned: str = "any",
    sort: SortKey = SortKey.RISK,
    dir: Literal["asc", "desc"] = "asc",
    vat_frequency: VatFrequency | None = None,
    page: int = Query(default=1, ge=1),
    # Contract: up to 1000 - the frontend's "Show all"; anything larger is a 422.
    page_size: int = Query(default=50, ge=1, le=1000),
    portfolio: Portfolio = Depends(PORTFOLIO_READ),
    repository: SqlWorklistRepository = Depends(get_worklist_repository),
) -> dict[str, object]:
    """FR-FRM-002. Every row is computed (the chip counts need all of them), then filtered,
    sorted and paged in memory - a portfolio is hundreds of rows, never millions, and the facts
    behind them are read set-based in a fixed number of queries."""
    filter_ = _assigned_filter(request, assigned, portfolio.user_id)
    rows = await load_rows(portfolio, repository, today=date.today())
    result = worklist_page(
        rows,
        chip=chip,
        query=q,
        assigned=filter_,
        sort=sort,
        descending=dir == "desc",
        page=page,
        page_size=page_size,
        vat_frequency=vat_frequency,
    )
    return {
        "rows": [row_json(row) for row in result.rows],
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
        "chip_counts": {c.value: n for c, n in result.chip_counts.items()},
    }


async def get_next_client(
    request: Request,
    after: uuid.UUID | None = None,
    chip: Chip = Chip.MY_MOVE,
    q: str | None = Query(default=None, max_length=200),
    assigned: str = "any",
    sort: SortKey = SortKey.RISK,
    dir: Literal["asc", "desc"] = "asc",
    vat_frequency: VatFrequency | None = None,
    portfolio: Portfolio = Depends(PORTFOLIO_READ),
    repository: SqlWorklistRepository = Depends(get_worklist_repository),
) -> dict[str, object]:
    """Contract-wave2 decision 7. Evaluated now, against current data, over the caller's
    authorized portfolio only - an `after` outside it is treated as "from the top" and its
    position is never used (ADR-115)."""
    query = WorklistQuery(
        chip=chip,
        q=q,
        assigned=_assigned_filter(request, assigned, portfolio.user_id),
        sort=sort,
        descending=dir == "desc",
        vat_frequency=vat_frequency,
    )
    rows = await load_rows(portfolio, repository, today=date.today())
    result = next_client(rows, query, after=after)
    return {
        "administration_id": (
            None if result.row is None else str(result.row.facts.administration_id)
        ),
        "display_name": None if result.row is None else result.row.facts.display_name,
        "remaining": result.remaining,
    }


async def get_deadlines(
    request: Request,
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    portfolio: Portfolio = Depends(PORTFOLIO_READ),
    repository: SqlWorklistRepository = Depends(get_worklist_repository),
) -> list[dict[str, object]]:
    """The statutory deadlines Boeklite tracks, across the portfolio: VAT only. ICP is not
    modelled by vat_returns (0068 stores the BTW return alone) and payroll is not in Boeklite,
    so neither is listed - a deadline we cannot track is one we must not pretend to."""
    today = date.today()
    start = from_ if from_ is not None else today.replace(day=1)
    end = to if to is not None else today + timedelta(days=DEFAULT_DEADLINE_DAYS_AHEAD)
    if end < start or (end - start).days > MAX_DEADLINE_RANGE_DAYS:
        raise problem(
            request,
            422,
            "errors.firm_deadline_range_invalid",
            reason="firm_deadline_range_invalid",
            max_days=MAX_DEADLINE_RANGE_DAYS,
        )
    periods = await repository.deadline_periods(portfolio.entries, from_date=start, to_date=end)
    return [deadline_json(d) for d in deadlines(periods, today=today)]
