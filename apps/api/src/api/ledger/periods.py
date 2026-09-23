"""Period locking and the suppletie flow: FR-GL-007 (PRD §6.2).

    FR-GL-007  Period locking per fiscal period with a defined unlock
               authority; VAT-filed periods are hard-locked and require a
               suppletie flow to change.

--- The "defined unlock authority" ---

Appendix A already defines it, so nothing here invents one. The capability
"Lock / unlock periods" is Full for **Owner** and **Accountant** and absent
for every other role - PRD §8.4 lists "Unlock closed periods" among the things
a Bookkeeper explicitly cannot do. That is the permission ("lock", "period")
in api.authz.matrix, and this service checks it through the single shared
authorization library (CLAUDE.md rule 3), per request, against live state.

Filing a period is a different authority: ("file", "vat_return"), also Owner
and Accountant. Opening a suppletie is ("prepare", "vat_return"), which
Bookkeepers hold - preparing a correction is bookkeeping, filing it is not.

The check being here is not what makes it unskippable. `period.status` can
only be changed by the SECURITY DEFINER functions in migration 0021, which run
as `ledgr_ledger`; a direct UPDATE from application code is refused by
`period_status_transition()`. So there is no path to the transition that does
not pass through a method on this class.

--- The hard lock ---

`vat_filed` is terminal. There is no unlock method, no force flag, and no
transition out of it in the database. Reopening a filed period would let the
ledger and a return already sent to the Belastingdienst diverge with nothing
recording that they had.

Changing a filed period therefore goes through `open_suppletie()`: the
correction is posted into whichever period is open, carrying `suppletie_id`,
which is the link back to the filing it corrects. What is built here is the
ledger side of that. The correction return itself - rubriek values, the
Digipoort submission, the receipt - is FR-VAT-005 and is not built.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import (
    AdministrationScope,
    AuthorizationRequest,
    ResourceAttributes,
)
from api.authz.service import AuthorizationService
from api.ledger.model import LedgerError

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession

#: Appendix A, "Lock / unlock periods": Full for Owner and Accountant only.
LOCK_PERIOD = ("lock", "period")
#: Appendix A, "File VAT return": Full for Owner and Accountant only.
FILE_VAT_RETURN = ("file", "vat_return")
#: Appendix A, "Prepare VAT return": Owner, Accountant and Bookkeeper.
PREPARE_VAT_RETURN = ("prepare", "vat_return")


class PeriodStatus(enum.Enum):
    OPEN = "open"
    LOCKED = "locked"
    #: Terminal. See the module docstring.
    VAT_FILED = "vat_filed"


class SuppletieStatus(enum.Enum):
    OPEN = "open"
    SUBMITTED = "submitted"
    FILED = "filed"
    WITHDRAWN = "withdrawn"


class PeriodError(LedgerError):
    """Base for period-lifecycle refusals."""


class NotAuthorized(PeriodError):
    """FR-GL-007's authority, refused.

    Carries the authorization decision's own reason rather than a generic
    message: "no live grant of lock period" and "your grant is restricted to
    other periods" are different problems for whoever hits them.
    """

    def __init__(self, action: str, resource_type: str, detail: str) -> None:
        self.action = action
        self.resource_type = resource_type
        self.detail = detail
        super().__init__(f"not authorized to {action} {resource_type}: {detail}")


class HardLocked(PeriodError):
    """The VAT-filed hard lock (FR-GL-007).

    Its own type because the caller's next step is specific and knowable: open
    a suppletie. A generic "not permitted" would send them looking for someone
    with a bigger role, and nobody has one.
    """


@dataclass(frozen=True, slots=True)
class Period:
    id: uuid.UUID
    administration_id: uuid.UUID
    fiscal_year_id: uuid.UUID
    period_number: int
    start_date: date
    end_date: date
    status: PeriodStatus
    locked_at: datetime | None = None
    locked_by_user_id: uuid.UUID | None = None
    filed_at: datetime | None = None
    filed_by_user_id: uuid.UUID | None = None
    filing_reference: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status is PeriodStatus.OPEN

    @property
    def is_hard_locked(self) -> bool:
        """FR-GL-007. Distinct from `not is_open`: a locked period can be
        unlocked by the authority, a filed one can never be.
        """
        return self.status is PeriodStatus.VAT_FILED


@dataclass(frozen=True, slots=True)
class Suppletie:
    id: uuid.UUID
    administration_id: uuid.UUID
    period_id: uuid.UUID
    status: SuppletieStatus
    reason: str
    opened_by_user_id: uuid.UUID
    opened_at: datetime
    submitted_at: datetime | None = None
    filed_at: datetime | None = None
    filing_reference: str | None = None


@dataclass(frozen=True, slots=True)
class SuppletieCorrection:
    entry_id: uuid.UUID
    entry_number: int
    entry_date: date
    period_id: uuid.UUID
    description: str
    total_debit: Decimal
    total_credit: Decimal


class PeriodRepository(Protocol):
    """Declared here with the domain types, and implemented in
    periods_repository.py - the same split api.audit.log / api.audit.repository
    uses. Note the absence of any method that writes `period.status` directly:
    every transition is a named lifecycle operation, because the database will
    only accept them through the `ledger.*` functions anyway.
    """

    async def period(self, period_id: uuid.UUID) -> Period | None: ...

    async def periods_for_year(
        self, *, administration_id: uuid.UUID, fiscal_year_id: uuid.UUID
    ) -> Sequence[Period]: ...

    async def organization_of(self, administration_id: uuid.UUID) -> uuid.UUID: ...

    async def lock(self, *, period_id: uuid.UUID, user_id: uuid.UUID) -> Period: ...

    async def unlock(self, *, period_id: uuid.UUID, user_id: uuid.UUID) -> Period: ...

    async def mark_filed(
        self,
        *,
        period_id: uuid.UUID,
        user_id: uuid.UUID,
        filing_reference: str | None,
    ) -> Period: ...

    async def open_suppletie(
        self, *, period_id: uuid.UUID, reason: str, user_id: uuid.UUID
    ) -> Suppletie: ...

    async def close_suppletie(
        self,
        *,
        suppletie_id: uuid.UUID,
        status: SuppletieStatus,
        filing_reference: str | None,
    ) -> Suppletie: ...

    async def suppletie(self, suppletie_id: uuid.UUID) -> Suppletie | None: ...

    async def open_suppletie_for(self, period_id: uuid.UUID) -> Suppletie | None: ...

    async def suppletie_corrections(
        self, suppletie_id: uuid.UUID
    ) -> Sequence[SuppletieCorrection]: ...


class PeriodService:
    """The only way a period's status changes.

    Not by convention: `period_status_transition()` refuses a status change
    that did not come through the `ledger.*` functions this class calls.
    """

    def __init__(
        self,
        repository: PeriodRepository,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit = audit_log

    # -- the authority ----------------------------------------------------

    async def _require(
        self,
        permission: tuple[str, str],
        *,
        user_id: uuid.UUID,
        period: Period,
    ) -> None:
        """Evaluated per call, against live state, through the one library.

        `period_id` is passed as a resource attribute so IAM-033's period
        restriction binds: an Accountant whose grant is conditioned to
        specific periods cannot lock one outside them. Without it the
        condition would find no value and deny - fail-closed, but for the
        wrong reason and with a confusing message.
        """
        action, resource_type = permission
        decision = await self._authorization.authorize(
            AuthorizationRequest(
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                # The administration is the TARGET (which scope the grant must
                # cover); the period is an ATTRIBUTE (what a condition on that
                # grant is checked against). Conflating them is why the first
                # version of this passed administration_id to both.
                target=AdministrationScope(period.administration_id),
                attributes=ResourceAttributes(period_id=period.id),
            )
        )
        if not decision.allowed:
            await self._record(
                period,
                user_id=user_id,
                action=f"{action}_{resource_type}",
                outcome=AuditOutcome.DENIED,
                detail={"reason": decision.reason, "detail": decision.detail},
                category=AuditCategory.CONFIGURATION,
            )
            # `detail` is optional on an AuthorizationDecision; falling back to
            # the reason keeps the message useful rather than reading "not
            # authorized to lock period: None".
            raise NotAuthorized(action, resource_type, decision.detail or decision.reason)

    async def _record(
        self,
        period: Period,
        *,
        user_id: uuid.UUID,
        action: str,
        outcome: AuditOutcome,
        detail: dict[str, object],
        category: AuditCategory,
        correlation_id: str | None = None,
    ) -> None:
        organization_id = await self._repository.organization_of(period.administration_id)
        await self._audit.record(
            AuditEvent(
                organization_id=organization_id,
                administration_id=period.administration_id,
                category=category,
                action=action,
                resource_type="period",
                resource_id=period.id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail={"period_number": period.period_number, **detail},
            )
        )

    # -- transitions ------------------------------------------------------

    async def lock(
        self,
        *,
        period_id: uuid.UUID,
        user_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> Period:
        """FR-GL-007. Closes a period to further postings."""
        period = await self._load(period_id)
        if period.is_hard_locked:
            raise HardLocked(
                f"period {period.period_number} is VAT-filed and already hard-locked (FR-GL-007)"
            )
        await self._require(LOCK_PERIOD, user_id=user_id, period=period)

        locked = await self._repository.lock(period_id=period_id, user_id=user_id)
        await self._record(
            locked,
            user_id=user_id,
            action="lock_period",
            outcome=AuditOutcome.SUCCESS,
            detail={"from": period.status.value, "to": locked.status.value},
            category=AuditCategory.CONFIGURATION,
            correlation_id=correlation_id,
        )
        return locked

    async def unlock(
        self,
        *,
        period_id: uuid.UUID,
        user_id: uuid.UUID,
        reason: str,
        correlation_id: str | None = None,
    ) -> Period:
        """FR-GL-007's unlock, restricted to the defined authority.

        `reason` is required. Reopening a closed period is the single most
        consequential thing this service does - figures someone has already
        relied on become editable - and an unlock with no stated reason is the
        one an inspector asks about. It goes into the audit entry, which is
        append-only and hash-chained.
        """
        if not reason.strip():
            raise PeriodError(
                "unlocking a period requires a stated reason (FR-GL-007): it is "
                "recorded in the audit log and is what the authority is "
                "answerable for"
            )

        period = await self._load(period_id)
        if period.is_hard_locked:
            # Checked BEFORE the permission check, deliberately. Nobody holds
            # an authority that can do this, so reporting it as a permission
            # problem would send the caller looking for a bigger role that
            # does not exist. The answer is a suppletie, and the error says so.
            raise HardLocked(
                f"period {period.period_number} is VAT-filed and hard-locked; "
                "correcting it requires a suppletie, not an unlock (FR-GL-007). "
                "Use open_suppletie()."
            )
        if period.is_open:
            return period

        await self._require(LOCK_PERIOD, user_id=user_id, period=period)

        unlocked = await self._repository.unlock(period_id=period_id, user_id=user_id)
        await self._record(
            unlocked,
            user_id=user_id,
            action="unlock_period",
            outcome=AuditOutcome.SUCCESS,
            detail={
                "from": period.status.value,
                "to": unlocked.status.value,
                "reason": reason,
                # Who held the lock before it was released. The columns are
                # cleared by the transition, so if this is not captured here it
                # is not recorded anywhere.
                "previously_locked_by": (
                    str(period.locked_by_user_id) if period.locked_by_user_id else None
                ),
            },
            category=AuditCategory.CONFIGURATION,
            correlation_id=correlation_id,
        )
        return unlocked

    async def mark_filed(
        self,
        *,
        period_id: uuid.UUID,
        user_id: uuid.UUID,
        filing_reference: str | None = None,
        correlation_id: str | None = None,
    ) -> Period:
        """The hard lock, applied. One-way: there is no counterpart method.

        Called by FR-VAT's filing flow when a return has been accepted. It is
        here rather than there because the ledger owns the period lifecycle -
        PRD §13 lists VatReturn as "locks its period", and this is that lock.
        """
        period = await self._load(period_id)
        if period.is_hard_locked:
            raise HardLocked(f"period {period.period_number} is already VAT-filed (FR-GL-007)")
        await self._require(FILE_VAT_RETURN, user_id=user_id, period=period)

        filed = await self._repository.mark_filed(
            period_id=period_id, user_id=user_id, filing_reference=filing_reference
        )
        await self._record(
            filed,
            user_id=user_id,
            action="mark_period_filed",
            outcome=AuditOutcome.SUCCESS,
            detail={
                "from": period.status.value,
                "to": filed.status.value,
                "filing_reference": filing_reference,
                "hard_locked": True,
            },
            category=AuditCategory.FILING,
            correlation_id=correlation_id,
        )
        return filed

    # -- the suppletie flow -----------------------------------------------

    async def open_suppletie(
        self,
        *,
        period_id: uuid.UUID,
        user_id: uuid.UUID,
        reason: str,
        correlation_id: str | None = None,
    ) -> Suppletie:
        """FR-GL-007's "requires a suppletie flow to change", entered.

        Only against a VAT-filed period: a suppletie corrects a filed return,
        and opening one against an ordinary period would both correct nothing
        and give a caller a way to dress routine postings as statutory
        corrections.

        Corrections are then posted normally, into an open period, carrying
        this suppletie's id. The filed period itself never changes.
        """
        if not reason.strip():
            raise PeriodError(
                "a suppletie requires a stated reason (FR-VAT-005): it is what "
                "the correction return has to explain"
            )

        period = await self._load(period_id)
        if not period.is_hard_locked:
            raise PeriodError(
                f"period {period.period_number} is {period.status.value} and has no "
                "filed return to correct; a suppletie corrects a VAT-filed period "
                "(FR-GL-007)"
            )

        await self._require(PREPARE_VAT_RETURN, user_id=user_id, period=period)

        suppletie = await self._repository.open_suppletie(
            period_id=period_id, reason=reason, user_id=user_id
        )
        await self._record(
            period,
            user_id=user_id,
            action="open_suppletie",
            outcome=AuditOutcome.SUCCESS,
            detail={"suppletie_id": str(suppletie.id), "reason": reason},
            category=AuditCategory.FILING,
            correlation_id=correlation_id,
        )
        return suppletie

    async def close_suppletie(
        self,
        *,
        suppletie_id: uuid.UUID,
        user_id: uuid.UUID,
        status: SuppletieStatus,
        filing_reference: str | None = None,
        correlation_id: str | None = None,
    ) -> Suppletie:
        """Closing takes the FILING authority, not the preparing one.

        A Bookkeeper may prepare a correction (Appendix A gives them "Prepare
        VAT return") and may not file it. Splitting open/close across the two
        permissions is what makes that division real rather than nominal.
        """
        if status is SuppletieStatus.OPEN:
            raise PeriodError("a suppletie cannot be closed as 'open'")

        suppletie = await self._repository.suppletie(suppletie_id)
        if suppletie is None:
            raise PeriodError(f"suppletie {suppletie_id} does not exist")
        period = await self._load(suppletie.period_id)

        await self._require(FILE_VAT_RETURN, user_id=user_id, period=period)

        closed = await self._repository.close_suppletie(
            suppletie_id=suppletie_id,
            status=status,
            filing_reference=filing_reference,
        )
        await self._record(
            period,
            user_id=user_id,
            action="close_suppletie",
            outcome=AuditOutcome.SUCCESS,
            detail={
                "suppletie_id": str(suppletie_id),
                "status": status.value,
                "filing_reference": filing_reference,
            },
            category=AuditCategory.FILING,
            correlation_id=correlation_id,
        )
        return closed

    async def suppletie_corrections(self, suppletie_id: uuid.UUID) -> Sequence[SuppletieCorrection]:
        """FR-VAT-005's "clear link to the original filing", read from the
        correction end: every entry posted under this suppletie.
        """
        return await self._repository.suppletie_corrections(suppletie_id)

    # -- reads ------------------------------------------------------------

    async def period(self, period_id: uuid.UUID) -> Period | None:
        return await self._repository.period(period_id)

    async def periods_for_year(
        self, *, administration_id: uuid.UUID, fiscal_year_id: uuid.UUID
    ) -> Sequence[Period]:
        """The Journal screen's period picker and lock/unlock list. A plain
        read, like `period()` above - the route's own `require_permission`
        gates it, the same way `get_trial_balance` gates `LedgerService.
        trial_balance` without PeriodService checking anything itself.
        """
        return await self._repository.periods_for_year(
            administration_id=administration_id, fiscal_year_id=fiscal_year_id
        )

    async def open_suppletie_for(self, period_id: uuid.UUID) -> Suppletie | None:
        return await self._repository.open_suppletie_for(period_id)

    async def _load(self, period_id: uuid.UUID) -> Period:
        period = await self._repository.period(period_id)
        if period is None:
            raise PeriodError(f"period {period_id} does not exist")
        return period


def build_period_service(
    session: AsyncSession, authorization: AuthorizationService, audit_log: AuditLog
) -> PeriodService:
    """A wired PeriodService, for callers outside this bounded context - the
    same widening `api.ledger.service.build_ledger_service` makes, and for
    the same reason: a caller that reached for `SqlPeriodRepository` directly
    would have reached past the narrow API this class is.

    The repository import is function-local so the only place naming it
    stays this one line.
    """
    from api.ledger.periods_repository import SqlPeriodRepository

    return PeriodService(SqlPeriodRepository(session), authorization, audit_log)
