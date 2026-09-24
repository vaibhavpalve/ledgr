"""CSV the way a Dutch Excel opens it (ADR-089).

Semicolon-separated with a decimal comma - Excel in a Dutch locale reads a comma-separated file
with decimal points as one column of text - and a UTF-8 byte-order mark, without which Excel
decodes "Privé" as mojibake. Amounts are written from Decimal, never through float (NFR-031),
and no thousands separator is written: a separator is a display choice a spreadsheet makes, and
one written into the data is a number it can no longer sum.

`dialect="international"` gives the plain form (comma-separated, decimal point, no BOM) for a
tool rather than a person.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Literal

Dialect = Literal["nl", "international"]

BOM = chr(0xFEFF)


def amount(value: Decimal, dialect: Dialect) -> str:
    text = f"{value:.2f}"
    return text.replace(".", ",") if dialect == "nl" else text


def render(
    header: Sequence[str], rows: Iterable[Sequence[object]], *, dialect: Dialect = "nl"
) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";" if dialect == "nl" else ",", lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(
            [amount(cell, dialect) if isinstance(cell, Decimal) else cell for cell in row]
        )
    body = buffer.getvalue()
    return BOM + body if dialect == "nl" else body
