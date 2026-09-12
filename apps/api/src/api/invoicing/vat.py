"""VAT on a sales invoice - FR-AR-001, FR-AR-002.

--- This is the OPPOSITE direction from an expense ---

`api.expenses.vat` splits a gross amount that is printed on a receipt. A sales
invoice runs the other way: the seller knows the price excluding VAT, and the
VAT is added on top.

    line net  = round(quantity x unit_price x (1 - discount/100))
    VAT       = round(sum of net for a treatment x rate / 100)
    gross     = net + VAT

Sharing a module with the expense side would have meant one function with a
direction flag, and the flag would eventually be wrong somewhere. The two
share `round_money` - the rounding RULE is one decision - and nothing else.

--- VAT is computed per TREATMENT GROUP, never per line ---

This is the decision with money in it.

Rounding each line's VAT and adding the results up can differ, by cents, from
rounding the VAT on the summed net. Twelve lines of EUR 0.05 at 21% are twelve
lots of 0.0105: rounded per line that is 12 x 0.01 = 0.12, and rounded on the
total it is round(0.63) = 0.13. Neither is a rounding error in the arithmetic
sense; they are different answers to different questions.

EU VAT Directive Art. 226 settles which question an invoice asks. An invoice
must show, per rate, the taxable amount and the VAT on it - so the VAT the
customer is charged is the VAT on the group, and that is the figure this module
produces. It is also the figure that reaches the aangifte (FR-VAT-001), so a
per-line total would put one number on the document and a different one on the
return.

The consequence is worth stating plainly: a line has a net amount, and it does
NOT have a VAT amount. Nothing here returns one, so nothing downstream can add
them up and get a different total from the invoice.

--- Zero is not one thing ---

Five of FR-AR-002's eight treatments produce no VAT for the seller, and they
are not interchangeable:

    zero (btw_0)          a rate that is 0%. Taxable, at nothing.
    exempt                outside the VAT system. Not taxable at all.
    reverse_charge        taxable, but the BUYER accounts for it.
    intra_community       taxable in the buyer's member state.
    export                outside the EU.

They produce the same number and different legal wording (see wording.py),
different rubrieken on the return, and different statutory obligations on the
invoice - an intra-Community supply must carry the customer's VAT number, an
exempt supply need not. Collapsing them into "rate 0" is what makes a return
wrong in a way the ledger cannot see, so the treatment travels with the amount
everywhere in this module.

--- The margin scheme shows no VAT at all ---

Under the margeregeling the seller accounts for VAT on the MARGIN, not on the
sale price, and it is unlawful to state that VAT separately on the invoice. So
a margin group carries a taxable amount and a VAT amount of zero, and
`rate` is None rather than 0 - there is no rate on the sale price to state,
which is a different fact from a rate that happens to be nought.

This is also why `api.expenses.model.VatTreatment` deliberately excludes
`btw_marge` and this module includes it. On the PURCHASE side, extracting VAT
from a gross receipt under the margin scheme would compute the tax on the whole
amount and overstate it. On the SALES side, the scheme is the seller's own and
the invoice simply shows no VAT. Same code, opposite correct handling, which is
why neither module reaches into the other.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from api.expenses.vat import MONEY, VatError, round_money
from api.vat.rules import TreatmentRole

__all__ = [
    "InvoiceLineAmounts",
    "VatGroup",
    "InvoiceTotals",
    "line_net",
    "group_vat",
    "totals_for",
    "PRODUCES_NO_OUTPUT_VAT",
    "SHOWS_NO_VAT_AT_ALL",
]

#: Treatments where the seller charges no VAT. Same number, five different
#: reasons - see the module docstring.
PRODUCES_NO_OUTPUT_VAT: frozenset[TreatmentRole] = frozenset(
    {
        TreatmentRole.ZERO,
        TreatmentRole.EXEMPT,
        TreatmentRole.REVERSE_CHARGE,
        TreatmentRole.INTRA_COMMUNITY,
        TreatmentRole.EXPORT,
        TreatmentRole.MARGIN,
    }
)

#: The one treatment where stating a rate at all would be wrong.
SHOWS_NO_VAT_AT_ALL: frozenset[TreatmentRole] = frozenset({TreatmentRole.MARGIN})


@dataclass(frozen=True, slots=True)
class InvoiceLineAmounts:
    """One line's contribution. Deliberately carries no VAT amount."""

    treatment: str
    role: TreatmentRole
    net: Decimal


@dataclass(frozen=True, slots=True)
class VatGroup:
    """One treatment's block on the invoice - Art. 226's "per rate"."""

    treatment: str
    role: TreatmentRole
    #: None for the margin scheme, where there is no rate on the sale price to
    #: state. Never conflated with Decimal("0"), which is btw_0's answer.
    rate: Decimal | None
    taxable: Decimal
    vat: Decimal

    def __post_init__(self) -> None:
        if self.role in SHOWS_NO_VAT_AT_ALL and self.rate is not None:
            raise VatError(
                f"{self.treatment} states a rate ({self.rate}) on the sale price. "
                f"Under the margin scheme VAT is due on the margin and stating it "
                f"on the invoice is not permitted (FR-AR-002)."
            )
        if self.role in PRODUCES_NO_OUTPUT_VAT and self.vat != 0:
            raise VatError(
                f"{self.treatment} produced VAT of {self.vat}; this treatment "
                f"charges the customer none (FR-AR-002)."
            )


