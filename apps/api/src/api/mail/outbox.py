"""The process-wide e-mail sender, and the development outbox behind it.

`api.mail.sender.build_email_sender` returns a fresh sender each call, which
is fine for SMTP (stateless) and useless for `CollectingEmailSender`, whose
whole value is the list of messages it kept. A verification link that is
"sent" into a sender nobody holds a reference to is gone. This module holds
that reference once, for the whole process, so an e-mail the signup route
collected can be read back by GET /v1/dev/outbox (api.mail.dev_outbox) in
local development, and by a test.

Only the collecting provider is shared; an SMTP sender is still built per
call from settings, exactly as before.
"""

from __future__ import annotations

from api.config import settings
from api.mail.sender import CollectingEmailSender, EmailMessage, EmailSender, build_email_sender

_collecting: CollectingEmailSender | None = None


def get_email_sender() -> EmailSender:
    global _collecting
    if settings.email_provider == "collecting":
        if _collecting is None:
            _collecting = CollectingEmailSender()
        return _collecting
    return build_email_sender(
        settings.email_provider,
        host=settings.email_smtp_host,
        port=settings.email_smtp_port,
        username=settings.email_smtp_username,
        password=settings.email_smtp_password,
        use_tls=settings.email_smtp_use_tls,
    )


def collected_messages() -> list[EmailMessage]:
    """Everything the collecting sender has been handed since the process
    started - empty when the provider is not `collecting`, because then
    nothing was collected.
    """
    return list(_collecting.sent) if _collecting is not None else []


def clear_collected_messages() -> None:
    """Test hygiene: one test's verification mail must not be the next
    test's fixture.
    """
    if _collecting is not None:
        _collecting.sent.clear()
