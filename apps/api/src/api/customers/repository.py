"""SQL for the customer master - migration 0039.

Thin on purpose, the same bargain `api.invoicing.repository` makes with 0037:
every rule with a consequence lives either in the database (tenant coherence,
the verdict/date constraint, `updated_at`, the absence of a DELETE grant) or in
a pure module (`vat_number`, `address`, `vies`), so this file is statements and
row mapping.

Two things here are worth a reader's attention:

  * No query names `organization_id`. RLS (ADR-003) scopes every statement to
    the caller's tenant, and `administration_id` is named because a request is
    about ONE administration, not because it is the isolation mechanism.
  * `search` sends the query string to `pg_trgm` rather than building a LIKE.
    See its docstring.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.customers.address import PostalAddress
from api.customers.model import Customer, DeliveryChannel, RetainedInvoiceRef
from api.customers.service import CustomerDetails
from api.customers.vies import ViesResult, ViesStatus
from api.firm.switcher import is_kvk_query
from api.i18n.language import Language

_CUSTOMER_COLUMNS = """
    id, organization_id, administration_id, name, trade_name,
    address_line1, address_line2, postal_code, city, country,
    kvk_number, vat_number, vat_number_status, vat_number_checked_at,
    vat_number_checked_name, vat_number_consultation_number,
    peppol_participant_id, peppol_checked_at,
    payment_terms_days, credit_limit, delivery_channel, invoice_email,
    language, notes, archived_at, erased_at, created_at, updated_at
