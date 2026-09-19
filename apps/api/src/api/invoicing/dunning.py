"""SI-04: the reminder ladder - FR-AR-010's rules, with no database and no email.

    FR-AR-010  Dunning: configurable reminder ladder with escalation, statutory
               interest and collection cost calculation, and pause-per-customer.

Everything that DECIDES is here and pure: which step an invoice is due for, what
statutory interest has accrued, what collection cost the law allows, and why
nothing should be sent. The service supplies the facts (what is outstanding, what
has been sent, which rates are loaded); this module never asks for any.

--- Two things in this file are LEGAL DATA, not engineering ---

`WIK_TIERS` and `FORMAL_NOTICE_DEADLINE_DAYS` encode Dutch law as the author
understands it. They are constants, named, with their source, and they are the
first things a tax or legal adviser should read - exactly the posture
`api.invoicing.wording` takes about its own provisional statements. Nothing about
the RATE of statutory interest is here at all: that changes every half year, is
published by the government, and lives in `statutory_interest_rate` (migration
0053), loaded by an operator. An empty table means interest cannot be computed,
and the ladder REFUSES rather than guesses: a formal notice with an invented rate
is a demand for money nobody is owed.

--- Escalation is a sequence, never a jump ---

An invoice 40 days overdue that nobody has chased does not receive the formal
notice first. It receives step 1, then step 2 when the ladder says it is due, and
so on: a customer's first contact about a debt is never a demand with costs. The
next step is always the lowest one not yet sent (`next_step`).

--- Costs are a consequence of a notice, not a surcharge ---

Collection cost may be claimed only after a formal notice (an "aanmaning", the
"14-dagenbrief") that gives the debtor a last chance to pay. So `charge_collection_
cost` is allowed on exactly one kind of step, `FORMAL_NOTICE`, and at most one step
per ladder - `validate_ladder` refuses anything else, in the ladder a person
configures rather than at send time.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from api.expenses.vat import MONEY, round_money

__all__ = [
    "DEFAULT_LADDER",
    "FORMAL_NOTICE_DEADLINE_DAYS",
    "MAX_STEPS",
    "WIK_TIERS",
    "Blocker",
    "DunningAssessment",
    "InterestRate",
    "InterestRateKind",
    "LadderInvalid",
    "LadderStep",
    "NoInterestRate",
    "StepKind",
    "assess",
    "collection_cost",
    "days_overdue",
    "next_step",
    "statutory_interest",
    "validate_ladder",
]

#: Reminders beyond this many are noise. A ladder is a short escalation, and a
#: fifteen-step one is a schedule for nagging.
MAX_STEPS = 6

#: A formal notice offers this many days to pay before costs are claimed.
#: The statutory minimum is 14 FULL days counted from the day after the notice is
#: RECEIVED, so 15 counted from the day it is SENT is the conservative reading:
#: a deadline that is a day too long costs nothing, and one a day too short can
#: void the cost claim. NEEDS LEGAL REVIEW, like every constant in this block.
FORMAL_NOTICE_DEADLINE_DAYS = 15

#: Besluit vergoeding voor buitengerechtelijke incassokosten (WIK), art. 2:
#: percentages of the PRINCIPAL, each applied to the slice of it between the
#: bounds. The last tier has no upper bound. NEEDS LEGAL REVIEW - transcribed from
#: the author's reading, not from the Staatsblad.
#:
#:   (slice upper bound, percentage of that slice)
WIK_TIERS: tuple[tuple[Decimal | None, Decimal], ...] = (
    (Decimal("2500"), Decimal("15")),
    (Decimal("5000"), Decimal("10")),
    (Decimal("10000"), Decimal("5")),
    (Decimal("200000"), Decimal("1")),
    (None, Decimal("0.5")),
)
#: The minimum charge whatever the percentages give, and the ceiling on the total.
WIK_MINIMUM = Decimal("40")
WIK_MAXIMUM = Decimal("6775")

#: Days in a year for statutory interest. The law counts simple interest per
#: year; a leap year is not treated differently, the convention the published
#: rates assume. NEEDS LEGAL REVIEW.
_DAYS_IN_YEAR = Decimal(365)


class StepKind(enum.Enum):
    """What a step SAYS, which is also what it may claim."""

    #: "Perhaps this slipped through." No claim beyond the invoice itself.
    FRIENDLY = "friendly"
    #: "This is overdue." Still no claim beyond the invoice, but firmer.
    REMINDER = "reminder"
    #: The aanmaning: a last chance to pay, after which costs are charged.
    FORMAL_NOTICE = "formal_notice"


class InterestRateKind(enum.Enum):
    """Which statutory rate applies. Different rates, different rules."""

    #: Handelsrente - between businesses (art. 6:119a BW).
    COMMERCIAL = "commercial"
    #: Wettelijke rente - with a consumer (art. 6:119 BW).
    CONSUMER = "consumer"


class Blocker(enum.Enum):
    """Why nothing should be sent for an invoice right now.

    A value rather than a bare `None`, so a screen can say WHICH: "paused for
    this customer" and "not yet due for step 2" are different sentences with
    different next actions.
    """

    NOTHING_OUTSTANDING = "nothing_outstanding"
    PAUSED = "paused"
    NO_DUE_DATE = "no_due_date"
    NOT_OVERDUE = "not_overdue"
    LADDER_COMPLETE = "ladder_complete"
    STEP_NOT_DUE = "step_not_due"
    #: The next step charges interest and no rate is loaded for the date. Refused
    #: rather than sent without: see the module docstring.
    INTEREST_RATE_MISSING = "interest_rate_missing"


class LadderInvalid(ValueError):
    """A ladder that could not be followed, or that would claim what the law does
    not allow. Refused when configured, not discovered when a customer is mailed."""


class NoInterestRate(Exception):
    """No statutory rate covers the date interest starts accruing."""


@dataclass(frozen=True, slots=True)
class LadderStep:
    position: int
    #: Days after the DUE date on which this step becomes due. 7 means the
    #: reminder is due once the invoice is 7 days overdue.
    days_after_due: int
    kind: StepKind
    charge_interest: bool = False
    charge_collection_cost: bool = False


#: What an administration gets before it configures anything: a friendly nudge, a
#: firmer one, then a formal notice that claims interest and costs. The defaults
#: are a starting point and every number is the administration's to change.
DEFAULT_LADDER: tuple[LadderStep, ...] = (
    LadderStep(1, 7, StepKind.FRIENDLY),
    LadderStep(2, 21, StepKind.REMINDER),
    LadderStep(3, 35, StepKind.FORMAL_NOTICE, charge_interest=True, charge_collection_cost=True),
)


@dataclass(frozen=True, slots=True)
class InterestRate:
    """A statutory rate in force from `valid_from` until the next one, as a
    percentage per year. `Decimal`, never float (NFR-031)."""

    valid_from: date
    rate: Decimal


def validate_ladder(steps: Sequence[LadderStep]) -> tuple[LadderStep, ...]:
    """The ladder, ordered, or `LadderInvalid`.

    An empty ladder is valid and means "this administration does not chase" - a
    real choice, distinct from "has not configured one" (which gets the default).
    """
    ordered = tuple(sorted(steps, key=lambda s: s.position))
    if len(ordered) > MAX_STEPS:
        raise LadderInvalid(f"a ladder has at most {MAX_STEPS} steps")

    for expected, step in enumerate(ordered, start=1):
        if step.position != expected:
            raise LadderInvalid(f"positions must run 1..{len(ordered)} with no gaps")
        if step.days_after_due < 1:
            raise LadderInvalid("a step is due at least one day after the due date")
        if expected > 1 and step.days_after_due <= ordered[expected - 2].days_after_due:
            raise LadderInvalid("each step must fall later than the one before it")

    with_costs = [s for s in ordered if s.charge_collection_cost]
    if any(s.kind is not StepKind.FORMAL_NOTICE for s in with_costs):
        raise LadderInvalid("only a formal notice may charge collection cost")
    if len(with_costs) > 1:
        raise LadderInvalid("collection cost is charged once, on one formal notice")

    notices = [s for s in ordered if s.kind is StepKind.FORMAL_NOTICE]
    if len(notices) > 1:
        raise LadderInvalid("a ladder has at most one formal notice")
    if notices and notices[0] is not ordered[-1]:
        raise LadderInvalid("the formal notice is the last step: nothing escalates past it")
    return ordered


def days_overdue(due_date: date | None, today: date) -> int:
    """Whole days past the due date, 0 when not yet overdue or when there is no
    due date. The due date itself is not overdue: the customer has until the end
    of it."""
    if due_date is None:
        return 0
    return max((today - due_date).days, 0)


def next_step(
    steps: Sequence[LadderStep], sent_positions: frozenset[int], overdue_days: int
) -> tuple[LadderStep | None, Blocker | None]:
    """The step to send now, or why there is none.

    Always the LOWEST step not yet sent - see the module docstring. Returns the
    step and no blocker when it is due, and a blocker when it is not.
    """
    for step in sorted(steps, key=lambda s: s.position):
        if step.position in sent_positions:
            continue
        if overdue_days >= step.days_after_due:
            return step, None
        return step, Blocker.STEP_NOT_DUE
    return None, Blocker.LADDER_COMPLETE


def statutory_interest(
    principal: Decimal,
    due_date: date,
    through: date,
    rates: Sequence[InterestRate],
) -> Decimal:
    """Simple statutory interest accrued from the day AFTER `due_date` up to and
    including `through`, at whichever rate was in force on each day.

    Rates change (twice a year, for the commercial rate), so the period is split
    at every rate change and each slice priced at its own rate - one rate for the
    whole period would over- or under-charge whoever's invoice straddles a change.

    Rounded once, at the end, to the cent (`round_money`), so the total does not
    depend on how many slices there were. Raises `NoInterestRate` when no rate
    covers the first day, rather than treating it as zero: an absent rate is a
    hole in the data, and interest of nothing is a different statement.
    """
    if isinstance(principal, float):
        raise TypeError("principal must be Decimal, not float (NFR-031)")
    if principal <= 0 or through <= due_date:
        return Decimal("0.00")

    ordered = sorted(rates, key=lambda r: r.valid_from)
    first_day = due_date + timedelta(days=1)
    if not ordered or ordered[0].valid_from > first_day:
        raise NoInterestRate(f"no statutory interest rate is loaded for {first_day}")

    total = Decimal(0)
    cursor = first_day
    last_day = through
    while cursor <= last_day:
        current = max((r for r in ordered if r.valid_from <= cursor), key=lambda r: r.valid_from)
        following = min((r.valid_from for r in ordered if r.valid_from > cursor), default=None)
        slice_end = last_day if following is None else min(last_day, following - timedelta(days=1))
        days = Decimal((slice_end - cursor).days + 1)
        total += principal * current.rate / Decimal(100) * days / _DAYS_IN_YEAR
        cursor = slice_end + timedelta(days=1)
    return round_money(total)


def collection_cost(principal: Decimal) -> Decimal:
    """The extrajudicial collection cost the WIK allows on `principal`.

    Tiered percentages of the principal, never below `WIK_MINIMUM` and never above
    `WIK_MAXIMUM`. The principal is the amount OWED, before interest - costs are
    not charged on interest. Zero for a debt of nothing.
    """
    if isinstance(principal, float):
        raise TypeError("principal must be Decimal, not float (NFR-031)")
    if principal <= 0:
        return Decimal("0.00")

    total = Decimal(0)
    lower = Decimal(0)
    for upper, percentage in WIK_TIERS:
        if principal <= lower:
            break
        slice_top = principal if upper is None else min(principal, upper)
        total += (slice_top - lower) * percentage / Decimal(100)
        if upper is None:
            break
        lower = upper
    return min(max(round_money(total), WIK_MINIMUM), WIK_MAXIMUM).quantize(MONEY)


@dataclass(frozen=True, slots=True)
class DunningAssessment:
    """What should happen to one invoice today, and why.

    `step` is the step in question even when `blocker` says it is not sendable yet
    (STEP_NOT_DUE), so a screen can say "step 2 is due in 4 days". It is `None`
    only when there is nothing to send at all.
    """

    invoice_id: object
    outstanding: Decimal
    due_date: date | None
    days_overdue: int
    step: LadderStep | None
    blocker: Blocker | None
    interest: Decimal | None = None
    collection_cost: Decimal | None = None
    #: The last day the customer is given to pay, for a formal notice.
    pay_by: date | None = None
    interest_kind: InterestRateKind = InterestRateKind.COMMERCIAL

    @property
    def can_send(self) -> bool:
        return self.blocker is None and self.step is not None


def assess(
    *,
    invoice_id: object,
    outstanding: Decimal,
    due_date: date | None,
    today: date,
    steps: Sequence[LadderStep],
    sent_positions: frozenset[int],
    paused: bool,
    is_business: bool,
    rates: Sequence[InterestRate],
) -> DunningAssessment:
    """One invoice, one day: the step, the amounts, or the reason for neither.

    `is_business` chooses the rate kind AND is the caller's judgement - see the
    service for how it is derived - so a wrong classification is visible on the
    assessment (`interest_kind`) rather than buried in a rate.
    """
    kind = InterestRateKind.COMMERCIAL if is_business else InterestRateKind.CONSUMER
    overdue = days_overdue(due_date, today)

    def blocked(why: Blocker, step: LadderStep | None = None) -> DunningAssessment:
        return DunningAssessment(
            invoice_id, outstanding, due_date, overdue, step, why, interest_kind=kind
        )

    if outstanding <= 0:
        return blocked(Blocker.NOTHING_OUTSTANDING)
    if paused:
        return blocked(Blocker.PAUSED)
    if due_date is None:
        return blocked(Blocker.NO_DUE_DATE)
    if overdue == 0:
        return blocked(Blocker.NOT_OVERDUE)

    step, why = next_step(steps, sent_positions, overdue)
    if why is not None:
        return blocked(why, step)
    assert step is not None  # next_step returns a step whenever it returns no blocker

    interest: Decimal | None = None
    if step.charge_interest:
        try:
            interest = statutory_interest(outstanding, due_date, today, rates)
        except NoInterestRate:
            return blocked(Blocker.INTEREST_RATE_MISSING, step)

    cost = collection_cost(outstanding) if step.charge_collection_cost else None
    pay_by = (
        today + timedelta(days=FORMAL_NOTICE_DEADLINE_DAYS)
        if step.kind is StepKind.FORMAL_NOTICE
        else None
    )
    return DunningAssessment(
        invoice_id,
        outstanding,
        due_date,
        overdue,
        step,
        None,
        interest=interest,
        collection_cost=cost,
        pay_by=pay_by,
        interest_kind=kind,
    )
