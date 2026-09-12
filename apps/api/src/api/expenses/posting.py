"""Confirming an expense into the ledger - FR-EXP-001d, FR-EXP-001e.

--- Through the ledger's own API, never around it ---

CLAUDE.md's first non-negotiable: "The ledger is a separate bounded context
with a narrow API. Nothing writes to posting tables except the ledger service."

So this module builds an `EntryInput` and hands it to `LedgerService.post`. It
holds no SQL against `journal_entry` or `journal_line`, and `ledgr_app` has no
grant that would let it if it tried. Everything the ledger guarantees -
balance at COMMIT, gapless numbering, period locks, immutability - applies to a
posted expense because it arrived the same way a manual journal does.

--- What the payment method decides ---

FR-EXP-001e says the method "determines the posting", and this is where that
becomes literal. The debits are the same whatever it was; only the credit
moves:

    business_account       Cr  the bank account
    business_card          Cr  the card liability, which settles later
    personal_reimbursable  Cr  the REIMBURSEMENT LIABILITY - the business owes
                               this person money, and FR-EXP-003's run pays it

That last one is the reason the field cannot be filled in afterwards. A card
slip looks the same whoever's card it was; only the person standing there knows
whether they are owed the money.

--- The shape of an expense entry ---

    Dr  expense account       net
    Dr  input VAT             vat        (omitted when the treatment carries none)
    Cr  funding account       gross

which balances because `net + vat == gross` is an identity in the schema
(migration 0033 generates net as `gross - vat`), not an arithmetic hope. The
ledger checks it again at COMMIT, which is the guarantee.

--- Why the reimbursement liability is not accounts payable ---

An AP control account would be the obvious home, and it is wrong here for a
mechanical reason and an accounting one. Mechanically, FR-GL-006 makes a
control account postable only through its sub-ledger with a party, so every
claimant would need a `subledger_party` - putting employees in the trade
creditors ledger. In accounting terms they do not belong there: money owed to
staff for expenses is its own position, conventionally "Te betalen
declaraties", and a Dutch bookkeeper reading a creditors list does not expect
to find colleagues in it.

--- Reverse charge is posted as it was paid ---

`btw_verlegd` carries rate 0, so the entry is net = gross with no VAT line -
correct for the money that changed hands. The notional VAT pair the buyer
accounts for is FR-VAT-001's, is not built, and can be derived later precisely
because migration 0035 records the treatment on the line.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import (
    AdministrationScope,
    AuthorizationRequest,
    ResourceAttributes,
)
from api.authz.service import AuthorizationService
from api.expenses.model import (
    Expense,
    ExpenseNotFound,
    ExpenseStatus,
    IncompleteExpense,
    NotAuthorizedToCapture,
    PaymentMethod,
)
from api.ledger.model import EntryInput, LineInput, PostedEntry
from api.ledger.service import LedgerService

#: Appendix A's "Post journal entries". Posting an expense IS posting to the
#: ledger, so it takes the ledger's own permission rather than the submitter's:
#: §8.4 lets an Expense Submitter submit a claim and not post one, and that
#: separation is the point (SoD). Reused rather than invented, per ADR-012.
POST_JOURNAL_ENTRY = ("post", "journal_entry")


class PostingConfigurationMissing(Exception):
    """No account is mapped for something this posting needs.

    Names what is missing rather than falling back to an account that looked
    close. Configuration that has not been done is visible; a wrong guess is
    not, and a lunch in "Loonheffingen" is found by an accountant months later.
    """

    def __init__(self, purpose: str, detail: str) -> None:
        self.purpose = purpose
        super().__init__(detail)


class ExpenseAlreadyPosted(Exception):
    """The claim has an entry. FR-GL-003: the correction is a reversing entry,
    never a second posting.
    """


class NoOpenPeriod(Exception):
    """The expense's date falls in no open period.

    Refused rather than moved to a period that is open: an expense dated inside
    a locked or VAT-filed period belongs there, and posting it somewhere else
    would put the cost in the wrong month to avoid an inconvenience.
    FR-VAT-005's suppletie is the route when the period is filed.
    """


class NoPurchaseJournal(Exception):
    """FR-GL-002's journal for this kind of entry is missing or ambiguous."""


@dataclass(frozen=True, slots=True)
class PostingAccounts:
    """The three accounts an expense entry needs, already resolved.

    Resolved together and passed as a value so the entry builder cannot reach
    for configuration halfway through and post a half-configured claim.
    """

    expense_account_id: uuid.UUID
    vat_input_account_id: uuid.UUID | None
    funding_account_id: uuid.UUID


