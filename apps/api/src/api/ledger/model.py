"""Value types for the ledger bounded context (PRD §6.2).

--- What this module does NOT do ---

It does not enforce the ledger's invariants. Every one of them lives in
migration 0020, as a constraint, a trigger, or a withheld privilege:

    balance (FR-GL-001)      deferred constraint trigger, at COMMIT
    immutability (FR-GL-003) no grants + RAISE triggers + a NOLOGIN owner
    control accounts (006)   BEFORE INSERT trigger on journal_line
    gapless numbers (013)    allocator row + UNIQUE index
    period locks (FR-GL-007) BEFORE INSERT trigger

The checks here are a fast, well-worded rejection before a round trip, and
nothing more. If this module and the database ever disagree, the database is
right. tests/ledger/test_invariants.py asserts the local checks exist;
tests/integration/test_ledger_invariants.py asserts the database ones hold
when the local ones are bypassed entirely, which is the pair that matters.

--- NFR-031, at the one place it can still be caught ---

Money is Decimal here and numeric(19,2) there, and `float` is rejected
explicitly rather than coerced. A float that reaches this boundary has
already lost the precision - 4335.09 + 2964.61 is 7299.700000000001 - and
accepting it would write a wrong number that every later check agrees with.
The tripwire has to be at the door.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

#: The functional currency's scale. FR-GL-010 (multi-currency, S/P3) will add
#: transaction-currency amounts beside the functional ones rather than change
#: this - a posted amount in EUR has two decimals and always will.
SCALE = Decimal("0.01")

ZERO = Decimal("0.00")


class AccountType(enum.Enum):
    """FR-GL-005's list, closed, and mirrored by a CHECK in 0020."""

    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class JournalType(enum.Enum):
    """FR-GL-002's list, closed. A type outside it is a schema change."""

    SALES = "sales"
    PURCHASE = "purchase"
    BANK = "bank"
    CASH = "cash"
    MEMORIAL = "memorial"
    OPENING = "opening"
    CLOSING = "closing"


class ControlKind(enum.Enum):
    """FR-GL-006. An account with one of these is a control account and can
    only be posted to through its sub-ledger.
    """

    ACCOUNTS_RECEIVABLE = "accounts_receivable"
    ACCOUNTS_PAYABLE = "accounts_payable"

    @property
    def party_kind(self) -> PartyKind:
        return PartyKind.CUSTOMER if self is ControlKind.ACCOUNTS_RECEIVABLE else PartyKind.SUPPLIER


class PartyKind(enum.Enum):
    CUSTOMER = "customer"
    SUPPLIER = "supplier"


class AccountStatus(enum.Enum):
    ACTIVE = "active"
    BLOCKED = "blocked"


class LedgerError(Exception):
    """Base for every rejection this context raises.

    Distinct from a database error on purpose: a LedgerError means the request
    was refused for a reason a caller can act on, and the caller should be able
    to catch that without also catching a connection failure.
    """


class UnbalancedEntry(LedgerError):
    """FR-GL-001."""


class InvalidAmount(LedgerError):
    """NFR-031, or an amount that is negative, both-sided, or unrepresentable
    at the ledger's scale.
    """


class ControlAccountViolation(LedgerError):
    """FR-GL-006."""


def amount_to_string(value: Decimal) -> str:
    """Render an amount for the jsonb payload `ledger.post_entry` expects.

    A *string*, never a JSON number. Postgres stores a jsonb number as numeric
    so the value would survive the database intact - the loss happens on the
    way in, when json.dumps() writes a Python float. Requiring a string means
    a float raises here instead of being written as a plausible-looking wrong
    number, and `ledger.post_entry` rejects a JSON number as a second line of
    defence in case a caller ever reaches it without going through this.
    """
    # bool is a subclass of int, and int is not Decimal - but check it first so
    # the error names the actual mistake rather than the coincidental one.
    if isinstance(value, bool):
        raise InvalidAmount("a boolean is not a monetary amount")
    if isinstance(value, float):
        raise InvalidAmount(
            f"monetary amounts must be Decimal, not float (NFR-031): got {value!r}. "
            "A float has already lost precision by the time it reaches here."
        )
    if not isinstance(value, Decimal):
        raise InvalidAmount(
            f"monetary amounts must be Decimal (NFR-031): got {type(value).__name__}"
        )
    if not value.is_finite():
        raise InvalidAmount(f"{value} is not a finite amount")
    if value < 0:
        raise InvalidAmount(
            f"amounts are unsigned: {value} is negative. Direction is carried by "
            "which of debit/credit the amount is on, not by its sign."
        )
    # Rejected, not rounded. A posted amount is already settled: rounding it
    # silently here would make the ledger disagree with the invoice that
    # produced it, and which way to round is a decision for the calculation
    # that made the number, not for the boundary that stores it.
    if value % SCALE != 0:
        raise InvalidAmount(
            f"{value} has more precision than the ledger stores (scale 2). "
            "Round it where the rounding rule is known, not here."
        )
    return f"{value:.2f}"


