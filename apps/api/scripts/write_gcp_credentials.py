"""Writes GCP_SERVICE_ACCOUNT_JSON to a file GOOGLE_APPLICATION_CREDENTIALS
can point at (apps/api/Dockerfile's CMD, ADR-062).

Its own script rather than a shell `printf '%s' "$VAR" > file` one-liner
because that version shipped a real production bug: a Windows-sourced JSON
value (PowerShell's `Out-File`/`Set-Content` default to UTF-8-WITH-BOM) can
carry a leading byte-order mark into the Railway variable, and Python's
`json.load` rejects a BOM outright - `google-auth` hit exactly this
decoding the credential file, and GcpKmsKeyManagementService (api.crypto.kms)
failed on the first request that needed it (TOTP enrollment, which wraps its
new secret through the KMS-backed envelope encryption service).

`str.lstrip("﻿")` is the fix: a BOM survives a shell environment
variable as that one leading Unicode character (the three raw BOM bytes,
already decoded by the time Python's `os.environ` hands back a `str`), and
stripping it is a safe no-op when the value never had one - which is why
this runs unconditionally rather than trying to detect whether a BOM is
present first.
"""

from __future__ import annotations

import os
import sys

if __name__ == "__main__":
    value = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "")
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        f.write(value.lstrip("﻿"))
