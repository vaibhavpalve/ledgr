"""The BTW return (aangifte omzetbelasting), computed from the ledger: FR-VAT-001/002/011.

    FR-VAT-001  VAT return preparation for monthly, quarterly and annual filers, mapped to the
                official rubrieken (1a-5b).
    FR-VAT-002  Pre-filing validation: unposted documents in period, unreconciled bank items,
                unbalanced VAT control accounts, prior-period movements - each blocking or
                warning.
    FR-VAT-011  Every figure in a filed return is drillable to the underlying postings and
                documents, permanently.

Pure: no database, no clock. `api.vat_returns.service` reads the ledger and the rules and hands
them here; everything that decides a figure is in this file and tested as a table.

--- What a line contributes, and where ---

Migration 0035 put the VAT treatment on every journal line that has one, so the return reads the
LEDGER - the one place every source (sales invoice, expense, manual journal) has already agreed
on - rather than joining back to whatever produced each entry. Which box a line lands in is
decided by two things: its treatment (through the effective-dated rules, ADR-027) and what the
line's account IS:

    revenue account          the sale's base            -> the treatment's turnover rubriek
    output-VAT account       VAT charged                -> the treatment's VAT rubriek
    input-VAT account        VAT paid, deductible       -> 5b
    any other account        a purchase's base          -> only a reverse-charged purchase
                                                           reports it (2a / 4a / 4b)

The account's role is resolved by the repository from the posting configuration
(`sales_posting_account.vat_output`, `expense_posting_account.vat_input`) and the RGS codes of
the two VAT accounts - never from the account's name.

--- Reverse-charged purchases ---

A purchase where the VAT is shifted to the buyer carries no VAT on the invoice, and the buyer
self-assesses it: the base and the VAT go in the box for where it came from, and the same VAT is
deducted in 5b. The shipped ruleset maps treatments for SALES only, so the purchase-side boxes are
this module's own table (`PURCHASE_BOX`), keyed by treatment ROLE:

    reverse_charge   (btw_verlegd on a purchase)   2a   domestic reverse charge received
    intra_community  (btw_icp on a purchase)       4b   goods and services from EU countries
    export           (btw_export on a purchase)    4a   goods and services from outside the EU

The VAT is self-assessed at the standard rate in force on the period's end date, and assumed fully
deductible. ADR-087 records all three as needing an accountant's confirmation for a business with
mixed or exempt activity, where deduction is partial.

--- Exempt supplies are not reported ---

The shipped mapping sends `btw_vrijgesteld` to 1e, but the Belastingdienst's instructions for the
aangifte say exempt supplies are not entered at all, and 1e is for 0%-rated and reverse-charged
sales. The mapping cannot express "not on the return" (turnover_rubriek is NOT NULL), so the
builder leaves exempt turnover out and reports it as its own figure (`exempt_turnover`), where a
reader can see it was not lost.

--- Rounding ---

The return is filed in whole euros. The Belastingdienst lets a filer round in their own favour:
turnover and VAT due DOWN, deductible VAT UP. Every box keeps its exact figure beside the rounded
one, 5a is the sum of the ROUNDED VAT boxes (as the Belastingdienst's own form adds them), and the
drill-down reconciles to the exact figures - so nothing a reviewer sees is unexplained.

--- The margin scheme ---

A margin-scheme sale reports the MARGIN, which is not on the invoice. A return cannot be computed
for a period containing one; it is a blocking check, the same "say it, do not guess" posture
ADR-069 takes in the invoice preview.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from api.vat.rules import EffectiveRules, RubriekKind, TreatmentRole, TreatmentRule

ZERO = Decimal("0.00")
CENT = Decimal("0.01")
EURO = Decimal("1")

#: Where a reverse-charged PURCHASE reports, by treatment role. See the module docstring.
PURCHASE_BOX: dict[TreatmentRole, str] = {
    TreatmentRole.REVERSE_CHARGE: "2a",
    TreatmentRole.INTRA_COMMUNITY: "4b",
    TreatmentRole.EXPORT: "4a",
}

#: The boxes of the form, in the form's own order. A box absent from the rules on the period's
#: date is left out rather than invented.
FORM_ORDER: tuple[str, ...] = (
    "1a",
    "1b",
    "1c",
    "1d",
    "1e",
    "2a",
    "3a",
    "3b",
    "3c",
    "4a",
    "4b",
    "5a",
    "5b",
)

#: Boxes with a VAT column. The rest are turnover only (1e, 3a-3c) or VAT only (5a, 5b).
VAT_COLUMN: frozenset[str] = frozenset({"1a", "1b", "1c", "1d", "2a", "4a", "4b", "5a", "5b"})
TURNOVER_COLUMN: frozenset[str] = frozenset(
    {"1a", "1b", "1c", "1d", "1e", "2a", "3a", "3b", "3c", "4a", "4b"}
)


class AccountRole(enum.Enum):
    """What a line's account is, for the return. Resolved by the repository."""

    REVENUE = "revenue"
    VAT_OUTPUT = "vat_output"
    VAT_INPUT = "vat_input"
    OTHER = "other"


