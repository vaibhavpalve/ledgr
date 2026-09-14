"""IAM-010b without a database: the verification mail (both languages, from
the catalogue), the issue/verify state machine over in-memory doubles, and
the ledger-posting gate as a FastAPI dependency in the style of
tests/test_ledger_posting_eligibility.py. The HTTP round trip - signup,
link in the dev outbox, POST /v1/auth/verify-email, GET /v1/me - is
tests/integration/test_account_security.py's job against a real Postgres.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.auth.ceremony import EMAIL_VERIFICATION_TTL
from api.auth.email_verification import (
    EmailVerificationChecker,
    EmailVerificationService,
    VerificationLinkInvalidError,
    build_verification_message,
    get_email_verification_checker,
    require_verified_email,
    verification_link,
)
from api.config import settings
from api.i18n.language import Language
from api.mail.sender import CollectingEmailSender
from api.tenancy import TenantContext, get_tenant_context
from tests.support.fake_auth_repository import InMemoryUserRepository
from tests.support.fake_ceremony_repository import InMemoryCeremonyRepository

# ---------------------------------------------------------------------------
# The mail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "subject_fragment", "body_fragment"),
    [
        (Language.NL, "Bevestig uw e-mailadres", "Bedankt voor het aanmaken"),
        (Language.EN, "Confirm your email address", "Thank you for creating"),
    ],
)
def test_the_verification_mail_exists_in_both_languages_and_carries_the_link(
    language: Language, subject_fragment: str, body_fragment: str
) -> None:
    """FR-LOC-001: every e-mail exists in both languages. And the one thing
    the mail is for - the link - is in it, pointing at the web app's
    verify-email page with the token as its query parameter.
    """
    token = uuid.uuid4()

    message = build_verification_message(to="a@b.nl", token=token, language=language)

    assert subject_fragment in message.subject
    assert body_fragment in message.body
    assert verification_link(token) in message.body
    assert verification_link(token) == f"{settings.app_base_url}/verify-email?token={token}"
    assert str(int(EMAIL_VERIFICATION_TTL.total_seconds() // 3600)) in message.body
    assert message.to == "a@b.nl"
    assert message.from_address == settings.email_from_address


# ---------------------------------------------------------------------------
# Issue and verify
# ---------------------------------------------------------------------------


def _service(
    users: InMemoryUserRepository, ceremonies: InMemoryCeremonyRepository
) -> tuple[EmailVerificationService, CollectingEmailSender]:
    sender = CollectingEmailSender()
    return EmailVerificationService(users, ceremonies, sender), sender


async def test_issue_sends_one_mail_and_verify_stamps_the_user_once() -> None:
    users = InMemoryUserRepository()
    user = await users.create("owner@example.com")
    assert not user.email_verified
    service, sender = _service(users, InMemoryCeremonyRepository())

    issued = await service.issue(user_id=user.id, email=user.email, language=Language.NL)
    assert len(sender.sent) == 1
    assert issued.outcome.accepted is True
    assert verification_link(issued.token) in sender.sent[0].body

    verified_user_id = await service.verify(issued.token)

    assert verified_user_id == user.id
    stamped = await users.get_by_id(user.id)
    assert stamped is not None and stamped.email_verified


async def test_the_collecting_provider_puts_the_link_where_a_developer_will_see_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With `email_provider=collecting` no mail is sent anywhere, so this log
    line is the only copy of the link outside the dev outbox.

    It asserts the link is in the MESSAGE and the level is WARNING, because
    both were wrong and each alone made it invisible: an application logger at
    INFO is not emitted by uvicorn's default configuration, and a link passed
    in `extra` is not rendered by the default formatter. The result was a
    person waiting for an e-mail that was never sent, with nothing in the
    console to tell them so.
    """
    users = InMemoryUserRepository()
    user = await users.create("owner@example.com")
    service, _sender = _service(users, InMemoryCeremonyRepository())

    with caplog.at_level(logging.WARNING, logger="api.auth.email_verification"):
        issued = await service.issue(user_id=user.id, email=user.email, language=Language.NL)

    records = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(records) == 1, "exactly one line, so it cannot be lost in noise"
    rendered = records[0].getMessage()
    assert verification_link(issued.token) in rendered
    assert user.email in rendered
    # Says plainly that nothing was sent — the fact the person on the other
    # side of the screen is missing.
    assert "NO E-MAIL WAS SENT" in rendered


