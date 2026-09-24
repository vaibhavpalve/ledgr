"""Reminder e-mails against a real Postgres (ADR-090).

An onboarded company posts one BTW-tagged sale in January 2026; the reminder run - as ledgr_ops,
the way scripts/send_reminders.py runs it - tells its owner once in the week before the deadline,
once after it, and never someone who switched reminders off. The preference routes are the
caller's own row only.
"""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import get_ops_engine
from api.mail.sender import CollectingEmailSender
from api.reminders.service import run_reminders
from tests.integration.test_opening_balance_routes import _world
from tests.integration.test_posting_defaults import _rows
from tests.integration.test_sales_invoice_posting import _exec
from tests.support.seed import SeededTenants


async def _sale_in_january(tenants: SeededTenants, admin: str, codes: dict[str, str]) -> None:
    [(period,)] = await _rows(
        "SELECT id FROM period WHERE administration_id = :a AND start_date = '2026-01-01'",
        a=admin,
    )
    [(journal,)] = await _rows(
        "SELECT id FROM ledger_journal WHERE administration_id = :a AND journal_type = 'memorial'",
        a=admin,
    )
    lines = [
        {"account_id": codes["1100"], "debit": "121.00", "credit": "0.00"},
        {
            "account_id": codes["8000"],
            "debit": "0.00",
            "credit": "100.00",
            "vat_treatment": "btw_21",
        },
        {
            "account_id": codes["1700"],
            "debit": "0.00",
            "credit": "21.00",
            "vat_treatment": "btw_21",
        },
    ]
    await _exec(
        tenants,
        "SELECT ledger.post_entry(p_administration_id => :admin, p_journal_id => :journal, "
        "  p_period_id => :period, p_entry_date => :on, p_description => 'Verkoop', "
        "  p_document_reference => NULL, p_posted_by_user_id => NULL, p_source_system => 'test', "
        "  p_lines => CAST(:lines AS jsonb))",
        admin=admin,
        journal=str(journal),
        period=str(period),
        on=date(2026, 1, 15),
        lines=json.dumps(lines),
    )


async def _run(today: date) -> CollectingEmailSender:
    sender = CollectingEmailSender()
    engine = get_ops_engine()
    try:
        async with engine.connect() as conn:
            session = AsyncSession(bind=conn)
            await run_reminders(session, sender, today=today)
            await session.commit()
    finally:
        await engine.dispose()
    return sender


async def _email_of(user: uuid.UUID) -> str:
    engine = get_ops_engine()
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT email FROM users WHERE id = :id"), {"id": str(user)}
            )
            return str(result.scalar_one())
    finally:
        await engine.dispose()


def _to(sender: CollectingEmailSender, email: str) -> list[str]:
    return [message.subject for message in sender.sent if message.to == email]


async def test_the_owner_is_reminded_once_before_and_once_after_the_deadline(
    two_organizations: SeededTenants,
) -> None:
    admin, _year, codes = await _world(two_organizations)
    await _sale_in_january(two_organizations, admin, codes)
    owner = await _email_of(two_organizations.owner_a)

    early = await _run(date(2026, 2, 20))
    assert _to(early, owner) == []

    due = await _run(date(2026, 2, 23))
    [subject] = _to(due, owner)
    assert "28-02-2026" in subject

    again = await _run(date(2026, 2, 24))
    assert _to(again, owner) == []

    late = await _run(date(2026, 3, 2))
    [overdue] = _to(late, owner)
    assert "te laat" in overdue

    # The other tenant's owner hears nothing about this administration.
    other = await _email_of(two_organizations.owner_b)
    assert all("Van Doorn" not in s for s in _to(due, other) + _to(late, other))


@pytest.mark.isolation("PUT", "/v1/me/reminders")
async def test_switching_reminders_off_is_the_callers_own_and_is_respected(
    two_organizations: SeededTenants,
) -> None:
    admin, _year, codes = await _world(two_organizations)
    await _sale_in_january(two_organizations, admin, codes)

    response = await _call_me(two_organizations, "PUT", {"enabled": False})
    assert response.status_code == 200, response.text
    assert response.json() == {"enabled": False}

    # The other user's preference is untouched.
    theirs = await _call_me(two_organizations, "GET", None, foreign=True)
    assert theirs.json() == {"enabled": True}

    sent = await _run(date(2026, 2, 25))
    assert _to(sent, await _email_of(two_organizations.owner_a)) == []


@pytest.mark.isolation("GET", "/v1/me/reminders")
async def test_reading_the_preference(two_organizations: SeededTenants) -> None:
    mine = await _call_me(two_organizations, "GET", None)
    assert mine.status_code == 200
    assert mine.json() == {"enabled": True}


async def _call_me(tenants: SeededTenants, method: str, body: object, *, foreign: bool = False):  # type: ignore[no-untyped-def]
    from httpx import ASGITransport, AsyncClient

    from api.main import app
    from tests.support.isolation import make_token

    token = (
        make_token(tenants.org_b, user_id=tenants.owner_b)
        if foreign
        else make_token(tenants.org_a, user_id=tenants.owner_a)
    )
    headers = {"Authorization": f"Bearer {token}"}
    if method != "GET":
        headers["Idempotency-Key"] = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        return await c.request(method, "/v1/me/reminders", headers=headers, json=body)