class Severity(enum.Enum):
    BLOCKING = "blocking"
    WARNING = "warning"


class CheckCode(enum.Enum):
    """FR-VAT-002's checks. The code is also the i18n key suffix (`vat.check.<code>`)."""

    PERIOD_NOT_ENDED = "period_not_ended"
    UNPLACED_TREATMENT = "unplaced_treatment"
    PROVISIONAL_RULESET = "provisional_ruleset"
    UNPOSTED_PURCHASES = "unposted_purchases"
    DRAFT_SALES_INVOICES = "draft_sales_invoices"
    UNRECONCILED_BANK = "unreconciled_bank"
    UNTAGGED_VAT_POSTINGS = "untagged_vat_postings"
    EARLIER_PERIOD_UNFILED = "earlier_period_unfiled"


#: The severity of each check. Blocking means the return cannot be filed as it stands; a warning
#: must be acknowledged by the person filing. Decided here, once, rather than at each call site.
SEVERITY: dict[CheckCode, Severity] = {
    CheckCode.PERIOD_NOT_ENDED: Severity.BLOCKING,
    CheckCode.UNPLACED_TREATMENT: Severity.BLOCKING,
    CheckCode.PROVISIONAL_RULESET: Severity.WARNING,
    CheckCode.UNPOSTED_PURCHASES: Severity.WARNING,
    CheckCode.DRAFT_SALES_INVOICES: Severity.WARNING,
    CheckCode.UNRECONCILED_BANK: Severity.WARNING,
    CheckCode.UNTAGGED_VAT_POSTINGS: Severity.WARNING,
    CheckCode.EARLIER_PERIOD_UNFILED: Severity.WARNING,
}


@dataclass(frozen=True, slots=True)
class VatLineTotal:
    """The ledger's lines for one period, summed by treatment and account role."""

    vat_treatment: str
    role: AccountRole
    debit: Decimal
    credit: Decimal


@dataclass(frozen=True, slots=True)
class BoxLine:
    """One journal line behind a box: FR-VAT-011's drill-down row."""

    entry_id: uuid.UUID
    entry_number: int
    entry_date: date
    description: str
    document_reference: str | None
    source_system: str | None
    account_code: str
    account_name: str
    vat_treatment: str
    role: AccountRole
    #: What this line contributes to the box, signed as the box reads it.
    amount: Decimal
    #: `turnover` or `vat` - which column of the box this line feeds.
    column: str


