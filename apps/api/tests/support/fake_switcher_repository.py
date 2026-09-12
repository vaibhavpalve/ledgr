"""In-memory SwitcherRepository double.

Mirrors the SQL's two search behaviours exactly, because they differ and the
difference is the point: names match by substring, KvK numbers by prefix. A
fake that matched both the same way would let a test pass on behaviour
production does not have.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from api.firm.switcher import ClientBadge, SwitcherEntry, initials_for, is_kvk_query


@dataclass
class _Client:
    administration_id: uuid.UUID
    legal_name: str
    colour_token: str
    role_name: str
    trade_name: str | None = None
    kvk_number: str | None = None
    vat_registered: bool = False
    expires_at: datetime | None = None


class InMemorySwitcherRepository:
    def __init__(self) -> None:
        self._granted: dict[uuid.UUID, list[_Client]] = {}

    def grant(
        self,
        *,
        user_id: uuid.UUID,
        administration_id: uuid.UUID,
        legal_name: str,
        colour_token: str,
        role_name: str = "Accountant",
        trade_name: str | None = None,
        kvk_number: str | None = None,
        vat_registered: bool = False,
        expires_at: datetime | None = None,
    ) -> None:
        self._granted.setdefault(user_id, []).append(
            _Client(
                administration_id=administration_id,
                legal_name=legal_name,
                colour_token=colour_token,
                role_name=role_name,
                trade_name=trade_name,
                kvk_number=kvk_number,
                vat_registered=vat_registered,
                expires_at=expires_at,
            )
        )

    async def granted_administrations(
        self, *, user_id: uuid.UUID, query: str | None, now: datetime
    ) -> Sequence[SwitcherEntry]:
        clients = [
            client
            for client in self._granted.get(user_id, [])
            if client.expires_at is None or client.expires_at > now
        ]

        if query is not None:
            if is_kvk_query(query):
                # Prefix, matching the text_pattern_ops index in 0018.
                clients = [c for c in clients if c.kvk_number and c.kvk_number.startswith(query)]
            else:
                needle = query.lower()
                clients = [
                    c
                    for c in clients
                    if needle in c.legal_name.lower()
                    or (c.trade_name and needle in c.trade_name.lower())
                ]
            clients.sort(key=lambda c: (_rank(c, query), (c.trade_name or c.legal_name)))
        else:
            clients.sort(key=lambda c: c.trade_name or c.legal_name)

        return [
            SwitcherEntry(
                badge=ClientBadge(
                    administration_id=c.administration_id,
                    name=c.legal_name,
                    trade_name=c.trade_name,
                    kvk_number=c.kvk_number,
                    colour_token=c.colour_token,
                    initials=initials_for(c.trade_name or c.legal_name),
                ),
                role_name=c.role_name,
                expires_at=c.expires_at,
                vat_registered=c.vat_registered,
            )
            for c in clients
        ]


def _rank(client: _Client, query: str) -> int:
    """Exact, then prefix, then substring - the CASE in the SQL's ORDER BY."""
    needle = query.lower()
    names = [client.legal_name.lower(), (client.trade_name or "").lower()]
    if any(name == needle for name in names):
        return 0
    if any(name.startswith(needle) for name in names):
        return 1
    return 2
