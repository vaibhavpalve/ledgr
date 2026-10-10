"""The receipt-chasing e-mail (ADR-114).

What it says: how many bank payments still need a receipt, for which administration, and a link
to the client's own list in the app. What it never says: an amount, a counterparty, a description
or anything from a document - the mail leaves the EU-hosted product for whatever mailbox the person
reads (FR-NTF-004's rule for push, applied to e-mail as ADR-111 does; PRIV-012's plain text, no
tracking). The list behind the link is behind sign-in and authorization like every other screen.
"""

from __future__ import annotations

import uuid

from api.config import settings
from api.i18n.catalogue import translate
from api.i18n.language import Language
from api.mail.sender import EmailMessage

#: The client's in-app list (frontend-client's `/receipts-needed`).
RECEIPTS_NEEDED_PATH = "/receipts-needed"


def receipts_needed_link(administration_id: uuid.UUID) -> str:
    """The list for THIS administration: the web app switches to it first if the person has it
    and it is not their active one (a client user with several administrations)."""
    return (
        f"{settings.app_base_url.rstrip('/')}{RECEIPTS_NEEDED_PATH}"
        f"?administration={administration_id}"
    )


def build_message(
    *,
    recipient_email: str,
    language: Language,
    administration_id: uuid.UUID,
    administration_name: str,
    missing_count: int,
) -> EmailMessage:
    product = settings.webauthn_rp_name
    body = "\n".join(
        [
            translate("reminders.greeting", language),
            "",
            translate(
                "reminders.chase.body",
                language,
                count=missing_count,
                name=administration_name,
            ),
            "",
            receipts_needed_link(administration_id),
            "",
            translate("reminders.chase.why", language, product=product),
            "",
            translate("reminders.signoff", language),
            product,
        ]
    )
    return EmailMessage(
        to=recipient_email,
        subject=translate("reminders.chase.subject", language, name=administration_name),
        body=body,
        from_address=settings.email_from_address,
        from_name=product,
    )