@dataclass(frozen=True, slots=True)
class Box:
    code: str
    kind: RubriekKind
    description_nl: str
    description_en: str | None
    #: None for a box with no such column - not the same statement as a column of zero.
    turnover: Decimal | None
    turnover_rounded: Decimal | None
    vat: Decimal | None
    vat_rounded: Decimal | None
    treatments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Check:
    code: CheckCode
    severity: Severity
    count: int | None = None
    amount: Decimal | None = None
    detail: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PeriodFacts:
    """What the service knows about the world around the period, for the checks."""

    today: date
    unposted_purchases: int = 0
    draft_sales_invoices: int = 0
    unreconciled_bank: int = 0
    #: Sum of |debit - credit| of lines on a VAT account with no treatment.
    untagged_vat_amount: Decimal = ZERO
    untagged_vat_lines: int = 0
    #: Period numbers of earlier periods with VAT postings that are not filed yet.
    earlier_unfiled: tuple[str, ...] = ()
    ruleset_provisional: bool = False


@dataclass(frozen=True, slots=True)
class VatReturn:
    period_start: date
    period_end: date
    boxes: tuple[Box, ...]
    #: 5a, 5b and 5c on the rounded figures; `total_due` negative is a refund.
    output_vat: Decimal
    input_vat: Decimal
    total_due: Decimal
    exempt_turnover: Decimal
    rules_fingerprint: str
    checks: tuple[Check, ...] = field(default_factory=tuple)

    @property
    def blocking(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.severity is Severity.BLOCKING)

    @property
    def warnings(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.severity is Severity.WARNING)

    @property
    def can_be_filed(self) -> bool:
        return not self.blocking

    def box(self, code: str) -> Box | None:
        return next((b for b in self.boxes if b.code == code), None)


class VatReturnError(Exception):
    pass


def round_down(amount: Decimal) -> Decimal:
    """Whole euros, down - carried at two decimals so a rounded figure reads the same whether it
    was just computed or comes back from vat_return's numeric(19, 2)."""
    return amount.quantize(EURO, rounding=ROUND_FLOOR).quantize(CENT)


def round_up(amount: Decimal) -> Decimal:
    return amount.quantize(EURO, rounding=ROUND_CEILING).quantize(CENT)


def _signed(role: AccountRole, debit: Decimal, credit: Decimal) -> Decimal:
    """The line's contribution in the direction the box reads it: a sale and the VAT charged on
    it are credits, a purchase and the VAT deducted on it are debits. A credit note or a reversal
    therefore reduces its box rather than being lost."""
    if role in (AccountRole.REVENUE, AccountRole.VAT_OUTPUT):
        return credit - debit
    return debit - credit


def _standard_rate(rules: EffectiveRules) -> Decimal:
    rule = rules.by_role(TreatmentRole.STANDARD)
    if rule.rate is None:
        raise VatReturnError("the ruleset has no standard rate on the period's end date")
    return rule.rate


def placement(rule: TreatmentRule, role: AccountRole) -> tuple[str | None, str] | None:
    """Where one (treatment, account role) pair lands: (box, column).

    Returns None when the pair does not reach the return (a purchase's base under a normal
    treatment, exempt turnover), and (None, reason) when it SHOULD reach it but cannot be placed.
    """
    if rule.role is TreatmentRole.MARGIN and role in (AccountRole.REVENUE, AccountRole.VAT_OUTPUT):
        return (None, "margin")
    if role is AccountRole.REVENUE:
        if rule.role is TreatmentRole.EXEMPT:
            return None
        if rule.turnover_rubriek is None:
            return (None, "unmapped")
        return (rule.turnover_rubriek, "turnover")
    if role is AccountRole.VAT_OUTPUT:
        if rule.vat_rubriek is None:
            return (None, "unmapped")
        return (rule.vat_rubriek, "vat")
    if role is AccountRole.VAT_INPUT:
        return ("5b", "vat")
    # OTHER: a purchase's base. Reported only when the VAT is reverse-charged to the buyer.
    box = PURCHASE_BOX.get(rule.role)
    return (box, "turnover") if box is not None else None


