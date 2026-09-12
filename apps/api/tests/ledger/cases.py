"""Every entry the ledger must refuse, as data.

Each case is an EntryInput the ledger must reject, plus a substring the
rejection has to contain. The substrings are chosen to appear in BOTH the
in-memory fake's message and Postgres's, so a case that passes for the wrong
reason - a typo'd account id rejected as "does not exist" rather than as the
violation under test - fails instead of quietly passing.

Written as a table rather than as fifteen test functions because the same
table is executed twice: once against the fake (fast, always runs) and once
against a real database (DB-gated, runs in CI). A case added here is
automatically covered by both, which is the only way the two stay in step.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from api.ledger.model import EntryInput, LineInput
from tests.ledger.world import World

TEN = Decimal("10.00")
FIVE = Decimal("5.00")


@dataclass(frozen=True, slots=True)
class RejectionCase:
    name: str
    requirement: str
    build: Callable[[World], EntryInput]
    expect: str


def balanced_lines(world: World) -> list[LineInput]:
    """The simplest entry that satisfies FR-GL-001: cash up, revenue up."""
    return [
        LineInput(account_id=world.cash_account_id, debit=TEN),
        LineInput(account_id=world.revenue_account_id, credit=TEN),
    ]


def entry(world: World, **overrides: object) -> EntryInput:
    defaults: dict[str, object] = {
        "administration_id": world.administration_id,
        "journal_id": world.journal_id,
        "period_id": world.open_period_id,
        "entry_date": world.entry_date,
        "description": "test entry",
        "lines": balanced_lines(world),
        "source_system": "pytest",
        # FR-GL-004. Not a convenience default: LedgerService refuses a
        # user-attributed posting that names nobody, so leaving it out would
        # make every case fail as MissingActor rather than as the violation
        # under test.
        "posted_by_user_id": world.actor_user_id,
    }
    defaults.update(overrides)
    return EntryInput(**defaults)  # type: ignore[arg-type]


# ===========================================================================
# FR-GL-001: debits equal credits
# ===========================================================================


def _unbalanced(world: World) -> EntryInput:
    return entry(
        world,
        lines=[
            LineInput(account_id=world.cash_account_id, debit=TEN),
            LineInput(account_id=world.revenue_account_id, credit=FIVE),
        ],
    )


def _single_line(world: World) -> EntryInput:
    return entry(world, lines=[LineInput(account_id=world.cash_account_id, debit=TEN)])


def _many_lines_unbalanced(world: World) -> EntryInput:
    """Balanced pairwise, unbalanced overall.

    A check that compared lines two at a time, or that stopped at the first
    pair, would pass this. The requirement is about the entry's totals.
    """
    return entry(
        world,
        lines=[
            LineInput(account_id=world.cash_account_id, debit=TEN),
            LineInput(account_id=world.revenue_account_id, credit=TEN),
            LineInput(account_id=world.cash_account_id, debit=FIVE),
        ],
    )


# ===========================================================================
# FR-GL-002: journal and period belong to this administration
# ===========================================================================


def _journal_from_another_administration(world: World) -> EntryInput:
    return entry(world, journal_id=world.other_journal_id)


def _period_from_another_administration(world: World) -> EntryInput:
    return entry(world, period_id=world.other_period_id)


def _account_from_another_administration(world: World) -> EntryInput:
    return entry(
        world,
        lines=[
            LineInput(account_id=world.other_account_id, debit=TEN),
            LineInput(account_id=world.revenue_account_id, credit=TEN),
        ],
    )


# ===========================================================================
# FR-GL-005: blocked accounts and journals
# ===========================================================================


def _blocked_account(world: World) -> EntryInput:
    return entry(
        world,
        lines=[
            LineInput(account_id=world.blocked_account_id, debit=TEN),
            LineInput(account_id=world.revenue_account_id, credit=TEN),
        ],
    )


def _blocked_journal(world: World) -> EntryInput:
    return entry(world, journal_id=world.blocked_journal_id)


# ===========================================================================
# FR-GL-006: a control account cannot be posted to directly
# ===========================================================================


def _control_account_without_party(world: World) -> EntryInput:
    """The case the requirement is actually about: a memorial entry that
    debits receivables without saying who owes it.
    """
    return entry(
        world,
        lines=[
            LineInput(account_id=world.receivables_control_id, debit=TEN),
            LineInput(account_id=world.revenue_account_id, credit=TEN),
        ],
    )


def _payables_control_without_party(world: World) -> EntryInput:
    return entry(
        world,
        lines=[
            LineInput(account_id=world.cash_account_id, debit=TEN),
            LineInput(account_id=world.payables_control_id, credit=TEN),
        ],
    )


def _party_on_ordinary_account(world: World) -> EntryInput:
    """The converse half of the biconditional.

    A receivable recorded against an ordinary account with a party attached
    would be a sub-ledger balance the control account cannot see - the drift
    FR-GL-006's "reconciled continuously" exists to prevent.
    """
    return entry(
        world,
        lines=[
            LineInput(
                account_id=world.cash_account_id,
                debit=TEN,
                subledger_party_id=world.customer_party_id,
            ),
            LineInput(account_id=world.revenue_account_id, credit=TEN),
        ],
    )


def _supplier_on_receivables(world: World) -> EntryInput:
    return entry(
        world,
        lines=[
            LineInput(
                account_id=world.receivables_control_id,
                debit=TEN,
                subledger_party_id=world.supplier_party_id,
            ),
            LineInput(account_id=world.revenue_account_id, credit=TEN),
        ],
    )


# ===========================================================================
# FR-GL-007: period locking
# ===========================================================================


def _locked_period(world: World) -> EntryInput:
    return entry(world, period_id=world.locked_period_id)


def _vat_filed_period(world: World) -> EntryInput:
    return entry(world, period_id=world.vat_filed_period_id)


def _date_outside_period(world: World) -> EntryInput:
    """Without this, a period lock protects nothing: a January date could be
    posted into an open December and land in the wrong VAT return.
    """
    return entry(world, entry_date=world.period_end + timedelta(days=1))


REJECTION_CASES: Sequence[RejectionCase] = (
    RejectionCase("debits do not equal credits", "FR-GL-001", _unbalanced, "unbalanced"),
    RejectionCase("a single line", "FR-GL-001", _single_line, "at least two"),
    RejectionCase(
        "balanced pairwise but not overall",
        "FR-GL-001",
        _many_lines_unbalanced,
        "unbalanced",
    ),
    RejectionCase(
        "journal from another administration",
        "FR-GL-002",
        _journal_from_another_administration,
        "FR-GL-002",
    ),
    RejectionCase(
        "period from another administration",
        "FR-GL-002",
        _period_from_another_administration,
        "FR-GL-002",
    ),
    RejectionCase(
        "account from another administration",
        "FR-GL-002",
        _account_from_another_administration,
        "another administration",
    ),
    RejectionCase(
        "a blocked account",
        "FR-GL-005",
        _blocked_account,
        "is blocked and cannot be posted to (FR-GL-005)",
    ),
    RejectionCase("a blocked journal", "FR-GL-005", _blocked_journal, "blocked"),
    RejectionCase(
        "receivables control account with no party",
        "FR-GL-006",
        _control_account_without_party,
        "cannot be posted to directly",
    ),
    RejectionCase(
        "payables control account with no party",
        "FR-GL-006",
        _payables_control_without_party,
        "cannot be posted to directly",
    ),
    RejectionCase(
        "a party on an ordinary account",
        "FR-GL-006",
        _party_on_ordinary_account,
        "cannot carry a sub-ledger party",
    ),
    RejectionCase(
        "a supplier posted to receivables",
        "FR-GL-006",
        _supplier_on_receivables,
        "cannot be posted to the",
    ),
    RejectionCase(
        "a locked period",
        "FR-GL-007",
        _locked_period,
        "cannot be posted to (FR-GL-007)",
    ),
    RejectionCase("a VAT-filed period", "FR-GL-007", _vat_filed_period, "hard-locked"),
    RejectionCase(
        "an entry date outside its period",
        "FR-GL-007",
        _date_outside_period,
        "falls outside period",
    ),
)
