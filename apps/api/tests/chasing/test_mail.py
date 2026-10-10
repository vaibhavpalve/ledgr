"""The chase e-mail says how many, never which (ADR-114, FR-NTF-004)."""

from __future__ import annotations

import uuid

import pytest

from api.chasing.mail import build_message, receipts_needed_link
from api.config import settings
from api.i18n.language import Language

ADMIN = uuid.UUID("11111111-2222-3333-4444-555555555555")


@pytest.mark.parametrize("language", [Language.NL, Language.EN])
def test_the_mail_counts_and_links_in_both_languages(language: Language) -> None:
    message = build_message(
        recipient_email="owner@example.com",
        language=language,
        administration_id=ADMIN,
        administration_name="Bakkerij Jansen",
        missing_count=3,
    )
    assert "Bakkerij Jansen" in message.subject
    assert "3" in message.body
    link = receipts_needed_link(ADMIN)
    assert link == f"{settings.app_base_url.rstrip('/')}/receipts-needed?administration={ADMIN}"
    assert link in message.body.splitlines()


def test_one_payment_reads_in_the_singular() -> None:
    nl = build_message(
        recipient_email="o@example.com",
        language=Language.NL,
        administration_id=ADMIN,
        administration_name="X",
        missing_count=1,
    )
    en = build_message(
        recipient_email="o@example.com",
        language=Language.EN,
        administration_id=ADMIN,
        administration_name="X",
        missing_count=1,
    )
    assert "1 betaling " in nl.body
    assert "1 bank payment " in en.body


def test_the_body_carries_no_amount_or_currency() -> None:
    """The message is built from a count and a name only - there is no parameter an amount or a
    counterparty could arrive through. This guards the template text itself."""
    for language in (Language.NL, Language.EN):
        body = build_message(
            recipient_email="o@example.com",
            language=language,
            administration_id=ADMIN,
            administration_name="X",
            missing_count=12,
        ).body
        assert "€" not in body
        assert "EUR" not in body
        assert not any(ch.isdigit() for ch in body.replace("12", "").split("http")[0])
