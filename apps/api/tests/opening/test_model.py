"""ADR-088: which opening lines can be posted, and what is posted."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from api.opening.model import OpeningAccount, OpeningBalanceError, OpeningLine, plan_opening


def account(code: str, kind: str, *, control: str | None = None, status: str = "active"):  # type: ignore[no-untyped-def]
    return OpeningAccount(
        id=uuid.uuid4(),
        code=code,
        name=code,
        account_type=kind,
        control_kind=control,
        status=status,
    )


BANK = account("1100", "asset")
LOAN = account("1790", "liability")
CAPITAL = account("0500", "equity")
RESULT = account("0530", "equity")
DEBTORS = account("1300", "asset", control="accounts_receivable")
REVENUE = account("8000", "revenue")
BLOCKED = account("1000", "asset", status="blocked")
CHART = {a.id: a for a in (BANK, LOAN, CAPITAL, RESULT, DEBTORS, REVENUE, BLOCKED)}


def line(acct: OpeningAccount, debit: str = "0", credit: str = "0") -> OpeningLine:
    return OpeningLine(account_id=acct.id, debit=Decimal(debit), credit=Decimal(credit))


def test_a_balanced_opening_posts_as_given_without_its_empty_rows() -> None:
    planned = plan_opening(
        [
            line(BANK, debit="5000.00"),
            line(LOAN, credit="1000"),
            line(CAPITAL, credit="4000.00"),
            line(RESULT),
        ],
        CHART,
    )
    assert [(p.account_id, p.debit, p.credit) for p in planned] == [
        (BANK.id, Decimal("5000.00"), Decimal("0.00")),
        (LOAN.id, Decimal("0.00"), Decimal("1000.00")),
        (CAPITAL.id, Decimal("0.00"), Decimal("4000.00")),
    ]


def test_a_difference_goes_to_the_chosen_equity_account() -> None:
    planned = plan_opening(
        [line(BANK, debit="5000.00"), line(CAPITAL, credit="4500.00")],
        CHART,
        balance_account_id=RESULT.id,
    )
    assert (planned[-1].account_id, planned[-1].credit) == (RESULT.id, Decimal("500.00"))


def test_a_debit_difference_is_a_debit_on_equity() -> None:
    planned = plan_opening(
        [line(BANK, debit="100.00"), line(LOAN, credit="300.00")],
        CHART,
        balance_account_id=RESULT.id,
    )
    assert (planned[-1].debit, planned[-1].credit) == (Decimal("200.00"), Decimal("0.00"))


@pytest.mark.parametrize(
    ("lines", "balance", "reason"),
    [
        ([line(BANK, debit="10.00")], None, "not_balanced"),
        ([line(BANK, debit="10.00")], BANK.id, "balance_account_invalid"),
        ([line(REVENUE, credit="10.00"), line(BANK, debit="10.00")], None, "not_balance_sheet"),
        ([line(DEBTORS, debit="10.00"), line(CAPITAL, credit="10.00")], None, "control_account"),
        ([line(BLOCKED, debit="10.00"), line(CAPITAL, credit="10.00")], None, "account_blocked"),
        ([line(BANK, debit="10.00", credit="10.00")], None, "both_sides"),
        ([line(BANK, debit="-10.00"), line(CAPITAL, debit="10.00")], None, "amount_invalid"),
        ([line(BANK, debit="10.001"), line(CAPITAL, credit="10.001")], None, "amount_invalid"),
        (
            [line(BANK, debit="5.00"), line(BANK, debit="5.00"), line(CAPITAL, credit="10.00")],
            None,
            "duplicate_account",
        ),
        ([line(BANK), line(CAPITAL)], None, "empty"),
    ],
)
def test_refusals_name_what_is_wrong(
    lines: list[OpeningLine], balance: uuid.UUID | None, reason: str
) -> None:
    with pytest.raises(OpeningBalanceError) as refused:
        plan_opening(lines, CHART, balance_account_id=balance)
    assert refused.value.reason == reason


def test_an_account_from_elsewhere_is_refused() -> None:
    stranger = account("9999", "asset")
    with pytest.raises(OpeningBalanceError) as refused:
        plan_opening([line(stranger, debit="1.00")], CHART)
    assert refused.value.reason == "account_unknown"
