"""An in-memory ChartRepository that REIMPLEMENTS migration 0024's rules over
the REAL RGS dataset shipped in apps/api/data/rgs/.

Two deliberate choices, both about keeping the test honest.

**It reimplements rather than stubs.** FR-ONB-005's "may extend but not break
RGS mapping integrity" is four rules - the code exists in the pinned version,
it is postable, its type matches the account's, and the version is derived
rather than supplied. A fake that accepted anything would let
tests/ledger/test_chart.py prove the service calls the repository and nothing
about the requirement. So the rules are here too, with the same messages, and
tests/integration/test_chart_of_accounts.py runs the same case table against
Postgres, where a divergence fails.

**It loads the shipped file.** Not a miniature fixture: the same JSON
scripts/load_rgs_version.py sends to ledger.load_rgs_version, parsed by the
same merge rule (common + profile accounts). So "seeding a BV produces a chart
with share capital and corporate income tax" is a statement about the dataset
that actually ships, and a mistake in it fails here rather than in
production's first onboarding.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.ledger.chart import (
    ChartAccount,
    ChartError,
    LegalForm,
    MappingDeviation,
    RgsElement,
    RgsMappingViolation,
    RgsReadiness,
    RgsSource,
    RgsVersion,
    RgsVersionStatus,
    SeedResult,
    UnknownLegalForm,
    UpgradeResolution,
    UpgradeResult,
    UpgradeStep,
    VatTreatment,
)
from api.ledger.model import AccountStatus, AccountType, ControlKind

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "rgs"
SHIPPED_DATASET = DATA_DIR / "rgs-3.8-mkb.json"

#: The rows migration 0024 seeds into legal_form_alias. Duplicated here rather
#: than parsed out of the SQL, and asserted equal to it by
#: tests/integration/test_chart_of_accounts.py - the same bargain every other
#: fake in this suite makes with its migration.
LEGAL_FORM_ALIASES: dict[str, str] = {
    "eenmanszaak": "eenmanszaak",
    "ez": "eenmanszaak",
    "zzp": "eenmanszaak",
    "sole trader": "eenmanszaak",
    "vof": "vof",
    "v.o.f.": "vof",
    "vennootschap onder firma": "vof",
    "maatschap": "vof",
    "bv": "bv",
    "b.v.": "bv",
    "besloten vennootschap": "bv",
    "besloten vennootschap met beperkte aansprakelijkheid": "bv",
    "stichting": "stichting",
    "foundation": "stichting",
    "vereniging": "vereniging",
    "association": "vereniging",
}


def canonical_legal_form(raw: str | None) -> str | None:
    return LEGAL_FORM_ALIASES.get((raw or "").strip().lower())


def load_document(path: Path = SHIPPED_DATASET) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@dataclass
class FakeAdministration:
    id: uuid.UUID
    organization_id: uuid.UUID
    legal_form: str


@dataclass
class FakeAccount:
    id: uuid.UUID
    administration_id: uuid.UUID
    code: str
    name: str
    account_type: AccountType
    status: AccountStatus = AccountStatus.ACTIVE
    rgs_code: str | None = None
    rgs_version_id: uuid.UUID | None = None
    default_vat_code: VatTreatment | None = None
    control_kind: ControlKind | None = None


@dataclass
class FakeProfileAccount:
    account_code: str
    rgs_code: str
    name_nl: str
    name_en: str | None
    default_vat_code: VatTreatment | None
    control_kind: ControlKind | None
    sort_order: int


@dataclass
class FakeVersion:
    """One loaded RGS version. `elements` and `profiles` are frozen once
    loaded, mirroring the append-only triggers on the real tables.
    """

    id: uuid.UUID
    version: str
    status: RgsVersionStatus
    source: RgsSource
    source_checksum: str
    supersedes_id: uuid.UUID | None = None
    elements: dict[str, RgsElement] = field(default_factory=dict)
    profiles: dict[tuple[str, str], list[FakeProfileAccount]] = field(default_factory=dict)


@dataclass
class FakePin:
    administration_id: uuid.UUID
    organization_id: uuid.UUID
    rgs_version_id: uuid.UUID
    profile_code: str
    legal_form: str
    previous_rgs_version_id: uuid.UUID | None = None


@dataclass
class InMemoryChartRepository:
    """Mirrors 0024's tables. Field names match the columns, so a reader
    comparing this to the migration can do it line by line.
    """

    administrations: dict[uuid.UUID, FakeAdministration] = field(default_factory=dict)
    accounts: dict[uuid.UUID, FakeAccount] = field(default_factory=dict)
    versions: dict[uuid.UUID, FakeVersion] = field(default_factory=dict)
    pins: dict[uuid.UUID, FakePin] = field(default_factory=dict)
    #: (from_version_id, to_version_id, from_code) -> list of (to_code, kind)
    mappings: dict[tuple[uuid.UUID, uuid.UUID, str], list[tuple[str | None, str]]] = field(
        default_factory=dict
    )

    # -- seeding the fixture ----------------------------------------------

    def add_administration(
        self, *, legal_form: str, organization_id: uuid.UUID | None = None
    ) -> FakeAdministration:
        administration = FakeAdministration(
            id=uuid.uuid4(),
            organization_id=organization_id or uuid.uuid4(),
            legal_form=legal_form,
        )
        self.administrations[administration.id] = administration
        return administration

    def load(
        self, document: dict[str, Any], *, checksum: str = "test", publish: bool = True
    ) -> FakeVersion:
        """ledger.load_rgs_version(), in memory - including the merge of
        `common` into every profile, which is the loader's one real rule.
        """
        name = document["rgs_version"]
        for existing in self.versions.values():
            if existing.version == name:
                if existing.source_checksum == checksum:
                    return existing
                raise ChartError(f"RGS version {name} is already loaded from a different document")

        supersedes = document.get("supersedes")
        supersedes_id = None
        if supersedes is not None:
            supersedes_id = self._version_by_name(supersedes).id

        version = FakeVersion(
            id=uuid.uuid4(),
            version=name,
            status=RgsVersionStatus.DRAFT,
            source=RgsSource(document["source"]),
            source_checksum=checksum,
            supersedes_id=supersedes_id,
        )

        for raw in document.get("elements") or []:
            version.elements[raw["code"]] = RgsElement(
                code=raw["code"],
                description_nl=raw["description_nl"],
                description_en=raw.get("description_en"),
                level=int(raw["level"]),
                is_postable=bool(raw.get("postable")),
                account_type=(
                    AccountType(raw["account_type"]) if raw.get("account_type") else None
                ),
                debit_credit=raw.get("debit_credit"),
                parent_code=raw.get("parent_code"),
            )

        profile_code = document.get("profile_code", "mkb")
        common = document.get("common") or []
        for profile in document.get("profiles") or []:
            rows = [
                FakeProfileAccount(
                    account_code=raw["account_code"],
                    rgs_code=raw["rgs_code"],
                    name_nl=raw["name_nl"],
                    name_en=raw.get("name_en"),
                    default_vat_code=(
                        VatTreatment(raw["default_vat_code"])
                        if raw.get("default_vat_code")
                        else None
                    ),
                    control_kind=(
                        ControlKind(raw["control_kind"]) if raw.get("control_kind") else None
                    ),
                    sort_order=int(raw.get("sort_order") or 0),
                )
                for raw in [*common, *(profile.get("accounts") or [])]
            ]
            rows.sort(key=lambda r: (r.sort_order, r.account_code))
            version.profiles[(profile_code, profile["legal_form"])] = rows

        for raw in document.get("mappings") or []:
            assert supersedes_id is not None, "mappings need a superseded version"
            key = (supersedes_id, version.id, raw["from_code"])
            self.mappings.setdefault(key, []).append((raw.get("to_code"), raw["change_kind"]))

        self.versions[version.id] = version
        if publish:
            self.publish(version.id)
        return version

    def publish(self, version_id: uuid.UUID) -> FakeVersion:
        for other in self.versions.values():
            if other.status is RgsVersionStatus.CURRENT and other.id != version_id:
                other.status = RgsVersionStatus.SUPERSEDED
        self.versions[version_id].status = RgsVersionStatus.CURRENT
        return self.versions[version_id]

    # -- ChartRepository ---------------------------------------------------

    async def seed(
        self,
        *,
        administration_id: uuid.UUID,
        rgs_version_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        profile_code: str,
    ) -> SeedResult:
        administration = self._administration(administration_id)
        legal_form = canonical_legal_form(administration.legal_form)
        if legal_form is None:
            raise UnknownLegalForm(
                f'legal form "{administration.legal_form}" has no RGS profile; '
                "FR-ONB-004 recognises eenmanszaak, VOF, BV, stichting and vereniging"
            )

        version = (
            self.versions[rgs_version_id] if rgs_version_id is not None else self._current_version()
        )
        if version.status is RgsVersionStatus.SUPERSEDED:
            raise ChartError(f"RGS version {version.version} is superseded")

        rows = version.profiles.get((profile_code, legal_form))
        if not rows:
            raise ChartError(
                f"RGS version {version.version} has no {profile_code} profile for "
                f"legal form {legal_form}"
            )

        pin = self.pins.get(administration_id)
        if pin is None:
            self.pins[administration_id] = FakePin(
                administration_id=administration_id,
                organization_id=administration.organization_id,
                rgs_version_id=version.id,
                profile_code=profile_code,
                legal_form=legal_form,
            )
        elif pin.rgs_version_id != version.id:
            raise ChartError(
                f"administration {administration_id} is pinned to another RGS "
                "version; changing it is an upgrade, not a re-seed"
            )

        seeded = skipped = 0
        existing_codes = {
            a.code for a in self.accounts.values() if a.administration_id == administration_id
        }
        for row in rows:
            if row.account_code in existing_codes:
                skipped += 1
                continue
            element = version.elements[row.rgs_code]
            assert element.account_type is not None
            account = FakeAccount(
                id=uuid.uuid4(),
                administration_id=administration_id,
                code=row.account_code,
                name=row.name_nl,
                account_type=element.account_type,
                rgs_code=row.rgs_code,
                default_vat_code=row.default_vat_code,
                control_kind=row.control_kind,
            )
            self._apply_rgs_integrity(account)
            self.accounts[account.id] = account
            existing_codes.add(account.code)
            seeded += 1

        return SeedResult(
            seeded_count=seeded,
            skipped_count=skipped,
            rgs_version=version.version,
            legal_form=LegalForm(legal_form),
        )

    async def chart(
        self, *, administration_id: uuid.UUID, include_blocked: bool
    ) -> Sequence[ChartAccount]:
        pin = self.pins.get(administration_id)
        rows = [
            self._as_chart_account(account, pin)
            for account in self.accounts.values()
            if account.administration_id == administration_id
            and (include_blocked or account.status is AccountStatus.ACTIVE)
        ]
        return sorted(rows, key=lambda a: a.code)

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
    ) -> ChartAccount:
        if any(
            a.administration_id == administration_id and a.code == code
            for a in self.accounts.values()
        ):
            raise ChartError(f"account code {code} already exists in this administration")

        account = FakeAccount(
            id=uuid.uuid4(),
            administration_id=administration_id,
            code=code,
            name=name,
            account_type=account_type,
            rgs_code=rgs_code,
            default_vat_code=default_vat_code,
            control_kind=control_kind,
        )
        self._apply_rgs_integrity(account)
        self.accounts[account.id] = account
        return self._as_chart_account(account, self.pins.get(administration_id))

    async def set_account_status(
        self, *, account_id: uuid.UUID, status: AccountStatus
    ) -> ChartAccount:
        account = self.accounts[account_id]
        account.status = status
        return self._as_chart_account(account, self.pins.get(account.administration_id))

    async def set_account_rgs_code(
        self, *, account_id: uuid.UUID, rgs_code: str | None
    ) -> ChartAccount:
        account = self.accounts[account_id]
        candidate = replace(account, rgs_code=rgs_code)
        self._apply_rgs_integrity(candidate)
        account.rgs_code = candidate.rgs_code
        account.rgs_version_id = candidate.rgs_version_id
        return self._as_chart_account(account, self.pins.get(account.administration_id))

    async def rgs_options(
        self, *, administration_id: uuid.UUID, account_type: AccountType
    ) -> Sequence[RgsElement]:
        pin = self.pins.get(administration_id)
        if pin is None:
            return []
        return sorted(
            (
                element
                for element in self.versions[pin.rgs_version_id].elements.values()
                if element.is_postable and element.account_type is account_type
            ),
            key=lambda e: e.code,
        )

    async def current_version(self) -> RgsVersion | None:
        for version in self.versions.values():
            if version.status is RgsVersionStatus.CURRENT:
                return self._as_version(version)
        return None

    async def version(self, *, version: str) -> RgsVersion | None:
        for candidate in self.versions.values():
            if candidate.version == version:
                return self._as_version(candidate)
        return None

    async def pinned_version(self, *, administration_id: uuid.UUID) -> RgsVersion | None:
        pin = self.pins.get(administration_id)
        return self._as_version(self.versions[pin.rgs_version_id]) if pin else None

    async def organization_of(self, *, administration_id: uuid.UUID) -> uuid.UUID | None:
        administration = self.administrations.get(administration_id)
        return administration.organization_id if administration else None

    async def plan_upgrade(
        self, *, administration_id: uuid.UUID, to_version_id: uuid.UUID
    ) -> Sequence[UpgradeStep]:
        pin = self.pins.get(administration_id)
        target = self.versions[to_version_id]
        steps: list[UpgradeStep] = []

        for account in sorted(
            (a for a in self.accounts.values() if a.administration_id == administration_id),
            key=lambda a: a.code,
        ):
            if account.rgs_code is None:
                steps.append(
                    self._step(
                        account,
                        None,
                        "unmapped",
                        UpgradeResolution.NOTHING_TO_DO,
                        "the account carries no RGS code and is unaffected",
                    )
                )
                continue

            assert pin is not None
            candidates = self.mappings.get(
                (pin.rgs_version_id, to_version_id, account.rgs_code), []
            )
            if not candidates:
                steps.append(
                    self._step(
                        account,
                        None,
                        "no_mapping",
                        UpgradeResolution.NEEDS_A_DECISION,
                        f"RGS code {account.rgs_code} has no mapping into the new version",
                    )
                )
                continue
            if len(candidates) > 1 or candidates[0][1] == "split":
                steps.append(
                    self._step(
                        account,
                        None,
                        "split",
                        UpgradeResolution.NEEDS_A_DECISION,
                        f"RGS code {account.rgs_code} was split; which target "
                        "applies is an accounting decision",
                    )
                )
                continue

            to_code, kind = candidates[0]
            if kind == "withdrawn" or to_code is None:
                steps.append(
                    self._step(
                        account,
                        None,
                        "withdrawn",
                        UpgradeResolution.NEEDS_A_DECISION,
                        f"RGS code {account.rgs_code} was withdrawn",
                    )
                )
                continue

            element = target.elements.get(to_code)
            if element is None:
                steps.append(
                    self._step(
                        account,
                        to_code,
                        kind,
                        UpgradeResolution.NEEDS_A_DECISION,
                        f"the new version does not define {to_code}",
                    )
                )
            elif not element.is_postable:
                steps.append(
                    self._step(
                        account,
                        to_code,
                        kind,
                        UpgradeResolution.NEEDS_A_DECISION,
                        f"{to_code} is a heading in the new version",
                    )
                )
            elif element.account_type is not account.account_type:
                became = element.account_type.value if element.account_type else None
                steps.append(
                    self._step(
                        account,
                        to_code,
                        kind,
                        UpgradeResolution.NEEDS_A_DECISION,
                        f"{to_code} is {became} in the new version but the account "
                        f"is {account.account_type.value}",
                    )
                )
            else:
                steps.append(
                    self._step(
                        account,
                        to_code,
                        kind,
                        UpgradeResolution.AUTOMATIC,
                        f"{account.rgs_code} -> {to_code} ({kind})",
                    )
                )
        return steps

    async def apply_upgrade(
        self,
        *,
        administration_id: uuid.UUID,
        to_version_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
    ) -> UpgradeResult:
        pin = self.pins[administration_id]
        target = self.versions[to_version_id]
        if target.status is RgsVersionStatus.SUPERSEDED:
            raise ChartError(f"RGS version {target.version} is superseded")

        blocked = [
            step
            for step in await self.plan_upgrade(
                administration_id=administration_id, to_version_id=to_version_id
            )
            if step.resolution is UpgradeResolution.NEEDS_A_DECISION
        ]
        if blocked:
            raise ChartError(
                f"{len(blocked)} account(s) need a decision before this upgrade can be applied"
            )

        # The pin moves FIRST, exactly as ledger.apply_rgs_upgrade does: the
        # integrity rule derives an account's version from it, so remapping
        # first would validate every new code against the old version.
        previous = pin.rgs_version_id
        pin.previous_rgs_version_id = previous
        pin.rgs_version_id = to_version_id

        remapped = 0
        for account in self.accounts.values():
            if account.administration_id != administration_id or account.rgs_code is None:
                continue
            candidates = self.mappings.get((previous, to_version_id, account.rgs_code), [])
            if not candidates:
                continue
            to_code, _ = candidates[0]
            account.rgs_code = to_code
            self._apply_rgs_integrity(account)
            remapped += 1

        unmapped = sum(
            1
            for a in self.accounts.values()
            if a.administration_id == administration_id and a.rgs_code is None
        )
        return UpgradeResult(
            remapped_count=remapped,
            unmapped_count=unmapped,
            rgs_version=target.version,
        )

    async def readiness(self, *, administration_id: uuid.UUID | None) -> Sequence[RgsReadiness]:
        rows: list[RgsReadiness] = []
        for pin in self.pins.values():
            if administration_id is not None and pin.administration_id != administration_id:
                continue
            version = self.versions[pin.rgs_version_id]
            accounts = [
                a for a in self.accounts.values() if a.administration_id == pin.administration_id
            ]
            rows.append(
                RgsReadiness(
                    administration_id=pin.administration_id,
                    legal_form=LegalForm(pin.legal_form),
                    rgs_version=version.version,
                    version_status=version.status,
                    version_source=version.source,
                    is_provisional=version.source is RgsSource.PROVISIONAL,
                    is_behind_current=version.status is not RgsVersionStatus.CURRENT,
                    accounts_total=len(accounts),
                    accounts_unmapped=sum(1 for a in accounts if a.rgs_code is None),
                )
            )
        return rows

    async def mapping_deviations(
        self, *, administration_id: uuid.UUID | None
    ) -> Sequence[MappingDeviation]:
        found: list[MappingDeviation] = []
        for account in self.accounts.values():
            if administration_id is not None and account.administration_id != administration_id:
                continue
            if account.rgs_code is None:
                continue
            pin = self.pins.get(account.administration_id)
            if pin is None:
                found.append(self._deviation(account, "chart_not_pinned", "no version pinned"))
                continue
            if account.rgs_version_id != pin.rgs_version_id:
                found.append(
                    self._deviation(
                        account,
                        "stale_rgs_version",
                        "the account was left on a version its administration has "
                        "upgraded away from",
                    )
                )
                continue
            element = self.versions[pin.rgs_version_id].elements.get(account.rgs_code)
            if element is None:
                found.append(
                    self._deviation(
                        account,
                        "rgs_code_not_in_version",
                        f"{account.rgs_code} is not defined in the pinned version",
                    )
                )
            elif not element.is_postable:
                found.append(
                    self._deviation(
                        account,
                        "rgs_code_not_postable",
                        f"{account.rgs_code} is a heading, not a postable element",
                    )
                )
            elif element.account_type is not account.account_type:
                found.append(
                    self._deviation(
                        account,
                        "rgs_type_mismatch",
                        f"the account is {account.account_type.value} but "
                        f"{account.rgs_code} is "
                        f"{element.account_type.value if element.account_type else None}",
                    )
                )
        return found

    # -- 0024's integrity trigger, in Python ------------------------------

    def _apply_rgs_integrity(self, account: FakeAccount) -> None:
        """ledger_account_rgs_integrity(), with the same four checks in the
        same order and messages sharing the substrings the case table matches.
        """
        if account.rgs_code is None:
            account.rgs_version_id = None
            return

        pin = self.pins.get(account.administration_id)
        if pin is None:
            raise RgsMappingViolation(
                f"administration {account.administration_id} has no RGS version; "
                "seed the chart of accounts before mapping an account to RGS "
                f"code {account.rgs_code} (FR-ONB-005)"
            )

        # Derive-don't-trust: the version is the administration's, never the
        # caller's.
        account.rgs_version_id = pin.rgs_version_id

        element = self.versions[pin.rgs_version_id].elements.get(account.rgs_code)
        if element is None:
            raise RgsMappingViolation(
                f"RGS code {account.rgs_code} does not exist in the version this "
                "administration uses (FR-ONB-005)"
            )
        if not element.is_postable:
            raise RgsMappingViolation(
                f"RGS code {element.code} ({element.description_nl}) is a heading, "
                "not a postable element; an account mapped to it would put an "
                "amount where the taxonomy expects the sum of its children "
                "(FR-ONB-005)"
            )
        if element.account_type is not account.account_type:
            raise RgsMappingViolation(
                f"account {account.code} is {account.account_type.value} but RGS "
                f"code {element.code} is "
                f"{element.account_type.value if element.account_type else None}; "
                "a chart may be extended, not remapped across types (FR-ONB-005)"
            )

    # -- helpers -----------------------------------------------------------

    def _administration(self, administration_id: uuid.UUID) -> FakeAdministration:
        administration = self.administrations.get(administration_id)
        if administration is None:
            raise ChartError(f"administration {administration_id} does not exist")
        return administration

    def _current_version(self) -> FakeVersion:
        for version in self.versions.values():
            if version.status is RgsVersionStatus.CURRENT:
                return version
        raise ChartError(
            "no current RGS version is loaded; load one before seeding a chart (CMP-003)"
        )

    def _version_by_name(self, name: str) -> FakeVersion:
        for version in self.versions.values():
            if version.version == name:
                return version
        raise ChartError(f"RGS version {name} is not loaded")

    def _as_version(self, version: FakeVersion) -> RgsVersion:
        return RgsVersion(
            id=version.id,
            version=version.version,
            status=version.status,
            source=version.source,
            source_checksum=version.source_checksum,
            supersedes_id=version.supersedes_id,
            loaded_at=datetime.now(UTC),
        )

    def _as_chart_account(self, account: FakeAccount, pin: FakePin | None) -> ChartAccount:
        element = None
        version_name = None
        seeded = False
        if account.rgs_version_id is not None:
            version = self.versions[account.rgs_version_id]
            version_name = version.version
            element = version.elements.get(account.rgs_code or "")
        if pin is not None:
            seeded = any(
                row.account_code == account.code
                for row in self.versions[pin.rgs_version_id].profiles.get(
                    (pin.profile_code, pin.legal_form), []
                )
            )
        return ChartAccount(
            account_id=account.id,
            code=account.code,
            name=account.name,
            account_type=account.account_type,
            status=account.status,
            rgs_code=account.rgs_code,
            rgs_description_nl=element.description_nl if element else None,
            rgs_description_en=element.description_en if element else None,
            rgs_version=version_name,
            default_vat_code=account.default_vat_code,
            control_kind=account.control_kind,
            is_seeded=seeded,
        )

    @staticmethod
    def _step(
        account: FakeAccount,
        to_code: str | None,
        change_kind: str,
        resolution: UpgradeResolution,
        detail: str,
    ) -> UpgradeStep:
        return UpgradeStep(
            account_id=account.id,
            account_code=account.code,
            account_name=account.name,
            account_type=account.account_type,
            resolution=resolution,
            change_kind=change_kind,
            detail=detail,
            from_rgs_code=account.rgs_code,
            to_rgs_code=to_code,
        )

    @staticmethod
    def _deviation(account: FakeAccount, deviation: str, detail: str) -> MappingDeviation:
        return MappingDeviation(
            account_id=account.id,
            administration_id=account.administration_id,
            account_code=account.code,
            deviation=deviation,
            detail=detail,
        )
