"""The expense entry form - FR-EXP-001b, FR-EXP-001e.

    FR-EXP-001b  The expense form asks for the minimum - date, supplier, gross
                 amount, VAT rate, category - with VAT and net calculated
                 automatically and the category defaulting from the user's
                 history. Everything else is optional and hidden by default.
    FR-EXP-001e  Payment method is captured at entry - paid by business
                 account, business card, or personally (reimbursable) -
                 because it determines the posting and cannot be reliably
                 inferred later.

--- Six inputs, and only five are typed ---

    date          from the receipt
    supplier      from the receipt
    gross amount  from the receipt - what was paid, the only figure a person
                  can read off the paper without doing arithmetic
    VAT           a TREATMENT, chosen from a short list. The RATE is looked up
                  from it and the date (CMP-014), never typed.
    category      pre-filled from the user's history, and always editable
    payment       FR-EXP-001e. Not on the receipt at all, which is exactly why
                  it has to be asked now.

`vat_amount` and `net_amount` are not inputs. They are computed from the gross
and the resolved rate, and the database holds the stored VAT to exactly what
the rule gives (`expense_vat_is_derived` in migration 0033) while generating
net as `gross - vat`.

--- Partial saves are the normal case ---

FR-EXP-001c: "the product never blocks on extraction being available". So
`update` takes any subset of the fields and refuses nothing for being
incomplete. Only `mark_ready` requires the full set, because that is the point
at which the claim goes to somebody else.

--- "Everything else is optional and hidden by default" ---

There is no "everything else" yet. The PRD names no other expense field, and
inventing some to hide would be worse than having none. What the server
contributes to that sentence is that nothing beyond the six is REQUIRED - the
required set is `Expense.REQUIRED_FIELDS`, and it is exactly FR-EXP-001b's
minimum plus FR-EXP-001e's payment method. Which fields a client collapses
behind a "more" control is a rendering decision.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import (
    AdministrationScope,
    AuthorizationRequest,
    ResourceAttributes,
)
from api.authz.service import AuthorizationService
from api.expenses.duplicates import DuplicateWarning, ExpenseTriple, describe
from api.expenses.model import (
    Expense,
    ExpenseAlreadyReady,
    ExpenseNotFound,
    ExpenseStatus,
    IncompleteExpense,
    NotAuthorizedToCapture,
    PaymentMethod,
    VatRateUnavailable,
    VatTreatment,
)
from api.expenses.vat import VatSplit, split_gross

#: Appendix A's "Submit expenses" row, the same one capture uses. Filling in
#: the form is part of submitting the expense, not a separate capability -
#: inventing one is what ADR-012 forbids.
SUBMIT_EXPENSE = ("submit", "expense")

#: Sentinel for "this field was not mentioned", so that `None` can mean "clear
#: it". A form that could not distinguish the two would make clearing a
#: mistyped supplier impossible.
UNSET: Any = object()


@dataclass(frozen=True, slots=True)
class ExpenseView:
    """What the form renders: the expense, what it still needs, and the
    category to offer.

    The suggestion travels WITH the expense rather than from a separate
    endpoint, because it is only meaningful next to the supplier it was derived
    from - and because a form that had to make two calls to draw itself would
    show an empty category field first and fill it in a moment later.
    """

    expense: Expense
    #: FR-EXP-001b's "defaulting from the user's history". None when they have
    #: no history to default from, which is every first expense.
    suggested_category: str | None
    #: What `mark_ready` would still refuse for. Empty when it would succeed.
    missing_fields: tuple[str, ...]
    #: FR-EXP-001b's split, when there is enough to compute it.
    split: VatSplit | None = None
    #: FR-EXP-001g. Existing claims matching this one on supplier, date and
    #: amount. Empty until all three are known, which is the normal state of a
    #: form somebody is still filling in.
    #:
    #: These NEVER prevent anything - see `can_be_marked_ready`, which does not
    #: consult them. Legitimate duplicates are ordinary, and a system that
    #: refused them would be wrong about the money and would teach people to
    #: work around it.
    duplicate_warnings: tuple[DuplicateWarning, ...] = ()
    #: The first stored original of the receipt, so the review screen can show
    #: what the fields were read from beside the fields. None for an expense with
    #: no page yet, which is not a state capture produces.
    document_id: uuid.UUID | None = None

    @property
    def can_be_marked_ready(self) -> bool:
        """Deliberately blind to `duplicate_warnings`.

        FR-EXP-001g says warn, and this is the property that would quietly turn
        that into "block" if somebody added `and not self.duplicate_warnings`
        to it. tests/expenses/test_duplicates.py asserts a claim with warnings
        can still be submitted and still be posted.
        """
        return not self.missing_fields

    @property
    def has_duplicate_warning(self) -> bool:
        return bool(self.duplicate_warnings)


class ExpenseFormRepository(Protocol):
    async def get(
        self, *, administration_id: uuid.UUID, expense_id: uuid.UUID
    ) -> Expense | None: ...

    async def update(
        self,
        *,
        administration_id: uuid.UUID,
        expense_id: uuid.UUID,
        changes: dict[str, object],
    ) -> Expense: ...

    async def set_status(
        self, *, administration_id: uuid.UUID, expense_id: uuid.UUID, status: str
    ) -> Expense: ...

    async def rate_on(self, *, treatment: str, on: date) -> Decimal | None:
        """`vat.rate_on` from migration 0028 - the rate in force on a date."""
        ...

    async def suggest_category(
        self, *, administration_id: uuid.UUID, user_id: uuid.UUID, supplier: str | None
    ) -> str | None: ...

    async def duplicate_candidates(
        self,
        *,
        administration_id: uuid.UUID,
        expense_id: uuid.UUID,
        triple: ExpenseTriple,
    ) -> Sequence[DuplicateWarning]:
        """FR-EXP-001g. Existing claims matching this one's triple.

        Returns rather than raises, at every layer: nothing about a duplicate
        is an error.
        """
        ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def first_document_id(
        self, *, administration_id: uuid.UUID, item_id: uuid.UUID
    ) -> uuid.UUID | None:
        """The receipt's first stored original (page 1), or None."""
        ...

    async def list_by_status(
        self,
        *,
        administration_id: uuid.UUID,
        status: str | None,
        limit: int,
    ) -> Sequence[Expense]:
        """MOB-004's Approve tab and View tab: a bounded list, newest first.

        `status` narrows to one `ExpenseStatus` value; `None` returns every
        status. No cursor pagination - see `api.customers.routes.list_customers`,
        the one other list endpoint in this codebase, which takes the same
        bounded-`limit`-with-no-cursor shape because nothing here paginates yet.
        """
        ...


