"""scripts/write_gcp_credentials.py's start-up warning (ADR-062, ADR-095)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "write_gcp_credentials.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("write_gcp_credentials", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_service_account_key_passes_silently() -> None:
    value = json.dumps({"type": "service_account", "client_email": "x@y.iam.gserviceaccount.com"})

    assert _script().credential_warning(value) is None


def test_a_persons_login_is_named_without_printing_it() -> None:
    secret = "1//0refresh-token-that-must-never-be-printed"
    value = json.dumps({"type": "authorized_user", "refresh_token": secret, "client_secret": "s"})

    warning = _script().credential_warning(value)

    assert warning is not None
    assert "'authorized_user'" in warning
    assert secret not in warning and "client_secret" not in warning


def test_a_value_that_is_not_json_is_named() -> None:
    warning = _script().credential_warning("not json")

    assert warning is not None and "not valid JSON" in warning
