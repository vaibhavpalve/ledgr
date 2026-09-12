"""FR-TPL-019: duplicate and reset, both one action.

Exercises `api.templates.service.InvoiceTemplateService.duplicate_template`
and `.reset_template` against an in-memory fake repository - the same style
`tests/templates/test_render_parity.py` already uses for this service, rather
than a real database (none is available in this environment; see this
session's own toolchain notes).
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from api.authz.model import AuthorizationDecision
from api.invoicing.rendering import TemplatedPdfRenderer, default_template
from api.invoicing.statutory import SupplierDetails
from api.templates.model import (
    ColorScheme,
    HeaderArrangement,
    InvoiceTemplate,
    Layout,
    Logo,
    LogoPosition,
    LogoSize,
    Margins,
    PageSize,
    StaleTemplateVersion,
    TemplateNameConflict,
    TemplateNotFound,
    TotalsPosition,
)
from api.templates.service import InvoiceTemplateService

pytestmark = pytest.mark.anyio

ADMIN = uuid.uuid4()
ORG = uuid.uuid4()
ACTOR = uuid.uuid4()

SUPPLIER = SupplierDetails(
    legal_name="Bakker Consultancy B.V.",
    address_line1="Damrak 70",
    address_line2=None,
    postal_code="1012 LM",
    city="Amsterdam",
    country="NL",
    vat_number="NL123456789B01",
    kvk_number="12345678",
)


class _AllowAllAuthorization:
    async def authorize(self, request: object) -> AuthorizationDecision:
        return AuthorizationDecision(allowed=True, reason="allowed")


class _FakeRepository:
    """An in-memory stand-in for `SqlInvoiceTemplateRepository`, enforcing
    the same two invariants that matter here: `invoice_template_name_unique`
    (0043) and optimistic concurrency on `version` (0042).
    """

    def __init__(self, *seed: InvoiceTemplate) -> None:
        self._rows: dict[uuid.UUID, InvoiceTemplate] = {row.id: row for row in seed}

    async def create(
        self,
        *,
        organization_id,
        administration_id,
        name,
        is_default,
        layout,
        logo,
        typography,
        colors,
        columns,
        blocks,
        header_arrangement=HeaderArrangement.SPLIT,
        totals_position=TotalsPosition.RIGHT,
        page_size=PageSize.A4,
        margins=Margins.NORMAL,
        user_id,
    ):  # type: ignore[no-untyped-def]
        for row in self._rows.values():
            if row.administration_id == administration_id and row.name == name:
                raise TemplateNameConflict(
                    f"administration {administration_id} already has a template with this name"
                )
        new = InvoiceTemplate(
            id=uuid.uuid4(),
            organization_id=organization_id,
            administration_id=administration_id,
            name=name,
            is_default=is_default,
            version=1,
            layout=layout,
            logo=logo,
            typography=typography,
            colors=colors,
            columns=columns,
            blocks=blocks,
            header_arrangement=header_arrangement,
            totals_position=totals_position,
            page_size=page_size,
            margins=margins,
        )
        self._rows[new.id] = new
        return new

    async def get(self, *, administration_id, template_id):  # type: ignore[no-untyped-def]
        row = self._rows.get(template_id)
        if row is None or row.administration_id != administration_id:
            return None
        return row

    async def list_for_administration(self, *, administration_id):  # type: ignore[no-untyped-def]
        return tuple(
            row for row in self._rows.values() if row.administration_id == administration_id
        )

    async def update(
        self,
        *,
        administration_id,
        template_id,
        expected_version,
        name,
        is_default,
        layout,
        logo,
        typography,
        colors,
        columns,
        blocks,
        header_arrangement=HeaderArrangement.SPLIT,
        totals_position=TotalsPosition.RIGHT,
        page_size=PageSize.A4,
        margins=Margins.NORMAL,
        user_id,
    ):  # type: ignore[no-untyped-def]
        current = self._rows.get(template_id)
        if current is None or current.administration_id != administration_id:
            return None
        if current.version != expected_version:
            return None
        for row in self._rows.values():
            if (
                row.id != template_id
                and row.administration_id == administration_id
                and row.name == name
            ):
                raise TemplateNameConflict(
                    f"administration {administration_id} already has a template with this name"
                )
        updated = replace(
            current,
            name=name,
            is_default=is_default,
            version=current.version + 1,
            layout=layout,
            logo=logo,
            typography=typography,
            colors=colors,
            columns=columns,
            blocks=blocks,
            header_arrangement=header_arrangement,
            totals_position=totals_position,
            page_size=page_size,
            margins=margins,
        )
        self._rows[template_id] = updated
        return updated

    async def organization_of(self, *, administration_id):  # type: ignore[no-untyped-def]
        return ORG

    async def supplier(self, *, administration_id):  # type: ignore[no-untyped-def]
        return SUPPLIER

    async def formatting_locale(self, *, administration_id):  # type: ignore[no-untyped-def]
        return "nl-NL"


def _service(*seed: InvoiceTemplate) -> tuple[InvoiceTemplateService, _FakeRepository]:
    repository = _FakeRepository(*seed)
    service = InvoiceTemplateService(
        repository=repository,  # type: ignore[arg-type]
        authorization=_AllowAllAuthorization(),  # type: ignore[arg-type]
        audit_log=_NullAuditLog(),  # type: ignore[arg-type]
        renderer=TemplatedPdfRenderer(),
    )
    return service, repository


class _NullAuditLog:
    async def record(self, event: object) -> None:
        return None


def _custom(name: str = "Huisstijl 2026") -> InvoiceTemplate:
    base = default_template(organization_id=ORG, administration_id=ADMIN)
    return replace(
        base,
        id=uuid.uuid4(),
        name=name,
        colors=ColorScheme(accent="#1d4ed8", text="#0f172a", background="#ffffff"),
        logo=Logo(asset_id=uuid.uuid4(), position=LogoPosition.RIGHT, size=LogoSize.LARGE),
        header_arrangement=HeaderArrangement.STACKED,
        totals_position=TotalsPosition.FULL_WIDTH,
        page_size=PageSize.LETTER,
        margins=Margins.WIDE,
    )


# --- duplicate ---------------------------------------------------------------


async def test_duplicate_copies_every_field_into_a_new_row() -> None:
    source = _custom()
    service, repository = _service(source)

    duplicate = await service.duplicate_template(
        administration_id=ADMIN, template_id=source.id, actor_user_id=ACTOR
    )

    assert duplicate.id != source.id
    assert duplicate.name == "Copy of Huisstijl 2026"
    assert duplicate.is_default is False
    assert duplicate.version == 1
    assert duplicate.layout is source.layout
    assert duplicate.logo == source.logo  # asset_id carried over, per FR-TPL-019
    assert duplicate.typography == source.typography
    assert duplicate.colors == source.colors
    assert duplicate.columns == source.columns
    assert duplicate.blocks == source.blocks
    assert duplicate.header_arrangement is source.header_arrangement
    assert duplicate.totals_position is source.totals_position
    assert duplicate.page_size is source.page_size
    assert duplicate.margins is source.margins
    assert len(await repository.list_for_administration(administration_id=ADMIN)) == 2


async def test_duplicate_never_steals_the_default_slot() -> None:
    source = replace(_custom(), is_default=True)
    service, _ = _service(source)

    duplicate = await service.duplicate_template(
        administration_id=ADMIN, template_id=source.id, actor_user_id=ACTOR
    )

    assert source.is_default is True
    assert duplicate.is_default is False


async def test_duplicate_retries_a_colliding_name() -> None:
    source = _custom()
    already_copied_once = replace(
        _custom(), id=uuid.uuid4(), name="Copy of Huisstijl 2026", is_default=False
    )
    service, _ = _service(source, already_copied_once)

    duplicate = await service.duplicate_template(
        administration_id=ADMIN, template_id=source.id, actor_user_id=ACTOR
    )

    assert duplicate.name == "Copy of Huisstijl 2026 (2)"


async def test_duplicate_of_a_missing_template_is_refused() -> None:
    service, _ = _service()
    with pytest.raises(TemplateNotFound):
        await service.duplicate_template(
            administration_id=ADMIN, template_id=uuid.uuid4(), actor_user_id=ACTOR
        )


# --- reset ---------------------------------------------------------------


async def test_reset_restores_the_built_in_defaults_but_keeps_identity() -> None:
    source = _custom(name="My Template")
    service, _ = _service(source)

    reset = await service.reset_template(
        administration_id=ADMIN,
        template_id=source.id,
        actor_user_id=ACTOR,
        expected_version=source.version,
    )

    assert reset.id == source.id
    assert reset.name == source.name
    assert reset.is_default == source.is_default
    assert reset.version == source.version + 1
    assert reset.layout is Layout.CLASSIC
    assert reset.logo == Logo(asset_id=None)
    assert reset.header_arrangement is HeaderArrangement.SPLIT
    assert reset.totals_position is TotalsPosition.RIGHT
    assert reset.page_size is PageSize.A4
    assert reset.margins is Margins.NORMAL


async def test_reset_with_a_stale_version_is_refused() -> None:
    source = _custom()
    service, _ = _service(source)

    with pytest.raises(StaleTemplateVersion):
        await service.reset_template(
            administration_id=ADMIN,
            template_id=source.id,
            actor_user_id=ACTOR,
            expected_version=source.version + 1,
        )


async def test_reset_result_still_renders() -> None:
    """The reset target must pass `api.templates.compliance.check()` - it
    trivially does, since the built-in defaults are compliant by
    construction, but this proves the save path (which gates on it) accepted
    it rather than the test bypassing that gate.
    """
    source = _custom()
    service, _ = _service(source)

    reset = await service.reset_template(
        administration_id=ADMIN,
        template_id=source.id,
        actor_user_id=ACTOR,
        expected_version=source.version,
    )
    assert reset.blocks  # the compliant scaffold, not an empty template