class ExpenseFormService:
    """FR-EXP-001b and FR-EXP-001e's entry point."""

    def __init__(
        self,
        repository: ExpenseFormRepository,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit = audit_log

    async def view(
        self, *, administration_id: uuid.UUID, expense_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> ExpenseView:
        """The form as it should be drawn."""
        await self._require(actor_user_id, administration_id)
        expense = await self._get(administration_id, expense_id)
        return await self._view_of(expense, actor_user_id)

    async def list_by_status(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        status: ExpenseStatus | None,
        limit: int,
    ) -> Sequence[Expense]:
        """MOB-004's Approve tab (`status=draft`, revisit a capture that has
        not yet been completed) and View tab (posted/ready expenses).

        Reuses `submit expense` - the same permission `view`/`update` already
        require - because filling in and reviewing the form IS submitting the
        expense, not a separate capability (ADR-012). Returns bare `Expense`
        rows rather than a full `ExpenseView` per row: the category suggestion
        and FR-EXP-001g's duplicate lookup are per-expense reads that a list
        screen does not need until somebody actually opens one, and doing them
        for every row here would be a query fan-out proportional to `limit`.
        """
        await self._require(actor_user_id, administration_id)
        return await self._repository.list_by_status(
            administration_id=administration_id,
            status=status.value if status is not None else None,
            limit=limit,
        )

    async def update(
        self,
        *,
        administration_id: uuid.UUID,
        expense_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        expense_date: date | None = UNSET,
        supplier: str | None = UNSET,
        gross_amount: Decimal | None = UNSET,
        vat_treatment: VatTreatment | None = UNSET,
        category: str | None = UNSET,
        payment_method: PaymentMethod | None = UNSET,
        invoice_number: str | None = UNSET,
        correlation_id: str | None = None,
    ) -> ExpenseView:
        """Save any subset of the form.

        Refuses nothing for being incomplete (FR-EXP-001c). What it does refuse
        is an expense already released to approval: `ready` means somebody else
        is looking at the claim, and FR-EXP-001c's "always editable" is about
        extraction never locking a field against the person filling it in, not
        about a submitted claim moving under an approver.

        The VAT rate and amounts are RECOMPUTED whenever the date, the gross or
        the treatment changes - all three are inputs to them, and a stale
        `vat_amount` beside a new gross is the kind of wrong number that adds
        up.
        """
        await self._require(actor_user_id, administration_id)
        expense = await self._get(administration_id, expense_id)
        if expense.status is ExpenseStatus.READY:
            raise ExpenseAlreadyReady(
                f"expense {expense_id} has been marked ready and is in front of an "
                f"approver; returning it for correction is FR-EXP-002's step"
            )

        changes: dict[str, object] = {}
        if expense_date is not UNSET:
            changes["expense_date"] = expense_date
        if supplier is not UNSET:
            changes["supplier"] = _clean(supplier)
        if gross_amount is not UNSET:
            changes["gross_amount"] = gross_amount
        if vat_treatment is not UNSET:
            changes["vat_treatment"] = vat_treatment.value if vat_treatment else None
        if category is not UNSET:
            changes["category"] = _clean(category)
        if payment_method is not UNSET:
            changes["payment_method"] = payment_method.value if payment_method else None
        if invoice_number is not UNSET:
            changes["invoice_number"] = _clean(invoice_number)

        # The state the row will be in once these land, so the rate and amounts
        # are computed from the WHOLE form rather than only from what changed.
        resulting_date = changes.get("expense_date", expense.expense_date)
        resulting_gross = changes.get("gross_amount", expense.gross_amount)
        resulting_treatment = changes.get(
            "vat_treatment", expense.vat_treatment.value if expense.vat_treatment else None
        )

        if _touches_vat(changes):
            rate, vat_amount = await self._resolve_vat(
                treatment=resulting_treatment,  # type: ignore[arg-type]
                on=resulting_date,  # type: ignore[arg-type]
                gross=resulting_gross,  # type: ignore[arg-type]
            )
            changes["vat_rate"] = rate
            changes["vat_amount"] = vat_amount

        updated = await self._repository.update(
            administration_id=administration_id, expense_id=expense_id, changes=changes
        )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="update_expense",
            resource_id=expense_id,
            correlation_id=correlation_id,
            detail={
                # WHICH fields were touched, never their values: an audit entry
                # holding a supplier and an amount is a second copy of the
                # claim in a table with different retention rules.
                "fields": sorted(changes),
                "still_missing": list(updated.missing_fields),
            },
        )
        return await self._view_of(updated, actor_user_id)

    async def mark_ready(
        self,
        *,
        administration_id: uuid.UUID,
        expense_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> ExpenseView:
        """Release the claim - FR-EXP-001b's minimum and FR-EXP-001e's payment
        method, both required.

        Checked here for a precise message naming the missing fields, and again
        by migration 0033's CHECK, which is what makes it true for any writer.
        """
        await self._require(actor_user_id, administration_id)
        expense = await self._get(administration_id, expense_id)
        if expense.status is ExpenseStatus.READY:
            return await self._view_of(expense, actor_user_id)

        if expense.missing_fields:
            raise IncompleteExpense(expense.missing_fields)

        # FR-EXP-001g: read BEFORE the write, and used for nothing but the
        # record. A warning outstanding at submission does not stop it - the
        # requirement says warn - but an approver reading this entry later
        # should be able to see what the submitter saw.
        outstanding = await self._view_of(expense, actor_user_id)

        updated = await self._repository.set_status(
            administration_id=administration_id,
            expense_id=expense_id,
            status=ExpenseStatus.READY.value,
        )
        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="submit_expense",
            resource_id=expense_id,
            correlation_id=correlation_id,
            detail={
                # How many, and how alike - never the matching claims' ids.
                # The audit log has its own retention (IAM-093) and is not the
                # place to accumulate a second index of who spent what.
                "duplicate_warnings": describe(list(outstanding.duplicate_warnings)),
                # FR-EXP-001e: which posting this will produce is the one thing
                # about the claim that cannot be re-derived later, so it is on
                # the record of the submission.
                "payment_method": expense.payment_method.value if expense.payment_method else None,
                "vat_treatment": expense.vat_treatment.value if expense.vat_treatment else None,
            },
        )
        return await self._view_of(updated, actor_user_id)

    # -- internals ---------------------------------------------------------

    async def _resolve_vat(
        self, *, treatment: str | None, on: date | None, gross: Decimal | None
    ) -> tuple[Decimal | None, Decimal | None]:
        """The rate from the treatment and the DATE, then the split.

        Both halves can legitimately be unknown while the form is half-filled,
        which is why this returns a pair of optionals rather than refusing. It
        refuses only when a treatment and a date ARE given and the ruleset has
        no rate for them - a claim carrying no VAT because nobody knew the rate
        is indistinguishable from a genuinely zero-rated one.
        """
        if treatment is None or on is None:
            return None, None

        rate = await self._repository.rate_on(treatment=treatment, on=on)
        if rate is None:
            raise VatRateUnavailable(
                f"no VAT rate is in force for {treatment!r} on {on}. The ruleset "
                f"(migration 0028) starts later than this date, or the treatment "
                f"is not one it defines."
            )
        if gross is None:
            return rate, None
        return rate, split_gross(gross, rate).vat

    async def _view_of(self, expense: Expense, actor_user_id: uuid.UUID) -> ExpenseView:
        suggested = await self._repository.suggest_category(
            administration_id=expense.administration_id,
            user_id=actor_user_id,
            supplier=expense.supplier,
        )
        split = None
        if expense.gross_amount is not None and expense.vat_rate is not None:
            split = split_gross(expense.gross_amount, expense.vat_rate)

        # FR-EXP-001g. Looked up only once the triple is complete - a form
        # somebody is halfway through has nothing to match on, and asking the
        # database on every keystroke would be a query per character.
        triple = ExpenseTriple(
            supplier=expense.supplier,
            on=expense.expense_date,
            gross_amount=expense.gross_amount,
        )
        warnings: Sequence[DuplicateWarning] = ()
        if triple.is_complete:
            warnings = await self._repository.duplicate_candidates(
                administration_id=expense.administration_id,
                expense_id=expense.id,
                triple=triple,
            )

        return ExpenseView(
            expense=expense,
            # Never overrides what is already there: FR-EXP-001b defaults a
            # field, it does not correct one somebody chose.
            suggested_category=None if expense.category else suggested,
            missing_fields=expense.missing_fields,
            split=split,
            duplicate_warnings=tuple(warnings),
            document_id=await self._repository.first_document_id(
                administration_id=expense.administration_id, item_id=expense.capture_item_id
            ),
        )

    async def _get(self, administration_id: uuid.UUID, expense_id: uuid.UUID) -> Expense:
        expense = await self._repository.get(
            administration_id=administration_id, expense_id=expense_id
        )
        if expense is None:
            raise ExpenseNotFound(f"expense {expense_id} not found")
        return expense

    async def _require(self, user_id: uuid.UUID, administration_id: uuid.UUID) -> None:
        action, resource_type = SUBMIT_EXPENSE
        decision = await self._authorization.authorize(
            AuthorizationRequest(
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                target=AdministrationScope(administration_id),
                attributes=ResourceAttributes(),
            )
        )
        if not decision.allowed:
            await self._record(
                administration_id=administration_id,
                user_id=user_id,
                action=f"{action}_{resource_type}",
                resource_id=administration_id,
                outcome=AuditOutcome.DENIED,
                detail={"reason": decision.reason, "detail": decision.detail},
            )
            raise NotAuthorizedToCapture(action, resource_type, decision.detail or decision.reason)

    async def _record(
        self,
        *,
        administration_id: uuid.UUID,
        user_id: uuid.UUID,
        action: str,
        resource_id: uuid.UUID,
        detail: dict[str, object],
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        correlation_id: str | None = None,
    ) -> None:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            raise ExpenseNotFound(f"administration {administration_id} does not exist")
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                category=AuditCategory.CONFIGURATION,
                action=action,
                resource_type="expense",
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=detail,
            )
        )


def _touches_vat(changes: dict[str, object]) -> bool:
    """Whether the rate and amounts have to be recomputed.

    All three are inputs: the treatment says which VAT, the date says which
    version of it (CMP-014), and the gross is what it applies to. Changing any
    one of them and leaving `vat_amount` alone would leave a figure that agrees
    with nothing.
    """
    return bool({"expense_date", "gross_amount", "vat_treatment"} & set(changes))


def _clean(value: str | None) -> str | None:
    """Trailing whitespace off a typed field, and an empty one becomes None.

    A supplier of `"  "` would satisfy a NOT NULL check and fail the CHECK in
    migration 0033, which is the right answer arriving from the wrong layer.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
