"""GET /v1/dev/outbox - what the collecting e-mail sender would have sent.

Development only, and structurally so: `register` adds the route only when
settings.expose_dev_outbox is true, which is never the default. With the
flag off the path does not exist - not "returns 404", not "returns empty" -
and the two exemption lists that name it (api.tenancy.EXEMPT_PATHS and
api.authz.dependencies.AUTHORIZATION_EXEMPT_PATHS) are built from the same
flag, so neither can outlive the route.

It is tenant-exempt because its consumer is a developer with curl or the
web app running locally, reading back the e-mail verification link that
IAM-010b would otherwise put in a mailbox this machine does not have. It
returns the collecting sender's own list and nothing else; with
email_provider=smtp there is nothing collected and it returns [].
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from api.config import settings
from api.mail.outbox import collected_messages

PATH = "/v1/dev/outbox"


def register(app: FastAPI) -> None:
    if not settings.expose_dev_outbox:
        return
    app.add_api_route(PATH, dev_outbox, methods=["GET"], name="dev_outbox")


async def dev_outbox() -> list[dict[str, Any]]:
    return [
        {
            "to": message.to,
            "from": message.from_address,
            "subject": message.subject,
            "body": message.body,
            "headers": dict(message.headers),
        }
        for message in collected_messages()
    ]