def build_return(
    *,
    period_start: date,
    period_end: date,
    totals: Iterable[VatLineTotal],
    rules: EffectiveRules,
    facts: PeriodFacts,
) -> VatReturn:
    """FR-VAT-001: the boxes, 5a-5c and the checks for one period."""
    turnover: dict[str, Decimal] = {}
    vat: dict[str, Decimal] = {}
    treatments_in: dict[str, set[str]] = {}
    exempt = ZERO
    unplaced: set[str] = set()
    self_assessed_base: dict[str, Decimal] = {}

    rules_by_code = {rule.code: rule for rule in rules.treatments}
    for line in totals:
        rule = rules_by_code.get(line.vat_treatment)
        amount = _signed(line.role, line.debit, line.credit)
        if rule is None:
            if amount != ZERO:
                unplaced.add(line.vat_treatment)
            continue
        if rule.role is TreatmentRole.EXEMPT and line.role is AccountRole.REVENUE:
            exempt += amount
            continue
        where = placement(rule, line.role)
        if where is None:
            continue
        box, column = where
        if box is None:
            if amount != ZERO:
                unplaced.add(line.vat_treatment)
            continue
        target = turnover if column == "turnover" else vat
        target[box] = target.get(box, ZERO) + amount
        treatments_in.setdefault(box, set()).add(line.vat_treatment)
        if line.role is AccountRole.OTHER:
            self_assessed_base[box] = self_assessed_base.get(box, ZERO) + amount

    # Reverse-charged purchases: the buyer's own VAT on the base, due in the box AND deductible.
    if self_assessed_base:
        rate = _standard_rate(rules)
        for box, base in self_assessed_base.items():
            charged = (base * rate / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)
            vat[box] = vat.get(box, ZERO) + charged
            vat["5b"] = vat.get("5b", ZERO) + charged

    known = {rubriek.code: rubriek for rubriek in rules.rubrieken}
    boxes: list[Box] = []
    output_rounded = ZERO
    for code in FORM_ORDER:
        rubriek = known.get(code)
        if rubriek is None or code == "5a":
            continue
        has_turnover = code in TURNOVER_COLUMN
        has_vat = code in VAT_COLUMN
        exact_turnover = turnover.get(code, ZERO) if has_turnover else None
        exact_vat = vat.get(code, ZERO) if has_vat else None
        if code == "5b":
            vat_rounded = round_up(exact_vat) if exact_vat is not None else None
        else:
            vat_rounded = round_down(exact_vat) if exact_vat is not None else None
            if vat_rounded is not None:
                output_rounded += vat_rounded
        boxes.append(
            Box(
                code=code,
                kind=rubriek.kind,
                description_nl=rubriek.description_nl,
                description_en=rubriek.description_en,
                turnover=exact_turnover,
                turnover_rounded=(
                    round_down(exact_turnover) if exact_turnover is not None else None
                ),
                vat=exact_vat,
                vat_rounded=vat_rounded,
                treatments=tuple(sorted(treatments_in.get(code, ()))),
            )
        )

    input_exact = vat.get("5b", ZERO)
    input_rounded = round_up(input_exact)
    output_exact = sum(
        (vat.get(code, ZERO) for code in FORM_ORDER if code in VAT_COLUMN and code[0] != "5"),
        ZERO,
    )
    if "5a" in known:
        rubriek = known["5a"]
        boxes.insert(
            next((i for i, b in enumerate(boxes) if b.code == "5b"), len(boxes)),
            Box(
                code="5a",
                kind=rubriek.kind,
                description_nl=rubriek.description_nl,
                description_en=rubriek.description_en,
                turnover=None,
                turnover_rounded=None,
                vat=output_exact,
                vat_rounded=output_rounded,
            ),
        )

    return VatReturn(
        period_start=period_start,
        period_end=period_end,
        boxes=tuple(boxes),
        output_vat=output_rounded,
        input_vat=input_rounded,
        total_due=output_rounded - input_rounded,
        exempt_turnover=exempt,
        rules_fingerprint=rules.fingerprint,
        checks=evaluate_checks(period_end=period_end, facts=facts, unplaced=sorted(unplaced)),
    )


