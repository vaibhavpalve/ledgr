"""The chart of accounts and its RGS mapping: FR-GL-005, FR-ONB-004,
FR-ONB-005, CMP-003 (PRD §6.1, §6.2, §11).

    FR-GL-005   chart of accounts with account type, RGS reference code, VAT
                default, and blocked/active state
    FR-ONB-004  the legal form drives the default chart
    FR-ONB-005  seeded from the RGS MKB profile matching the legal form; the
                user may extend it but not break RGS mapping integrity
    CMP-003     RGS 3.8 and successors, with a versioned upgrade path

--- Where the rules live ---

Not here. Migration 0024 holds every one of them, as a constraint, a foreign
key or a trigger:

    the code exists          composite FK (rgs_version_id, rgs_code)
    in the pinned version    ledger_account_rgs_integrity(), derived not trusted
    it is postable           the same trigger
    its type matches         the same trigger
    reference data is fixed  unconditional RAISE triggers, no grants

So the checks in this module are a better error message before a round trip,
and nothing more. If this file and the database disagree, the database is
right - the same bargain api/ledger/model.py makes with 0020.

--- Why the RGS version is a row ---

CMP-003 asks for "a versioned upgrade path when a new RGS version is
released". A hardcoded seed cannot have one: upgrading would mean editing a
constant, deploying, and hoping every administration's chart still meant what
it said afterwards. With the version as data:

    load     scripts/load_rgs_version.py, a JSON file in, a version row out
    plan     plan_upgrade() - what it would do to this chart, per account
    apply    apply_upgrade() - refuses while anything needs a human

and the old version stays in the database, because CMP-014 wants historical
periods to keep the rules that applied at the time. Supporting RGS 4.0 is a
file, not a release.

--- Authorization ---

Appendix A's "View chart of accounts" capability, whose read permission is
("view", "chart_of_accounts") and whose full permission is ("manage",
"chart_of_accounts"). Evaluated per call through the single shared library,
against live state (CLAUDE.md rule 3).

Appendix A names no separate capability for an RGS version upgrade, so it
rides on `manage` - the same permission that lets a Bookkeeper add an account.
That is a wider grant than the operation deserves and it is recorded in
ADR-026 rather than fixed here, because inventing a permission the PRD does
not name is exactly what ADR-012 forbids: Appendix A is the source of truth,
and a gap in it is a question for the PRD, not a decision for this file.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.authz.model import (
    AdministrationScope,
    AuthorizationRequest,
    ResourceAttributes,
)
from api.authz.service import AuthorizationService
from api.ledger.model import AccountStatus, AccountType, ControlKind, LedgerError

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession

#: Appendix A, "View chart of accounts". R for Approver, Invoicer and Viewer.
VIEW_CHART = ("view", "chart_of_accounts")
#: The same capability's F column: Owner, Accountant, Bookkeeper.
MANAGE_CHART = ("manage", "chart_of_accounts")


class LegalForm(enum.Enum):
    """FR-ONB-004's list, closed, and mirrored by a CHECK in 0024.

    These are the CANONICAL forms, not what was captured. administration.
    legal_form holds whatever the KvK API returned (FR-ONB-002) - 'BV',
    'Besloten Vennootschap', 'bv' - and legal_form_alias maps those onto
    these. A spelling nobody has an alias for fails loudly at seeding rather
    than quietly seeding the wrong chart.
    """

    EENMANSZAAK = "eenmanszaak"
    VOF = "vof"
    BV = "bv"
    STICHTING = "stichting"
    VERENIGING = "vereniging"


class VatTreatment(enum.Enum):
    """FR-AR-002's treatments, which is what an account's VAT default is.

    Not a rate: rates change (CMP-014 effective-dates them) and a treatment
    does not. The full VAT code table with rates and rubriek mapping is
    FR-VAT's; this is the closed set an account can default to, mirrored by a
    CHECK constraint in 0024.
    """

    STANDARD = "btw_21"
    REDUCED = "btw_9"
    ZERO = "btw_0"
    EXEMPT = "btw_vrijgesteld"
    REVERSE_CHARGE = "btw_verlegd"
    INTRA_COMMUNITY = "btw_icp"
    EXPORT = "btw_export"
    MARGIN = "btw_marge"


class RgsVersionStatus(enum.Enum):
    DRAFT = "draft"
    CURRENT = "current"
    #: Terminal. Postings made while it was current still reference it
    #: (CMP-014), so it is never deleted and never revived.
    SUPERSEDED = "superseded"


class RgsSource(enum.Enum):
    OFFICIAL = "official-publication"
    #: A dataset that was not verified against the official publication. Safe
    #: to build and test against; not a basis for an XAF export (CMP-002) or
    #: an SBR filing (CMP-004), both of which carry these codes to a reader
    #: outside this system.
    PROVISIONAL = "provisional-subset"


class UpgradeResolution(enum.Enum):
    """What plan_upgrade() says about one account."""

    #: The mapping is unambiguous; apply_upgrade() will remap it.
    AUTOMATIC = "automatic"
    #: A split, a withdrawal, a missing mapping, or a target whose type or
    #: postability does not fit. A human decides.
    NEEDS_A_DECISION = "needs_a_decision"
    #: The account carries no RGS code, so an upgrade does not touch it.
    NOTHING_TO_DO = "nothing_to_do"


class ChartError(LedgerError):
    """Base for the chart's refusals."""


