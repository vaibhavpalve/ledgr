"""Sending e-mail - the transport, and nothing above it.

CLAUDE.md's fourth architectural non-negotiable: a Protocol the domain depends
on, implementations selected by config, and nothing above this module knowing
whether the provider speaks SMTP or HTTP. The same shape `api.crypto.kms`,
`api.customers.vies` and `api.documents.scanning` take.

--- Why this is its own package rather than part of invoicing ---

FR-AR-005 is the first requirement that needs to send mail and it will not be
the last: FR-AR-010's dunning ladder, FR-RPT-010's scheduled reports and
FR-NTF-002's notification channel are all the same act with a different body.
Putting the transport under `api.invoicing` would mean the second of those
either imports from invoicing or writes its own SMTP client, and the second
client is where the TLS settings drift apart.

So this module knows about messages, attachments and providers. It does not
know what an invoice is.

--- "Accepted" is not "delivered", and this returns only the first ---

`send` returns when the provider has ACCEPTED the message. That is a real fact
and a limited one: acceptance means the message is queued at the provider, not
that it reached a mailbox. A bounce arrives minutes later, asynchronously, and
`api.invoicing.delivery` models it as a separate state for that reason.

Conflating them here would make every caller inherit the confusion, which is
how somebody ends up telling a customer "we sent it" about an invoice that
bounced an hour ago.

--- Failure is a returned outcome, not an exception ---

A provider outage is not a caller's mistake, and NFR-026 says queued work
resumes rather than failing the operation that scheduled it. So a send that
could not be handed over comes back as `EmailOutcome(accepted=False, ...)` and
the caller decides whether that is a retry or a refusal - the posture
`api.customers.vies` takes about VIES being down.

The one thing that DOES raise is a message this module considers unsendable
(no recipient, no subject): that is a programming error rather than an outage,
and returning it as a soft failure would put it in a retry queue forever.

--- Data residency ---

PRIV-010 and PRIV-011 put all customer data, and every sub-processor touching
it, inside the EU. An invoice e-mail carries a customer's name, address and the
amount they owe, so the provider configured here is a sub-processor handling
personal data and the choice is a compliance decision rather than an
operational one. `build_email_sender` names no default provider host for that
reason - there is nothing sensible to default to.
"""

from __future__ import annotations

import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, make_msgid
from typing import Protocol

__all__ = [
    "Attachment",
    "EmailMessage",
    "EmailOutcome",
    "EmailSender",
    "SmtpEmailSender",
    "CollectingEmailSender",
    "UnsendableMessage",
    "build_email_sender",
]

#: Deliberately loose. A full RFC 5322 address grammar accepts things no mail
#: provider will, and rejecting a legitimate address is worse than handing a
#: doubtful one to a provider that will bounce it visibly. This catches the
#: mistakes that are certainly mistakes: no `@`, whitespace, empty parts.
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UnsendableMessage(ValueError):
    """A message this module will not attempt to send.

    Distinct from a provider failure on purpose: this is a defect in the
    message, so it must NOT go into a retry queue - retrying a message with no
    recipient produces the same result forever while looking like an outage.
    """


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str
    content: bytes
    content_type: str = "application/pdf"

    def __post_init__(self) -> None:
        if "/" not in self.content_type:
            raise UnsendableMessage(
                f"{self.content_type!r} is not a MIME type; an attachment needs "
                f"one so the recipient's client knows what it received"
            )
        if not self.content:
            raise UnsendableMessage(f"attachment {self.filename!r} has no content")


@dataclass(frozen=True, slots=True)
class EmailMessage:
    to: str
    subject: str
    #: Plain text. No HTML alternative is built: an invoice e-mail's job is to
    #: say what is attached and from whom, and a plain-text body renders
    #: identically in every client, cannot leak a tracking pixel (PRIV-012),
    #: and has no rendering surface for the reader to be attacked through.
    body: str
    from_address: str
    from_name: str | None = None
    reply_to: str | None = None
    attachments: tuple[Attachment, ...] = ()
    #: RFC 5322 headers a provider echoes back on a bounce. Used by
    #: `api.invoicing.delivery` to match a webhook to a dispatch.
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for label, value in (("recipient", self.to), ("sender", self.from_address)):
            if not _ADDRESS.match(value.strip()):
                raise UnsendableMessage(f"{value!r} is not a usable {label} address")
        if not self.subject.strip():
            raise UnsendableMessage("an e-mail needs a subject")
        if not self.body.strip():
            raise UnsendableMessage("an e-mail needs a body")


@dataclass(frozen=True, slots=True)
class EmailOutcome:
    """What the provider said.

    `accepted` is the only claim this makes: the provider took the message. It
    is emphatically not a claim that anybody received it.
    """

    accepted: bool
    #: Which implementation answered, for the delivery record.
    provider: str
    #: The provider's own handle for this message - a Message-ID, or whatever
    #: an API returned. It is what a later bounce notification quotes, so a
    #: delivery with no reference cannot be reconciled against one.
    reference: str | None = None
    #: Operator-facing. Never rendered to a user (FR-UX-007).
    detail: str | None = None


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> EmailOutcome: ...


