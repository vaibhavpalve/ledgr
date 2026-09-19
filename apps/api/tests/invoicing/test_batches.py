"""SI-17's rules - api.invoicing.batches (ADR-079)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from api.invoicing.batches import (
    MAX_DUE_DAYS,
    MAX_ENTRIES,
    BatchEntry,
    BatchInvalid,
    resolve_entries,
)
from api.invoicing.quotes import QuoteLine

D = Decimal
TODAY = date(2026, 9, 21)


def line(**kw: object) -> QuoteLine:
    defaults: dict[str, object] = dict(
        description="Jaarlijkse bijdrage",
        quantity=D("1"),
        unit_price=D("250"),
        vat_treatment="btw_21",
    )
    defaults.update(kw)
    return QuoteLine(**defaults)  # type: ignore[arg-type]


def resolve(entries: list[BatchEntry], **kw: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = dict(
        name="Contributie 2026",
        entries=entries,
        default_lines=None,
        default_notes=None,
        default_due_days=None,
        today=TODAY,
    )
    args.update(kw)
    return resolve_entries(**args)  # type: ignore[arg-type]


def entry(**kw: object) -> BatchEntry:
    defaults: dict[str, object] = dict(customer_id=uuid.uuid4())
    defaults.update(kw)
    return BatchEntry(**defaults)  # type: ignore[arg-type]


def test_the_same_default_lines_go_to_every_customer() -> None:
    default = (line(),)
    resolved = resolve([entry(), entry(), entry()], default_lines=default)
    assert [r.position for r in resolved] == [1, 2, 3]
    assert all(r.lines == default for r in resolved)


def test_an_entry_with_its_own_lines_overrides_the_default() -> None:
    own = (line(description="Verbruik", quantity=D("37.5"), unit_price=D("0.18")),)
    resolved = resolve([entry(), entry(lines=own)], default_lines=(line(),))
    assert resolved[0].lines[0].description == "Jaarlijkse bijdrage"
    assert resolved[1].lines == own


def test_notes_and_payment_term_inherit_and_can_be_overridden() -> None:
    resolved = resolve(
        [entry(), entry(notes="Speciaal", due_days=30), entry(notes="  ")],
        default_lines=(line(),),
        default_notes="  Bedankt  ",
        default_due_days=14,
    )
    assert [(r.notes, r.due_days) for r in resolved] == [
        ("Bedankt", 14),
        ("Speciaal", 30),
        (None, 14),  # an explicit blank clears the note rather than inheriting
    ]


def test_an_entry_with_no_lines_and_no_default_is_refused_naming_it() -> None:
    with pytest.raises(BatchInvalid) as refused:
        resolve([entry(lines=(line(),)), entry()])
    assert (refused.value.code, refused.value.position) == ("lines", 2)


def test_an_unusable_line_is_refused_naming_the_entry() -> None:
    bad = (line(quantity=D("0")),)
    with pytest.raises(BatchInvalid) as refused:
        resolve([entry(), entry(lines=bad)], default_lines=(line(),))
    assert (refused.value.code, refused.value.position) == ("lines", 2)


def test_a_float_price_is_refused() -> None:
    with pytest.raises(BatchInvalid, match="floats"):
        resolve([entry(lines=(line(unit_price=1.5),))])  # type: ignore[arg-type]


@pytest.mark.parametrize("days", [-1, MAX_DUE_DAYS + 1])
def test_a_payment_term_out_of_range_is_refused(days: int) -> None:
    with pytest.raises(BatchInvalid) as refused:
        resolve([entry(due_days=days)], default_lines=(line(),))
    assert refused.value.position == 1


def test_zero_days_is_a_legal_term() -> None:
    (resolved,) = resolve([entry(due_days=0)], default_lines=(line(),))
    assert resolved.due_days == 0


def test_an_empty_batch_a_nameless_one_and_an_oversized_one_are_refused() -> None:
    with pytest.raises(BatchInvalid) as empty:
        resolve([], default_lines=(line(),))
    assert empty.value.code == "empty"
    with pytest.raises(BatchInvalid) as nameless:
        resolve([entry()], name="   ", default_lines=(line(),))
    assert nameless.value.code == "name_required"
    with pytest.raises(BatchInvalid) as big:
        resolve([entry() for _ in range(MAX_ENTRIES + 1)], default_lines=(line(),))
    assert big.value.code == "too_many"
    resolve([entry() for _ in range(MAX_ENTRIES)], default_lines=(line(),))  # the limit itself


def test_the_same_customer_twice_is_two_invoices() -> None:
    """Two contracts with one customer are two invoices; nothing here merges them."""
    customer = uuid.uuid4()
    resolved = resolve(
        [entry(customer_id=customer), entry(customer_id=customer)], default_lines=(line(),)
    )
    assert [r.customer_id for r in resolved] == [customer, customer]