"""


def _customer(row: Any) -> Customer:
    return Customer(
        id=row.id,
        organization_id=row.organization_id,
        administration_id=row.administration_id,
        name=row.name,
        trade_name=row.trade_name,
        address=PostalAddress(
            address_line1=row.address_line1,
            address_line2=row.address_line2,
            postal_code=row.postal_code,
            city=row.city,
            country=row.country,
        ),
        kvk_number=row.kvk_number,
        vat_number=row.vat_number,
        vat_number_status=ViesStatus(row.vat_number_status),
        vat_number_checked_at=row.vat_number_checked_at,
        vat_number_checked_name=row.vat_number_checked_name,
        vat_number_consultation_number=row.vat_number_consultation_number,
        peppol_participant_id=row.peppol_participant_id,
        peppol_checked_at=row.peppol_checked_at,
        payment_terms_days=int(row.payment_terms_days),
        # numeric(19,2) arrives as Decimal from asyncpg. Left exactly as it
        # came (NFR-031) - a float() here would be the whole rule undone in one
        # cast.
        credit_limit=row.credit_limit,
        delivery_channel=DeliveryChannel(row.delivery_channel),
        invoice_email=row.invoice_email,
        language=Language(row.language),
        notes=row.notes,
        archived_at=row.archived_at,
        erased_at=row.erased_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _details_params(details: CustomerDetails) -> dict[str, Any]:
    """One mapping, used by INSERT and UPDATE alike.

    Written once because the two statements must accept exactly the same set of
    fields: a column settable at create and not at update is a value a person
    can enter and then never correct, which is the sort of asymmetry nobody
    notices until they need to fix a typo.
    """
    return {
        "name": details.name,
        "trade_name": details.trade_name,
        "address_line1": details.address_line1,
        "address_line2": details.address_line2,
        "postal_code": details.postal_code,
        "city": details.city,
        "country": details.country,
        "kvk_number": details.kvk_number,
        "vat_number": details.vat_number,
        "peppol_participant_id": details.peppol_participant_id,
        "payment_terms_days": details.payment_terms_days,
        "credit_limit": details.credit_limit,
        "delivery_channel": details.delivery_channel.value,
        "invoice_email": details.invoice_email,
        "language": details.language.value,
        "notes": details.notes,
    }


class SqlCustomerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        details: CustomerDetails,
        user_id: uuid.UUID,
    ) -> Customer:
        result = await self._session.execute(
            text(
                f"""
                INSERT INTO customer (
                    organization_id, administration_id, name, trade_name,
                    address_line1, address_line2, postal_code, city, country,
                    kvk_number, vat_number, peppol_participant_id,
                    payment_terms_days, credit_limit, delivery_channel,
                    invoice_email, language, notes, created_by_user_id
                ) VALUES (
                    :org, :admin, :name, :trade_name,
                    :address_line1, :address_line2, :postal_code, :city, :country,
                    :kvk_number, :vat_number, :peppol_participant_id,
                    :payment_terms_days, :credit_limit, :delivery_channel,
                    :invoice_email, :language, :notes, :user
                )
                RETURNING {_CUSTOMER_COLUMNS}
                """
            ),
            {
                "org": str(organization_id),
                "admin": str(administration_id),
                "user": str(user_id),
                **_details_params(details),
            },
        )
        return _customer(result.one())

    async def get(self, *, administration_id: uuid.UUID, customer_id: uuid.UUID) -> Customer | None:
        result = await self._session.execute(
            text(
                f"SELECT {_CUSTOMER_COLUMNS} FROM customer "
                "WHERE id = :id AND administration_id = :admin"
            ),
            {"id": str(customer_id), "admin": str(administration_id)},
        )
        row = result.first()
        return None if row is None else _customer(row)

    async def update(
        self,
        *,
        administration_id: uuid.UUID,
        customer_id: uuid.UUID,
        details: CustomerDetails,
    ) -> Customer:
        """A whole-record replace, not a patch.

        The caller edits a customer on one form and sends the form back, so a
        field absent from the payload means "cleared", not "unchanged" - the
        same choice `api.invoicing.repository.replace_lines` makes and for the
        same reason: a partial update has to decide what an omitted field
        means, and both answers are surprising to somebody.

        The VIES columns are deliberately NOT touched here. They are written
        only by `record_vies_result`, so no ordinary edit can assert a
        consultation that did not happen.
        """
        result = await self._session.execute(
            text(
                f"""
                UPDATE customer SET
                    name = :name,
                    trade_name = :trade_name,
                    address_line1 = :address_line1,
                    address_line2 = :address_line2,
                    postal_code = :postal_code,
                    city = :city,
                    country = :country,
                    kvk_number = :kvk_number,
                    vat_number = :vat_number,
                    peppol_participant_id = :peppol_participant_id,
                    payment_terms_days = :payment_terms_days,
                    credit_limit = :credit_limit,
                    delivery_channel = :delivery_channel,
                    invoice_email = :invoice_email,
                    language = :language,
                    notes = :notes,
                    -- 0039's customer_vat_verdict_needs_a_number: clearing the
                    -- number has to clear the verdict with it, or the row
                    -- would carry an answer about nothing. Done in the same
                    -- statement rather than a second one, so there is no
                    -- instant at which the constraint is violated.
                    vat_number_status = CASE
                        WHEN :vat_number IS NULL THEN 'unchecked'
                        WHEN vat_number IS DISTINCT FROM :vat_number THEN 'unchecked'
                        ELSE vat_number_status
                    END,
                    vat_number_checked_at = CASE
                        WHEN :vat_number IS NULL THEN NULL
                        WHEN vat_number IS DISTINCT FROM :vat_number THEN NULL
                        ELSE vat_number_checked_at
                    END,
                    vat_number_checked_name = CASE
                        WHEN :vat_number IS NULL THEN NULL
                        WHEN vat_number IS DISTINCT FROM :vat_number THEN NULL
                        ELSE vat_number_checked_name
                    END,
                    vat_number_consultation_number = CASE
                        WHEN :vat_number IS NULL THEN NULL
                        WHEN vat_number IS DISTINCT FROM :vat_number THEN NULL
                        ELSE vat_number_consultation_number
                    END
                 WHERE id = :id AND administration_id = :admin
                RETURNING {_CUSTOMER_COLUMNS}
                """
            ),
            {
                "id": str(customer_id),
                "admin": str(administration_id),
                **_details_params(details),
            },
        )
        return _customer(result.one())

    async def search(
        self,
        *,
        administration_id: uuid.UUID,
        query: str | None,
        include_archived: bool,
        limit: int,
    ) -> Sequence[Customer]:
        """FR-FRM-000's shape and ordering, applied to customers.

        Exact, then prefix, then substring - so the first result is what
        somebody typing a specific name is reaching for, which is what makes
        type-then-Enter work. Deliberately the same ranking
        `api.firm.switcher_repository` uses: a customer picker that ordered
        differently from the client switcher would be two behaviours to learn
        for one gesture.

        `is_kvk_query` decides which column an all-digit query addresses, and
        it is imported rather than reimplemented - "eight digits means a KvK
        number" is one judgment, and two copies of it would eventually
        disagree.

        The wildcards are built in Python and bound as parameters, never
        concatenated into the SQL: the switcher's own shape, and it keeps `%`
        out of a statement string where it is easy to mistake for a format
        specifier. 0039's trigram GIN indexes serve the `ILIKE '%x%'` form
        directly, so this filters in the database rather than transferring
        every customer to filter here.
        """
        params: dict[str, Any] = {
            "admin": str(administration_id),
            "include_archived": include_archived,
            "limit": limit,
        }
        filters = "AND (:include_archived OR archived_at IS NULL)"
        order = "ORDER BY name"

        if query is not None and query.strip():
            query = query.strip()
            if is_kvk_query(query):
                filters += "\n AND kvk_number LIKE :prefix"
                params["prefix"] = f"{query}%"
            else:
                filters += "\n AND (name ILIKE :contains OR trade_name ILIKE :contains)"
                params["contains"] = f"%{query}%"
            params["exact"] = query.lower()
            params["starts"] = f"{query.lower()}%"
            order = """
                ORDER BY
                    CASE
                        WHEN lower(name) = :exact THEN 0
                        WHEN lower(coalesce(trade_name, '')) = :exact THEN 0
                        WHEN lower(name) LIKE :starts THEN 1
                        WHEN lower(coalesce(trade_name, '')) LIKE :starts THEN 1
                        ELSE 2
                    END,
                    name
            """

        result = await self._session.execute(
            text(
                f"""
                SELECT {_CUSTOMER_COLUMNS}
                  FROM customer
                 WHERE administration_id = :admin
                   {filters}
                {order}
                 LIMIT :limit
                """
            ),
            params,
        )
        return [_customer(row) for row in result]

    async def set_archived(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID, archived: bool
    ) -> Customer:
        """0039 grants no DELETE, so this is the only way a customer leaves a
        picker. Idempotent: archiving an archived customer keeps the ORIGINAL
        timestamp, because when trading stopped is a fact and re-archiving is
        not a new one.
        """
        result = await self._session.execute(
            text(
                f"""
                UPDATE customer
                   SET archived_at = CASE
                        WHEN :archived THEN coalesce(archived_at, now())
                        ELSE NULL
                   END
                 WHERE id = :id AND administration_id = :admin
                RETURNING {_CUSTOMER_COLUMNS}
                """
            ),
            {"id": str(customer_id), "admin": str(administration_id), "archived": archived},
        )
        return _customer(result.one())

    async def record_vies_result(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID, result: ViesResult
    ) -> Customer:
        """The only writer of the four VIES columns.

        `checked_at` comes from the ViesResult rather than from `now()`: it is
        the moment the CONSULTATION happened, which is the evidence an
        intra-Community supply rests on, and a database clock reading taken
        when the row was written would be a different fact wearing the same
        name.

        The name and consultation number are cleared on a verdict that carries
        neither, so a later UNAVAILABLE cannot leave a stale company name
        sitting beside it looking like corroboration.
        """
        row = await self._session.execute(
            text(
                f"""
                UPDATE customer SET
                    vat_number_status = :status,
                    vat_number_checked_at = :checked_at,
                    vat_number_checked_name = :name,
                    vat_number_consultation_number = :consultation_number
                 WHERE id = :id AND administration_id = :admin
                RETURNING {_CUSTOMER_COLUMNS}
                """
            ),
            {
                "id": str(customer_id),
                "admin": str(administration_id),
                "status": result.status.value,
                "checked_at": result.checked_at,
                "name": result.name,
                "consultation_number": result.consultation_number,
            },
        )
        return _customer(row.one())

    async def record_peppol_id(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID, participant_id: str | None
    ) -> Customer:
        """Written only after a lookup that actually found something, or by an
        explicit edit. `peppol_checked_at` is set either way - it records that
        somebody looked, which is what makes "no id" mean something.
        """
        row = await self._session.execute(
            text(
                f"""
                UPDATE customer
                   SET peppol_participant_id = :participant_id,
                       peppol_checked_at = now()
                 WHERE id = :id AND administration_id = :admin
                RETURNING {_CUSTOMER_COLUMNS}
                """
            ),
            {
                "id": str(customer_id),
                "admin": str(administration_id),
                "participant_id": participant_id,
            },
        )
        return _customer(row.one())

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        result = await self._session.execute(
            text("SELECT organization_id FROM administration WHERE id = :id"),
            {"id": str(administration_id)},
        )
        row = result.first()
        return None if row is None else row.organization_id

    async def erase(self, *, administration_id: uuid.UUID, customer_id: uuid.UUID) -> Customer:
        """PRIV-022. 0039 grants no DELETE on `customer` - see `set_archived`
        - so erasure is field-level: the identifying columns are overwritten
        and `erased_at` is set. `customer_erasure_is_frozen_trg` (0044) is
        what makes this the only time they can change: a second call is a
        no-op (`coalesce(erased_at, now())` keeps the original timestamp) and
        the trigger refuses any OTHER caller from reinstating a value this
        statement clears.
        """
        placeholder = f"Erased customer {customer_id}"
        result = await self._session.execute(
            text(
                f"""
                UPDATE customer SET
                    name = :placeholder,
                    trade_name = NULL,
                    address_line1 = NULL,
                    address_line2 = NULL,
                    postal_code = NULL,
                    city = NULL,
                    kvk_number = NULL,
                    vat_number = NULL,
                    vat_number_status = 'unchecked',
                    vat_number_checked_at = NULL,
                    vat_number_checked_name = NULL,
                    vat_number_consultation_number = NULL,
                    peppol_participant_id = NULL,
                    peppol_checked_at = NULL,
                    invoice_email = NULL,
                    notes = NULL,
                    erased_at = coalesce(erased_at, now())
                 WHERE id = :id AND administration_id = :admin
                RETURNING {_CUSTOMER_COLUMNS}
                """
            ),
            {"id": str(customer_id), "admin": str(administration_id), "placeholder": placeholder},
        )
        return _customer(result.one())

    async def retained_invoices(
        self, *, administration_id: uuid.UUID, customer_id: uuid.UUID, today: date
    ) -> Sequence[RetainedInvoiceRef]:
        """Issued invoices to this customer whose fiscal retention has not yet
        elapsed. `documents.retention_until` (0031) is reused rather than
        reimplemented - CMP-001 gives an invoice the same seven-years-from-
        fiscal-year-end rule FR-DOC-002 gives a document, and 0037 names no
        immovable-property case for an invoice, so the basis is always
        `standard`.
        """
        result = await self._session.execute(
            text(
                """
                SELECT i.id, i.invoice_reference, i.invoice_date,
                       documents.retention_until(f.end_date, 'standard') AS retained_until
                  FROM sales_invoice i
                  JOIN fiscal_year f ON f.id = i.fiscal_year_id
                 WHERE i.administration_id = :admin
                   AND i.customer_id = :customer_id
                   AND i.status = 'issued'
                   AND documents.retention_until(f.end_date, 'standard') >= :today
                 ORDER BY i.invoice_date
                """
            ),
            {"admin": str(administration_id), "customer_id": str(customer_id), "today": today},
        )
        return [
            RetainedInvoiceRef(
                invoice_id=row.id,
                invoice_reference=row.invoice_reference,
                invoice_date=row.invoice_date,
                retained_until=row.retained_until,
            )
            for row in result
        ]

    async def record_erasure_request(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        resource_id: uuid.UUID,
        requested_by_user_id: uuid.UUID,
        decision: str,
        explanation: str,
        retained_until: date | None,
    ) -> None:
        await self._session.execute(
            text(
                """
                INSERT INTO data_subject_erasure_request (
                    organization_id, administration_id, resource_type, resource_id,
                    requested_by_user_id, decision, explanation, retained_until
                ) VALUES (
                    :organization_id, :administration_id, 'customer', :resource_id,
                    :requested_by_user_id, :decision, :explanation, :retained_until
                )
                """
            ),
            {
                "organization_id": str(organization_id),
                "administration_id": str(administration_id),
                "resource_id": str(resource_id),
                "requested_by_user_id": str(requested_by_user_id),
                "decision": decision,
                "explanation": explanation,
                "retained_until": retained_until,
            },
        )