class ExpensePostingRepository(Protocol):
    async def get(
        self, *, administration_id: uuid.UUID, expense_id: uuid.UUID
    ) -> Expense | None: ...

    async def account_for(
        self, *, administration_id: uuid.UUID, purpose: str, category: str | None = None
    ) -> uuid.UUID | None:
        """The mapped account, or None. For `expense_category` this is the
        exact category match, falling back to the administration's default row.
        """
        ...

    async def open_period_for(self, *, administration_id: uuid.UUID, on: date) -> uuid.UUID | None:
        """The OPEN period containing `on`, or None when there is none - which
        covers "no period exists" and "it is locked" alike.
        """
        ...

    async def purchase_journal(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def mark_posted(
        self, *, administration_id: uuid.UUID, expense_id: uuid.UUID, entry_id: uuid.UUID
    ) -> Expense: ...

    async def link_documents_to_entry(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        expense_id: uuid.UUID,
        entry_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> int:
        """FR-EXP-001d's bidirectional link, finally completable.

        Every original captured for this claim is linked to the entry it
        produced. Returns how many.
        """
        ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...


class ExpensePostingService:
    """FR-EXP-001d and FR-EXP-001e's last step."""

    def __init__(
        self,
        repository: ExpensePostingRepository,
        ledger: LedgerService,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._ledger = ledger
        self._authorization = authorization
        self._audit = audit_log

    async def post(
        self,
        *,
        administration_id: uuid.UUID,
        expense_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> PostedEntry:
        """Confirm the claim into the ledger.

        Everything is resolved and refused BEFORE the ledger is called, so a
        misconfigured administration produces a message naming what is missing
        rather than a half-written entry. Once `LedgerService.post` returns, the
        entry exists and the rest of this method runs in the same transaction -
        if the link or the status write fails, the posting rolls back with it.
        """
        await self._require(actor_user_id, administration_id)

        expense = await self._get(administration_id, expense_id)
        if expense.status is ExpenseStatus.POSTED:
            raise ExpenseAlreadyPosted(
                f"expense {expense_id} is already posted as entry "
                f"{expense.journal_entry_id}; a correction is a reversing entry "
                f"(FR-GL-003)"
            )
        # `ready` is not required, but completeness is - and they are the same
        # set. Checking the fields rather than the status means a draft that
        # happens to be complete can be confirmed in one step, which is what a
        # single-receipt capture wants.
        if expense.missing_fields:
            raise IncompleteExpense(expense.missing_fields)

        accounts = await self._resolve_accounts(expense)
        journal_id = await self._repository.purchase_journal(administration_id=administration_id)
        if journal_id is None:
            raise NoPurchaseJournal(
                f"administration {administration_id} has no single active purchase "
                f"journal to post an expense into (FR-GL-002)"
            )

        assert expense.expense_date is not None  # guaranteed by missing_fields
        period_id = await self._repository.open_period_for(
            administration_id=administration_id, on=expense.expense_date
        )
        if period_id is None:
            raise NoOpenPeriod(
                f"{expense.expense_date} falls in no open period. An expense is "
                f"posted in the period it belongs to; if that period is filed, the "
                f"route is a suppletie (FR-VAT-005), not another period."
            )

        entry = EntryInput(
            administration_id=administration_id,
            journal_id=journal_id,
            period_id=period_id,
            entry_date=expense.expense_date,
            description=_description(expense),
            lines=_lines(expense, accounts),
            document_reference=expense.supplier,
            posted_by_user_id=actor_user_id,
            # Where this entry came from, so a reader of the ledger can tell an
            # expense claim from a manual journal without joining anything.
            source_system="expenses",
            # NFR-032: the claim's own id. A retry of the confirmation posts
            # once, and the unique index on expense.journal_entry_id catches
            # the case two requests get past this at the same moment.
            idempotency_key=f"expense:{expense_id}",
        )

        posted = await self._ledger.post(
            entry, actor_user_id=actor_user_id, correlation_id=correlation_id
        )

        organization_id = await self._organization_of(administration_id)
        linked = await self._repository.link_documents_to_entry(
            organization_id=organization_id,
            administration_id=administration_id,
            expense_id=expense_id,
            entry_id=posted.id,
            user_id=actor_user_id,
        )
        await self._repository.mark_posted(
            administration_id=administration_id, expense_id=expense_id, entry_id=posted.id
        )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="post_expense",
            resource_id=expense_id,
            correlation_id=correlation_id,
            detail={
                "journal_entry_id": str(posted.id),
                "entry_number": posted.entry_number,
                # FR-EXP-001e: the two facts that determined the shape of the
                # entry, on the record beside it.
                "payment_method": expense.payment_method.value if expense.payment_method else None,
                "vat_treatment": expense.vat_treatment.value if expense.vat_treatment else None,
                "reimbursable": bool(
                    expense.payment_method and expense.payment_method.is_reimbursable
                ),
                # FR-EXP-001d, closed at last: how many originals now point at
                # this entry.
                "documents_linked": linked,
            },
        )
        return posted

    # -- internals ---------------------------------------------------------

    async def _resolve_accounts(self, expense: Expense) -> PostingAccounts:
        """Every account this entry needs, or a refusal naming the first gap."""
        administration_id = expense.administration_id

        expense_account = await self._repository.account_for(
            administration_id=administration_id,
            purpose="expense_category",
            category=expense.category,
        )
        if expense_account is None:
            raise PostingConfigurationMissing(
                "expense_category",
                f"no ledger account is mapped for the category {expense.category!r}, "
                f"and this administration has no default expense account. Map one "
                f"before posting.",
            )

        # Only when there is VAT to post. A zero-rated treatment produces no
        # line, so an administration that only ever books btw_verlegd never
        # needs an input-VAT account mapped.
        vat_account: uuid.UUID | None = None
        if expense.vat_amount and expense.vat_amount != Decimal("0.00"):
            vat_account = await self._repository.account_for(
                administration_id=administration_id, purpose="vat_input"
            )
            if vat_account is None:
                raise PostingConfigurationMissing(
                    "vat_input",
                    "no input-VAT account is mapped, and this expense carries "
                    f"{expense.vat_amount} of deductible BTW. Map one before posting.",
                )

        assert expense.payment_method is not None  # guaranteed by missing_fields
        purpose = _FUNDING_PURPOSE[expense.payment_method]
        funding = await self._repository.account_for(
            administration_id=administration_id, purpose=purpose
        )
        if funding is None:
            raise PostingConfigurationMissing(
                purpose,
                f"no account is mapped for payment method "
                f"{expense.payment_method.value!r}. FR-EXP-001e makes the method "
                f"determine the posting, so there is nothing to credit without it.",
            )

        return PostingAccounts(
            expense_account_id=expense_account,
            vat_input_account_id=vat_account,
            funding_account_id=funding,
        )

    async def _get(self, administration_id: uuid.UUID, expense_id: uuid.UUID) -> Expense:
        expense = await self._repository.get(
            administration_id=administration_id, expense_id=expense_id
        )
        if expense is None:
            raise ExpenseNotFound(f"expense {expense_id} not found")
        return expense

    async def _organization_of(self, administration_id: uuid.UUID) -> uuid.UUID:
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            raise ExpenseNotFound(f"administration {administration_id} does not exist")
        return organization_id

    async def _require(self, user_id: uuid.UUID, administration_id: uuid.UUID) -> None:
        action, resource_type = POST_JOURNAL_ENTRY
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
        organization_id = await self._organization_of(administration_id)
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=administration_id,
                # The ledger records its own POSTING event; this one is about
                # the CLAIM reaching the books, which is a different question
                # an auditor asks - "who confirmed this expense" rather than
                # "what was posted".
                category=AuditCategory.POSTING,
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


#: FR-EXP-001e's three methods and what each credits. A mapping rather than a
#: chain of conditionals, so adding a method is a row and a migration rather
#: than a branch somebody forgets in one of two places.
_FUNDING_PURPOSE: dict[PaymentMethod, str] = {
    PaymentMethod.BUSINESS_ACCOUNT: "business_account",
    PaymentMethod.BUSINESS_CARD: "business_card",
    PaymentMethod.PERSONAL_REIMBURSABLE: "reimbursement_liability",
}


def _description(expense: Expense) -> str:
    """FR-GL-004 requires one, and it is what a person scanning a journal
    reads. Supplier and category, because those are what identify the cost -
    the amount is in the columns beside it.
    """
    parts = [part for part in (expense.supplier, expense.category) if part]
    return " - ".join(parts) if parts else "Expense"


def _lines(expense: Expense, accounts: PostingAccounts) -> list[LineInput]:
    """The entry's lines: net and VAT debited, gross credited.

    Balances by construction, because `net + vat == gross` is generated in the
    schema rather than computed here. The ledger checks it again at COMMIT,
    which is where the guarantee actually lives (FR-GL-001).

    The VAT TREATMENT goes on both the cost line and the VAT line. On the VAT
    line it is obvious; on the cost line it is what lets FR-VAT-001 find the
    turnover a rubriek needs, which is the net amount and not the tax.
    """
    assert expense.net_amount is not None
    assert expense.gross_amount is not None
    treatment = expense.vat_treatment.value if expense.vat_treatment else None

    lines = [
        LineInput(
            account_id=accounts.expense_account_id,
            debit=expense.net_amount,
            description=_description(expense),
            vat_treatment=treatment,
        )
    ]

    if accounts.vat_input_account_id is not None and expense.vat_amount:
        lines.append(
            LineInput(
                account_id=accounts.vat_input_account_id,
                debit=expense.vat_amount,
                description=f"Voorbelasting {expense.vat_rate}%",
                vat_treatment=treatment,
            )
        )

    lines.append(
        LineInput(
            account_id=accounts.funding_account_id,
            credit=expense.gross_amount,
            # Names WHY this account is credited, so a reimbursement is
            # readable as one in the journal rather than only in the account's
            # name.
            description=_credit_description(expense),
            # No treatment: the funding side is money moving, not turnover, and
            # a code here would put the payment in a rubriek beside the cost.
        )
    )
    return lines


def _credit_description(expense: Expense) -> str:
    if expense.payment_method is PaymentMethod.PERSONAL_REIMBURSABLE:
        return "Te betalen declaratie"
    if expense.payment_method is PaymentMethod.BUSINESS_CARD:
        return "Betaald met zakelijke kaart"
    return "Betaald van zakelijke rekening"
