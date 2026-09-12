"""Catches drift between api.authz.matrix and the SQL the database is
seeded from.

test_appendix_a_conformance.py links prd.md to the module; this links the
module to migrations/0010_role_catalogue.sql. Together they make the chain
mechanical end to end: PRD -> matrix.py -> migration -> database (the last
link is asserted by tests/integration/test_authorization_isolation.py
against live rows).

No database and no subprocess: the generator is imported and its output
compared to the checked-in file in memory.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

from api.authz.matrix import ROLES, permission_catalogue, permissions_for_role

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _generator() -> ModuleType:
    """Loads scripts/generate_role_catalogue.py by path - scripts/ is not a
    package (none of apps/api/scripts/ is), so it cannot simply be imported.
    """
    spec = importlib.util.spec_from_file_location(
        "generate_role_catalogue", SCRIPTS / "generate_role_catalogue.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_checked_in_migration_is_what_the_generator_produces_today() -> None:
    """The staleness check. Changing matrix.py without regenerating leaves
    the database seeded from the old matrix, which no other test would
    catch without a live Postgres.
    """
    generator = _generator()

    assert generator.MIGRATION.exists(), f"{generator.MIGRATION} is missing"
    assert generator.MIGRATION.read_text(encoding="utf-8") == generator.render(), (
        "migrations/0010_role_catalogue.sql is out of date with "
        "api/authz/matrix.py. Run `make generate-role-catalogue` and commit the result."
    )


def test_the_checked_in_profile_migration_is_current_too() -> None:
    """The same staleness check for IAM-101's built-in profiles: changing
    BUILTIN_PROFILES without regenerating would leave the database offering
    profiles the code no longer describes.
    """
    generator = _generator()

    assert generator.PROFILE_MIGRATION.exists()
    assert generator.PROFILE_MIGRATION.read_text(encoding="utf-8") == generator.render_profiles()


def test_the_profile_migration_seeds_every_builtin_with_its_permissions() -> None:
    from api.authz.profiles import BUILTIN_PROFILES

    text = _generator().PROFILE_MIGRATION.read_text(encoding="utf-8")

    for name, description, permissions, _ in BUILTIN_PROFILES:
        assert f"('{name}', '{description}', true)" in text
        for action, resource_type in permissions:
            assert f"('{name}', '{action}', '{resource_type}')" in text


def test_the_generated_migration_is_marked_as_generated() -> None:
    """A generated file that does not say so invites a hand edit that the
    next regeneration silently discards.
    """
    text = _generator().MIGRATION.read_text(encoding="utf-8")

    assert "GENERATED FILE - DO NOT EDIT BY HAND" in text
    assert "api/authz/matrix.py" in text
    assert "make generate-role-catalogue" in text


def test_the_migration_seeds_every_permission_in_the_catalogue() -> None:
    text = _generator().MIGRATION.read_text(encoding="utf-8")

    for permission, scope in permission_catalogue():
        row = f"('{permission.action}', '{permission.resource_type}', '{scope}'"
        assert row in text, f"{permission.key} is missing from the generated catalogue"


def test_the_migration_seeds_all_twelve_roles_with_their_scopes() -> None:
    text = _generator().MIGRATION.read_text(encoding="utf-8")

    assert len(ROLES) == 12
    for role in ROLES:
        assert f"('{role.name}', '{role.scope_type}', true)" in text


def test_the_migration_bundles_exactly_what_the_matrix_derives() -> None:
    """Parses the generated VALUES list back out and compares it to the
    module's own derivation - so a bug in the generator (a dropped role, a
    swapped column) fails here rather than shipping a subtly wrong catalogue.
    """
    text = _generator().MIGRATION.read_text(encoding="utf-8")
    block = text[text.index("join (values") : text.index(") as m(role_name")]

    parsed: dict[str, set[tuple[str, str]]] = {}
    for role_name, action, resource_type in re.findall(
        r"\('([^']+)',\s*'([a-z_]+)',\s*'([a-z_]+)'\)", block
    ):
        parsed.setdefault(role_name, set()).add((action, resource_type))

    expected = {role.name: set(permissions_for_role(role)) for role in ROLES}

    assert parsed == expected
