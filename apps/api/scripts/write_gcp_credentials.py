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

It also says, on stderr at container start, when the value is not a
service-account key. An `authorized_user` credential - the file
`gcloud auth application-default login` writes - is a PERSON's Google login:
KMS accepts it, so production appears to work, but it stops the day that
person's session is revoked or they leave, and it has no `client_email` or
`private_key`, so invoice reading on Vertex (api.expenses.extraction.
google_auth) cannot use it at all. Warned, not refused: refusing would take
document encryption down on a deploy that is working today. Only the
credential's `type` is ever printed, never its contents.
"""

from __future__ import annotations

import json
import os
import sys


def credential_warning(value: str) -> str | None:
    """A sentence for the deploy log when `value` is not a service-account key."""
    try:
        info = json.loads(value)
    except ValueError:
        return "GCP_SERVICE_ACCOUNT_JSON is not valid JSON; Google Cloud KMS will fail."
    kind = info.get("type") if isinstance(info, dict) else None
    if kind == "service_account":
        return None
    return (
        f"GCP_SERVICE_ACCOUNT_JSON has type {kind!r}, not 'service_account'. "
        "Replace it with a JSON key for the KMS service account "
        "(IAM & Admin > Service Accounts > Keys > Add key > JSON); see ADR-062."
    )


#: Spelled out rather than pasted: an invisible character in source is one an
#: editor can drop without anyone noticing.
_BOM = chr(0xFEFF)


if __name__ == "__main__":
    value = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "").lstrip(_BOM)
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        f.write(value)
    warning = credential_warning(value)
    if warning is not None:
        print(f"WARNING: {warning}", file=sys.stderr)
