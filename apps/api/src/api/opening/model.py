"""The opening balance (beginbalans): the balance sheet a business brings into its first year here.

Pure: the rules that decide whether a set of opening lines can be posted, and what is posted. The
service reads the chart and the ledger and posts through `LedgerService.post`; nothing here
touches a database. See ADR-088.

--- Why it exists ---

Every business that signs up has a past: money in the bank, equipment, a loan, share capital.
Without a way to bring it in, the first receipt booked from the bank makes the dashboard show
negative cash, the balance sheet starts at zero, and nothing the product says about the business's
position is true. The golden path showed exactly that: EUR -121.00 of cash for a bakery that had
money in the bank the whole time.

--- The rules ---

- Balance-sheet accounts only (asset, liability, equity). A year's result starts at nothing; an
  opening line on a revenue or expense account would put last year's trading into this year's
  profit.
- Not a control account (debtors, creditors). Those carry one balance per customer or supplier
  (FR-GL-006), and a single opening figure cannot say who owes what. Open invoices come in as
  invoices; the ADR records it as the next step.
- Amounts are positive, at most two decimals, one side per line, one line per account.
- The entry must balance. If it does not, the difference can go to an equity account the person
  chooses - the usual "beginbalans verschil" onto capital or undistributed result - and nowhere
  else: parking a difference on an asset would invent money.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0.00")
CENT = Decimal("0.01")

BALANCE_SHEET_TYPES = frozenset({"asset", "liability", "equity"})


@dataclass(frozen=True, slots=True)
class OpeningAccount:
    id: uuid.UUID
    code: str
    name: str
    account_type: str
    control_kind: str | None
    status: str

    @property
    def eligible(self) -> bool:
        return (
            self.account_type in BALANCE_SHEET_TYPES
            and self.control_kind is None
            and self.status == "active"
        )


@dataclass(frozen=True, slots=True)
class OpeningLine:
    account_id: uuid.UUID
    debit: Decimal = ZERO
    credit: Decimal = ZERO


class OpeningBalanceError(Exception):
    """A refusal with an i18n reason (`errors.opening_<reason>`) and the account it concerns."""

    def __init__(self, reason: str, *, account_code: str | None = None, detail: str = "") -> None:
        super().__init__(f"{reason}: {account_code or ''} {detail}".strip())
        self.reason = reason
        self.account_code = account_code
        self.detail = detail


def _amount(value: Decimal, account: OpeningAccount) -> Decimal:
    if value < ZERO:
        raise OpeningBalanceError("amount_invalid", account_code=account.code)
    if value != value.quantize(CENT):
        raise OpeningBalanceError("amount_invalid", account_code=account.code)
    return value.quantize(CENT)


def plan_opening(
    lines: Sequence[OpeningLine],
    accounts: Mapping[uuid.UUID, OpeningAccount],
    *,
    balance_account_id: uuid.UUID | None = None,
) -> list[OpeningLine]:
    """The lines to post, or a refusal naming the first problem.

    Zero lines are dropped (a form row left empty is not a line). The balancing line, when one
    is needed and allowed, is appended last.
    """
    planned: list[OpeningLine] = []
    seen: set[uuid.UUID] = set()
    for line in lines:
        account = accounts.get(line.account_id)
        if account is None:
            raise OpeningBalanceError("account_unknown")
        debit = _amount(line.debit, account)
        credit = _amount(line.credit, account)
        if debit == ZERO and credit == ZERO:
            continue
        if debit != ZERO and credit != ZERO:
            raise OpeningBalanceError("both_sides", account_code=account.code)
        if account.account_type not in BALANCE_SHEET_TYPES:
            raise OpeningBalanceError("not_balance_sheet", account_code=account.code)
        if account.control_kind is not None:
            raise OpeningBalanceError("control_account", account_code=account.code)
        if account.status != "active":
            raise OpeningBalanceError("account_blocked", account_code=account.code)
        if account.id in seen:
            raise OpeningBalanceError("duplicate_account", account_code=account.code)
        seen.add(account.id)
        planned.append(OpeningLine(account_id=account.id, debit=debit, credit=credit))

    if not planned:
        raise OpeningBalanceError("empty")

    difference = sum((line.debit - line.credit for line in planned), ZERO)
    if difference == ZERO:
        return planned

    if balance_account_id is None:
        raise OpeningBalanceError("not_balanced", detail=str(difference))
    target = accounts.get(balance_account_id)
    if target is None or target.account_type != "equity" or target.status != "active":
        raise OpeningBalanceError("balance_account_invalid")
    if target.id in seen:
        raise OpeningBalanceError("balance_account_invalid", account_code=target.code)
    planned.append(
        OpeningLine(
            account_id=target.id,
            debit=-difference if difference < ZERO else ZERO,
            credit=difference if difference > ZERO else ZERO,
        )
    )
    return planned
