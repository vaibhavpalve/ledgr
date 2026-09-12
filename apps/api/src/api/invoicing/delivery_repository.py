"""SQL for invoice delivery - migration 0041.

Thin on purpose, the same bargain `api.invoicing.posting_repository` makes with
0040: the rules with consequences are in the database (only an issued invoice
is deliverable, a settled dispatch is history, tenant coherence) or in a pure
module, so this file is statements and row mapping.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.i18n.language import Language
from api.invoicing.delivery import DeliveryChannel, DeliveryStatus, Recipient
from api.invoicing.delivery_service import DeliveryRecord
from api.invoicing.model import SalesInvoice
from api.invoicing.repository import SqlInvoiceRepository

_COLUMNS = """
    id, invoice_id, channel, status, recipient, language, attempts,
    document_id, provider, provider_reference, last_error,
    requested_at, sent_at, settled_at, next_attempt_at
"""


def _record(row: Any) -> DeliveryRecord:
    return DeliveryRecord(
        id=row.id,
        invoice_id=row.invoice_id,
        channel=DeliveryChannel(row.channel),
        status=DeliveryStatus(row.status),
        recipient=row.recipient,
        language=Language(row.language),
        attempts=int(row.attempts),
        document_id=row.document_id,
        provider=row.provider,
        provider_reference=row.provider_reference,
        last_error=row.last_error,
        requested_at=row.requested_at,
        sent_at=row.sent_at,
        settled_at=row.settled_at,
        next_attempt_at=row.next_attempt_at,
    )


class SqlDeliveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        # Reused rather than reimplemented: one definition of how a
        # `sales_invoice` row becomes a `SalesInvoice`, so a column added to
        # 0037's snapshot block reaches delivery without a second mapping to
        # remember.
        self._invoices = SqlInvoiceRepository(session)

    async def invoice(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> SalesInvoice | None:
        return await self._invoices.get(administration_id=administration_id, invoice_id=invoice_id)

    async def recipient_for(
        self, *, administration_id: uuid.UUID, invoice: SalesInvoice
    ) -> Recipient:
        """Every way this customer can be addressed.

        The NAME and the LANGUAGE come from the invoice's own frozen snapshot,
        because those are what the document was made out to and written in
        (0037, 0039). The ADDRESSES come from the customer master, because
        those are where they read mail today - a customer who moved mailbox
        last month should get this invoice at the new one.

        A one-off customer (`customer_id is null`) has no master row, so the
        addresses come back empty and the caller supplies one or is refused.
        """
        recipient = Recipient(
            name=invoice.customer_name,
            language=invoice.customer_language,
        )
        if invoice.customer_id is None:
            return recipient

        result = await self._session.execute(
            text(
                "SELECT invoice_email, peppol_participant_id, address_line1, "
                "       address_line2, postal_code, city, country "
                "  FROM customer WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(invoice.customer_id), "admin": str(administration_id)},
        )
        row = result.first()
        if row is None:
            return recipient

        return Recipient(
            name=invoice.customer_name,
            language=invoice.customer_language,
            email=row.invoice_email,
            peppol_participant_id=row.peppol_participant_id,
            # The invoice's own snapshot, not the master's: a posted letter
            # goes to the address the document carries.
            postal_address=invoice.customer_address,
        )

    async def supplier_contact(self, *, administration_id: uuid.UUID) -> tuple[str, str | None]:
        """The name a customer sees in the From line, and where replies go.

        `email` on `administration` does not exist, so the reply-to is None
        today - recorded in ADR-040 rather than papered over with a mailbox
        nobody reads.
        """
        result = await self._session.execute(
            text("SELECT legal_name FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return (row.legal_name if row is not None else "", None)

    async def preferred_channel(
        self, *, administration_id: uuid.UUID, invoice: SalesInvoice
    ) -> DeliveryChannel | None:
        """FR-AR-006's stated preference, or None for a one-off customer."""
        if invoice.customer_id is None:
            return None

        result = await self._session.execute(
            text(
                "SELECT delivery_channel FROM customer "
                " WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(invoice.customer_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else DeliveryChannel(row.delivery_channel)

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        invoice_id: uuid.UUID,
        channel: DeliveryChannel,
        recipient: str,
        language: Language,
        document_id: uuid.UUID | None,
        user_id: uuid.UUID,
    ) -> DeliveryRecord:
        result = await self._session.execute(
            text(
                f"""
                INSERT INTO invoice_delivery (
                    organization_id, administration_id, invoice_id, channel,
                    recipient, language, document_id, requested_by_user_id
                ) VALUES (
                    -- Overwritten by invoice_delivery_needs_an_issued_invoice();
                    -- the placeholder exists only because the column is NOT NULL.
                    :org, :admin, :invoice, :channel, :recipient, :language,
                    :document, :user
                )
                RETURNING {_COLUMNS}
                """
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "invoice": str(invoice_id),
                "channel": channel.value,
                "recipient": recipient,
                "language": language.value,
                "document": str(document_id) if document_id else None,
                "user": str(user_id),
            },
        )
        return _record(result.one())

    async def record_outcome(
        self,
        *,
        administration_id: uuid.UUID,
        delivery_id: uuid.UUID,
        status: DeliveryStatus,
        provider: str | None,
        provider_reference: str | None,
        last_error: str | None,
        next_attempt_at: datetime | None,
    ) -> DeliveryRecord:
        """The transition, in one statement.

        `attempts` increments here rather than at hand-over, so it counts what
        was actually tried. The three timestamps are set from the status by
        CASE rather than by the caller, because 0041 constrains them together -
        `invoice_delivery_sent_is_dated` and `..._settled_is_dated` would both
        be violated for the instant between two statements.
        """
        result = await self._session.execute(
            text(
                f"""
                UPDATE invoice_delivery SET
                    status = :status,
                    attempts = attempts + 1,
                    provider = :provider,
                    provider_reference = coalesce(:reference, provider_reference),
                    last_error = :last_error,
                    last_attempt_at = now(),
                    sent_at = CASE
                        WHEN :status IN ('sent', 'delivered')
                            THEN coalesce(sent_at, now())
                        ELSE sent_at
                    END,
                    settled_at = CASE
                        WHEN :status IN ('delivered', 'bounced', 'failed')
                            THEN coalesce(settled_at, now())
                        ELSE settled_at
                    END,
                    next_attempt_at = :next_attempt_at
                 WHERE id = :id AND administration_id = :admin
                RETURNING {_COLUMNS}
                """
            ),
            {
                "status": status.value,
                "provider": provider,
                "reference": provider_reference,
                "last_error": last_error,
                "next_attempt_at": next_attempt_at,
                "id": str(delivery_id),
                "admin": str(administration_id),
            },
        )
        return _record(result.one())

    async def state_of(
        self, *, administration_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> Sequence[DeliveryRecord]:
        """FR-AR-005's per-channel status, from 0041's own function.

        The function rather than a query assembled here, so a screen, a report
        and this code all read the same definition of "the current state of a
        channel" - which is the latest dispatch, and is easy to get subtly
        different when written out twice.
        """
        result = await self._session.execute(
            text(
                "SELECT channel, status, recipient, language, attempts, provider, "
                "       provider_reference, requested_at, sent_at, settled_at, "
                "       last_error "
                "  FROM invoicing.delivery_state_of(:invoice)"
            ),
            {"invoice": str(invoice_id)},
        )
        return [
            DeliveryRecord(
                # The function answers per channel rather than per row, so
                # there is no dispatch id to return - the id belongs to a
                # dispatch and this is a state.
                id=uuid.UUID(int=0),
                invoice_id=invoice_id,
                channel=DeliveryChannel(row.channel),
                status=DeliveryStatus(row.status),
                recipient=row.recipient,
                language=Language(row.language),
                attempts=int(row.attempts),
                provider=row.provider,
                provider_reference=row.provider_reference,
                last_error=row.last_error,
                requested_at=row.requested_at,
                sent_at=row.sent_at,
                settled_at=row.settled_at,
            )
            for row in result
        ]

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id