@dataclass(frozen=True, slots=True)
class InvoiceTotals:
    """What the document shows at the bottom."""

    groups: tuple[VatGroup, ...]
    net: Decimal
    vat: Decimal
    gross: Decimal

    def __post_init__(self) -> None:
        # The identity the whole module exists to preserve, asserted on the way
        # out - the device api.expenses.vat.VatSplit uses. A future change that
        # rounded the total independently of the groups becomes a failure here
        # rather than an invoice whose columns do not add up.
        if self.net + self.vat != self.gross:
            raise VatError(
                f"net {self.net} + VAT {self.vat} is {self.net + self.vat}, not the "
                f"gross {self.gross}"
            )
        if sum((group.vat for group in self.groups), Decimal(0)) != self.vat:
            raise VatError("the VAT groups do not sum to the invoice VAT total")
        if sum((group.taxable for group in self.groups), Decimal(0)) != self.net:
            raise VatError("the VAT groups do not sum to the invoice net total")


def _numeric(value: object, name: str) -> Decimal:
    """A Decimal, or a refusal. Never a conversion from a float.

    NFR-031 and CLAUDE.md rule four. The same tripwire `api.expenses.vat` puts
    at the form's door, put at the invoice's - a float has already lost the
    precision, and converting it here would launder the loss into a figure that
    looks exact.
    """
    if isinstance(value, float):
        raise VatError(
            f"{name} arrived as a float ({value!r}). Monetary and quantity values "
            f"are Decimal everywhere in the calculation path (NFR-031)."
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except ArithmeticError as exc:
            raise VatError(f"{name} is not a decimal number: {value!r}") from exc
    raise VatError(f"{name} must be a Decimal, got {type(value).__name__}")


def line_net(
    *, quantity: Decimal, unit_price: Decimal, discount_percent: Decimal = Decimal(0)
) -> Decimal:
    """FR-AR-001's quantity, unit price and discount, as one line total.

    Rounded to the cent HERE, once, because this is the figure the invoice
    prints on that line and the customer adds up by hand. Carrying the
    unrounded product forward into the group total instead would make the
    printed lines disagree with the printed subtotal by a cent, which is the
    single most common reason somebody telephones about an invoice.

    Mirrors `line_net` in migration 0037, which computes the same expression as
    a generated column - so the database and this module cannot drift, and
    tests/invoicing/line_cases.py runs one table against both.
    """
    quantity_amount = _numeric(quantity, "quantity")
    price = _numeric(unit_price, "unit price")
    discount = _numeric(discount_percent, "discount percentage")

    if not (Decimal(0) <= discount <= Decimal(100)):
        raise VatError(
            f"a discount is a percentage between 0 and 100, not {discount}. 10 means 10%, not 0.10."
        )

    return round_money(quantity_amount * price * (Decimal(1) - discount / Decimal(100)))


def group_vat(*, role: TreatmentRole, taxable: Decimal, rate: Decimal | None) -> Decimal:
    """The VAT on one treatment's taxable total.

    Refuses rather than guesses when a rate-bearing treatment has no rate:
    computing zero for a date the ruleset does not cover would put a wrong
    figure on an invoice AND on a return, with nothing to show it was wrong.
    That is `TreatmentRule.vat_on`'s posture, kept here because this function
    is reachable without one.
    """
    amount = _numeric(taxable, "taxable amount")

    if role in PRODUCES_NO_OUTPUT_VAT:
        return round_money(Decimal(0))

    if rate is None:
        raise VatError(
            f"no VAT rate is defined for a {role.value} supply on the invoice date; "
            f"the ruleset does not cover it (CMP-014)"
        )
    return round_money(amount * _numeric(rate, "VAT rate") / Decimal(100))


def totals_for(
    lines: Iterable[InvoiceLineAmounts],
    *,
    rate_for: dict[str, Decimal | None],
) -> InvoiceTotals:
    """Group the lines by treatment and compute what the invoice shows.

    `rate_for` is the rate that applied ON THE INVOICE DATE for each treatment,
    resolved by the caller through `VatRulesService` and passed in. Passed
    rather than looked up, so this function is pure: the arithmetic that decides
    what a customer is charged is testable without a database, which is what
    lets tests/invoicing/vat_cases.py be a table of worked examples.

    Groups are ordered by treatment code, so an invoice rendered twice puts its
    VAT blocks in the same order both times.
    """
    by_treatment: dict[str, list[InvoiceLineAmounts]] = {}
    for line in lines:
        by_treatment.setdefault(line.treatment, []).append(line)

    groups: list[VatGroup] = []
    for treatment in sorted(by_treatment):
        members = by_treatment[treatment]
        role = members[0].role
        if any(member.role is not role for member in members):
            # One code, two roles, is a corrupt lookup rather than a user
            # error - and it would put two different legal statements under one
            # heading on the document.
            raise VatError(f"{treatment} was given more than one role on one invoice")

        taxable = sum((member.net for member in members), Decimal(0))
        rate = None if role in SHOWS_NO_VAT_AT_ALL else rate_for.get(treatment)
        groups.append(
            VatGroup(
                treatment=treatment,
                role=role,
                rate=rate,
                taxable=taxable,
                vat=group_vat(role=role, taxable=taxable, rate=rate),
            )
        )

    net = sum((group.taxable for group in groups), Decimal(0))
    vat = sum((group.vat for group in groups), Decimal(0))
    return InvoiceTotals(
        groups=tuple(groups),
        net=net.quantize(MONEY),
        vat=vat.quantize(MONEY),
        gross=(net + vat).quantize(MONEY),
    )


def treatments_on(lines: Sequence[InvoiceLineAmounts]) -> frozenset[TreatmentRole]:
    """Which roles appear, for the statutory checks that depend on them."""
    return frozenset(line.role for line in lines)