@dataclass(frozen=True, slots=True)
class LineInput:
    """One posting. Exactly one of debit/credit carries the amount.

    Two fields rather than a signed amount because the sign convention differs
    by account type, and a reader checking a journal against a bank statement
    reads debit and credit. The invariant that exactly one is non-zero is a
    CHECK constraint in 0020; it is asserted here too so the error names the
    line.
    """

    account_id: uuid.UUID
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    subledger_party_id: uuid.UUID | None = None
    cost_centre_id: uuid.UUID | None = None
    description: str | None = None
    #: FR-VAT-001: which VAT treatment applied to THIS line (migration 0028's
    #: effective-dated set, stored by 0035).
    #:
    #: A plain string rather than an enum, because the ledger does not own the
    #: VAT vocabulary - `vat_treatment` is a reference table loaded from a data
    #: file (ADR-027), and a rate change is a load rather than a deploy. An
    #: enum here would make the ledger need a release to accept a treatment the
    #: tax authority already recognises. The FK on the column is what keeps it
    #: honest.
    #:
    #: Null where none applies - a transfer between two own accounts carries no
    #: VAT, and inventing a code for it would put a line in a return that
    #: belongs in no rubriek.
    vat_treatment: str | None = None

    def __post_init__(self) -> None:
        # Validates type and scale as a side effect; the strings are rebuilt in
        # as_payload() rather than cached, because the dataclass is frozen and
        # a second field would have to be kept in step with these.
        amount_to_string(self.debit)
        amount_to_string(self.credit)

        if (self.debit == 0) == (self.credit == 0):
            side = "both sides" if self.debit != 0 else "neither side"
            raise InvalidAmount(
                f"a line carries an amount on exactly one side; this one has {side} "
                f"(debit {self.debit}, credit {self.credit})"
            )

    @property
    def signed_amount(self) -> Decimal:
        """Positive for a debit, negative for a credit. Used by the balance
        check, where summing one column is less error-prone than comparing two.
        """
        return self.debit - self.credit

    def as_payload(self) -> dict[str, str | None]:
        return {
            "account_id": str(self.account_id),
            "debit": amount_to_string(self.debit),
            "credit": amount_to_string(self.credit),
            "subledger_party_id": (
                str(self.subledger_party_id) if self.subledger_party_id else None
            ),
            "cost_centre_id": str(self.cost_centre_id) if self.cost_centre_id else None,
            "description": self.description,
            "vat_treatment": self.vat_treatment,
        }


