"""scripts/verify_ledger_integrity.py: the on-demand entry point (NFR-033).

The job's logic is tested in test_integrity.py and against a real database in
tests/integration/test_ledger_integrity.py. What is left, and what this file
covers, is the part a caller actually depends on when they wire this into a
schedule or a CI step: the exit codes.

They are the whole interface. `make verify-ledger-integrity && deploy` is a
sentence about exit status, and a job that returned 0 for "I could not read
the ledger" would make it a lie. No database here - the engine and the run are
substituted, because what is under test is the mapping from report to exit
code, not the SQL.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from api.ledger.integrity import (
    Deviation,
    IntegrityCheck,
    IntegrityReport,
    IntegrityScope,
)

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _script() -> ModuleType:
    """Loads the script by path - scripts/ is not a package, the same way
    tests/authz/test_role_catalogue_generation.py loads its generator.
    """
    spec = importlib.util.spec_from_file_location(
        "verify_ledger_integrity", SCRIPTS / "verify_ledger_integrity.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _StubEngine:
    """Stands in for the ops engine. `main()` disposes of it, and nothing
    else about it is reached once run_report is substituted.
    """

    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def report(
    *,
    deviations: Sequence[Deviation] = (),
    administrations: int = 1,
    entries: int = 3,
) -> IntegrityReport:
    return IntegrityReport(
        checked_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
        scope=IntegrityScope(
            administrations=administrations,
            journals=1,
            accounts=5,
            entries=entries,
            lines=entries * 2,
        ),
        deviations=tuple(deviations),
    )


def deviation(check: IntegrityCheck = IntegrityCheck.BALANCE) -> Deviation:
    return Deviation(
        check=check,
        requirement="FR-GL-001",
        deviation="entry_unbalanced",
        organization_id=uuid.uuid4(),
        administration_id=uuid.uuid4(),
        subject_type="journal_entry",
        subject_id=uuid.uuid4(),
        summary="entry 1 in journal MEM is unbalanced: debits 11.00 <> credits 10.00",
        detail={"total_debit": "11.00", "total_credit": "10.00", "difference": "1.00"},
    )


async def _run(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    result: IntegrityReport,
) -> int:
    engine = _StubEngine()
    captured: dict[str, Any] = {}

    async def fake_run_report(
        _engine: object,
        *,
        administration_id: uuid.UUID | None,
        organization_id: uuid.UUID | None,
    ) -> IntegrityReport:
        captured["administration_id"] = administration_id
        captured["organization_id"] = organization_id
        return result

    monkeypatch.setattr(module, "_engine", lambda _app: engine)
    monkeypatch.setattr(module, "run_report", fake_run_report)
    monkeypatch.setattr(sys, "argv", ["verify_ledger_integrity.py", *argv])

    code = await module.main()
    captured["disposed"] = engine.disposed
    _run.captured = captured  # type: ignore[attr-defined]
    return int(code)


# ===========================================================================
# Exit codes
# ===========================================================================


async def test_intact_books_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _script()

    code = await _run(module, monkeypatch, [], report())

    assert code == module.EXIT_INTACT == 0


async def test_a_deviation_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _script()

    code = await _run(module, monkeypatch, [], report(deviations=[deviation()]))

    assert code == module.EXIT_DEVIATIONS == 1


async def test_examining_nothing_is_not_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distinction the whole script exists to preserve. There were no
    findings, and there was no verification either.
    """
    module = _script()

    code = await _run(module, monkeypatch, [], report(administrations=0, entries=0))

    assert code == module.EXIT_EXAMINED_NOTHING == 3


async def test_an_empty_scope_can_be_declared_expected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _script()

    code = await _run(module, monkeypatch, ["--allow-empty"], report(administrations=0, entries=0))

    assert code == module.EXIT_INTACT


async def test_an_empty_scope_with_deviations_still_reports_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contradictory state - findings from a scope that saw no
    administration - but if it ever happens, the findings are the more urgent
    half and --allow-empty must not swallow them.
    """
    module = _script()

    code = await _run(
        module,
        monkeypatch,
        ["--allow-empty"],
        report(deviations=[deviation()], administrations=0, entries=0),
    )

    assert code == module.EXIT_DEVIATIONS


async def test_an_app_connection_without_a_tenant_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused rather than run, because the run would look like a success:
    RLS would hide every row and the job would report no deviations over a
    ledger it never read.
    """
    module = _script()
    monkeypatch.setattr(sys, "argv", ["verify_ledger_integrity.py", "--app-connection"])

    code = await module.main()

    assert code == module.EXIT_CANNOT_RUN == 2


async def test_a_missing_ops_connection_string_exits_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """api.db.get_ops_engine raises RuntimeError with an actionable message
    when OPS_DATABASE_URL is unset. That is a "could not run", not a "clean".
    """
    module = _script()

    def _no_engine(_app: bool) -> object:
        raise RuntimeError("OPS_DATABASE_URL is not set")

    monkeypatch.setattr(module, "_engine", _no_engine)
    monkeypatch.setattr(sys, "argv", ["verify_ledger_integrity.py"])

    code = await module.main()

    assert code == module.EXIT_CANNOT_RUN


# ===========================================================================
# Arguments and output
# ===========================================================================


async def test_the_scope_arguments_reach_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _script()
    administration = uuid.uuid4()
    organization = uuid.uuid4()

    await _run(
        module,
        monkeypatch,
        [
            "--administration",
            str(administration),
            "--organization",
            str(organization),
        ],
        report(),
    )

    captured = _run.captured  # type: ignore[attr-defined]
    assert captured["administration_id"] == administration
    assert captured["organization_id"] == organization
    assert captured["disposed"], "the ops engine is this script's to close"


async def test_json_output_is_the_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _script()
    result = report(deviations=[deviation()])

    await _run(module, monkeypatch, ["--json"], result)

    printed = json.loads(capsys.readouterr().out)
    assert printed == result.as_dict()
    assert printed["deviations"][0]["detail"]["difference"] == "1.00"


async def test_text_output_names_every_check_including_the_quiet_ones(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "control_account: none" is evidence that check ran. A section omitted
    because it found nothing is indistinguishable from one that never
    executed.
    """
    module = _script()

    await _run(module, monkeypatch, [], report(deviations=[deviation()]))

    out = capsys.readouterr().out
    assert "balance: 1 deviation(s)" in out
    assert "control_account: none" in out
    assert "numbering: none" in out
    assert "entry_unbalanced" in out
    assert "FR-GL-001" in out