class ChartNotAuthorized(ChartError):
    """Named differently from periods.NotAuthorized on purpose: they are the
    same shape but not the same type, and collapsing them would mean a caller
    catching one silently catches the other's failures too.
    """

    def __init__(self, action: str, resource_type: str, detail: str) -> None:
        self.action = action
        self.resource_type = resource_type
        self.detail = detail
        super().__init__(f"not authorized to {action} {resource_type}: {detail}")


class UnknownLegalForm(ChartError):
    """FR-ONB-004's five forms, and a captured value that maps to none of them.

    Its own type because the fix is specific and knowable: add an alias, or
    correct the administration's legal form. Seeding the wrong chart would be
    worse than refusing.
    """


class RgsMappingViolation(ChartError):
    """FR-ONB-005's second clause. The account exists, the RGS code exists,
    and pairing them would misstate the taxonomy.
    """


class UpgradeBlocked(ChartError):
    """CMP-003. Accounts remain that no mapping can resolve, so applying would
    leave the chart half on each taxonomy.
    """

    def __init__(self, steps: Sequence[UpgradeStep]) -> None:
        self.steps = tuple(steps)
        super().__init__(
            f"{len(self.steps)} account(s) need a decision before this RGS upgrade can be applied"
        )


@dataclass(frozen=True, slots=True)
class RgsVersion:
    id: uuid.UUID
    version: str
    status: RgsVersionStatus
    source: RgsSource
    source_checksum: str
    published_at: date | None = None
    effective_from: date | None = None
    source_note: str | None = None
    supersedes_id: uuid.UUID | None = None
    loaded_at: datetime | None = None

    @property
    def is_provisional(self) -> bool:
        return self.source is RgsSource.PROVISIONAL


@dataclass(frozen=True, slots=True)
class RgsElement:
    """One reference code. `account_type` is LEDGR's FR-GL-005 classification
    of it, which is a mapping decision and therefore stored as data rather
    than derived in code.
    """

    code: str
    description_nl: str
    level: int
    is_postable: bool
    description_en: str | None = None
    account_type: AccountType | None = None
    debit_credit: str | None = None
    parent_code: str | None = None


@dataclass(frozen=True, slots=True)
class ChartAccount:
    """A row of the chart as a user reads it: the account, plus what its RGS
    code means, plus whether the seed put it there.
    """

    account_id: uuid.UUID
    code: str
    name: str
    account_type: AccountType
    status: AccountStatus
    rgs_code: str | None = None
    rgs_description_nl: str | None = None
    rgs_description_en: str | None = None
    rgs_version: str | None = None
    default_vat_code: VatTreatment | None = None
    control_kind: ControlKind | None = None
    #: FR-ONB-005's two populations. Derived from the profile rather than
    #: stored, so it stays true when a later profile adds an account the user
    #: had already created themselves.
    is_seeded: bool = False

    @property
    def is_mapped(self) -> bool:
        return self.rgs_code is not None


@dataclass(frozen=True, slots=True)
class SeedResult:
    seeded_count: int
    skipped_count: int
    rgs_version: str
    legal_form: LegalForm

    @property
    def total(self) -> int:
        return self.seeded_count + self.skipped_count


