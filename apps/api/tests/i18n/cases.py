"""The shared formatting case table, read from packages/i18n.

FR-LOC-002's "amounts read identically to every user" is a claim about two
implementations - api.i18n.formatting here and packages/i18n/src/format.ts on
the client - so the cases they are checked against are one file, loaded by
both. Same arrangement as tests/ledger/fiscal_cases.py, which feeds one table
to the Python and the SQL derivations of a fiscal year's periods.

This module deliberately does no work beyond loading. If the path is wrong or
the file is malformed, that must fail loudly at collection rather than produce
an empty case list - a table-driven suite that silently finds no rows passes
completely and guarantees nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: tests/i18n/cases.py -> tests/i18n -> tests -> apps/api -> apps -> repo root
CASES_FILE = Path(__file__).resolve().parents[4] / "packages" / "i18n" / "formatting-cases.json"


def _load() -> dict[str, Any]:
    if not CASES_FILE.is_file():
        raise RuntimeError(
            f"the shared formatting case table is missing: {CASES_FILE}. It is what "
            f"makes 'the API and the web app format an amount identically' "
            f"(FR-LOC-002) a checked claim rather than an assumption."
        )
    return json.loads(CASES_FILE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


_CASES = _load()

MONEY: list[dict[str, Any]] = _CASES["money"]
NUMBERS: list[dict[str, Any]] = _CASES["number"]
DATES: list[dict[str, Any]] = _CASES["date"]
PLURALS: list[dict[str, Any]] = _CASES["plural"]
REJECTED_MONEY: list[dict[str, Any]] = _CASES["rejected"]["money"]
REJECTED_DATES: list[dict[str, Any]] = _CASES["rejected"]["date"]
