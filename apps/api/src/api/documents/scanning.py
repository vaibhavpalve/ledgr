"""Malware scanning - SEC-005, and the §9 threat table's "Malicious document
upload".

    SEC-005  File uploads: ... malware-scanned ...

--- The verdict is a state on the row, not a boolean in a function ---

A scanner answers slowly and can be unavailable, so "scan it" is not something
an upload handler can simply do and move on from. `document.scan_status` in
migration 0031 is the state machine:

    pending   stored, not yet cleared. NOT downloadable.
    clean     downloadable.
    infected  quarantined. Never downloadable, and never deleted either -
              FR-DOC-005's write-once applies to it like anything else, and the
              file is evidence about an incident.
    failed    the scanner could not answer. NOT downloadable.

`pending` and `failed` both refuse the download, which is the only safe default:
an unscanned file and an infected one present the same risk to whoever opens
them, and the difference between them is only time.

--- Why an infected document is kept ---

Deleting it would be the intuitive response and is wrong twice over. The row is
inside its retention period, so FR-DOC-005 forbids it without a documented
legal basis and a second approver; and the file is the evidence for the
incident it represents. `documents.integrity_deviations()` reports infected
rows so they are visibly quarantined rather than quietly present.

--- The scan runs on the bytes, before they are stored ---

`api.documents.service` scans before it writes the blob. A store-then-scan
pipeline leaves a window in which the object exists and nothing has judged it,
and windows like that are exactly what a retry storm widens.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol


class ScanStatus(enum.Enum):
    """Mirrors the `scan_status` CHECK in migration 0031."""

    PENDING = "pending"
    CLEAN = "clean"
    INFECTED = "infected"
    FAILED = "failed"

    @property
    def is_downloadable(self) -> bool:
        """Only `clean`. Stated as a property rather than left to each caller
        to remember, because "which states may be served" is exactly the
        decision that drifts when it is written out three times.
        """
        return self is ScanStatus.CLEAN


@dataclass(frozen=True, slots=True)
class ScanResult:
    status: ScanStatus
    #: Which scanner and which signature set answered. Kept because a verdict
    #: is only re-evaluable if it is attributable - a scanner later found to
    #: have been broken for a week leaves rows that have to be found again.
    scanner: str
    #: The threat name, when there is one. Never shown to a user.
    detail: str | None = None


class MalwareScanner(Protocol):
    """CLAUDE.md non-negotiable #4's adapter seam.

    Takes the bytes, not a path or a key: the thing that gets judged has to be
    the thing that gets stored, and a scanner handed a location could be given
    different bytes than the ones the caller hashed.
    """

    async def scan(self, data: bytes) -> ScanResult: ...


#: The EICAR test string - the industry-standard harmless file that every real
#: scanner is required to report as a detection. Split so this source file does
#: not itself trip a scanner watching the repository, which is a real thing
#: that happens and a confusing one to debug.
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


class LocalPatternScanner:
    """Dev and test only. NOT a malware scanner.

    It detects EICAR and nothing else, which is enough to exercise every branch
    of the pipeline - clean, infected, and the refusal to serve - without a
    network or a licence.

    Production must run a real scanner. This class is named for what it is so
    that finding it in a production configuration is obviously wrong, the same
    posture `api.auth.breach_check` and `api.crypto.kms` take for their own
    local stand-ins.
    """

    name = "local-pattern-scanner"

    async def scan(self, data: bytes) -> ScanResult:
        if EICAR in data:
            return ScanResult(ScanStatus.INFECTED, self.name, "EICAR-Test-File")
        return ScanResult(ScanStatus.CLEAN, self.name)


class RefusingScanner:
    """A scanner that always fails, for exercising the `failed` path.

    Worth having as its own class rather than a mock: the property under test -
    that a document whose scan failed is not downloadable - is one the real
    system has to hold, and a test that patched a method could pass while the
    pipeline treated `failed` as clean.
    """

    name = "refusing-scanner"

    async def scan(self, data: bytes) -> ScanResult:
        return ScanResult(ScanStatus.FAILED, self.name, "scanner unavailable")


def build_scanner(provider: str) -> MalwareScanner:
    """Selects the scanner from configuration.

    There is no `none` option, deliberately. SEC-005 lists malware scanning as
    a control, and a switch that turns it off is a switch somebody eventually
    finds in an incident review.
    """
    if provider == "local":
        return LocalPatternScanner()
    raise ValueError(
        f"unknown malware scanner provider {provider!r}. 'local' is dev and test "
        f"only and detects nothing but EICAR; a production deployment needs a "
        f"real scanner wired in here (SEC-005)."
    )