@dataclass(frozen=True, slots=True)
class UpgradeStep:
    """What an upgrade would do to one account, before anything is written."""

    account_id: uuid.UUID
    account_code: str
    account_name: str
    account_type: AccountType
    resolution: UpgradeResolution
    change_kind: str
    detail: str
    from_rgs_code: str | None = None
    to_rgs_code: str | None = None


@dataclass(frozen=True, slots=True)
class UpgradePlan:
    from_version: str
    to_version: str
    steps: tuple[UpgradeStep, ...] = ()

    def by_resolution(self, resolution: UpgradeResolution) -> tuple[UpgradeStep, ...]:
        return tuple(s for s in self.steps if s.resolution is resolution)

    @property
    def blocked(self) -> tuple[UpgradeStep, ...]:
        return self.by_resolution(UpgradeResolution.NEEDS_A_DECISION)

    @property
    def can_apply(self) -> bool:
        return not self.blocked


@dataclass(frozen=True, slots=True)
class UpgradeResult:
    remapped_count: int
    unmapped_count: int
    rgs_version: str


@dataclass(frozen=True, slots=True)
class RgsReadiness:
    """CMP-003 and CMP-013, as something an operator can query.

    Two questions: is this administration on a dataset nobody verified against
    the official publication, and is it behind the current version.
    """

    administration_id: uuid.UUID
    legal_form: LegalForm
    rgs_version: str
    version_status: RgsVersionStatus
    version_source: RgsSource
    is_provisional: bool
    is_behind_current: bool
    accounts_total: int
    accounts_unmapped: int

    @property
    def ready_to_file(self) -> bool:
        """CMP-002's XAF export and CMP-004's SBR filing both carry RGS codes
        to a reader outside this system. Neither is safe from a provisional
        dataset, a superseded version, or a chart with unmapped accounts.
        """
        return (
            not self.is_provisional and not self.is_behind_current and self.accounts_unmapped == 0
        )


@dataclass(frozen=True, slots=True)
class MappingDeviation:
    """An account whose stored mapping contradicts the rules 0024 enforces.

    Structurally impossible - the trigger derives the version and checks the
    type on every write - and reported anyway, for the reason 0020 gives about
    its gap report: a check that can only ever be empty is the check on the
    thing that makes it empty.
    """

    account_id: uuid.UUID
    administration_id: uuid.UUID
    account_code: str
    deviation: str
    detail: str


class ChartRepository(Protocol):
    async def seed(
        self,
        *,
        administration_id: uuid.UUID,
        rgs_version_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        profile_code: str,
    ) -> SeedResult: ...

    async def chart(
        self, *, administration_id: uuid.UUID, include_blocked: bool
    ) -> Sequence[ChartAccount]: ...

    async def add_account(
        self,
        *,
        administration_id: uuid.UUID,
        code: str,
        name: str,
        account_type: AccountType,
        rgs_code: str | None,
        default_vat_code: VatTreatment | None,
        control_kind: ControlKind | None,
    ) -> ChartAccount: ...

    async def set_account_status(
        self, *, account_id: uuid.UUID, status: AccountStatus
    ) -> ChartAccount: ...

    async def set_account_rgs_code(
        self, *, account_id: uuid.UUID, rgs_code: str | None
    ) -> ChartAccount: ...

    async def rgs_options(
        self, *, administration_id: uuid.UUID, account_type: AccountType
    ) -> Sequence[RgsElement]: ...

    async def current_version(self) -> RgsVersion | None: ...

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def version(self, *, version: str) -> RgsVersion | None: ...

    async def pinned_version(self, *, administration_id: uuid.UUID) -> RgsVersion | None: ...

    async def plan_upgrade(
        self, *, administration_id: uuid.UUID, to_version_id: uuid.UUID
    ) -> Sequence[UpgradeStep]: ...

    async def apply_upgrade(
        self,
        *,
        administration_id: uuid.UUID,
        to_version_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
    ) -> UpgradeResult: ...

    async def readiness(self, *, administration_id: uuid.UUID | None) -> Sequence[RgsReadiness]: ...

    async def mapping_deviations(
        self, *, administration_id: uuid.UUID | None
    ) -> Sequence[MappingDeviation]: ...