async def test_a_link_is_single_use() -> None:
    users = InMemoryUserRepository()
    user = await users.create("owner@example.com")
    service, _ = _service(users, InMemoryCeremonyRepository())
    issued = await service.issue(user_id=user.id, email=user.email, language=Language.EN)
    await service.verify(issued.token)

    with pytest.raises(VerificationLinkInvalidError):
        await service.verify(issued.token)

    # And the first click's effect is not undone by the refused second one.
    stamped = await users.get_by_id(user.id)
    assert stamped is not None and stamped.email_verified


async def test_a_resend_retires_the_earlier_link() -> None:
    """ "Request a new link" means the newest one works and an older one
    found in a forwarded mail does not.
    """
    users = InMemoryUserRepository()
    user = await users.create("owner@example.com")
    service, _ = _service(users, InMemoryCeremonyRepository())

    first = await service.issue(user_id=user.id, email=user.email, language=Language.EN)
    second = await service.issue(user_id=user.id, email=user.email, language=Language.EN)

    with pytest.raises(VerificationLinkInvalidError):
        await service.verify(first.token)
    assert await service.verify(second.token) == user.id


async def test_an_expired_link_is_refused() -> None:
    now = datetime(2026, 9, 14, tzinfo=UTC)
    clock = lambda: now  # noqa: E731 - a mutable closure is the point
    ceremonies = InMemoryCeremonyRepository(clock=lambda: clock())
    users = InMemoryUserRepository()
    user = await users.create("owner@example.com")
    service, _ = _service(users, ceremonies)
    issued = await service.issue(user_id=user.id, email=user.email, language=Language.EN)

    now = now + EMAIL_VERIFICATION_TTL + timedelta(seconds=1)

    with pytest.raises(VerificationLinkInvalidError):
        await service.verify(issued.token)


async def test_an_unknown_token_is_refused_the_same_way() -> None:
    service, _ = _service(InMemoryUserRepository(), InMemoryCeremonyRepository())

    with pytest.raises(VerificationLinkInvalidError):
        await service.verify(uuid.uuid4())


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _probe_app(*, users: InMemoryUserRepository, user_id: uuid.UUID) -> tuple[FastAPI, list[bool]]:
    handler_calls: list[bool] = []
    app = FastAPI()

    @app.post("/v1/ledger/postings")
    def post_entry(_: None = Depends(require_verified_email)) -> dict[str, str]:
        handler_calls.append(True)
        return {"status": "posted"}

    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        organization_id=uuid.uuid4(), user_id=user_id, session_id=uuid.uuid4()
    )
    app.dependency_overrides[get_email_verification_checker] = lambda: EmailVerificationChecker(
        users
    )
    return app, handler_calls


async def test_an_unverified_account_is_blocked_before_the_handler_runs() -> None:
    users = InMemoryUserRepository()
    user = await users.create("owner@example.com")
    app, handler_calls = _probe_app(users=users, user_id=user.id)

    response = TestClient(app).post("/v1/ledger/postings", headers={"Accept-Language": "en"})

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "email_not_verified"
    assert "Confirm your email address" in response.json()["detail"]["message"]
    assert handler_calls == []


async def test_a_verified_account_is_allowed_through() -> None:
    users = InMemoryUserRepository()
    user = await users.create("owner@example.com")
    await users.mark_email_verified(user.id, at=datetime.now(UTC))
    app, handler_calls = _probe_app(users=users, user_id=user.id)

    response = TestClient(app).post("/v1/ledger/postings")

    assert response.status_code == 200
    assert handler_calls == [True]


async def test_the_gate_is_evaluated_fresh_on_every_request() -> None:
    """Verification between two requests is seen by the second one - the
    same never-cached posture api.auth.account_continuity takes.
    """
    users = InMemoryUserRepository()
    user = await users.create("owner@example.com")
    app, handler_calls = _probe_app(users=users, user_id=user.id)
    client = TestClient(app)

    assert client.post("/v1/ledger/postings").status_code == 403
    await users.mark_email_verified(user.id, at=datetime.now(UTC))
    assert client.post("/v1/ledger/postings").status_code == 200
    assert handler_calls == [True]
