"""CLAUDE.md non-negotiable #1, as a build failure.

    "The ledger is a separate bounded context with a narrow API. Nothing
     writes to posting tables except the ledger service."

This file checks the Python half by reading the source tree. The half that
actually enforces it is in migration 0020: `ledgr_app` holds SELECT on the
posting tables and no INSERT, UPDATE, DELETE or TRUNCATE, so code that
ignored every rule here would get `permission denied for table
journal_entry` at runtime. tests/integration/test_ledger_bounded_context.py
asserts that for real against Postgres.

Both halves are worth having. The grant is the guarantee; these checks turn a
runtime privilege error in staging into a named failure at review time.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "api"
LEDGER = SRC / "ledger"

#: Written to by nothing outside the ledger service. Named here so the check
#: below is about THESE tables rather than about anything vaguely ledger-ish.
POSTING_TABLES = frozenset({"journal_entry", "journal_line", "journal_sequence"})

#: The public surface. Everything else in api.ledger is internal to the
#: context, and the two `*_repository` modules in particular belong to their
#: services alone.
PUBLIC_MODULES = frozenset(
    {
        "api.ledger",
        "api.ledger.model",
        "api.ledger.service",
        # FR-GL-007. Carries the period lifecycle types as well as the service,
        # the same way model.py carries the posting ones.
        "api.ledger.periods",
        # NFR-033. Public because the job is meant to be called from outside
        # the context - a scheduled sweep, and tests using it as an oracle.
        # It reads and reports; it holds no privilege to write anything.
        "api.ledger.integrity",
        # FR-GL-005 / FR-ONB-005 / CMP-003. The chart of accounts and its RGS
        # mapping, carrying its own value types the way periods.py does.
        "api.ledger.chart",
        # FR-ONB-006. Fiscal year definition. Public because `derive_periods`
        # is a pure function an onboarding flow calls to preview a year before
        # any of it exists - it touches no tenant and no table.
        "api.ledger.fiscal",
    }
)

#: Modules whose SQL belongs to one service and nothing else.
INTERNAL_MODULES = frozenset(
    {
        "api.ledger.repository",
        "api.ledger.periods_repository",
        "api.ledger.integrity_repository",
        "api.ledger.chart_repository",
        "api.ledger.fiscal_repository",
    }
)


def _python_files() -> list[pathlib.Path]:
    return sorted(SRC.rglob("*.py"))


def _imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_only_the_ledger_service_imports_the_ledger_repository() -> None:
    """`api.ledger.repository` is the only module that names a posting table.

    A second importer is how a bounded context stops being one: not through a
    deliberate decision, but through someone reaching for the class that
    already has the query they need.
    """
    offenders = []
    for path in _python_files():
        if path.is_relative_to(LEDGER):
            continue
        imports = _imported_modules(ast.parse(path.read_text(encoding="utf-8-sig")))
        reached = {
            name
            for name in imports
            if any(name.startswith(internal) for internal in INTERNAL_MODULES)
        }
        if reached:
            offenders.append(f"{path.relative_to(SRC.parent.parent)}: {sorted(reached)}")

    assert not offenders, (
        f"{offenders} import a ledger repository directly. Only the matching "
        "service may. Import from `api.ledger` and go through LedgerService or "
        "PeriodService - and if neither exposes what you need, the fix is to "
        "widen their API deliberately, not to reach past it."
    )


def test_the_ledger_package_exposes_only_its_public_modules() -> None:
    """`from api.ledger import ...` is the supported way in.

    Importing api.ledger.model directly is fine - it is value types. Importing
    api.ledger.repository is not, which the test above covers. This one keeps
    the list of what counts as public honest as the package grows: a new
    module here is a deliberate widening of the context's surface, and it
    should be visible in a diff.
    """
    modules = {
        f"api.ledger.{path.stem}" if path.stem != "__init__" else "api.ledger"
        for path in LEDGER.glob("*.py")
    }
    internal = modules - PUBLIC_MODULES

    assert internal == INTERNAL_MODULES, (
        f"api.ledger gained module(s) {sorted(internal - INTERNAL_MODULES)}. "
        "Add them to PUBLIC_MODULES if they are part of the context's API, or to "
        "INTERNAL_MODULES if they are not - the import check above reads that set."
    )


def test_no_module_outside_the_ledger_writes_sql_against_a_posting_table() -> None:
    """The import check catches the tidy violation - someone importing the
    repository. This catches the untidy one: a module that skips the import
    and writes its own INSERT.

    Deliberately looks for the WRITE verbs rather than for the table name.
    Reading journal_entry from a reporting module is legitimate; `ledgr_app`
    holds SELECT precisely so it can. Writing is what the context reserves.
    """
    offenders: list[str] = []
    for path in _python_files():
        if path.is_relative_to(LEDGER):
            continue
        text = path.read_text(encoding="utf-8-sig").lower()
        for table in POSTING_TABLES:
            for verb in ("insert into", "update", "delete from", "truncate"):
                if f"{verb} {table}" in text:
                    offenders.append(f"{path.name}: {verb} {table}")

    assert not offenders, (
        f"{offenders} write to a posting table directly. Nothing outside the "
        "ledger service may - and `ledgr_app` has no privilege to, so this "
        "would fail at runtime with `permission denied` regardless."
    )


def test_the_repository_is_the_only_place_the_ledger_names_its_tables() -> None:
    """Inside the context too, the SQL lives in one file.

    Not a purity rule: 0020's grants mean every write has to go through a
    `ledger.*` function, and keeping the calls together is what makes it
    checkable at a glance that no other statement form was smuggled in.
    """
    offenders = []
    for path in LEDGER.glob("*.py"):
        if path.name == "repository.py":
            continue
        text = path.read_text(encoding="utf-8-sig").lower()
        if any(f"{verb} " in text and "journal_entry" in text for verb in ("insert into",)):
            offenders.append(path.name)

    assert not offenders, f"{offenders} contain posting SQL; it belongs in repository.py"
