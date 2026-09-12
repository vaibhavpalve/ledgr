"""FR-EXP-001b's split, as one table run against both implementations.

`api.expenses.vat.vat_from_gross` and migration 0033's
`expenses.vat_from_gross()` compute the same thing and cannot borrow each
other's answer: the form has to show net and VAT before anything is stored, and
the database has to hold the stored value to a CHECK that it is what the rule
gives. tests/expenses/test_vat.py runs this against the Python;
tests/integration/test_expense_form.py runs it against the SQL.

The device is 0029's (fiscal periods) and 0031's (retention), for the same
reason: two implementations of one rule agreeing by inspection is not
agreement.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class VatCase:
    gross: str
    rate: str
    vat: str
    net: str
    why: str

    @property
    def gross_amount(self) -> Decimal:
        return Decimal(self.gross)

    @property
    def rate_percent(self) -> Decimal:
        return Decimal(self.rate)

    @property
    def expected_vat(self) -> Decimal:
        return Decimal(self.vat)

    @property
    def expected_net(self) -> Decimal:
        return Decimal(self.net)


VAT_CASES: tuple[VatCase, ...] = (
    VatCase(
        "121.00",
        "21.00",
        "21.00",
        "100.00",
        "the case everyone checks by hand: 121 at 21% is exactly 100 + 21",
    ),
    VatCase(
        "100.00",
        "21.00",
        "17.36",
        "82.64",
        "a round gross, which is what a receipt actually shows. 100 x 21/121 is "
        "17.3553..., and the residue lands in net so the two still sum to 100",
    ),
    VatCase(
        "109.00",
        "9.00",
        "9.00",
        "100.00",
        "the low rate's exact case",
    ),
    VatCase(
        "50.00",
        "9.00",
        "4.13",
        "45.87",
        "50 x 9/109 is 4.1284..., rounding down; net absorbs the rest",
    ),
    VatCase(
        "0.00",
        "21.00",
        "0.00",
        "0.00",
        "a zero receipt is arithmetically fine and must not divide by anything",
    ),
    VatCase(
        "0.01",
        "21.00",
        "0.00",
        "0.01",
        "one cent at 21% contains 0.0017 of VAT, which rounds to nothing - and "
        "net must then be the whole cent, not zero",
    ),
    VatCase(
        "0.03",
        "21.00",
        "0.01",
        "0.02",
        "0.0052 rounds UP to a cent. The smallest amount that carries any VAT "
        "at all, and the first place a half-even rounding mode would differ",
    ),
    VatCase(
        "100.00",
        "0.00",
        "0.00",
        "100.00",
        "btw_0, btw_vrijgesteld, btw_verlegd, btw_icp and btw_export all carry "
        "rate 0 in the shipped ruleset: no VAT, and net is the whole amount",
    ),
    VatCase(
        "119.00",
        "19.00",
        "19.00",
        "100.00",
        "the 19% era. A receipt dated before 2012-10-01 gets this rate from "
        "vat.rate_on, which is why the rate is looked up by DATE and not typed",
    ),
    VatCase(
        "1234567.89",
        "21.00",
        "214263.85",
        "1020304.04",
        "a large gross, well past the point a float would start losing cents",
    ),
    VatCase(
        "7299.70",
        "21.00",
        "1266.89",
        "6032.81",
        "4335.09 + 2964.61, the sum api.ledger.model names as the float trap. "
        "Its VAT has to come from the decimal total, not from 7299.700000000001",
    ),
    VatCase(
        "999999999999999.99",
        "21.00",
        "173553719008264.46",
        "826446280991735.53",
        "numeric(19,2)'s territory. 2^53 is exhausted around 9.0e15, so a float "
        "implementation is already wrong several digits before this",
    ),
)

#: Inputs both implementations must REFUSE. Each is a way a wrong number
#: reaches a claim looking right.
REJECTED: tuple[tuple[str, str, str], ...] = (
    ("-1.00", "21.00", "a negative gross: a refund is a credit note, not an expense with a minus"),
    (
        "10.005",
        "21.00",
        "three decimals; numeric(19,2) would round it and the difference "
        "would land in net unnoticed",
    ),
    ("100.00", "-1.00", "a negative rate"),
    ("100.00", "101.00", "a rate above 100%"),
)

#: NOT rejected, and worth recording as a decision rather than an oversight:
#: a rate of 0.21 is accepted, because 0.21% is a legitimate rate that
#: `vat_rate.rate` in migration 0028 can hold (numeric(6,3), 0..100). Somebody
#: passing 0.21 to mean 21% would get a fifth of a percent and no complaint.
#:
#: The defence is structural rather than a validation rule: the form does not
#: take a rate at all. It takes a TREATMENT, and the rate comes from
#: `vat.rate_on(treatment, expense_date)` - so there is no field for the
#: mistake to arrive through. A validator that guessed which of the two a
#: caller meant would be wrong for whichever real rate it excluded.
AMBIGUOUS_BUT_VALID: tuple[tuple[str, str], ...] = (("100.00", "0.21"),)
