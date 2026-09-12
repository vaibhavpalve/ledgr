"""Generates the two reference-data migrations from their Python sources:

    migrations/0010_role_catalogue.sql            <- api.authz.matrix
    migrations/0014_builtin_client_access_profiles.sql  <- api.authz.profiles


The permission catalogue and the twelve standard-role bundles are data in
one place (api/authz/matrix.py, itself checked against prd.md by
tests/authz/test_appendix_a_conformance.py). This script is what turns that
data into the SQL the database is seeded from, so the two cannot disagree:
tests/authz/test_role_catalogue_generation.py regenerates and compares, and
fails if the checked-in file is stale.

    make generate-role-catalogue     # rewrite the migration
    python scripts/generate_role_catalogue.py --check   # CI: is it current?

Why generate a checked-in file rather than seed at runtime from the module:
migrations are applied by scripts/bootstrap_test_db.py and by CI as plain
.sql, in order, and a reviewer diffing a schema change should see the actual
rows that will be written. A generated file keeps both properties while
leaving exactly one source of truth.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

# Importable both as `python scripts/generate_role_catalogue.py` and via
# `python -m`, matching the other scripts in this directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from api.authz.matrix import (  # noqa: E402
    ROLES,
    permission_catalogue,
    permissions_for_role,
)
from api.authz.profiles import BUILTIN_PROFILES  # noqa: E402

_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION = _MIGRATIONS / "0010_role_catalogue.sql"
PROFILE_MIGRATION = _MIGRATIONS / "0014_builtin_client_access_profiles.sql"

HEADER = """\
-- 0010_role_catalogue.sql
--
-- GENERATED FILE - DO NOT EDIT BY HAND.
-- Source of truth: apps/api/src/api/authz/matrix.py
-- Regenerate with: make generate-role-catalogue
--
-- The permission catalogue (PRD Appendix A's capability rows, one or two
-- permission triples each) and the twelve standard roles of PRD §8.4, with
-- the bundles Appendix A's matrix assigns them. matrix.py is checked against
-- prd.md itself by tests/authz/test_appendix_a_conformance.py, and this file
-- is checked against matrix.py by tests/authz/test_role_catalogue_generation.py,
-- so a change to the PRD, to the module, or to this file that is not made in
-- all three places fails CI.
--
-- Appendix A's "C" (conditional) cells grant the same permissions as "F".
-- The narrowing lives on the individual role_assignment's conditions column
-- (IAM-033) so it can vary per person - see matrix.py's CONDITIONAL_CELLS
-- and docs/decisions/ADR-011-authorization-model.md.

begin;

"""

FOOTER = "\ncommit;\n"


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render() -> str:
    parts = [HEADER]

    catalogue = sorted(permission_catalogue(), key=lambda row: (row[1], row[0].key))
    parts.append(
        "insert into permission (action, resource_type, resource_scope, description) values\n"
    )
    rows = [
        f"    ({_sql_literal(p.action)}, {_sql_literal(p.resource_type)}, "
        f"{_sql_literal(scope)}, {_sql_literal(p.description)})"
        for p, scope in catalogue
    ]
    parts.append(",\n".join(rows) + ";\n\n")

    parts.append('insert into "role" (name, scope_type, is_system) values\n')
    rows = [
        f"    ({_sql_literal(role.name)}, {_sql_literal(role.scope_type)}, true)" for role in ROLES
    ]
    parts.append(",\n".join(rows) + ";\n\n")

    parts.append(
        "insert into role_permission (role_id, permission_id)\n"
        "select r.id, p.id\n"
        'from "role" r\n'
        "join (values\n"
    )
    triples: list[str] = []
    for role in ROLES:
        name = _sql_literal(role.name)
        for action, resource_type in sorted(permissions_for_role(role)):
            triples.append(f"    ({name}, {_sql_literal(action)}, {_sql_literal(resource_type)})")
    parts.append(",\n".join(triples) + "\n")
    parts.append(
        ") as m(role_name, action, resource_type) on m.role_name = r.name\n"
        "join permission p on p.action = m.action and p.resource_type = m.resource_type\n"
        "where r.is_system;\n"
    )

    parts.append(FOOTER)
    return "".join(parts)


PROFILE_HEADER = """\
-- 0014_builtin_client_access_profiles.sql
--
-- GENERATED FILE - DO NOT EDIT BY HAND.
-- Source of truth: apps/api/src/api/authz/profiles.py (BUILTIN_PROFILES)
-- Regenerate with: make generate-role-catalogue
--
-- IAM-101's three built-in client access profiles. Generated from the same
-- definition the service and the test fake read, so the profiles a firm can
-- select cannot differ from the ones the code describes.
--
-- Each ships as version 1. A built-in is never edited (a firm composes its
-- own instead - see ClientAccessProfileService.publish_version), so there is
-- no version 2 to generate.

begin;

"""


def render_profiles() -> str:
    parts = [PROFILE_HEADER]

    parts.append("insert into client_access_profile (name, description, is_builtin) values\n")
    parts.append(
        ",\n".join(
            f"    ({_sql_literal(name)}, {_sql_literal(description)}, true)"
            for name, description, _, _ in BUILTIN_PROFILES
        )
        + ";\n\n"
    )

    for name, description, _, restrictions in BUILTIN_PROFILES:
        parts.append(
            "insert into client_access_profile_version "
            "(profile_id, version, summary, restrictions)\n"
            f"select p.id, 1, {_sql_literal(description)}, "
            f"{_sql_literal(json.dumps(restrictions.to_mapping(), sort_keys=True))}::jsonb\n"
            "from client_access_profile p "
            f"where p.is_builtin and p.name = {_sql_literal(name)};\n\n"
        )

    parts.append(
        "insert into client_access_profile_permission (profile_version_id, permission_id)\n"
        "select v.id, perm.id\n"
        "from client_access_profile_version v\n"
        "join client_access_profile p on p.id = v.profile_id\n"
        "join (values\n"
    )
    rows: list[str] = []
    for name, _, permissions, _ in BUILTIN_PROFILES:
        for action, resource_type in sorted(permissions):
            rows.append(
                f"    ({_sql_literal(name)}, {_sql_literal(action)}, {_sql_literal(resource_type)})"
            )
    parts.append(",\n".join(rows) + "\n")
    parts.append(
        ") as m(profile_name, action, resource_type) on m.profile_name = p.name\n"
        "join permission perm on perm.action = m.action "
        "and perm.resource_type = m.resource_type\n"
        "where p.is_builtin and v.version = 1;\n"
    )

    parts.append(FOOTER)
    return "".join(parts)


_TARGETS: tuple[tuple[Path, Callable[[], str]], ...] = (
    (MIGRATION, render),
    (PROFILE_MIGRATION, render_profiles),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the checked-in migration is stale, without rewriting it",
    )
    args = parser.parse_args()

    stale = False
    for path, renderer in _TARGETS:
        generated = renderer()
        if args.check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != generated:
                print(
                    f"{path.name} is out of date with its source module.\n"
                    "Run `make generate-role-catalogue` and commit the result.",
                    file=sys.stderr,
                )
                stale = True
            else:
                print(f"{path.name} is up to date.")
        else:
            path.write_text(generated, encoding="utf-8")
            print(f"wrote {path}")

    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
