"""The fixture set every ledger invariant case is written against.

One shape, two backings. tests/ledger/test_invariants.py builds a World over
the in-memory fake; tests/integration/test_ledger_invariants.py builds the
same World over a real Postgres with migration 0020 applied, and both run the
identical case table from tests/ledger/cases.py.

That is what keeps the fake honest. A fake that has drifted from the database
keeps its own tests green while testing something production does not do -
the failure mode 0019 guarded against by comparing the fake's hash to
Postgres's on a real row. Here the equivalent is: every rejection the fake
claims to make, the database must also make, for the same entry.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class World:
    """Ids for a seeded administration with everything the cases need.

    Deliberately explicit rather than a dict: a case that reaches for a
    fixture nobody seeded fails at attribute access with a name, not at
    runtime with a KeyError three frames down.
    """

    administration_id: uuid.UUID
    fiscal_year_id: uuid.UUID

    #: FR-GL-004's `actor`. A real users(id) in the DB-backed World, because
    #: journal_entry.posted_by_user_id is a foreign key - a made-up uuid would
    #: fail there for a reason unrelated to the case under test.
    actor_user_id: uuid.UUID

    #: FR-GL-007's three states. All three exist in every World so a case can
    #: reach for the one it needs without seeding its own.
    open_period_id: uuid.UUID
    locked_period_id: uuid.UUID
    vat_filed_period_id: uuid.UUID
    period_start: date
    period_end: date

    journal_id: uuid.UUID
    blocked_journal_id: uuid.UUID

    #: Two ordinary accounts, so a balanced entry needs no control account.
    cash_account_id: uuid.UUID
    revenue_account_id: uuid.UUID
    blocked_account_id: uuid.UUID

    #: FR-GL-006.
    receivables_control_id: uuid.UUID
    payables_control_id: uuid.UUID
    customer_party_id: uuid.UUID
    supplier_party_id: uuid.UUID

    #: A second administration in the SAME organization, so FR-GL-002's
    #: "belongs to this administration" cases are not accidentally satisfied
    #: by tenant isolation instead. An id from another ORGANIZATION would be
    #: invisible under RLS and would prove the wrong thing.
    other_administration_id: uuid.UUID
    other_journal_id: uuid.UUID
    other_period_id: uuid.UUID
    other_account_id: uuid.UUID

    @property
    def entry_date(self) -> date:
        return self.period_start
