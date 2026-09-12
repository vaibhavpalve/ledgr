"""VAT and net, from a gross amount - FR-EXP-001b.

    FR-EXP-001b  The expense form asks for the minimum - date, supplier, gross
                 amount, VAT rate, category - with VAT AND NET CALCULATED
                 AUTOMATICALLY ...

--- Why gross is the input ---

A receipt shows what was paid. That is the gross, and it is the only figure a
person can read off the paper without doing arithmetic - so it is the field the
form asks for, and net and VAT are derived from it. Asking for net would make
somebody compute backwards from the total in their hand, which is the
calculation this requirement exists to remove.

--- One is rounded; the other is subtracted ---

    vat = round(gross x rate / (100 + rate))
    net = gross - vat

Deliberately NOT both rounded independently. `round(gross/(1+r))` and
`round(gross x r/(1+r))` computed separately can differ from `gross` by a cent,
and that cent is a ledger that does not balance (FR-GL-001) - a debit of net
plus VAT against a credit of gross. Subtracting makes `net + vat == gross` an
identity rather than something that usually holds.

VAT is the one that gets rounded rather than net, because the VAT figure is
what is reported on the aangifte (FR-VAT-001's rubrieken 1a-5b). The rounded
number should be the one filed; the residue belongs in net, where nobody files
it.

--- The rounding mode is chosen, not inherited ---

ROUND_HALF_UP. Python's default decimal context is ROUND_HALF_EVEN - banker's
rounding - so `quantize()` without an explicit mode would round 0.025 to 0.02.
That is not how a Dutch tax figure is rounded, and it is not how anybody
checking the sum by hand would round it. The mode is stated at every call site
in this module for that reason: inheriting it from a context somebody else can
change is how a rounding rule silently moves.

`expenses.vat_from_gross()` in migration 0033 computes the same thing with
Postgres `round(numeric, 2)`, which rounds half away from zero and therefore
agrees. tests/expenses/vat_cases.py runs one table against both, the device
0029 and 0031 already use for period derivation and retention.

--- Float never appears ---

NFR-031 and CLAUDE.md rule four. `gross` and `rate` are Decimal, a float
argument is REFUSED rather than converted, and the intermediate division stays
in Decimal. A float that reaches this boundary has already lost the precision -
which is exactly `api.ledger.model`'s argument, applied to the first
multiplication rather than to the posting.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

#: The scale every monetary figure carries, matching journal_line's
#: numeric(19,2) in migration 0020 and `api.ledger.model.SCALE`.
MONEY = Decimal("0.01")

#: Not the decimal module's default. See the module docstring.
ROUNDING = ROUND_HALF_UP

#: A rate is a percentage: 21, not 0.21. Mirrors `vat_rate.rate` in migration
#: 0028, which says so in its own comment.
MIN_RATE = Decimal("0")
MAX_RATE = Decimal("100")


class VatError(ValueError):
    """The amounts cannot be split."""


@dataclass(frozen=True, slots=True)
class VatSplit:
    """What the form shows once a gross amount and a rate are known.

    Carries all three rather than two, so a caller never has to re-derive the
    one it did not ask for - and so `net + vat == gross` is visible in the
    value rather than being a fact about how it was made.
    """

    gross: Decimal
    net: Decimal
    vat: Decimal
    rate: Decimal

    def __post_init__(self) -> None:
        # The identity the whole module exists to preserve, asserted on the way
        # out. Cheap, and it turns a future refactor that rounds both halves
        # independently into a failure here rather than into a ledger that does
        # not balance.
        if self.net + self.vat != self.gross:
            raise VatError(
                f"net {self.net} + VAT {self.vat} is {self.net + self.vat}, not the "
                f"gross {self.gross}. The two must sum exactly (FR-GL-001)."
            )


def _decimal(value: object, name: str) -> Decimal:
    """A Decimal, or a refusal. Never a conversion from float.

    Accepting a float here would launder a value that has already lost
    precision into a calculation that looks exact - the tripwire
    `api.ledger.model` puts at the ledger's door, put at the form's.
    """
    if isinstance(value, float):
        raise VatError(
            f"{name} arrived as a float ({value!r}). Monetary values are Decimal "
            f"everywhere in the calculation path (NFR-031); a float has already "
            f"lost the precision this refuses to pretend it still has."
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise VatError(f"{name} is not a decimal number: {value!r}") from exc
    raise VatError(f"{name} must be a Decimal, got {type(value).__name__}")


def round_money(value: Decimal) -> Decimal:
    """To the cent, half away from zero.

    Exported and named rather than inlined, because "which way does this round"
    is the question somebody will ask of a figure that is one cent off, and it
    should have one answer they can read.
    """
    return value.quantize(MONEY, rounding=ROUNDING)


def vat_from_gross(gross: Decimal, rate: Decimal) -> Decimal:
    """The VAT contained in a gross amount at `rate` percent.

    `gross x rate / (100 + rate)`, rounded to the cent. At 21%, a gross of
    121.00 contains exactly 21.00; a gross of 100.00 contains 17.36.
    """
    gross, rate = _validate(gross, rate)
    if rate == 0:
        # Not an optimisation - it keeps a zero-rated treatment from depending
        # on the division at all. btw_0, btw_vrijgesteld, btw_verlegd, btw_icp
        # and btw_export all arrive here (migration 0028's shipped ruleset).
        return round_money(Decimal(0))
    return round_money(gross * rate / (Decimal(100) + rate))


def split_gross(gross: Decimal, rate: Decimal) -> VatSplit:
    """FR-EXP-001b's "VAT and net calculated automatically", in one value."""
    gross, rate = _validate(gross, rate)
    vat = vat_from_gross(gross, rate)
    return VatSplit(gross=gross, net=gross - vat, vat=vat, rate=rate)


def _validate(gross: object, rate: object) -> tuple[Decimal, Decimal]:
    gross_amount = _decimal(gross, "gross amount")
    rate_percent = _decimal(rate, "VAT rate")

    if gross_amount < 0:
        # A negative expense is a credit note, which is its own thing with its
        # own posting - not a claim with a minus sign in front of it.
        raise VatError(
            f"a gross amount is not negative ({gross_amount}). A refund or credit "
            f"note is a separate document, not an expense with a negative total."
        )
    if gross_amount != round_money(gross_amount):
        # numeric(19,2) would silently round this on the way in, and the
        # difference would land in net without anybody choosing it.
        raise VatError(
            f"a gross amount has at most two decimals ({gross_amount}); this one "
            f"would be rounded on storage and the difference would appear in net."
        )
    if not (MIN_RATE <= rate_percent <= MAX_RATE):
        raise VatError(
            f"a VAT rate is a percentage between {MIN_RATE} and {MAX_RATE}, not "
            f"{rate_percent}. 21 means 21%, not 0.21 (migration 0028)."
        )
    return gross_amount, rate_percent