class ChartOfAccountsService:
    """FR-GL-005 and FR-ONB-005's entry point.

    Every method that changes anything is authorized per call and audited.
    IAM-090 names configuration changes, and the chart of accounts is the
    configuration every later number is expressed in: an account's type or its
    RGS code is what a filing is built from, so a change to one with nobody's
    name on it is exactly what an audit log is for.
    """

    def __init__(
        self,
        repository: ChartRepository,
        authorization: AuthorizationService,
        audit_log: AuditLog,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._audit = audit_log

    # -- FR-ONB-005: seeding ----------------------------------------------

    async def seed(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        rgs_version_id: uuid.UUID | None = None,
        profile_code: str = "mkb",
        correlation_id: str | None = None,
    ) -> SeedResult:
        """Seed the chart from the MKB profile for the administration's legal
        form, pinning it to an RGS version.

        Idempotent: re-running adds what is missing and touches nothing that
        exists. FR-ONB-010 makes onboarding resumable, so this WILL be called
        twice, and an administration that has already posted must not have its
        chart rebuilt underneath it.

        The legal form is not a parameter. It is read from the administration
        (FR-ONB-004), because a caller that could name a different one could
        seed a BV's chart into an eenmanszaak and the two differ exactly where
        it matters - equity, payroll, corporate income tax.
        """
        await self._require(
            MANAGE_CHART, user_id=actor_user_id, administration_id=administration_id
        )

        result = await self._repository.seed(
            administration_id=administration_id,
            rgs_version_id=rgs_version_id,
            actor_user_id=actor_user_id,
            profile_code=profile_code,
        )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="seed_chart_of_accounts",
            resource_type="chart_of_accounts",
            resource_id=administration_id,
            correlation_id=correlation_id,
            detail={
                "rgs_version": result.rgs_version,
                "legal_form": result.legal_form.value,
                "profile_code": profile_code,
                "seeded": result.seeded_count,
                "skipped": result.skipped_count,
            },
        )
        return result

    # -- FR-GL-005: reading -----------------------------------------------

    async def chart(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        include_blocked: bool = True,
    ) -> Sequence[ChartAccount]:
        """FR-GL-005. Blocked accounts are included by default: they keep
        their history and a chart that hid them would make last year's numbers
        unreadable.
        """
        await self._require(VIEW_CHART, user_id=actor_user_id, administration_id=administration_id)
        return await self._repository.chart(
            administration_id=administration_id, include_blocked=include_blocked
        )

    async def rgs_options(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        account_type: AccountType,
    ) -> Sequence[RgsElement]:
        """The elements an account of this type may legally be mapped to.

        Populated from the same rule the trigger enforces, so a picker cannot
        offer a choice the database will refuse - PRD design principle D5, no
        dead ends.
        """
        await self._require(VIEW_CHART, user_id=actor_user_id, administration_id=administration_id)
        return await self._repository.rgs_options(
            administration_id=administration_id, account_type=account_type
        )

    # -- FR-ONB-005: extending --------------------------------------------

    async def add_account(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        code: str,
        name: str,
        account_type: AccountType,
        rgs_code: str | None = None,
        default_vat_code: VatTreatment | None = None,
        control_kind: ControlKind | None = None,
        correlation_id: str | None = None,
    ) -> ChartAccount:
        """ "The user may extend" - the half of FR-ONB-005 that is a feature.

        An account may be added with no RGS code at all: 0020 made the column
        nullable deliberately, because a customer may carry an account with no
        RGS equivalent. It is then reported as unmapped by `readiness()`
        rather than passing silently, because an unmapped account is one the
        XAF export (CMP-002) cannot describe.
        """
        await self._require(
            MANAGE_CHART, user_id=actor_user_id, administration_id=administration_id
        )

        account = await self._repository.add_account(
            administration_id=administration_id,
            code=code,
            name=name,
            account_type=account_type,
            rgs_code=rgs_code,
            default_vat_code=default_vat_code,
            control_kind=control_kind,
        )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="add_account",
            resource_type="ledger_account",
            resource_id=account.account_id,
            correlation_id=correlation_id,
            detail={
                "code": account.code,
                "account_type": account.account_type.value,
                "rgs_code": account.rgs_code,
                "control_kind": (account.control_kind.value if account.control_kind else None),
            },
        )
        return account

    async def remap_account(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        account_id: uuid.UUID,
        rgs_code: str | None,
        correlation_id: str | None = None,
    ) -> ChartAccount:
        """Correct one account's RGS code, or clear it.

        The type must still match and the element must still be postable -
        ledger_account_rgs_integrity() runs on the UPDATE exactly as on the
        INSERT. Remapping across account types is not a correction, it is a
        different account.
        """
        await self._require(
            MANAGE_CHART, user_id=actor_user_id, administration_id=administration_id
        )

        before = await self._account(administration_id, account_id)
        account = await self._repository.set_account_rgs_code(
            account_id=account_id, rgs_code=rgs_code
        )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="remap_account_rgs_code",
            resource_type="ledger_account",
            resource_id=account_id,
            correlation_id=correlation_id,
            detail={
                "code": account.code,
                "from_rgs_code": before.rgs_code if before else None,
                "to_rgs_code": account.rgs_code,
            },
        )
        return account

    async def set_account_status(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        account_id: uuid.UUID,
        status: AccountStatus,
        correlation_id: str | None = None,
    ) -> ChartAccount:
        """FR-GL-005's blocked/active state.

        A blocked account keeps every posting it already has and takes no new
        ones. Deleting is not offered and never will be: its history is in an
        append-only table.
        """
        await self._require(
            MANAGE_CHART, user_id=actor_user_id, administration_id=administration_id
        )

        account = await self._repository.set_account_status(account_id=account_id, status=status)

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="set_account_status",
            resource_type="ledger_account",
            resource_id=account_id,
            correlation_id=correlation_id,
            detail={"code": account.code, "status": status.value},
        )
        return account

    # -- CMP-003: the upgrade path ----------------------------------------

    async def plan_upgrade(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        to_version: str,
    ) -> UpgradePlan:
        """What an upgrade would do to this chart, account by account.

        A read, so it needs only `view`: seeing the consequences of a change
        is not the change. Nothing is written, which is the point - an
        administration can be shown this and decide.
        """
        await self._require(VIEW_CHART, user_id=actor_user_id, administration_id=administration_id)

        target = await self._require_version(to_version)
        current = await self._repository.pinned_version(administration_id=administration_id)
        if current is None:
            raise ChartError(
                f"administration {administration_id} has no chart of accounts to "
                "upgrade; seed it first (FR-ONB-005)"
            )

        steps = await self._repository.plan_upgrade(
            administration_id=administration_id, to_version_id=target.id
        )
        return UpgradePlan(
            from_version=current.version,
            to_version=target.version,
            steps=tuple(steps),
        )

    async def apply_upgrade(
        self,
        *,
        administration_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        to_version: str,
        correlation_id: str | None = None,
    ) -> UpgradeResult:
        """Move this administration's chart to a new RGS version.

        The plan is re-read here and refused if anything is unresolved, even
        though `apply_rgs_upgrade` refuses too. Two reasons: the caller gets
        the blocking rows rather than a message, and the check is close enough
        to the decision to be worth reading. The database's refusal is the
        guarantee; this is the explanation.
        """
        await self._require(
            MANAGE_CHART, user_id=actor_user_id, administration_id=administration_id
        )

        target = await self._require_version(to_version)
        plan = await self._repository.plan_upgrade(
            administration_id=administration_id, to_version_id=target.id
        )
        blocked = [step for step in plan if step.resolution is UpgradeResolution.NEEDS_A_DECISION]
        if blocked:
            raise UpgradeBlocked(blocked)

        result = await self._repository.apply_upgrade(
            administration_id=administration_id,
            to_version_id=target.id,
            actor_user_id=actor_user_id,
        )

        await self._record(
            administration_id=administration_id,
            user_id=actor_user_id,
            action="upgrade_rgs_version",
            resource_type="chart_of_accounts",
            resource_id=administration_id,
            correlation_id=correlation_id,
            detail={
                "to_rgs_version": result.rgs_version,
                "remapped": result.remapped_count,
                "unmapped": result.unmapped_count,
            },
        )
        return result

    async def readiness(
        self, *, administration_id: uuid.UUID | None = None
    ) -> Sequence[RgsReadiness]:
        """CMP-003 / CMP-013's operator view, across tenants when called with
        no administration.

        Deliberately NOT authorized against a user: the caller is an operator
        sweep on a `ledgr_ops` connection with no user at all, the same shape
        scripts/anchor_audit_chain.py has. A tenant-facing "is my chart ready
        to file" is a different call on a different connection, and RLS scopes
        it there.
        """
        return await self._repository.readiness(administration_id=administration_id)

    async def mapping_deviations(
        self, *, administration_id: uuid.UUID | None = None
    ) -> Sequence[MappingDeviation]:
        return await self._repository.mapping_deviations(administration_id=administration_id)

    # -- internals ---------------------------------------------------------

    async def _account(
        self, administration_id: uuid.UUID, account_id: uuid.UUID
    ) -> ChartAccount | None:
        for account in await self._repository.chart(
            administration_id=administration_id, include_blocked=True
        ):
            if account.account_id == account_id:
                return account
        return None

    async def _require_version(self, version: str) -> RgsVersion:
        found = await self._repository.version(version=version)
        if found is None:
            raise ChartError(
                f"RGS version {version} is not loaded. Load it with "
                "scripts/load_rgs_version.py before upgrading onto it (CMP-003)."
            )
        return found

    async def _require(
        self,
        permission: tuple[str, str],
        *,
        user_id: uuid.UUID,
        administration_id: uuid.UUID,
    ) -> None:
        """Evaluated per call, against live state, through the one library.

        The administration is the TARGET - which scope the grant must cover.
        No resource attributes: a chart has no period and no amount, so
        IAM-033's conditions have nothing to bind to here, and passing
        something irrelevant would only make a conditioned grant deny for a
        reason nobody could act on.
        """
        action, resource_type = permission
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
                resource_type="chart_of_accounts",
                resource_id=administration_id,
                outcome=AuditOutcome.DENIED,
                detail={"reason": decision.reason, "detail": decision.detail},
            )
            raise ChartNotAuthorized(action, resource_type, decision.detail or decision.reason)

    async def _record(
        self,
        *,
        administration_id: uuid.UUID,
        user_id: uuid.UUID,
        action: str,
        resource_type: str,
        resource_id: uuid.UUID,
        detail: Mapping[str, object] | None = None,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        correlation_id: str | None = None,
    ) -> None:
        await self._audit.record(
            AuditEvent(
                organization_id=await self._organization_of(administration_id),
                administration_id=administration_id,
                category=AuditCategory.CONFIGURATION,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=outcome,
                actor_type=ActorType.USER,
                actor_user_id=user_id,
                correlation_id=correlation_id,
                detail=dict(detail or {}),
            )
        )

    async def _organization_of(self, administration_id: uuid.UUID) -> uuid.UUID:
        """The audit entry's tenant.

        Read through the repository rather than passed in, for the reason
        every other derive-don't-trust rule in this codebase exists: a caller
        that could name the organization an entry is filed under could file it
        under someone else's.

        Read from the ADMINISTRATION, not from the RGS pin. A denied attempt
        on an administration whose chart has never been seeded still has to be
        auditable - IAM-090 wants the denial recorded - and keying it on the
        pin would have made exactly that case raise instead.
        """
        organization_id = await self._repository.organization_of(
            administration_id=administration_id
        )
        if organization_id is None:
            raise ChartError(f"administration {administration_id} does not exist")
        return organization_id


def build_chart_service(
    session: AsyncSession, authorization: AuthorizationService, audit_log: AuditLog
) -> ChartOfAccountsService:
    """A wired ChartOfAccountsService, for callers outside this bounded context.

    The same deliberate widening `api.ledger.service.build_ledger_service`
    is - see its docstring. `tests/ledger/test_bounded_context.py` keeps
    `api.ledger.chart_repository` internal to this package (it belongs to this
    service alone), so a caller that needs the chart of accounts imports this
    function rather than the repository. `api.dashboard.service` is the first
    caller: it needs `rgs_code` per account to identify liquid-means accounts
    for MOB-006's cash-position figure, which `LedgerService.trial_balance()`
    does not carry (RGS mapping belongs to this context, not the ledger's
    posting reports).

    The repository import is function-local for the same reason
    `build_ledger_service`'s is: so the only place naming
    `api.ledger.chart_repository` outside a test stays this one line, where
    the bounded-context check can see it is the context's own.
    """
    from api.ledger.chart_repository import SqlChartRepository

    return ChartOfAccountsService(SqlChartRepository(session), authorization, audit_log)
