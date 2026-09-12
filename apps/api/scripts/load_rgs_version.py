"""Loads an RGS reference dataset into the database (CMP-003).

    CMP-003  RGS 3.8 (and successors) reference codes maintained on the chart
             of accounts, with a versioned upgrade path when a new RGS version
             is released.

This is what "the RGS version is data, not a hardcoded seed" means in
practice: supporting a new RGS release is running this against a new file. No
migration, no deploy, no code change. The upgrade path for administrations
already on the old version is ledger.plan_rgs_upgrade / apply_rgs_upgrade,
which read the `mappings` section of that same file.

--- Usage ---

    # validate the shipped dataset without touching a database (CI does this)
    uv run python scripts/load_rgs_version.py --check

    # load it, then make it the version new administrations are seeded from
    uv run python scripts/load_rgs_version.py --allow-provisional --publish

    # a future release
    uv run python scripts/load_rgs_version.py --file data/rgs/rgs-4.0-mkb.json --publish

--- Exit codes ---

    0  loaded (or, with --check, the document is valid)
    1  the document is invalid
    2  could not run: no connection string, missing file, unreadable JSON

--- Which connection ---

`ledgr_ops` (OPS_DATABASE_URL). `ledger.load_rgs_version` is granted to that
role and not to `ledgr_app`: a new RGS release is an operator action on the
schedule CMP-013 sets, and reference data every tenant maps accounts to is not
something one tenant's request should be able to introduce.

It is the only write privilege ledgr_ops holds near the ledger, and it is
bounded: the reference tables are append-only, so this can add a version and
cannot alter one (CMP-009's "including support tooling" is unaffected - ops
still cannot post, and still cannot touch an administration's chart).

--- Provenance ---

The shipped dataset is a provisional subset, not the official publication, and
loading it requires --allow-provisional. See the file's own _readme, and
ledger.rgs_readiness(), which reports every administration pinned to a
provisional version.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text  # noqa: E402

DEFAULT_FILE = Path(__file__).resolve().parents[1] / "data" / "rgs" / "rgs-3.8-mkb.json"

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_CANNOT_RUN = 2

ACCOUNT_TYPES = frozenset({"asset", "liability", "equity", "revenue", "expense"})
LEGAL_FORMS = frozenset({"eenmanszaak", "vof", "bv", "stichting", "vereniging"})
CONTROL_KINDS = frozenset({"accounts_receivable", "accounts_payable"})
#: FR-AR-002's treatments, mirroring the CHECK constraint in 0024.
VAT_CODES = frozenset(
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
SOURCES = frozenset({"official-publication", "provisional-subset"})
CHANGE_KINDS = frozenset({"unchanged", "renamed", "split", "merged", "withdrawn"})


def checksum(path: Path) -> str:
    """sha256 of the file's BYTES, not of the parsed document.

    The database stores this and refuses a second load of the same version
    name from a different document (a published RGS release is frozen). Hashing
    the bytes means a whitespace-only edit is still a different document, which
    is the conservative direction: it asks a human whether the change was
    intended rather than deciding for them.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(document: dict[str, Any]) -> list[str]:
    """Everything about the document that can be checked without a database.

    The database enforces most of this too - foreign keys, CHECK constraints,
    the parent-resolves test inside ledger.load_rgs_version - but a load that
    aborts halfway through a 4000-element file reports one violation, and this
    reports all of them at once against a file somebody is editing.
    """
    problems: list[str] = []

    if not str(document.get("rgs_version") or "").strip():
        problems.append("rgs_version is missing")
    if document.get("source") not in SOURCES:
        problems.append(f"source must be one of {sorted(SOURCES)}")

    elements = document.get("elements") or []
    if not elements:
        problems.append("the document defines no elements")

    by_code: dict[str, dict[str, Any]] = {}
    for element in elements:
        code = element.get("code")
        if not code:
            problems.append("an element has no code")
            continue
        if code in by_code:
            problems.append(f"element {code} is defined twice")
        by_code[code] = element

        # Only a postable element needs a type: that is exactly where an
        # account's own type has to agree with it (FR-ONB-005).
        if element.get("postable") and element.get("account_type") not in ACCOUNT_TYPES:
            problems.append(
                f"element {code} is postable but its account_type is "
                f"{element.get('account_type')!r}"
            )
        level = element.get("level")
        if not isinstance(level, int) or not 1 <= level <= 6:
            problems.append(f"element {code} has level {level!r}")
        if element.get("debit_credit") not in (None, "D", "C"):
            problems.append(f"element {code} has debit_credit {element.get('debit_credit')!r}")

    for code, element in by_code.items():
        parent = element.get("parent_code")
        if parent is not None and parent not in by_code:
            problems.append(f"element {code} names parent {parent}, which is not defined")

    common = document.get("common") or []
    profiles = document.get("profiles") or []
    if not profiles:
        problems.append("the document defines no profiles")

    seen_forms: set[str] = set()
    for profile in profiles:
        form = profile.get("legal_form")
        if form not in LEGAL_FORMS:
            problems.append(f"profile legal_form {form!r} is not one FR-ONB-004 names")
        if form in seen_forms:
            problems.append(f"legal form {form} has two profiles")
        seen_forms.add(str(form))

        accounts = [*common, *(profile.get("accounts") or [])]
        problems.extend(_validate_profile(str(form), accounts, by_code))

    missing_forms = LEGAL_FORMS - seen_forms
    if missing_forms:
        # FR-ONB-004 names five forms and FR-ONB-005 seeds "the profile
        # matching the legal form", so a form with no profile is an
        # administration that cannot be onboarded.
        problems.append(
            f"no profile for legal form(s) {sorted(missing_forms)}; FR-ONB-004 names all five"
        )

    for mapping in document.get("mappings") or []:
        kind = mapping.get("change_kind")
        if kind not in CHANGE_KINDS:
            problems.append(f"mapping change_kind {kind!r} is unknown")
        if (kind == "withdrawn") != (mapping.get("to_code") is None):
            problems.append(
                f"mapping from {mapping.get('from_code')} is {kind} but "
                f"to_code is {mapping.get('to_code')!r}; only a withdrawn code "
                "has no target"
            )
    if (document.get("mappings") or []) and not document.get("supersedes"):
        problems.append("the document has mappings but supersedes no version")

    return problems


