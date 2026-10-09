"""The firm home's "since you were last here" summary: FR-FRM-001 (ADR-109).

    GET  /v1/firm/summary?since=<iso>   the counts and what changed since
    POST /v1/firm/summary/seen          "I've read it" - moves the window to now

The window is contract decision 6: an explicit `since`, else `users.firm_activity_seen_at`
(set only by POST .../seen, so opening the app on a phone does not reset it), else
`users.previous_login_at` (the login hook in api.auth.repository), else seven days.

Both are portfolio routes (`api.firm.worklist_access`): the counts and the clients named in the
activity lines are only ever administrations the caller is authorized for, per administration.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import Depends, FastAPI, Query, Response

from api.audit.log import AuditCategory
from api.firm.worklist_access import PORTFOLIO_READ, Portfolio, require_portfolio_permission
from api.firm.worklist_model import activity_lines, resolve_since
from api.firm.worklist_repository import SqlWorklistRepository
from api.firm.worklist_routes import get_worklist_repository

#: POST .../seen writes only the caller's own `users` row. Declared as a portfolio route so the
#: authorization and audit coverage checks see a requirement, and so the act is recorded (IAM-090,
#: configuration) - it moves what the next summary reports.
_MARK_SEEN = require_portfolio_permission(audit=AuditCategory.CONFIGURATION)


def register(app: FastAPI) -> None:
    app.add_api_route("/v1/firm/summary", get_summary, methods=["GET"], name="get_firm_summary")
    app.add_api_route(
        "/v1/firm/summary/seen",
        mark_summary_seen,
        methods=["POST"],
        status_code=204,
        name="mark_firm_summary_seen",
    )


async def get_summary(
    since: datetime | None = Query(default=None),
    portfolio: Portfolio = Depends(PORTFOLIO_READ),
    repository: SqlWorklistRepository = Depends(get_worklist_repository),
) -> dict[str, object]:
    now = datetime.now(UTC)
    window = await repository.login_window(portfolio.user_id)
    resolved = resolve_since(
        requested=since,
        seen_at=window.firm_activity_seen_at,
        previous_login_at=window.previous_login_at,
        now=now,
    )
    ids = portfolio.administration_ids
    facts = await repository.client_facts(portfolio.entries, today=date.today())
    activity = activity_lines(
        await repository.activity(ids, since=resolved), names=portfolio.names()
    )
    return {
        "previous_login_at": (
            None if window.previous_login_at is None else window.previous_login_at.isoformat()
        ),
        "since": resolved.isoformat(),
        "client_count": len(ids),
        "counts": {
            "auto_bookings": sum(f.auto_bookings for f in facts),
            "receipts_to_book": sum(f.to_book for f in facts),
            "missing_receipts": sum(f.missing_receipts for f in facts),
            "bank_to_match": sum(f.bank_to_match for f in facts),
            "open_questions": sum(f.open_questions for f in facts),
            "broken_feeds": await repository.broken_feed_count(ids),
        },
        "activity": [
            {
                "kind": line.kind.value,
                "count": line.count,
                "client_count": line.client_count,
                "clients": [
                    {"administration_id": str(aid), "display_name": name}
                    for aid, name in line.clients
                ],
            }
            for line in activity
        ],
    }


async def mark_summary_seen(
    portfolio: Portfolio = Depends(_MARK_SEEN),
    repository: SqlWorklistRepository = Depends(get_worklist_repository),
) -> Response:
    await repository.mark_activity_seen(portfolio.user_id, at=datetime.now(UTC))
    return Response(status_code=204)
