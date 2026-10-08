"""BankFeedService (ADR-108): the consent lifecycle and the sync window, with in-memory fakes.

The database-backed behaviour (RLS, the shared import path's de-duplication, the routes) is in
tests/integration/test_bank_feed_routes.py; this pins the decisions the service makes.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from api.audit.log import AuditLog
from api.bank.adapters import (
    ConsentLink,
    ConsentState,
    ConsentStatus,
    Institution,
    ProviderRefused,
    ProviderUnavailable,
)
from api.bank.csv_parser import StatementRow
from api.bank.feed import (
    BankFeedService,
    FeedAlreadyLinked,
    FeedNotLinked,
    FeedSettings,
    FeedUnavailable,
)
from api.bank.feed_repository import ConnectionStatus, FeedConnection
from api.bank.model import BankAccount, BankAccountNotFound, BankAccountStatus, ImportResult
from tests.support.fake_audit_repository import InMemoryAuditRepository

ORG = uuid.uuid4()
ADMIN = uuid.uuid4()
ACCOUNT = uuid.uuid4()
USER = uuid.uuid4()
NOW = datetime(2026, 10, 8, 7, 0, tzinfo=UTC)


class FakeRepository:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, FeedConnection] = {}

    async def active_for_account(self, *, administration_id, bank_account_id):  # type: ignore[no-untyped-def]
        for row in self.rows.values():
            if row.bank_account_id == bank_account_id and row.status in {
                ConnectionStatus.PENDING,
                ConnectionStatus.LINKED,
            }:
                return row
        return None

    async def latest_for_account(self, *, administration_id, bank_account_id):  # type: ignore[no-untyped-def]
        rows = [r for r in self.rows.values() if r.bank_account_id == bank_account_id]
        return rows[-1] if rows else None

    async def get(self, *, administration_id, connection_id):  # type: ignore[no-untyped-def]
        row = self.rows.get(connection_id)
        return row if row is not None and row.administration_id == administration_id else None

    async def create_pending(self, **kwargs):  # type: ignore[no-untyped-def]
        row = FeedConnection(
            id=uuid.uuid4(),
            organization_id=kwargs["organization_id"],
            administration_id=kwargs["administration_id"],
            bank_account_id=kwargs["bank_account_id"],
            provider=kwargs["provider"],
            institution_id=kwargs["institution_id"],
            institution_name=kwargs["institution_name"],
            provider_reference=None,
            provider_account_id=None,
            status=ConnectionStatus.PENDING,
            consent_expires_at=None,
            last_synced_at=None,
            last_error=None,
            created_at=NOW,
        )
        self.rows[row.id] = row
        return row

    async def update(self, *, administration_id, connection_id, **fields):  # type: ignore[no-untyped-def]
        row = dataclasses.replace(self.rows[connection_id], **fields)
        self.rows[connection_id] = row
        return row


class FakeBank:
    def __init__(self, iban: str | None = "NL91ABNA0417164300") -> None:
        self.account = BankAccount(
            id=ACCOUNT,
            administration_id=ADMIN,
            name="ABN AMRO",
            iban=iban,
            currency="EUR",
            ledger_account_id=uuid.uuid4(),
            status=BankAccountStatus.ACTIVE,
            created_at=NOW,
        )
        self.imports: list[dict[str, object]] = []

    async def list_accounts(self, *, administration_id):  # type: ignore[no-untyped-def]
        return [self.account] if administration_id == ADMIN else []

    async def import_rows(self, **kwargs):  # type: ignore[no-untyped-def]
        self.imports.append(kwargs)
        return ImportResult(
            import_id=uuid.uuid4(), transaction_count=len(kwargs["rows"]), duplicate_count=0
        )


ROW = StatementRow(
    booking_date=date(2026, 10, 1),
    amount=Decimal("-12.50"),
    counterparty_name="KPN",
    counterparty_iban=None,
    description="Telefoon",
)


class FakeProvider:
    name = "gocardless"
    is_configured = True

    def __init__(self) -> None:
        self.consent = ConsentStatus(state=ConsentState.LINKED, account_ids=("acc-1", "acc-2"))
        self.ibans = {"acc-1": "NL02ABNA0123456789", "acc-2": "NL91 ABNA 0417 1643 00"}
        self.fetch_error: Exception | None = None
        self.revoke_error: Exception | None = None
        self.started: list[dict[str, object]] = []
        self.fetched: list[tuple[str, date]] = []
        self.revoked: list[str] = []

    async def list_institutions(self, *, country: str) -> Sequence[Institution]:
        return [Institution(id="ING", name="ING", bic=None, logo=None)]

    async def start_consent(self, **kwargs):  # type: ignore[no-untyped-def]
        self.started.append(kwargs)
        return ConsentLink(reference="req-1", link="https://bank.example/consent")

    async def consent_status(self, *, reference: str) -> ConsentStatus:
        return self.consent

    async def account_iban(self, *, account_id: str) -> str | None:
        return self.ibans.get(account_id)

    async def fetch_transactions(self, *, account_id: str, date_from: date):  # type: ignore[no-untyped-def]
        if self.fetch_error is not None:
            raise self.fetch_error
        self.fetched.append((account_id, date_from))
        return [ROW]

    async def revoke(self, *, reference: str) -> None:
        if self.revoke_error is not None:
            raise self.revoke_error
        self.revoked.append(reference)


class World:
    def __init__(self, *, iban: str | None = "NL91ABNA0417164300") -> None:
        self.repository = FakeRepository()
        self.bank = FakeBank(iban)
        self.provider = FakeProvider()
        self.audit = InMemoryAuditRepository()
        self.now = NOW
        self.service = BankFeedService(
            repository=self.repository,  # type: ignore[arg-type]
            bank=self.bank,  # type: ignore[arg-type]
            provider=self.provider,
            audit_log=AuditLog(self.audit),
            settings=FeedSettings(
                redirect_url="https://boeklite.nl/bank/feed-return",
                history_days=90,
                consent_days=90,
            ),
            clock=lambda: self.now,
        )

    async def connect(self) -> FeedConnection:
        started = await self.service.connect(
            organization_id=ORG,
            administration_id=ADMIN,
            bank_account_id=ACCOUNT,
            institution_id="ABNAMRO_ABNANL2A",
            institution_name="ABN AMRO",
            language="nl",
            actor_user_id=USER,
        )
        return started.connection

    async def link(self) -> FeedConnection:
        connection = await self.connect()
        return await self.service.complete(
            administration_id=ADMIN, connection_id=connection.id, actor_user_id=USER
        )

    def actions(self) -> list[str]:
        return [entry.action for entry in self.audit._entries]


# -- connect -----------------------------------------------------------------------------------


async def test_connect_starts_a_consent_named_after_the_connection() -> None:
    world = World()
    connection = await world.connect()

    assert connection.status is ConnectionStatus.PENDING
    assert connection.provider_reference == "req-1"
    started = world.provider.started[0]
    assert started["reference"] == str(connection.id)
    assert started["redirect_url"] == "https://boeklite.nl/bank/feed-return"
    assert started["institution_id"] == "ABNAMRO_ABNANL2A"
    assert "connect_bank_feed" in world.actions()


async def test_an_abandoned_consent_is_replaced_by_a_new_start() -> None:
    world = World()
    first = await world.connect()
    second = await world.connect()

    assert world.repository.rows[first.id].status is ConnectionStatus.FAILED
    assert world.repository.rows[first.id].last_error == "bank_feed_consent_abandoned"
    assert second.status is ConnectionStatus.PENDING


async def test_a_linked_account_cannot_be_connected_twice() -> None:
    world = World()
    await world.link()
    with pytest.raises(FeedAlreadyLinked):
        await world.connect()


async def test_nothing_starts_without_a_provider() -> None:
    world = World()
    world.provider.is_configured = False
    with pytest.raises(FeedUnavailable):
        await world.connect()


async def test_an_account_of_another_administration_is_not_found() -> None:
    world = World()
    with pytest.raises(BankAccountNotFound):
        await world.service.connect(
            organization_id=ORG,
            administration_id=uuid.uuid4(),
            bank_account_id=ACCOUNT,
            institution_id="ING",
            institution_name=None,
            language="nl",
            actor_user_id=USER,
        )


# -- complete ----------------------------------------------------------------------------------


async def test_completing_picks_the_account_by_iban_and_reads_history() -> None:
    world = World()
    connection = await world.link()

    assert connection.status is ConnectionStatus.LINKED
    assert connection.provider_account_id == "acc-2"  # NL91ABNA..., not acc-1
    assert connection.consent_expires_at == NOW + timedelta(days=90)
    assert connection.last_synced_at == NOW
    assert world.provider.fetched == [("acc-2", date(2026, 7, 10))]  # 90 days back
    assert world.bank.imports[0]["source_format"] == "psd2"
    assert world.bank.imports[0]["actor_user_id"] == USER


async def test_a_consent_still_in_progress_stays_pending() -> None:
    world = World()
    world.provider.consent = ConsentStatus(state=ConsentState.PENDING, account_ids=())
    connection = await world.connect()
    again = await world.service.complete(
        administration_id=ADMIN, connection_id=connection.id, actor_user_id=USER
    )
    assert again.status is ConnectionStatus.PENDING
    assert world.bank.imports == []


async def test_a_refused_consent_is_recorded_as_failed_and_returned() -> None:
    world = World()
    world.provider.consent = ConsentStatus(state=ConsentState.FAILED, account_ids=())
    connection = await world.connect()
    result = await world.service.complete(
        administration_id=ADMIN, connection_id=connection.id, actor_user_id=USER
    )
    assert result.status is ConnectionStatus.FAILED
    assert result.last_error == "bank_feed_consent_failed"


async def test_no_matching_iban_links_nothing() -> None:
    world = World()
    world.provider.ibans = {"acc-1": "NL02ABNA0123456789", "acc-2": "NL55INGB0000000000"}
    result = await world.link()
    assert result.status is ConnectionStatus.FAILED
    assert result.last_error == "bank_feed_account_mismatch"
    assert world.bank.imports == []


async def test_without_our_own_iban_only_a_single_account_is_taken() -> None:
    world = World(iban=None)
    assert (await world.link()).last_error == "bank_feed_account_mismatch"

    single = World(iban=None)
    single.provider.consent = ConsentStatus(state=ConsentState.LINKED, account_ids=("acc-9",))
    assert (await single.link()).provider_account_id == "acc-9"


async def test_completing_twice_changes_nothing() -> None:
    world = World()
    linked = await world.link()
    again = await world.service.complete(
        administration_id=ADMIN, connection_id=linked.id, actor_user_id=USER
    )
    assert again == linked
    assert len(world.bank.imports) == 1


# -- sync --------------------------------------------------------------------------------------


async def test_a_later_sync_reads_again_from_a_week_before_the_last() -> None:
    world = World()
    await world.link()
    world.now = NOW + timedelta(days=1)
    result = await world.service.sync(
        organization_id=ORG, administration_id=ADMIN, bank_account_id=ACCOUNT, actor_user_id=None
    )
    assert result.imported is not None
    assert world.provider.fetched[-1] == ("acc-2", (NOW - timedelta(days=7)).date())
    assert world.bank.imports[-1]["actor_user_id"] is None  # the scheduled job: audited as system
    assert result.connection.last_synced_at == NOW + timedelta(days=1)


async def test_a_consent_the_bank_no_longer_honours_expires_the_connection() -> None:
    world = World()
    await world.link()
    world.provider.fetch_error = ProviderRefused("409")
    result = await world.service.sync(
        organization_id=ORG, administration_id=ADMIN, bank_account_id=ACCOUNT, actor_user_id=USER
    )
    assert result.imported is None
    assert result.connection.status is ConnectionStatus.EXPIRED
    assert result.connection.last_error == "bank_feed_consent_expired"


async def test_an_outage_is_recorded_and_the_link_kept() -> None:
    world = World()
    await world.link()
    world.provider.fetch_error = ProviderUnavailable("503")
    result = await world.service.sync(
        organization_id=ORG, administration_id=ADMIN, bank_account_id=ACCOUNT, actor_user_id=USER
    )
    assert result.imported is None
    assert result.connection.status is ConnectionStatus.LINKED
    assert result.connection.last_error == "bank_feed_provider_unavailable"


async def test_a_consent_past_its_end_date_expires_before_any_read() -> None:
    world = World()
    await world.link()
    world.now = NOW + timedelta(days=91)
    fetched = len(world.provider.fetched)
    result = await world.service.sync(
        organization_id=ORG, administration_id=ADMIN, bank_account_id=ACCOUNT, actor_user_id=None
    )
    assert result.imported is None
    assert result.connection.status is ConnectionStatus.EXPIRED
    assert len(world.provider.fetched) == fetched


async def test_sync_needs_a_linked_connection() -> None:
    world = World()
    with pytest.raises(FeedNotLinked):
        await world.service.sync(
            organization_id=ORG,
            administration_id=ADMIN,
            bank_account_id=ACCOUNT,
            actor_user_id=USER,
        )


# -- disconnect and status ---------------------------------------------------------------------


async def test_disconnect_deletes_the_consent_at_the_provider() -> None:
    world = World()
    await world.link()
    revoked = await world.service.disconnect(
        administration_id=ADMIN, bank_account_id=ACCOUNT, actor_user_id=USER
    )
    assert revoked.status is ConnectionStatus.REVOKED
    assert world.provider.revoked == ["req-1"]
    assert "disconnect_bank_feed" in world.actions()


async def test_disconnect_still_stops_reading_when_the_provider_is_down() -> None:
    world = World()
    await world.link()
    world.provider.revoke_error = ProviderUnavailable("503")
    revoked = await world.service.disconnect(
        administration_id=ADMIN, bank_account_id=ACCOUNT, actor_user_id=USER
    )
    assert revoked.status is ConnectionStatus.REVOKED


async def test_status_reports_whether_a_provider_is_configured() -> None:
    world = World()
    status = await world.service.status(administration_id=ADMIN, bank_account_id=ACCOUNT)
    assert status.configured is True and status.connection is None
    await world.link()
    status = await world.service.status(administration_id=ADMIN, bank_account_id=ACCOUNT)
    assert status.connection is not None
    assert status.connection.status is ConnectionStatus.LINKED