def _validate_profile(
    form: str, accounts: list[dict[str, Any]], by_code: dict[str, dict[str, Any]]
) -> list[str]:
    problems: list[str] = []
    seen_accounts: set[str] = set()
    controls: dict[str, str] = {}

    for account in accounts:
        code = account.get("account_code")
        rgs_code = account.get("rgs_code")
        where = f"{form}/{code}"

        if not code:
            problems.append(f"{form}: an account has no account_code")
            continue
        if code in seen_accounts:
            problems.append(f"{where}: account_code appears twice in the profile")
        seen_accounts.add(code)

        if not account.get("name_nl"):
            problems.append(f"{where}: no Dutch name")

        element = by_code.get(str(rgs_code))
        if element is None:
            problems.append(f"{where}: rgs_code {rgs_code!r} is not defined")
        elif not element.get("postable"):
            problems.append(f"{where}: rgs_code {rgs_code} is a heading, not a postable element")

        vat = account.get("default_vat_code")
        if vat is not None and vat not in VAT_CODES:
            problems.append(f"{where}: default_vat_code {vat!r} is not an FR-AR-002 treatment")

        control = account.get("control_kind")
        if control is not None:
            if control not in CONTROL_KINDS:
                problems.append(f"{where}: control_kind {control!r} is unknown")
            elif control in controls:
                problems.append(
                    f"{where}: a second {control} control account "
                    f"(already {controls[control]}); FR-GL-006 needs exactly one"
                )
            else:
                controls[str(control)] = str(code)

    # FR-GL-006 needs both control accounts to exist, or the sub-ledgers have
    # nothing to reconcile to and the first sales invoice has nowhere to post.
    for kind in sorted(CONTROL_KINDS):
        if kind not in controls:
            problems.append(f"{form}: the profile seeds no {kind} control account")

    return problems


async def load(
    document: dict[str, Any],
    digest: str,
    *,
    allow_provisional: bool,
    publish: bool,
) -> dict[str, Any]:
    from api.db import get_ops_engine

    engine = get_ops_engine()
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT id, version, status, source, source_checksum "
                        "FROM ledger.load_rgs_version("
                        "  cast(:document as jsonb), :checksum, :allow_provisional)"
                    ),
                    {
                        "document": json.dumps(document),
                        "checksum": digest,
                        "allow_provisional": allow_provisional,
                    },
                )
            ).one()

            result = {
                "id": str(row.id),
                "version": row.version,
                "status": row.status,
                "source": row.source,
                "already_loaded": row.source_checksum == digest and row.status != "draft",
            }

            if publish:
                published = (
                    await conn.execute(
                        text(
                            "SELECT version, status "
                            "FROM ledger.publish_rgs_version(cast(:id as uuid))"
                        ),
                        {"id": str(row.id)},
                    )
                ).one()
                result["status"] = published.status
        return result
    finally:
        await engine.dispose()


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Load an RGS reference dataset (CMP-003).",
        epilog="Exit codes: 0 ok, 1 invalid document, 2 could not run.",
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
            "load a dataset that is not the official publication. Never the "
            "basis for an XAF export (CMP-002) or an SBR filing (CMP-004)."
        ),
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help=(
            "make this the current version, superseding whatever was. New "
            "administrations are seeded from the current version."
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
        print(f"{args.file} is not a valid RGS document:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_INVALID

    digest = checksum(args.file)
    if args.check:
        print(
            json.dumps(
                {
                    "file": str(args.file),
                    "rgs_version": document.get("rgs_version"),
                    "source": document.get("source"),
                    "elements": len(document.get("elements") or []),
                    "profiles": len(document.get("profiles") or []),
                    "checksum": digest,
                    "valid": True,
                },
                indent=2,
            )
        )
        return EXIT_OK

    try:
        result = await load(
            document,
            digest,
            allow_provisional=args.allow_provisional,
            publish=args.publish,
        )
    except RuntimeError as exc:
        # get_ops_engine raises this with an actionable message when
        # OPS_DATABASE_URL is unset.
        print(str(exc), file=sys.stderr)
        return EXIT_CANNOT_RUN

    print(json.dumps(result, indent=2))
    if result["source"] == "provisional-subset":
        print(
            "loaded a PROVISIONAL dataset. ledger.rgs_readiness() reports every "
            "administration pinned to it; replace it with the official "
            "publication before any XAF export or SBR filing.",
            file=sys.stderr,
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
