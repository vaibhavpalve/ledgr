"""Client access profiles: IAM-100 through IAM-104 (PRD §8.6).

In Model A the firm decides what the CLIENT's own users may do inside their
administration. A profile is a **cap, not a grant**: resolution intersects
it with what the user's roles already carry, so a profile can only ever
withhold. That asymmetry is the reason profiles compose safely with the rest
of the authorization model, and the reason IAM-102's ceiling is the one
escalation surface here worth guarding.

    IAM-100  per managed administration the firm selects a profile;
             profiles are firm-defined, reusable across clients, versioned
    IAM-101  three built-ins ship, and a firm may compose its own
    IAM-102  a firm cannot grant a client's users a permission the firm
             itself does not hold on that administration
    IAM-103  changes take effect within 60 seconds, are logged, and the
             client's Owner is notified with a plain-language summary;
             silent reduction of a client's access is prohibited
    IAM-104  the firm may restrict, per profile: visible journals, bank
             transaction detail, reports, period editing, approval ceilings

--- IAM-102, the constraint that matters ---

"The firm itself" is the FIRM, not the acting user. A Firm Manager holds no
ledger permissions at all (§8.4) yet assigning profiles is precisely their
job, so checking the acting user's own ceiling - the IAM-036 rule for role
authoring - would make the feature unusable and is not what the requirement
says. The ceiling here is the union of what the firm's staff hold on THAT
administration, which is exactly "what the firm can do there".

It is checked in both directions that can widen a client's access:
assigning a profile, and publishing a new version of a profile that
administrations are already using. Skipping the second would let a firm
assign a modest profile and then edit it upward.

--- IAM-104 restrictions compile down, they do not add a second path ---

A restriction is firm-facing configuration. `resolve()` turns it into the
two things the existing model already understands: a smaller permission set
(the booleans withhold `view bank_transaction`, `view report`, `lock
period`) and ordinary IAM-033 attribute conditions (visible journals,
approval ceiling). Nothing downstream of resolve() knows profiles exist.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from api.audit.log import AuditOutcome
from api.audit.trail import AuditTrail
from api.authz.rights_floor import ADMINISTRATION_FLOOR

# IAM-104's closed set, mirroring client_access_profile_restrictions_guard().
RESTRICTION_KEYS = frozenset(
    {
        "visible_journal_ids",
        "bank_detail_visible",
        "reports_visible",
        "periods_editable",
        "approval_amount_ceiling",
    }
)

Permission = tuple[str, str]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ProfileCeilingError(Exception):
    """IAM-102: the profile would give the client something the firm does
    not hold on that administration.
    """

    def __init__(
        self,
        missing: Iterable[Permission],
        *,
        administration_id: uuid.UUID | None = None,
    ) -> None:
        self.missing = sorted(missing)
        self.administration_id = administration_id
        listed = ", ".join(f"{a} {r}" for a, r in self.missing)
        where = f" on administration {administration_id}" if administration_id else ""
        super().__init__(f"the firm does not hold{where}, and so cannot grant the client: {listed}")


class ProfileNotAvailableError(Exception):
    """No such profile, another firm's profile, or an archived one - one
    message for all three, so a firm cannot probe for other firms' profile
    names.
    """


class UnknownRestrictionError(Exception):
    """See RESTRICTION_KEYS."""


@dataclass(frozen=True, slots=True)
class ProfileRestrictions:
    """IAM-104. Every field's default is the UNRESTRICTED value, so a
    profile that says nothing restricts nothing - and a restriction only
    ever takes access away, never adds it.
    """

    visible_journal_ids: tuple[uuid.UUID, ...] | None = None
    bank_detail_visible: bool = True
    reports_visible: bool = True
    periods_editable: bool = True
    approval_amount_ceiling: Decimal | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ProfileRestrictions:
        unknown = set(raw) - RESTRICTION_KEYS
        if unknown:
            raise UnknownRestrictionError(
                f"unknown profile restriction(s): {sorted(unknown)}; IAM-104's set is closed"
            )

        journals = raw.get("visible_journal_ids")
        ceiling = raw.get("approval_amount_ceiling")

        if isinstance(ceiling, float):
            # NFR-031. The migration refuses to store a JSON number; this is
            # the second line, for a value that reached memory another way.
            raise UnknownRestrictionError(
                "approval_amount_ceiling must be a decimal string, never a float"
            )
        parsed_ceiling: Decimal | None = None
        if ceiling is not None:
            try:
                parsed_ceiling = Decimal(str(ceiling))
            except (InvalidOperation, ValueError) as exc:
                raise UnknownRestrictionError(
                    f"approval_amount_ceiling {ceiling!r} is not a decimal"
                ) from exc

        return cls(
            visible_journal_ids=(
                tuple(uuid.UUID(str(j)) for j in journals) if journals is not None else None
            ),
            bank_detail_visible=bool(raw.get("bank_detail_visible", True)),
            reports_visible=bool(raw.get("reports_visible", True)),
            periods_editable=bool(raw.get("periods_editable", True)),
            approval_amount_ceiling=parsed_ceiling,
        )

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.visible_journal_ids is not None:
            out["visible_journal_ids"] = [str(j) for j in self.visible_journal_ids]
        if not self.bank_detail_visible:
            out["bank_detail_visible"] = False
        if not self.reports_visible:
            out["reports_visible"] = False
        if not self.periods_editable:
            out["periods_editable"] = False
        if self.approval_amount_ceiling is not None:
            out["approval_amount_ceiling"] = str(self.approval_amount_ceiling)
        return out


@dataclass(frozen=True, slots=True)
class ProfileVersion:
    id: uuid.UUID
    profile_id: uuid.UUID
    version: int
    summary: str
    permissions: frozenset[Permission]
    restrictions: ProfileRestrictions
    published_by_user_id: uuid.UUID | None = None
    published_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ClientAccessProfile:
    id: uuid.UUID
    name: str
    description: str
    is_builtin: bool
    firm_organization_id: uuid.UUID | None = None
    archived_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResolvedAccess:
    """What a profile actually permits, in the terms the rest of the
    authorization model already speaks: a permission set, plus IAM-033
    conditions to apply to what survives.
    """

    permissions: frozenset[Permission]
    conditions: Mapping[str, Any] = field(default_factory=dict)
    profile_name: str = ""
    profile_version: int = 0


# ---------------------------------------------------------------------------
# IAM-105's client rights floor, as it bears on profiles
# ---------------------------------------------------------------------------
# Defined in api.authz.rights_floor - this module needs only the
# administration-scoped subset, because a profile cannot contain an
# organization-scope permission at all (0013's permission guard), so the
# other two floor rights are beyond a profile's reach by construction.
#
# These survive every profile, for the client's OWNER. A profile that omits
# them is not rejected - it simply cannot take them away.
CLIENT_RIGHTS_FLOOR: frozenset[Permission] = ADMINISTRATION_FLOOR


# ---------------------------------------------------------------------------
# IAM-101's three built-in profiles
# ---------------------------------------------------------------------------
# Each is stated as the PRD states it, then as permissions. The prose is
# kept alongside so a reader can check the translation without opening §8.6.
BUILTIN_PROFILES: tuple[tuple[str, str, frozenset[Permission], ProfileRestrictions], ...] = (
    (
        "Capture only",
        "Upload documents, submit expenses, view own submissions.",
        frozenset(
            {
                ("view", "administration"),
                ("upload", "document"),
                ("view", "document"),
                ("submit", "expense"),
            }
        ),
        # Nothing financial is visible at this level.
        ProfileRestrictions(
            bank_detail_visible=False, reports_visible=False, periods_editable=False
        ),
    ),
    (
        "Invoice and capture",
        "Adds sales invoicing, customers, and viewing own reports.",
        frozenset(
            {
                ("view", "administration"),
                ("upload", "document"),
                ("view", "document"),
                ("submit", "expense"),
                ("create", "sales_invoice"),
                ("send", "sales_invoice"),
                ("manage", "customer"),
                ("view", "report"),
                ("view", "chart_of_accounts"),
            }
        ),
        ProfileRestrictions(bank_detail_visible=False, periods_editable=False),
    ),
    (
        "Full self-service",
        "Adds coding, bank reconciliation and VAT preparation, while the firm "
        "retains filing and period control.",
        frozenset(
            {
                ("view", "administration"),
                ("upload", "document"),
                ("view", "document"),
                ("submit", "expense"),
                ("create", "sales_invoice"),
                ("send", "sales_invoice"),
                ("manage", "customer"),
                ("view", "report"),
                ("view", "chart_of_accounts"),
                ("view", "purchase_invoice"),
                ("code", "purchase_invoice"),
                ("view", "bank_transaction"),
                ("reconcile", "bank_transaction"),
                ("prepare", "vat_return"),
                ("export", "report_data"),
                ("read", "audit_log"),
                # Deliberately absent, per IAM-101's "while the firm retains
                # filing and period control": file vat_return, lock period,
                # close fiscal_year. Also absent: post/reverse journal_entry,
                # release payment_batch - "full self-service" in §8.6's sense
                # is bookkeeping preparation, not the firm's own ledger and
                # payment authority.
            }
        ),
        # periods_editable stays False even at the most permissive level:
        # IAM-101 says the firm retains period control.
        ProfileRestrictions(periods_editable=False),
    ),
)


def builtin_profile_names() -> tuple[str, ...]:
    return tuple(name for name, _, _, _ in BUILTIN_PROFILES)


# ---------------------------------------------------------------------------
# Restriction compilation (IAM-104)
# ---------------------------------------------------------------------------
# Which permission each boolean restriction withholds. Stated as data so the
# mapping is checkable rather than buried in an if-chain.
_BOOLEAN_RESTRICTIONS: tuple[tuple[str, frozenset[Permission]], ...] = (
    ("bank_detail_visible", frozenset({("view", "bank_transaction")})),
    ("reports_visible", frozenset({("view", "report"), ("export", "report_data")})),
    ("periods_editable", frozenset({("lock", "period"), ("close", "fiscal_year")})),
)


def resolve(
    version: ProfileVersion, *, profile_name: str = "", is_client_owner: bool = False
) -> ResolvedAccess:
    """Compiles a profile version into a permission set and IAM-033
    conditions.

    is_client_owner adds IAM-105's floor back. The floor is added AFTER the
    restrictions are applied, deliberately: a firm that switches reports off
    still cannot take the client Owner's export of their own data away,
    which is the whole point of a floor.
    """
    permissions = set(version.permissions)

    for attribute, withheld in _BOOLEAN_RESTRICTIONS:
        if not getattr(version.restrictions, attribute):
            permissions -= withheld

    conditions: dict[str, Any] = {}
    if version.restrictions.visible_journal_ids is not None:
        conditions["journal_ids"] = [str(j) for j in version.restrictions.visible_journal_ids]
    if version.restrictions.approval_amount_ceiling is not None:
        # A decimal STRING, matching how IAM-033 conditions are stored and
        # compared everywhere else (NFR-031).
        conditions["amount_ceiling"] = str(version.restrictions.approval_amount_ceiling)

    if is_client_owner:
        permissions |= CLIENT_RIGHTS_FLOOR

    return ResolvedAccess(
        permissions=frozenset(permissions),
        conditions=conditions,
        profile_name=profile_name,
        profile_version=version.version,
    )


# ---------------------------------------------------------------------------
# IAM-103: plain-language change summaries
# ---------------------------------------------------------------------------
_HUMAN_READABLE: Mapping[Permission, str] = {
    ("view", "administration"): "see the administration",
    ("upload", "document"): "upload documents",
    ("view", "document"): "view source documents",
    ("submit", "expense"): "submit expenses",
    ("approve", "expense"): "approve expenses",
    ("create", "sales_invoice"): "create sales invoices",
    ("send", "sales_invoice"): "send sales invoices",
    ("manage", "customer"): "manage customers",
    ("view", "report"): "view reports",
    ("export", "report_data"): "export data",
    ("view", "chart_of_accounts"): "view the chart of accounts",
    ("view", "purchase_invoice"): "view purchase invoices",
    ("code", "purchase_invoice"): "code purchase invoices",
    ("approve", "purchase_invoice"): "approve purchase invoices",
    ("view", "bank_transaction"): "view bank transactions",
    ("reconcile", "bank_transaction"): "reconcile the bank",
    ("prepare", "vat_return"): "prepare VAT returns",
    ("file", "vat_return"): "file VAT returns",
    ("post", "journal_entry"): "post journal entries",
    ("reverse", "journal_entry"): "reverse postings",
    ("lock", "period"): "lock and unlock periods",
    ("close", "fiscal_year"): "run the year-end close",
    ("read", "audit_log"): "read the audit log",
    ("create", "payment_batch"): "create payment batches",
    ("release", "payment_batch"): "release payments to the bank",
    ("manage", "bank_consent"): "connect and revoke bank consent",
}


def _phrase(permission: Permission) -> str:
    action, resource_type = permission
    return _HUMAN_READABLE.get(permission, f"{action} {resource_type.replace('_', ' ')}")


@dataclass(frozen=True, slots=True)
class AccessChange:
    """IAM-103's notification payload. `removed` is the field that matters:
    "silent reduction of a client's access is prohibited", so a change that
    takes something away must be describable, and is.
    """

    administration_id: uuid.UUID
    profile_name: str
    added: tuple[Permission, ...]
    removed: tuple[Permission, ...]
    summary: str

    @property
    def is_reduction(self) -> bool:
        return bool(self.removed)


def describe_change(
    *,
    administration_id: uuid.UUID,
    profile_name: str,
    before: frozenset[Permission],
    after: frozenset[Permission],
) -> AccessChange:
    """Plain language, per IAM-103 - written for a client Owner who has
    never seen a permission triple, not for a developer reading a diff.
    """
    added = tuple(sorted(after - before))
    removed = tuple(sorted(before - after))

    parts: list[str] = []
    if added:
        parts.append("You can now " + _join(_phrase(p) for p in added) + ".")
    if removed:
        parts.append("You can no longer " + _join(_phrase(p) for p in removed) + ".")
    if not parts:
        parts.append("Nothing about what you can do has changed.")

    summary = (
        f"Your accountant set the access level for this administration to "
        f"'{profile_name}'. " + " ".join(parts)
    )

    return AccessChange(
        administration_id=administration_id,
        profile_name=profile_name,
        added=added,
        removed=removed,
        summary=summary,
    )


def _join(phrases: Iterable[str]) -> str:
    items = list(phrases)
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


class ProfileChangeNotifier(Protocol):
    """IAM-103: the client's Owner is notified. Not optional and not
    best-effort - see ClientAccessProfileService.assign_profile for why a
    failure here aborts the change rather than being swallowed.
    """

    async def notify_client_owner(self, change: AccessChange) -> None: ...


class LoggingProfileChangeNotifier:
    """Real, not a stub: a structured log line is a durable record. Wiring
    this to email/in-app notification is future work this interface is ready
    for.
    """

    def __init__(self) -> None:
        import logging

        self._logger = logging.getLogger("api.authz.profiles")

    async def notify_client_owner(self, change: AccessChange) -> None:
        self._logger.info(
            "client_access_profile_changed",
            extra={
                "administration_id": str(change.administration_id),
                "profile_name": change.profile_name,
                "added": [f"{a} {r}" for a, r in change.added],
                "removed": [f"{a} {r}" for a, r in change.removed],
                "summary": change.summary,
            },
        )


class ProfileRepository(Protocol):
    async def get_profile(self, profile_id: uuid.UUID) -> ClientAccessProfile | None: ...

    async def get_profile_by_name(self, name: str) -> ClientAccessProfile | None: ...

    async def current_version(self, profile_id: uuid.UUID) -> ProfileVersion | None: ...

    async def current_profile_for(
        self, administration_id: uuid.UUID
    ) -> tuple[ClientAccessProfile, ProfileVersion] | None: ...

    async def administrations_using(self, profile_id: uuid.UUID) -> Sequence[uuid.UUID]: ...

    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None:
        """Whose audit log a profile change belongs in (IAM-090, IAM-111)."""
        ...

    async def firm_permissions_on(
        self, *, firm_organization_id: uuid.UUID, administration_id: uuid.UUID
    ) -> set[Permission]:
        """IAM-102's ceiling: what the FIRM can do on this administration -
        the union of live, unconditioned grants held by the firm's users
        there. See this module's docstring for why it is the firm's holdings
        and not the acting user's.
        """
        ...

    async def create_profile(
        self,
        *,
        firm_organization_id: uuid.UUID,
        name: str,
        description: str,
        created_by_user_id: uuid.UUID,
    ) -> uuid.UUID: ...

    async def publish_version(
        self,
        *,
        profile_id: uuid.UUID,
        version: int,
        summary: str,
        permissions: frozenset[Permission],
        restrictions: Mapping[str, Any],
        published_by_user_id: uuid.UUID,
    ) -> uuid.UUID: ...

    async def assign_profile(
        self,
        *,
        administration_id: uuid.UUID,
        profile_id: uuid.UUID,
        assigned_by_user_id: uuid.UUID,
        at: datetime,
    ) -> uuid.UUID: ...


class ClientAccessProfileService:
    def __init__(
        self,
        repository: ProfileRepository,
        notifier: ProfileChangeNotifier,
        *,
        clock: Callable[[], datetime] = _utcnow,
        audit: AuditTrail | None = None,
    ) -> None:
        self._repository = repository
        self._notifier = notifier
        self._clock = clock
        # IAM-090's "configuration changes", and the half of IAM-111 that
        # concerns the CLIENT's log ("client access profiles, and every
        # change to them, appear in both the firm's and the client's audit
        # log"). The firm's own copy of the same event needs the firm's
        # organization on the entry, which this log's per-tenant chain cannot
        # carry twice - see ADR-021.
        self._audit = audit

    async def assign_profile(
        self,
        *,
        firm_organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        profile_id: uuid.UUID,
        assigned_by_user_id: uuid.UUID,
    ) -> AccessChange:
        """IAM-100 plus IAM-102 plus IAM-103, in that order: select the
        profile, check the firm may confer what it contains, then tell the
        client what changed.
        """
        profile = await self._available_profile(profile_id, firm_organization_id)
        version = await self._repository.current_version(profile.id)
        if version is None:
            raise ProfileNotAvailableError(f"profile {profile.name!r} has no published version")

        await self._enforce_ceiling(
            firm_organization_id=firm_organization_id,
            administration_id=administration_id,
            permissions=resolve(version, profile_name=profile.name).permissions,
        )

        before = await self._current_permissions(administration_id)
        after = resolve(version, profile_name=profile.name).permissions

        await self._repository.assign_profile(
            administration_id=administration_id,
            profile_id=profile.id,
            assigned_by_user_id=assigned_by_user_id,
            at=self._clock(),
        )

        change = describe_change(
            administration_id=administration_id,
            profile_name=profile.name,
            before=before,
            after=after,
        )
        # IAM-103: "silent reduction of a client's access is prohibited."
        # The notification is part of the change, not a side effect of it -
        # a notifier that raises aborts the caller's transaction rather than
        # leaving a reduction nobody was told about. That is the opposite of
        # the geolocation resolver's degrade-to-None (ADR-010), and the
        # difference is that this one is a requirement rather than
        # enrichment.
        await self._notifier.notify_client_owner(change)

        if self._audit is not None:
            owner = await self._repository.owning_organization(administration_id)
            if owner is not None:
                await self._audit.configuration_change(
                    organization_id=owner,
                    administration_id=administration_id,
                    actor_user_id=assigned_by_user_id,
                    action="assign",
                    resource_type="client_access_profile",
                    resource_id=profile.id,
                    outcome=AuditOutcome.SUCCESS,
                    detail={
                        "profile": profile.name,
                        "firm_organization_id": str(firm_organization_id),
                        "added": [f"{a} {r}" for a, r in change.added],
                        "removed": [f"{a} {r}" for a, r in change.removed],
                        # IAM-103 forbids silent reduction; recording which
                        # changes took something away makes "was any client
                        # quietly restricted" a query rather than a diff.
                        "is_reduction": change.is_reduction,
                    },
                )

        return change

    async def publish_version(
        self,
        *,
        firm_organization_id: uuid.UUID,
        profile_id: uuid.UUID,
        summary: str,
        permissions: frozenset[Permission],
        restrictions: ProfileRestrictions | None = None,
        published_by_user_id: uuid.UUID,
    ) -> uuid.UUID:
        """IAM-100's "versioned". Editing a profile publishes a new version;
        the old one is never mutated.

        The IAM-102 ceiling is re-checked against EVERY administration
        already using this profile. Without that, a firm could assign a
        modest profile to a client it has limited access to and then edit
        the profile upward - the same escalation nesting was for custom
        roles (ADR-013), one level up.
        """
        profile = await self._available_profile(profile_id, firm_organization_id)
        if profile.is_builtin:
            raise ProfileNotAvailableError(
                f"{profile.name!r} is a built-in profile and cannot be edited; "
                "compose your own instead (IAM-101)"
            )
        if not summary.strip():
            raise ValueError("IAM-103 requires a plain-language summary for every version")

        effective = ProfileRestrictions() if restrictions is None else restrictions
        candidate = ProfileVersion(
            id=uuid.UUID(int=0),
            profile_id=profile.id,
            version=0,
            summary=summary,
            permissions=permissions,
            restrictions=effective,
        )
        resolved = resolve(candidate, profile_name=profile.name).permissions

        for administration_id in await self._repository.administrations_using(profile.id):
            await self._enforce_ceiling(
                firm_organization_id=firm_organization_id,
                administration_id=administration_id,
                permissions=resolved,
            )

        current = await self._repository.current_version(profile.id)
        next_version = 1 if current is None else current.version + 1

        version_id = await self._repository.publish_version(
            profile_id=profile.id,
            version=next_version,
            summary=summary.strip(),
            permissions=permissions,
            restrictions=effective.to_mapping(),
            published_by_user_id=published_by_user_id,
        )

        # IAM-103 again: a new version takes effect immediately for every
        # administration using this profile, so each of their Owners is told.
        for administration_id in await self._repository.administrations_using(profile.id):
            before = (
                resolve(current, profile_name=profile.name).permissions
                if current is not None
                else frozenset()
            )
            await self._notifier.notify_client_owner(
                describe_change(
                    administration_id=administration_id,
                    profile_name=profile.name,
                    before=before,
                    after=resolved,
                )
            )

        return version_id

    async def create_profile(
        self,
        *,
        firm_organization_id: uuid.UUID,
        name: str,
        description: str,
        created_by_user_id: uuid.UUID,
    ) -> uuid.UUID:
        """IAM-101: "a firm may compose its own". Creates the profile shell;
        publish_version puts contents in it, and that is where the IAM-102
        ceiling applies.
        """
        if not name.strip():
            raise ValueError("a client access profile needs a name")
        return await self._repository.create_profile(
            firm_organization_id=firm_organization_id,
            name=name.strip(),
            description=description,
            created_by_user_id=created_by_user_id,
        )

    async def effective_access(
        self, administration_id: uuid.UUID, *, is_client_owner: bool = False
    ) -> ResolvedAccess | None:
        """What the profile permits for a client user of this
        administration, or None when no profile governs it (self-managed
        administrations, and firm-managed ones the firm has not capped).
        None means "no cap", never "no access".
        """
        current = await self._repository.current_profile_for(administration_id)
        if current is None:
            return None
        profile, version = current
        return resolve(version, profile_name=profile.name, is_client_owner=is_client_owner)

    async def _available_profile(
        self, profile_id: uuid.UUID, firm_organization_id: uuid.UUID
    ) -> ClientAccessProfile:
        profile = await self._repository.get_profile(profile_id)
        if profile is None or profile.archived_at is not None:
            raise ProfileNotAvailableError(f"profile {profile_id} is not available")
        if not profile.is_builtin and profile.firm_organization_id != firm_organization_id:
            # Same message as "no such profile" - a firm must not be able to
            # probe for another firm's profile names.
            raise ProfileNotAvailableError(f"profile {profile_id} is not available")
        return profile

    async def _enforce_ceiling(
        self,
        *,
        firm_organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        permissions: frozenset[Permission],
    ) -> None:
        firm_holds = await self._repository.firm_permissions_on(
            firm_organization_id=firm_organization_id, administration_id=administration_id
        )
        # IAM-105's floor is excluded from the ceiling check: those rights
        # are the client's own and are not the firm's to confer or withhold,
        # so a firm that cannot itself read the client's audit log still
        # does not stop the client's Owner reading it.
        missing = permissions - firm_holds - CLIENT_RIGHTS_FLOOR
        if missing:
            raise ProfileCeilingError(missing, administration_id=administration_id)

    async def _current_permissions(self, administration_id: uuid.UUID) -> frozenset[Permission]:
        current = await self._repository.current_profile_for(administration_id)
        if current is None:
            return frozenset()
        profile, version = current
        return resolve(version, profile_name=profile.name).permissions