@dataclass(frozen=True, slots=True)
class EntryInput:
    """A journal entry as a caller supplies it.

    Note what is NOT here: `entry_number`, `fiscal_year_id`, `organization_id`
    and `posted_at`. All four are derived by triggers in 0020 from the period
    and the sequence allocator, because a caller that could name its own entry
    number could start a second series or fill a hole, and one that could name
    its own fiscal year could number itself into the wrong year.
    """

    administration_id: uuid.UUID
    journal_id: uuid.UUID
    period_id: uuid.UUID
    entry_date: date
    description: str
    lines: Sequence[LineInput]

    document_reference: str | None = None
    posted_by_user_id: uuid.UUID | None = None
    source_system: str = "api"
    reverses_entry_id: uuid.UUID | None = None
    idempotency_key: str | None = None
    #: FR-GL-007 / FR-VAT-005. Set when this entry corrects a VAT-filed
    #: period. The entry itself still lands in an OPEN period - the filed one
    #: is hard-locked - and this is the link back to the filing it corrects.
    suppletie_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise LedgerError("a journal entry needs a description (FR-GL-004)")
        if not self.source_system.strip():
            raise LedgerError("a journal entry needs a source system (FR-GL-004)")

    @property
    def total_debit(self) -> Decimal:
        return sum((line.debit for line in self.lines), ZERO)

    @property
    def total_credit(self) -> Decimal:
        return sum((line.credit for line in self.lines), ZERO)

    @property
    def is_balanced(self) -> bool:
        """FR-GL-001, checked locally for a good error message.

        An entry with no lines sums to zero on both sides, so the emptiness
        check in `assert_balanced` is separate and comes first - the same trap
        `journal_entry_assert_balanced()` guards in SQL, where SUM() over no
        rows returns NULL and NULL <> 0 is not TRUE.
        """
        return self.total_debit == self.total_credit

    def assert_balanced(self) -> None:
        if len(self.lines) == 0:
            raise UnbalancedEntry(
                "a journal entry with no lines is not a balanced entry (FR-GL-001)"
            )
        if len(self.lines) < 2:
            raise UnbalancedEntry("double-entry requires at least two lines (FR-GL-001)")
        if not self.is_balanced:
            # Worded to match journal_entry_assert_balanced()'s message on
            # purpose. tests/ledger/cases.py matches one substring against
            # both, so a caller reading a log cannot tell which layer refused
            # - and does not need to.
            raise UnbalancedEntry(
                f"entry is unbalanced: debits {self.total_debit} <> credits "
                f"{self.total_credit} (FR-GL-001)"
            )

    def as_line_payload(self) -> list[dict[str, str | None]]:
        return [line.as_payload() for line in self.lines]


@dataclass(frozen=True, slots=True)
class PostedLine:
    id: uuid.UUID
    line_number: int
    account_id: uuid.UUID
    debit: Decimal
    credit: Decimal
    subledger_party_id: uuid.UUID | None = None
    cost_centre_id: uuid.UUID | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class PostedEntry:
    """An entry as read back: what the database assigned, not what was asked
    for. `entry_number` and `posted_at` in particular come from the triggers.
    """

    id: uuid.UUID
    organization_id: uuid.UUID
    administration_id: uuid.UUID
    fiscal_year_id: uuid.UUID
    period_id: uuid.UUID
    journal_id: uuid.UUID
    entry_number: int
    entry_date: date
    description: str
    source_system: str
    posted_at: datetime
    document_reference: str | None = None
    posted_by_user_id: uuid.UUID | None = None
    reverses_entry_id: uuid.UUID | None = None
    idempotency_key: str | None = None
    suppletie_id: uuid.UUID | None = None
    lines: Sequence[PostedLine] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class TrialBalanceRow:
    account_id: uuid.UUID
    account_code: str
    account_name: str
    account_type: AccountType
    total_debit: Decimal
    total_credit: Decimal
    balance: Decimal


@dataclass(frozen=True, slots=True)
class SubledgerRow:
    party_id: uuid.UUID
    party_name: str
    total_debit: Decimal
    total_credit: Decimal
    balance: Decimal


@dataclass(frozen=True, slots=True)
class ReconciliationRow:
    """FR-GL-006's "reconciled continuously", as something showable.

    `difference` is the part of the control account's balance that no party
    accounts for. It is structurally zero: `journal_line_validate()` refuses a
    line on a control account that names no party, so the unattributed bucket
    cannot receive one. A non-zero value here means the trigger is gone.
    """

    control_kind: ControlKind
    control_account_code: str
    control_balance: Decimal
    subledger_balance: Decimal
    difference: Decimal

    @property
    def reconciled(self) -> bool:
        return self.difference == 0


@dataclass(frozen=True, slots=True)
class Account:
    id: uuid.UUID
    administration_id: uuid.UUID
    code: str
    name: str
    account_type: AccountType
    status: AccountStatus = AccountStatus.ACTIVE
    rgs_code: str | None = None
    default_vat_code: str | None = None
    control_kind: ControlKind | None = None

    @property
    def is_control_account(self) -> bool:
        return self.control_kind is not None


@dataclass(frozen=True, slots=True)
class Journal:
    id: uuid.UUID
    administration_id: uuid.UUID
    code: str
    name: str
    journal_type: JournalType
    status: str = "active"


@dataclass(frozen=True, slots=True)
class Party:
    id: uuid.UUID
    administration_id: uuid.UUID
    party_kind: PartyKind
    name: str
    external_reference: str | None = None
