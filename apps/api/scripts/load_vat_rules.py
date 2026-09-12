"""Loads an effective-dated VAT ruleset into the database (CMP-014, CMP-013).

    CMP-014  Rate and rule changes are effective-dated, so historical periods
             keep the rules that applied at the time.
    CMP-013  A regulatory watch function tracks changes to VAT rates,
             rubrieken, taxonomy versions, RGS versions and e-invoicing
             mandates, with a defined lead time for product changes.

A rate change is a new file loaded with this script. No migration, no deploy,
no code change - the same shape scripts/load_rgs_version.py has for RGS.

--- The lead time is not decoration ---

Every rule row carries `valid_from`, and the database refuses one that falls on
or before the last day any period has been VAT-filed through. So a rate change
has to be loaded BEFORE the period it applies to is filed, which is exactly the
"defined lead time" CMP-013 asks for, enforced rather than scheduled.

If a load is refused for that reason, the fix is never to edit the rule: a
filed period is corrected by a suppletie (FR-VAT-005).

--- Usage ---

    # validate without touching a database (CI does this)
    uv run python scripts/load_vat_rules.py --check

    # load the shipped Dutch ruleset
    uv run python scripts/load_vat_rules.py --allow-provisional

    # a rate change
    uv run python scripts/load_vat_rules.py --file data/vat/nl-2027-01.json

--- Exit codes ---

    0  loaded (or, with --check, the document is valid)
    1  the document is invalid, or a rule would land behind a filed period
    2  could not run: no connection string, missing file, unreadable JSON

--- Which connection ---

`ledgr_ops` (OPS_DATABASE_URL). `vat.load_ruleset` is granted to that role and
not to `ledgr_app`: a rate is public law that every tenant computes against,
and not something one tenant's request should be able to introduce.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text  # noqa: E402

DEFAULT_FILE = Path(__file__).resolve().parents[1] / "data" / "vat" / "nl-vat-rules.json"

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_CANNOT_RUN = 2

#: FR-AR-002's treatments, mirroring the CHECK in 0024 and 0028.
TREATMENTS = frozenset(
    {
        "btw_21",
        "btw_9",
        "btw_0",
        "btw_vrijgesteld",
        "btw_verlegd",
        "btw_icp",
        "btw_export",
        "btw_marge",
    }
)
ROLES = frozenset(
    {
        "standard",
        "reduced",
        "zero",
        "exempt",
        "reverse_charge",
        "intra_community",
        "export",
        "margin",
    }
)
RUBRIEK_KINDS = frozenset({"turnover", "vat", "input", "subtotal"})
SOURCES = frozenset({"official-publication", "provisional-subset"})


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def validate(document: dict[str, Any]) -> list[str]:
    """Everything checkable without a database.

    The database enforces most of it again - CHECK constraints, the
    append-only triggers, the filed-frontier barrier - but a load that aborts
    on the first bad row reports one problem, and this reports all of them
    against a file somebody is editing.
    """
    problems: list[str] = []

    if not str(document.get("ruleset_version") or "").strip():
        problems.append("ruleset_version is missing")
    if document.get("source") not in SOURCES:
        problems.append(f"source must be one of {sorted(SOURCES)}")

    treatments = document.get("treatments") or []
    by_code: dict[str, dict[str, Any]] = {}
    seen_roles: set[str] = set()
    for treatment in treatments:
        code = treatment.get("code")
        role = treatment.get("role")
        if code not in TREATMENTS:
            problems.append(f"treatment code {code!r} is not one FR-AR-002 names")
            continue
        if code in by_code:
            problems.append(f"treatment {code} is defined twice")
        by_code[code] = treatment
        if role not in ROLES:
            problems.append(f"treatment {code} has role {role!r}")
        elif role in seen_roles:
            problems.append(f"role {role} is claimed by two treatments")
        else:
            seen_roles.add(str(role))
        if not treatment.get("description_nl"):
            problems.append(f"treatment {code} has no Dutch description")

    missing = TREATMENTS - set(by_code)
    if missing:
        problems.append(
            f"no treatment defined for {sorted(missing)}; ledger_account."
            "default_vat_code allows all eight, so a chart can already name them"
        )

    problems.extend(_validate_rates(document.get("rates") or [], set(by_code)))
    problems.extend(_validate_rubrieken(document.get("rubrieken") or []))
    problems.extend(
        _validate_mapping(
            document.get("treatment_rubriek") or [],
            set(by_code),
            {b.get("code") for b in (document.get("rubrieken") or [])},
        )
    )
    return problems


def _validate_rates(rates: list[dict[str, Any]], treatments: set[str]) -> list[str]:
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    covered: set[str] = set()

    for entry in rates:
        treatment = entry.get("treatment")
        raw_from = entry.get("valid_from")
        where = f"{treatment}@{raw_from}"

        if treatment not in treatments:
            problems.append(f"rate {where}: unknown treatment")
        else:
            covered.add(str(treatment))

        if _parse_date(raw_from) is None:
            problems.append(f"rate {where}: valid_from is not an ISO date")

        key = (str(treatment), str(raw_from))
        if key in seen:
            problems.append(f"rate {where}: two rates share one valid_from")
        seen.add(key)

        # NFR-031: a decimal string, never a JSON number. json.dumps of a
        # Python float produces a number, so this is where an already-lossy
        # rate is caught rather than multiplied by every invoice line.
        rate = entry.get("rate")
        if not isinstance(rate, str):
            problems.append(
                f"rate {where}: rate must be a decimal string, not {type(rate).__name__} (NFR-031)"
            )
            continue
        try:
            value = float(rate)  # validation only; never used for arithmetic
        except ValueError:
            problems.append(f"rate {where}: {rate!r} is not a decimal")
            continue
        if not 0 <= value <= 100:
            problems.append(f"rate {where}: {rate} is not a percentage")

    uncovered = treatments - covered
    if uncovered:
        problems.append(
            f"no rate at any date for {sorted(uncovered)}; vat.rate_on would "
            "return NULL, which reads as a data gap rather than a rate of zero"
        )
    return problems


def _validate_rubrieken(rubrieken: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()

    for entry in rubrieken:
        code = entry.get("code")
        raw_from = entry.get("valid_from")
        where = f"{code}@{raw_from}"

        if not code:
            problems.append("a rubriek has no code")
        if _parse_date(raw_from) is None:
            problems.append(f"rubriek {where}: valid_from is not an ISO date")
        if entry.get("kind") not in RUBRIEK_KINDS:
            problems.append(f"rubriek {where}: kind {entry.get('kind')!r} is unknown")
        if not entry.get("description_nl"):
            problems.append(f"rubriek {where}: no Dutch description")

        key = (str(code), str(raw_from))
        if key in seen:
            problems.append(f"rubriek {where}: defined twice at one valid_from")
        seen.add(key)

    return problems


def _validate_mapping(
    mapping: list[dict[str, Any]], treatments: set[str], rubrieken: set[str | None]
) -> list[str]:
    problems: list[str] = []
    covered: set[str] = set()

    for entry in mapping:
        treatment = entry.get("treatment")
        where = f"{treatment}@{entry.get('valid_from')}"

        if treatment not in treatments:
            problems.append(f"mapping {where}: unknown treatment")
        else:
            covered.add(str(treatment))
        if _parse_date(entry.get("valid_from")) is None:
            problems.append(f"mapping {where}: valid_from is not an ISO date")

        turnover = entry.get("turnover_rubriek")
        if turnover not in rubrieken:
            problems.append(f"mapping {where}: turnover_rubriek {turnover!r} undefined")
        vat_box = entry.get("vat_rubriek")
        if vat_box is not None and vat_box not in rubrieken:
            problems.append(f"mapping {where}: vat_rubriek {vat_box!r} undefined")

    uncovered = treatments - covered
    if uncovered:
        problems.append(f"no rubriek mapping for {sorted(uncovered)} (FR-VAT-001)")
    return problems


async def load(document: dict[str, Any], digest: str, *, allow_provisional: bool) -> dict[str, Any]:
    from api.db import get_ops_engine

    engine = get_ops_engine()
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT id, jurisdiction, version, source "
                        "FROM vat.load_ruleset("
                        "  cast(:document as jsonb), :checksum, :allow_provisional)"
                    ),
                    {
                        "document": json.dumps(document),
                        "checksum": digest,
                        "allow_provisional": allow_provisional,
                    },
                )
            ).one()
            return {
                "id": str(row.id),
                "jurisdiction": row.jurisdiction,
                "version": row.version,
                "source": row.source,
            }
    finally:
        await engine.dispose()


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load an effective-dated VAT ruleset (CMP-014).",
        epilog="Exit codes: 0 ok, 1 invalid or refused, 2 could not run.",
    )
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the document and exit; no database is touched",
    )
    parser.add_argument(
        "--allow-provisional",
        action="store_true",
        help=(
            "load a ruleset that is not the official publication. Never the "
            "basis for a filed return (FR-VAT-003)."
        ),
    )
    args = parser.parse_args()

    if not args.file.exists():
        print(f"{args.file} does not exist", file=sys.stderr)
        return EXIT_CANNOT_RUN
    try:
        document = json.loads(args.file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"{args.file} is not valid JSON: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    problems = validate(document)
    if problems:
        print(f"{args.file} is not a valid VAT ruleset:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_INVALID

    digest = checksum(args.file)
    if args.check:
        print(
            json.dumps(
                {
                    "file": str(args.file),
                    "ruleset_version": document.get("ruleset_version"),
                    "source": document.get("source"),
                    "treatments": len(document.get("treatments") or []),
                    "rates": len(document.get("rates") or []),
                    "rubrieken": len(document.get("rubrieken") or []),
                    "checksum": digest,
                    "valid": True,
                },
                indent=2,
            )
        )
        return EXIT_OK

    try:
        result = await load(document, digest, allow_provisional=args.allow_provisional)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CANNOT_RUN
    except Exception as exc:  # noqa: BLE001 - the message is the product here
        # The barrier in 0028 raises through here. Reported as an invalid load
        # rather than a crash, because "this rule would restate a filed period"
        # is a decision the operator has to act on, not a bug.
        print(f"the ruleset was refused: {exc}", file=sys.stderr)
        return EXIT_INVALID

    print(json.dumps(result, indent=2))
    if result["source"] == "provisional-subset":
        print(
            "loaded a PROVISIONAL ruleset. The rates are the published ones; "
            "the treatment-to-rubriek mapping is not verified and belongs to "
            "FR-VAT-001. Do not file against it.",
            file=sys.stderr,
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
