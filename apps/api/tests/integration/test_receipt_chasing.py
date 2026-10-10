"""Receipt chasing against a real Postgres (migration 0083, ADR-114).

The world:
  * client A   - org_a owns admin_a; owner_a holds Owner at org_a (organization-scoped, which a
                 firm session cannot see). A client-side Viewer can read the list but cannot
                 submit expenses, so is never mailed.
  * the firm   - Kantoor De Jong, engaged on admin_a; `staff` holds Accountant on admin_a.
                 `junior` works at the firm but holds no grant on admin_a.
  * B          - org_b with admin_b and the same kind of lines; nobody here may reach it.
  * firm D     - another firm with no engagement on anything.

    admin_a / admin_b   one unmatched debit of 50.00 (Tankstation)  -> missing receipt
                        one unmatched credit of 100.00              -> not asked for

Skipped without TENANT_ISOLATION_TESTS_ENABLED=1; the sweep tests need OPS_DATABASE_URL.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from api.chasing.sweep import SweepReport, run_chase_sweep
from api.db import engine as app_engine
from api.mail.sender import CollectingEmailSender
from api.main import app
from tests.support.isolation import make_token
from tests.support.seed import (
    SeededTenants,
    grant_role,
    seed_user,
    signup_firm_organization,
)

MISSING = "/v1/administrations/{administration_id}/missing-receipts"
SETTING = "/v1/administrations/{administration_id}/chase-setting"
PREVIEW = "/v1/firm/chase/preview"
SEND = "/v1/firm/chase/send"


async def _as_org(org_id: uuid.UUID, sql: str, **params: object) -> Any:
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(org_id)}
        )
        result = await conn.execute(text(sql), params)
        return result.scalar_one() if result.returns_rows else None


async def _email(user: uuid.UUID) -> str:
    async with app_engine.begin() as conn:
        return str(
            (
                await conn.execute(text("SELECT email FROM users WHERE id = :id"), {"id": user})
            ).scalar_one()
        )


@dataclass(frozen=True)
class World:
    tenants: SeededTenants
    firm: uuid.UUID
    staff: uuid.UUID
    junior: uuid.UUID
    viewer: uuid.UUID
    other_firm: uuid.UUID
    other_firm_user: uuid.UUID

    @property
    def a(self) -> uuid.UUID:
        return self.tenants.admin_a

    @property
    def b(self) -> uuid.UUID:
        return self.tenants.admin_b


async def _books(org: uuid.UUID, admin: uuid.UUID, *, tag: str) -> None:
    ledger_account = await _as_org(
        org,
        "SELECT (ledger.create_account(:admin, '1100', 'Bank', 'asset', NULL, NULL, NULL)).id",
        admin=str(admin),
    )
    account = await _as_org(
        org,
        "INSERT INTO bank_account (organization_id, administration_id, name, ledger_account_id) "
        "VALUES (:org, :admin, :name, :ledger) RETURNING id",
        org=str(org),
        admin=str(admin),
        name=f"Rekening {tag}",
        ledger=str(ledger_account),
    )
    for amount, counterparty in (("-50.00", "Tankstation"), ("100.00", "Klant")):
        await _as_org(
            org,
            "INSERT INTO bank_transaction (organization_id, administration_id, bank_account_id, "
            "  booking_date, amount, counterparty_name, description, external_id) "
            "VALUES (:org, :admin, :account, :on, CAST(:amount AS numeric), :cp, :d, :ext)",
            org=str(org),
            admin=str(admin),
            account=str(account),
            on=date(2026, 9, 10),
            amount=amount,
            cp=f"{counterparty} {tag}",
            d=f"Pinbetaling {tag}",
            ext=f"{tag}-{amount}-{uuid.uuid4().hex[:6]}",
        )


async def _engage(firm: uuid.UUID, admin: uuid.UUID, *, client: uuid.UUID) -> None:
    await _as_org(
        firm,
        "INSERT INTO firm_engagement (firm_organization_id, administration_id, status, "
        "  initiated_by) VALUES (:firm, :admin, 'pending', 'firm')",
        firm=str(firm),
        admin=str(admin),
    )
    await _as_org(
        client,
        "UPDATE firm_engagement SET status = 'active' "
        "WHERE firm_organization_id = :firm AND administration_id = :admin",
        firm=str(firm),
        admin=str(admin),
    )


@pytest_asyncio.fixture
async def w(two_organizations: SeededTenants) -> AsyncIterator[World]:
    t = two_organizations
    suffix = uuid.uuid4().hex[:8]
    firm = await signup_firm_organization(app_engine, name="Kantoor De Jong", kvk="33333333")
    staff = await seed_user(app_engine, email=f"staff+{suffix}@dejong.example")
    junior = await seed_user(app_engine, email=f"junior+{suffix}@dejong.example")
    for person in (staff, junior):
        await grant_role(
            app_engine,
            acting_org_id=firm,
            user_id=person,
            role_name="Owner" if person == staff else "Billing Admin",
            scope_type="organization",
            scope_id=firm,
        )
    await _engage(firm, t.admin_a, client=t.org_a)
    await grant_role(
        app_engine,
        acting_org_id=firm,
        user_id=staff,
        role_name="Accountant",
        scope_type="administration",
        scope_id=t.admin_a,
    )
    viewer = await seed_user(app_engine, email=f"viewer+{suffix}@client.example")
    await grant_role(
        app_engine,
        acting_org_id=t.org_a,
        user_id=viewer,
        role_name="Viewer",
        scope_type="administration",
        scope_id=t.admin_a,
        granted_by=t.owner_a,
    )
    other_firm = await signup_firm_organization(app_engine, name="Kantoor Elders", kvk="55555555")
    other_firm_user = await seed_user(app_engine, email=f"elders+{suffix}@example.com")
    await grant_role(
        app_engine,
        acting_org_id=other_firm,
        user_id=other_firm_user,
        role_name="Owner",
        scope_type="organization",
        scope_id=other_firm,
    )
    await _books(t.org_a, t.admin_a, tag="AAA")
    await _books(t.org_b, t.admin_b, tag="BBB")
    yield World(t, firm, staff, junior, viewer, other_firm, other_firm_user)


async def _call(
    org: uuid.UUID, user: uuid.UUID, method: str, path: str, body: object = None
) -> Response:
    headers = {"Authorization": f"Bearer {make_token(org, user_id=user)}"}
    if method != "GET":
        headers["Idempotency-Key"] = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        return await c.request(method, path, headers=headers, json=body)


def _p(template: str, admin: uuid.UUID) -> str:
    return template.format(administration_id=admin)


async def _sweep(
    w: World, *, now: datetime | None = None, sender: CollectingEmailSender | None = None
) -> tuple[SweepReport, CollectingEmailSender]:
    sender = sender or CollectingEmailSender()
    ops = create_async_engine(os.environ["OPS_DATABASE_URL"])
    try:
        async with ops.connect() as conn:
            report = await run_chase_sweep(
                ops_session=AsyncSession(bind=conn),
                app_engine=app_engine,
                sender=sender,
                now=now,
                only=[w.a, w.b],
            )
    finally:
        await ops.dispose()
    return report, sender


# ---------------------------------------------------------------------------
# GET missing-receipts
# ---------------------------------------------------------------------------


@pytest.mark.isolation("GET", MISSING)
async def test_the_missing_list_is_the_worklists_bucket_and_only_its_own(w: World) -> None:
    for org, user in ((w.tenants.org_a, w.tenants.owner_a), (w.firm, w.staff)):
        response = await _call(org, user, "GET", _p(MISSING, w.a))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["count"] == 1
        [item] = body["items"]
        assert (item["amount"], item["counterparty"], item["booking_date"]) == (
            "-50.00",
            "Tankstation AAA",
            "2026-09-10",
        )
        assert "BBB" not in response.text

    # The same bucket the accountant's worklist counts (wave-1 decision 5).
    row = (await _call(w.firm, w.staff, "GET", "/v1/firm/worklist?chip=all")).json()["rows"]
    assert [r["counts"]["missing_receipts"] for r in row if r["administration_id"] == str(w.a)] == [
        1
    ]

    for org, user, admin in (
        (w.tenants.org_b, w.tenants.owner_b, w.a),
        (w.firm, w.staff, w.b),
        (w.other_firm, w.other_firm_user, w.a),
        (w.firm, w.junior, w.a),
    ):
        refused = await _call(org, user, "GET", _p(MISSING, admin))
        assert refused.status_code in {403, 404}, refused.text
        assert "Tankstation" not in refused.text


# ---------------------------------------------------------------------------
# chase-setting
# ---------------------------------------------------------------------------


@pytest.mark.isolation("GET", SETTING)
async def test_the_setting_defaults_to_off_and_is_private(w: World) -> None:
    response = await _call(w.firm, w.staff, "GET", _p(SETTING, w.a))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "enabled": False,
        "cadence": "weekly",
        "set_at": None,
        "last_chased_at": None,
    }
    for org, user, admin in (
        (w.tenants.org_b, w.tenants.owner_b, w.a),
        (w.firm, w.staff, w.b),
        (w.other_firm, w.other_firm_user, w.a),
    ):
        refused = await _call(org, user, "GET", _p(SETTING, admin))
        assert refused.status_code in {403, 404}, refused.text


@pytest.mark.isolation("POST", SETTING)
async def test_opting_in_is_for_those_who_reconcile_and_is_append_only(w: World) -> None:
    on = await _call(
        w.firm, w.staff, "POST", _p(SETTING, w.a), {"enabled": True, "cadence": "fortnightly"}
    )
    assert on.status_code == 200, on.text
    assert (on.json()["enabled"], on.json()["cadence"]) == (True, "fortnightly")
    seen = (await _call(w.tenants.org_a, w.tenants.owner_a, "GET", _p(SETTING, w.a))).json()
    assert (seen["enabled"], seen["cadence"]) == (True, "fortnightly")

    # A Viewer may read the list but not make the product e-mail the client.
    viewer = await _call(w.tenants.org_a, w.viewer, "POST", _p(SETTING, w.a), {"enabled": False})
    assert viewer.status_code == 403, viewer.text
    for org, user, admin in (
        (w.tenants.org_b, w.tenants.owner_b, w.a),
        (w.firm, w.staff, w.b),
        (w.other_firm, w.other_firm_user, w.a),
    ):
        refused = await _call(org, user, "POST", _p(SETTING, admin), {"enabled": True})
        assert refused.status_code in {403, 404}, refused.text
    theirs = (await _call(w.tenants.org_b, w.tenants.owner_b, "GET", _p(SETTING, w.b))).json()
    assert theirs["enabled"] is False

    bad = await _call(w.firm, w.staff, "POST", _p(SETTING, w.a), {"enabled": True, "cadence": "x"})
    assert bad.status_code == 422

    for statement in ("UPDATE chase_setting SET enabled = false", "DELETE FROM chase_setting"):
        with pytest.raises(DBAPIError, match="permission denied"):
            await _as_org(w.tenants.org_a, statement)


# ---------------------------------------------------------------------------
# preview and send
# ---------------------------------------------------------------------------


@pytest.mark.isolation("POST", PREVIEW)
async def test_the_preview_covers_only_granted_clients(w: World) -> None:
    response = await _call(
        w.firm, w.staff, "POST", PREVIEW, {"administration_ids": [str(w.a), str(w.b)]}
    )
    assert response.status_code == 200, response.text
    [item] = response.json()["items"]
    assert item == {
        "administration_id": str(w.a),
        "display_name": item["display_name"],
        "missing_count": 1,
        "recipient_count": None,  # not counted yet: the sweep has not visited this client
        "last_chased_at": None,
        "blocked_reason": None,
    }
    assert str(w.b) not in response.text

    # A colleague without a grant on the client, another firm and another client see nothing.
    for org, user in (
        (w.firm, w.junior),
        (w.other_firm, w.other_firm_user),
        (w.tenants.org_b, w.tenants.owner_b),
    ):
        refused = await _call(org, user, "POST", PREVIEW, {"administration_ids": [str(w.a)]})
        assert refused.status_code == 200, refused.text
        assert refused.json() == {"items": []}

    # Once the sweep has counted: the Owner qualifies, the client's Viewer and the firm do not.
    await _sweep(w)
    counted = (
        await _call(w.firm, w.staff, "POST", PREVIEW, {"administration_ids": [str(w.a)]})
    ).json()
    assert counted["items"][0]["recipient_count"] == 1

    too_many = await _call(
        w.firm,
        w.staff,
        "POST",
        PREVIEW,
        {"administration_ids": [str(uuid.uuid4()) for _ in range(1001)]},
    )
    assert too_many.status_code == 422
    assert too_many.json()["detail"]["reason"] == "chase_too_many_administrations"


@pytest.mark.isolation("POST", SEND)
async def test_a_manual_chase_reaches_the_owner_once_and_says_no_amounts(w: World) -> None:
    owner_email, viewer_email, staff_email = (
        await _email(w.tenants.owner_a),
        await _email(w.viewer),
        await _email(w.staff),
    )
    # Ungranted: a colleague without a grant, another firm, another client.
    for org, user in (
        (w.firm, w.junior),
        (w.other_firm, w.other_firm_user),
        (w.tenants.org_b, w.tenants.owner_b),
    ):
        refused = await _call(org, user, "POST", SEND, {"administration_ids": [str(w.a)]})
        assert refused.status_code == 200, refused.text
        assert refused.json() == {
            "sent": 0,
            "skipped": [{"administration_id": str(w.a), "reason": "administration_not_found"}],
        }

    sent = await _call(w.firm, w.staff, "POST", SEND, {"administration_ids": [str(w.a), str(w.b)]})
    assert sent.status_code == 200, sent.text
    assert sent.json() == {
        "sent": 1,
        "skipped": [{"administration_id": str(w.b), "reason": "administration_not_found"}],
    }
    again = await _call(w.firm, w.staff, "POST", SEND, {"administration_ids": [str(w.a)]})
    assert again.json()["skipped"] == [{"administration_id": str(w.a), "reason": "chased_recently"}]

    # The firm's session cannot record a send itself.
    with pytest.raises(DBAPIError):
        await _as_org(
            w.firm,
            "INSERT INTO chase_send (organization_id, administration_id, kind, missing_count, "
            "  recipient_count, window_key) VALUES (:org, :admin, 'scheduled', 1, 1, 'x')",
            org=str(w.tenants.org_a),
            admin=str(w.a),
        )

    sender = CollectingEmailSender()
    first, _ = await _sweep(w, sender=sender)
    second, _ = await _sweep(w, sender=sender)
    assert first.sent == 1, first.details
    assert second.sent == 0
    ours = [m for m in sender.sent if m.to in {owner_email, viewer_email, staff_email}]
    assert [m.to for m in ours] == [owner_email]
    body = ours[0].body
    for leaked in ("50", "Tankstation", "Pinbetaling", "AAA", "€"):
        assert leaked not in body
    assert f"/receipts-needed?administration={w.a}" in body

    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(w.tenants.org_a)},
        )
        rows = (
            await conn.execute(
                text(
                    "SELECT kind, missing_count, recipient_count, requested_by_user_id "
                    "FROM chase_send WHERE administration_id = :a"
                ),
                {"a": w.a},
            )
        ).all()
        audited = (
            await conn.execute(
                text(
                    "SELECT actor_type, detail->>'kind' FROM audit_log "
                    "WHERE administration_id = :a AND action = 'send_receipt_chase'"
                ),
                {"a": w.a},
            )
        ).all()
        requested = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit_log "
                    "WHERE administration_id = :a AND action = 'request_receipt_chase'"
                ),
                {"a": w.a},
            )
        ).scalar_one()
    assert [(r.kind, r.missing_count, r.recipient_count, r.requested_by_user_id) for r in rows] == [
        ("manual", 1, 1, w.staff)
    ]
    assert [tuple(r) for r in audited] == [("system", "manual")]
    assert requested == 1

    # The worklist's client row and the setting now carry the chase.
    setting = (await _call(w.firm, w.staff, "GET", _p(SETTING, w.a))).json()
    assert setting["last_chased_at"] is not None
    with pytest.raises(DBAPIError, match="permission denied"):
        await _as_org(w.tenants.org_a, "UPDATE chase_send SET missing_count = 2")


async def test_a_scheduled_chase_is_sent_once_per_window(w: World) -> None:
    await _call(w.firm, w.staff, "POST", _p(SETTING, w.a), {"enabled": True, "cadence": "weekly"})
    monday = datetime(2031, 1, 6, 9, 0, tzinfo=UTC)  # in send hours, ISO week 2031-W02
    sender = CollectingEmailSender()
    owner_email = await _email(w.tenants.owner_a)

    first, _ = await _sweep(w, now=monday, sender=sender)
    assert first.sent == 1, first.details
    for later in (
        monday + timedelta(minutes=5),  # the next run
        monday + timedelta(days=1, hours=1),  # past 24 hours, same window
        monday + timedelta(days=4),  # Friday, same window
    ):
        again, _ = await _sweep(w, now=later, sender=sender)
        assert again.sent == 0, (later, again.details)
    # Saturday of the next week is outside send hours; Monday of the next week sends.
    weekend, _ = await _sweep(w, now=monday + timedelta(days=12), sender=sender)
    assert weekend.sent == 0
    next_week, _ = await _sweep(w, now=monday + timedelta(days=7), sender=sender)
    assert next_week.sent == 1, next_week.details
    assert [m.to for m in sender.sent if m.to == owner_email] == [owner_email, owner_email]


async def test_nothing_is_sent_when_disabled_or_nothing_is_missing(w: World) -> None:
    monday = datetime(2031, 2, 3, 9, 0, tzinfo=UTC)
    sender = CollectingEmailSender()
    # Enabled, then switched off: the latest row wins.
    await _call(w.firm, w.staff, "POST", _p(SETTING, w.a), {"enabled": True})
    await _call(w.firm, w.staff, "POST", _p(SETTING, w.a), {"enabled": False})
    off, _ = await _sweep(w, now=monday, sender=sender)
    assert off.sent == 0
    assert sender.sent == []

    # Enabled, but the only debit is now an auto-booking (a pending HIGH proposal), not missing.
    await _call(w.firm, w.staff, "POST", _p(SETTING, w.a), {"enabled": True})
    await _as_org(
        w.tenants.org_a,
        "INSERT INTO booking_proposal (organization_id, administration_id, bank_transaction_id, "
        "  confidence, group_key, amount) "
        "SELECT t.organization_id, t.administration_id, t.id, 'high', 'tankstation|4000', 50 "
        "  FROM bank_transaction t WHERE t.administration_id = :a AND t.amount < 0",
        a=str(w.a),
    )
    nothing, _ = await _sweep(w, now=monday, sender=sender)
    assert nothing.sent == 0
    assert any("nothing missing" in d for d in nothing.details), nothing.details
    assert sender.sent == []
    preview = (
        await _call(w.firm, w.staff, "POST", PREVIEW, {"administration_ids": [str(w.a)]})
    ).json()
    assert preview["items"][0]["blocked_reason"] == "nothing_missing"


async def test_a_client_who_opted_out_of_reminders_is_not_mailed(w: World) -> None:
    """`users.reminder_emails` (0069) is the person's own opt-out; a chase respects it, and when it
    leaves nobody the preview says so."""
    async with app_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET reminder_emails = false WHERE id = :id"),
            {"id": w.tenants.owner_a},
        )
    try:
        sender = CollectingEmailSender()
        await _sweep(w, sender=sender)  # recounts: the Owner no longer qualifies
        preview = (
            await _call(w.firm, w.staff, "POST", PREVIEW, {"administration_ids": [str(w.a)]})
        ).json()
        assert preview["items"][0]["recipient_count"] == 0
        assert preview["items"][0]["blocked_reason"] == "no_recipient"

        sent = await _call(w.firm, w.staff, "POST", SEND, {"administration_ids": [str(w.a)]})
        assert sent.json()["skipped"] == [{"administration_id": str(w.a), "reason": "no_recipient"}]
        report, _ = await _sweep(w, sender=sender)
        assert report.sent == 0
        owner_email = await _email(w.tenants.owner_a)
        assert [m for m in sender.sent if m.to == owner_email] == []
    finally:
        async with app_engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET reminder_emails = true WHERE id = :id"),
                {"id": w.tenants.owner_a},
            )
