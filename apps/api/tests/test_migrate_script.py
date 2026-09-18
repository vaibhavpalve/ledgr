"""scripts/migrate.py: the guard rails, which are the whole point of it.

No database here - asyncpg is substituted, because what is under test is when
the runner REFUSES. Applying SQL correctly is Postgres's job; deciding not to
apply anything is this script's, and every one of those decisions protects a
live database from being half-migrated or silently skipped.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load() -> ModuleType:
    """Loads the script by path - scripts/ is not a package, the same way
    tests/ledger/test_integrity_script.py loads its own.
    """
    spec = importlib.util.spec_from_file_location("migrate", SCRIPTS / "migrate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeConnection:
    """Answers only the three questions the runner asks before it decides."""

    def __init__(
        self,
        *,
        has_app_schema: bool = True,
        has_tracking_table: bool = True,
        applied: Sequence[tuple[str, str]] = (),
    ) -> None:
        self._has_app_schema = has_app_schema
        self._has_tracking_table = has_tracking_table
        self._applied = list(applied)
        self.executed: list[str] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append(sql.strip())
        if "insert into app.schema_migrations" in sql:
            self._applied.append((str(args[0]), str(args[1])))
        return "OK"

    async def fetchval(self, sql: str, *args: Any) -> bool:
        if "schema_name = 'app'" in sql:
            return self._has_app_schema
        if "schema_migrations" in sql:
            return self._has_tracking_table
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, str]]:
        return [{"filename": name, "checksum": digest} for name, digest in self._applied]

    async def close(self) -> None:
        return None

    @property
    def applied_files(self) -> list[str]:
        return [name for name, _ in self._applied]


def _write_migrations(directory: Path, names: Sequence[str]) -> list[Path]:
    paths = []
    for name in names:
        path = directory / name
        path.write_text(f"begin;\n-- {name}\ncommit;\n", encoding="utf-8")
        paths.append(path)
    return paths


async def _run(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    conn: _FakeConnection,
    *,
    migrations: Sequence[Path],
    argv: Sequence[str] = (),
) -> None:
    async def fake_connect(dsn: str) -> _FakeConnection:
        return conn

    monkeypatch.setenv("DATABASE_ADMIN_URL", "postgresql://stub/stub")
    monkeypatch.setattr(module.asyncpg, "connect", fake_connect)
    monkeypatch.setattr(module, "_migrations", lambda: list(migrations))
    monkeypatch.setattr(sys, "argv", ["migrate.py", *argv])
    await module.main()


def test_a_bom_does_not_change_a_migrations_checksum(tmp_path: Path) -> None:
    """utf-8-sig is what the runner reads with, so the same SQL saved by an
    editor that writes a BOM must not look like an edited migration.
    """
    module = _load()
    plain = tmp_path / "0001_a.sql"
    plain.write_text("begin;\ncommit;\n", encoding="utf-8")
    with_bom = tmp_path / "0002_a.sql"
    with_bom.write_text("begin;\ncommit;\n", encoding="utf-8-sig")

    assert module.checksum(plain) == module.checksum(with_bom)


def test_editing_a_migration_changes_its_checksum(tmp_path: Path) -> None:
    module = _load()
    migration = tmp_path / "0001_a.sql"
    migration.write_text("begin;\ncommit;\n", encoding="utf-8")
    before = module.checksum(migration)
    migration.write_text("begin;\nalter table t add column c int;\ncommit;\n", encoding="utf-8")

    assert module.checksum(migration) != before


def test_the_real_migrations_sort_into_numeric_order() -> None:
    """Zero-padded names make lexicographic order the applied order. A file
    added as `051_x.sql` would sort before `0001_` and silently reorder
    history, so this guards the naming convention rather than the sort.
    """
    module = _load()
    names = [path.name for path in module._migrations()]

    assert names == sorted(names)
    assert all(name[:4].isdigit() for name in names)


@pytest.mark.asyncio
async def test_an_unbootstrapped_database_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load()
    conn = _FakeConnection(has_app_schema=False)

    with pytest.raises(SystemExit) as exit_info:
        await _run(
            module, monkeypatch, conn, migrations=_write_migrations(tmp_path, ["0001_a.sql"])
        )

    assert exit_info.value.code == 1
    assert conn.applied_files == []


@pytest.mark.asyncio
async def test_a_database_predating_tracking_is_refused_not_reapplied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The dangerous case: bootstrapped, so every migration is already there,
    but nothing recorded it. Re-applying would fail partway through and leave
    a half-migrated database, so the runner stops and asks for --baseline.
    """
    module = _load()
    conn = _FakeConnection(has_app_schema=True, has_tracking_table=False)

    with pytest.raises(SystemExit) as exit_info:
        await _run(
            module, monkeypatch, conn, migrations=_write_migrations(tmp_path, ["0001_a.sql"])
        )

    assert exit_info.value.code == 1
    assert conn.applied_files == []


@pytest.mark.asyncio
async def test_baseline_records_every_migration_without_applying_any(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load()
    conn = _FakeConnection(has_app_schema=True, has_tracking_table=False)
    migrations = _write_migrations(tmp_path, ["0001_a.sql", "0002_b.sql"])

    await _run(module, monkeypatch, conn, migrations=migrations, argv=["--baseline"])

    assert conn.applied_files == ["0001_a.sql", "0002_b.sql"]
    assert not any("-- 0001_a.sql" in statement for statement in conn.executed)


@pytest.mark.asyncio
async def test_only_unrecorded_migrations_are_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load()
    migrations = _write_migrations(tmp_path, ["0001_a.sql", "0002_b.sql", "0003_c.sql"])
    already = [(m.name, module.checksum(m)) for m in migrations[:2]]
    conn = _FakeConnection(applied=already)

    await _run(module, monkeypatch, conn, migrations=migrations)

    assert conn.applied_files == ["0001_a.sql", "0002_b.sql", "0003_c.sql"]
    bodies = [statement for statement in conn.executed if statement.startswith("begin;")]
    assert len(bodies) == 1 and "-- 0003_c.sql" in bodies[0]


@pytest.mark.asyncio
async def test_a_migration_edited_after_it_was_applied_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The database does not contain what the file now says, and applying the
    NEXT migration on top of that assumption is how drift becomes permanent.
    """
    module = _load()
    migrations = _write_migrations(tmp_path, ["0001_a.sql", "0002_b.sql"])
    conn = _FakeConnection(applied=[("0001_a.sql", "a-checksum-from-before-the-edit")])

    with pytest.raises(SystemExit) as exit_info:
        await _run(module, monkeypatch, conn, migrations=migrations)

    assert exit_info.value.code == 1
    assert "0002_b.sql" not in conn.applied_files


@pytest.mark.asyncio
async def test_dry_run_applies_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load()
    migrations = _write_migrations(tmp_path, ["0001_a.sql"])
    conn = _FakeConnection()

    await _run(module, monkeypatch, conn, migrations=migrations, argv=["--dry-run"])

    assert conn.applied_files == []