def evaluate_checks(
    *, period_end: date, facts: PeriodFacts, unplaced: Sequence[str] = ()
) -> tuple[Check, ...]:
    """FR-VAT-002. Only the checks that fire are returned; an empty tuple is a clean period."""
    checks: list[Check] = []

    def add(
        code: CheckCode,
        *,
        count: int | None = None,
        amount: Decimal | None = None,
        detail: tuple[str, ...] = (),
    ) -> None:
        checks.append(
            Check(code=code, severity=SEVERITY[code], count=count, amount=amount, detail=detail)
        )

    if facts.today <= period_end:
        add(CheckCode.PERIOD_NOT_ENDED)
    if unplaced:
        add(CheckCode.UNPLACED_TREATMENT, detail=tuple(unplaced))
    if facts.ruleset_provisional:
        add(CheckCode.PROVISIONAL_RULESET)
    if facts.unposted_purchases:
        add(CheckCode.UNPOSTED_PURCHASES, count=facts.unposted_purchases)
    if facts.draft_sales_invoices:
        add(CheckCode.DRAFT_SALES_INVOICES, count=facts.draft_sales_invoices)
    if facts.unreconciled_bank:
        add(CheckCode.UNRECONCILED_BANK, count=facts.unreconciled_bank)
    if facts.untagged_vat_lines:
        add(
            CheckCode.UNTAGGED_VAT_POSTINGS,
            count=facts.untagged_vat_lines,
            amount=facts.untagged_vat_amount,
        )
    if facts.earlier_unfiled:
        add(
            CheckCode.EARLIER_PERIOD_UNFILED,
            count=len(facts.earlier_unfiled),
            detail=facts.earlier_unfiled,
        )
    return tuple(checks)


def box_lines_for(
    code: str, lines: Iterable[BoxLine], rules: EffectiveRules
) -> tuple[BoxLine, ...]:
    """FR-VAT-011: the journal lines behind one box, each with the amount it contributes.

    `lines` are raw journal lines with `amount` unset (zero) and `column` empty; this decides,
    with the same `placement` the totals use, which of them feed `code` and with what. The
    self-assessed VAT of a reverse-charged purchase has no journal line of its own; the base
    line that caused it is returned under the box's turnover column, and 5b lists it too.
    """
    rules_by_code = {rule.code: rule for rule in rules.treatments}
    matched: list[BoxLine] = []
    for line in lines:
        rule = rules_by_code.get(line.vat_treatment)
        if rule is None:
            continue
        signed = line.amount
        where = placement(rule, line.role)
        if where is None or where[0] is None:
            continue
        box, column = where
        if (
            code == "5a"
            and box is not None
            and box[0] != "5"
            and (column == "vat" or line.role is AccountRole.OTHER)
        ):
            # 5a is every VAT box above it - including the self-assessed VAT a
            # reverse-charged purchase's base line gives rise to.
            matched.append(_with(line, signed, "vat" if column == "vat" else "base"))
        elif box == code:
            matched.append(_with(line, signed, column))
        elif code == "5b" and line.role is AccountRole.OTHER:
            # The deductible half of a reverse-charged purchase.
            matched.append(_with(line, signed, "base"))
    return tuple(matched)


def _with(line: BoxLine, amount: Decimal, column: str) -> BoxLine:
    return BoxLine(
        entry_id=line.entry_id,
        entry_number=line.entry_number,
        entry_date=line.entry_date,
        description=line.description,
        document_reference=line.document_reference,
        source_system=line.source_system,
        account_code=line.account_code,
        account_name=line.account_name,
        vat_treatment=line.vat_treatment,
        role=line.role,
        amount=amount,
        column=column,
    )


def signed_amount(role: AccountRole, debit: Decimal, credit: Decimal) -> Decimal:
    """Public for the repository, which builds `BoxLine`s with the signed amount."""
    return _signed(role, debit, credit)