def build_mime(message: EmailMessage) -> tuple[MimeMessage, str]:
    """The MIME message and its Message-ID.

    Shared by every implementation rather than written per provider, because
    the parts that are easy to get wrong - the display-name encoding on a
    sender called "Bakker & Zonen", the attachment's maintype/subtype split -
    are the same wherever the bytes end up.
    """
    mime = MimeMessage()
    message_id = make_msgid()

    mime["Message-ID"] = message_id
    mime["From"] = (
        formataddr((message.from_name, message.from_address))
        if message.from_name
        else message.from_address
    )
    mime["To"] = message.to
    mime["Subject"] = message.subject
    if message.reply_to:
        mime["Reply-To"] = message.reply_to
    for key, value in message.headers.items():
        mime[key] = value

    # `set_content` picks the charset; a Dutch body with an é becomes UTF-8
    # quoted-printable rather than being mangled into ASCII.
    mime.set_content(message.body)

    for attachment in message.attachments:
        maintype, _, subtype = attachment.content_type.partition("/")
        mime.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )

    return mime, message_id


class CollectingEmailSender:
    """Dev and test only. Sends nothing and keeps what it was given.

    Named for what it is, so finding it in a production configuration is
    obviously wrong - the posture `api.documents.scanning.LocalPatternScanner`
    and `api.customers.vies.SyntaxOnlyViesValidator` take for the same reason.

    It reports `accepted=True`, which is the right answer for the state it
    models: the message was successfully handed to this "provider". A local
    stand-in that reported failure would put every development invoice into a
    retry queue.
    """

    name = "collecting"

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> EmailOutcome:
        # Built even though it is discarded, so a message that would fail to
        # assemble in production fails here too rather than in front of a
        # customer.
        _, message_id = build_mime(message)
        self.sent.append(message)
        return EmailOutcome(
            accepted=True,
            provider=self.name,
            reference=message_id,
            detail="not sent: this deployment is configured with the collecting sender",
        )


class SmtpEmailSender:
    """Production. SMTP over STARTTLS or implicit TLS.

    SMTP rather than a provider's HTTP API because every EU mail provider
    speaks it, which keeps PRIV-011's "choose an EU sub-processor" a
    configuration decision rather than a rewrite. A provider whose API offers
    something SMTP cannot express gets its own adapter beside this one.

    Blocking `smtplib` inside an async method, run directly rather than in a
    thread: this is called from a request that is already waiting on the send,
    the timeout is bounded, and a thread pool would add a failure mode
    (exhaustion under a mail outage) worse than the one it removes. If delivery
    moves to a background worker the call moves with it and this comment is the
    thing to revisit.
    """

    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = False,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        # `use_tls` is implicit TLS (port 465). False means STARTTLS, which is
        # still upgraded before authentication - see `send`. There is no
        # plaintext option: SEC-020 requires TLS for external traffic and an
        # invoice e-mail carries a customer's name and what they owe.
        self._use_tls = use_tls
        self._timeout = timeout_seconds

    async def send(self, message: EmailMessage) -> EmailOutcome:
        mime, message_id = build_mime(message)
        context = ssl.create_default_context()

        try:
            if self._use_tls:
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    self._host, self._port, timeout=self._timeout, context=context
                )
            else:
                client = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
            with client:
                if not self._use_tls:
                    # Refused rather than continued in plaintext. A server that
                    # cannot STARTTLS is one this message must not cross.
                    client.starttls(context=context)
                    client.ehlo()
                if self._username:
                    client.login(self._username, self._password or "")
                refused = client.send_message(mime)
        except (OSError, smtplib.SMTPException) as exc:
            # Every transport failure is an outage rather than a caller's
            # mistake: connection refused, TLS negotiation, auth, a 4xx from
            # the server. The caller queues a retry.
            return EmailOutcome(
                accepted=False,
                provider=self.name,
                detail=f"{type(exc).__name__}: {exc}",
            )

        if refused:
            # Per-recipient refusal with an otherwise successful conversation.
            # There is one recipient, so any entry here means it was rejected.
            return EmailOutcome(
                accepted=False,
                provider=self.name,
                reference=message_id,
                detail=f"recipient refused: {refused}",
            )

        return EmailOutcome(accepted=True, provider=self.name, reference=message_id)


def build_email_sender(
    provider: str,
    *,
    host: str | None = None,
    port: int = 587,
    username: str | None = None,
    password: str | None = None,
    use_tls: bool = False,
) -> EmailSender:
    """Selects the sender from configuration.

    No default host. PRIV-010 and PRIV-011 make the provider an EU
    sub-processor decision, and a default would be a decision made by whoever
    wrote this line rather than by whoever is accountable for it.
    """
    if provider == "collecting":
        return CollectingEmailSender()
    if provider == "smtp":
        if not host:
            raise ValueError(
                "the SMTP sender needs a host. PRIV-010/PRIV-011 make the mail "
                "provider an EU sub-processor decision, so there is no default "
                "worth having here - set EMAIL_SMTP_HOST."
            )
        return SmtpEmailSender(
            host=host,
            port=port,
            username=username,
            password=password,
            use_tls=use_tls,
        )
    raise ValueError(
        f"unknown e-mail provider {provider!r}. 'collecting' is dev and test only "
        f"and sends nothing; a production deployment needs 'smtp'."
    )
