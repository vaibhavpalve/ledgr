"""The shape of a data subject erasure decision - PRIV-021..023.

    PRIV-022  Erasure requests are honoured except where fiscal retention law
              requires preservation. The system distinguishes the two and
              gives a clear, specific explanation of what was retained and
              why.
    PRIV-023  Where erasure is blocked by retention law, the record is
              restricted: access limited to fiscal purposes, excluded from
              analytics and search, deleted automatically at the end of the
              retention period.

`ErasureDecision` is the one shape both `api.documents.service.
DocumentService.request_erasure` and `api.customers.service.CustomerService.
request_erasure` return. Neither module's own retention rule lives here -
FR-DOC-002's fiscal-year anchor and CMP-001's invoice-snapshot freeze are
different facts about different tables, and forcing one shared rule to
explain both would be the rule fitting neither. What is shared is only the
ANSWER'S shape: what was decided, in plain language why, and until when.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import date, datetime


class ErasureOutcome(enum.Enum):
    """PRIV-022's fork. `RESTRICTED` is not a softer kind of `ERASED` - the
    record is retained in full, exactly as PRIV-023 describes.
    """

    #: The request was honoured. Nothing about this resource remains under
    #: fiscal retention that would have blocked it.
    ERASED = "erased"
    #: Fiscal retention law blocks erasure. The resource is restricted
    #: instead - see `ErasureDecision.explanation` for what and why.
    RESTRICTED = "restricted"


@dataclass(frozen=True, slots=True)
class ErasureDecision:
    """PRIV-022's "clear, specific explanation", as data rather than only as
    a sentence - `retained_until` is what a caller checks programmatically,
    `explanation` is what a person reads.
    """

    resource_type: str
    resource_id: uuid.UUID
    outcome: ErasureOutcome
    #: Specific to THIS request: names the record, the rule, and the date -
    #: never a generic "retention law applies" that a data subject could not
    #: act on or verify.
    explanation: str
    #: Set only when `outcome` is RESTRICTED. PRIV-023's "deleted
    #: automatically at the end of the retention period" - the date the next
    #: scheduled sweep is expected to remove what fiscal law is holding onto.
    retained_until: date | None
    decided_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "resource_type": self.resource_type,
            "resource_id": str(self.resource_id),
            "outcome": self.outcome.value,
            "explanation": self.explanation,
            "retained_until": self.retained_until.isoformat() if self.retained_until else None,
            "decided_at": self.decided_at.isoformat(),
        }
