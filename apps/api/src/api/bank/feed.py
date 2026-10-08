"""The live bank feed: connect a bank account to its bank, then sync its lines - FR-BNK-001,
ADR-108.

    connect     a consent is started at the provider; the person is sent to their bank
    complete    they come back; the consent is checked and the right account chosen by IBAN
    sync        booked lines since the last sync are fetched and imported, through
                `BankService.import_rows`, the same write path and de-duplication a statement file
                uses; run on request, after linking, and daily by scripts/sync_bank_feeds.py
    disconnect  the consent is deleted at the provider and the connection revoked here

Nothing here writes a posting: a fetched line is an unmatched `bank_transaction`, reconciled the
way every imported line is (CLAUDE.md non-negotiable #1).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.bank.adapters import (
    BankFeedProvider,
    ConsentState,
    FeedNotConfigured,
    Institution,
    ProviderRefused,
    ProviderUnavailable,
)
from api.bank.feed_repository import ConnectionStatus, FeedConnection, SqlBankFeedRepository
from api.bank.model import BankAccount, BankAccountNotFound, BankError, ImportResult
from api.bank.service import BankService
from api.bank.statement_formats import normalise_iban

#: How far before the last sync each sync reads again. Banks book some lines late; the overlap is
#: de-duplicated by the import's content hash, so reading it twice costs nothing.
SYNC_OVERLAP = timedelta(days=7)


class FeedError(BankError):
    """Base for every refusal of the bank feed. Each carries the i18n reason the route returns."""

    reason = "bank_feed_failed"


class FeedUnavailable(FeedError):
    reason = "bank_feed_not_configured"


class FeedAlreadyLinked(FeedError):
    reason = "bank_feed_already_linked"


class FeedNotLinked(FeedError):
    reason = "bank_feed_not_linked"


class FeedConnectionNotFound(FeedError):
    reason = "bank_feed_connection_not_found"


class FeedConsentFailed(FeedError):
    reason = "bank_feed_consent_failed"


class FeedProviderDown(FeedError):
    reason = "bank_feed_provider_unavailable"


@dataclass(frozen=True, slots=True)
class FeedStatus:
    configured: bool
    provider: str
    connection: FeedConnection | None


@dataclass(frozen=True, slots=True)
class ConnectStarted:
    connection: FeedConnection
    link: str


@dataclass(frozen=True, slots=True)
class SyncResult:
    """`imported` is None when nothing could be read; `connection.last_error` says why (its
    status is `expired` when the bank no longer honours the consent). Returned, not raised: the
    request's transaction must commit so that state is kept."""

    connection: FeedConnection
    imported: ImportResult | None


@dataclass(frozen=True, slots=True)
class FeedSettings:
    #: Where the bank sends the person back; the provider appends ?ref=<connection id>.
    redirect_url: str
    history_days: int
    consent_days: int


def _now() -> datetime:
    return datetime.now(UTC)


class BankFeedService:
    def __init__(
        self,
        *,
        repository: SqlBankFeedRepository,
        bank: BankService,
        provider: BankFeedProvider,
        audit_log: AuditLog,
        settings: FeedSettings,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._repository = repository
        self._bank = bank
        self._provider = provider
        self._audit = audit_log
        self._settings = settings
        self._clock = clock

    # -- reads --------------------------------------------------------------------------------

    async def status(
        self, *, administration_id: uuid.UUID, bank_account_id: uuid.UUID
    ) -> FeedStatus:
        await self._account(administration_id, bank_account_id)
        connection = await self._repository.latest_for_account(
            administration_id=administration_id, bank_account_id=bank_account_id
        )
        if connection is not None:
            connection = await self._expire_if_due(connection)
        return FeedStatus(
            configured=self._provider.is_configured,
            provider=self._provider.name,
            connection=connection,
        )

    async def institutions(self, *, country: str) -> Sequence[Institution]:
        try:
            return await self._provider.list_institutions(country=country)
        except FeedNotConfigured as exc:
            raise FeedUnavailable() from exc
        except (ProviderUnavailable, ProviderRefused) as exc:
            raise FeedProviderDown() from exc

    # -- connect and complete -------------------------------------------------------------------

    async def connect(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        bank_account_id: uuid.UUID,
        institution_id: str,
        institution_name: str | None,
        language: str,
        actor_user_id: uuid.UUID,
    ) -> ConnectStarted:
        if not self._provider.is_configured:
            raise FeedUnavailable()
        await self._account(administration_id, bank_account_id)
        active = await self._repository.active_for_account(
            administration_id=administration_id, bank_account_id=bank_account_id
        )
        if active is not None and active.status is ConnectionStatus.LINKED:
            raise FeedAlreadyLinked()
        if active is not None:
            # A consent started and abandoned (the person closed the bank's page): a new start
            # replaces it rather than leaving the account stuck behind it.
            await self._repository.update(
                administration_id=administration_id,
                connection_id=active.id,
                status=ConnectionStatus.FAILED,
                last_error="bank_feed_consent_abandoned",
            )

        connection = await self._repository.create_pending(
            organization_id=organization_id,
            administration_id=administration_id,
            bank_account_id=bank_account_id,
            provider=self._provider.name,
            institution_id=institution_id,
            institution_name=institution_name,
            user_id=actor_user_id,
        )
        try:
            started = await self._provider.start_consent(
                institution_id=institution_id,
                redirect_url=self._settings.redirect_url,
                reference=str(connection.id),
                language=language,
                history_days=self._settings.history_days,
                consent_days=self._settings.consent_days,
            )
        except ProviderRefused as exc:
            raise FeedConsentFailed() from exc
        except ProviderUnavailable as exc:
            raise FeedProviderDown() from exc

        connection = await self._repository.update(
            administration_id=administration_id,
            connection_id=connection.id,
            provider_reference=started.reference,
        )
        await self._record(connection, "connect_bank_feed", actor_user_id)
        return ConnectStarted(connection=connection, link=started.link)

    async def complete(
        self,
        *,
        administration_id: uuid.UUID,
        connection_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> FeedConnection:
        """The person came back from their bank. Idempotent: completing a linked connection again
        returns it unchanged, and a consent still in progress stays pending. A refused consent or
        an account mismatch is RETURNED as a failed connection with its reason, not raised, so
        the request's transaction keeps that state."""
        connection = await self._repository.get(
            administration_id=administration_id, connection_id=connection_id
        )
        if connection is None:
            raise FeedConnectionNotFound()
        if connection.status is not ConnectionStatus.PENDING:
            return connection
        if connection.provider_reference is None:
            raise FeedConsentFailed()

        try:
            consent = await self._provider.consent_status(reference=connection.provider_reference)
        except FeedNotConfigured as exc:
            raise FeedUnavailable() from exc
        except ProviderUnavailable as exc:
            raise FeedProviderDown() from exc
        except ProviderRefused:
            consent = None

        if consent is None or consent.state in {ConsentState.FAILED, ConsentState.EXPIRED}:
            failed = await self._repository.update(
                administration_id=administration_id,
                connection_id=connection.id,
                status=ConnectionStatus.FAILED,
                last_error="bank_feed_consent_failed",
            )
            await self._record(failed, "complete_bank_feed", actor_user_id, AuditOutcome.FAILURE)
            return failed
        if consent.state is ConsentState.PENDING:
            return connection

        account = await self._account(administration_id, connection.bank_account_id)
        chosen = await self._choose_account(account.iban, consent.account_ids)
        if chosen is None:
            mismatch = await self._repository.update(
                administration_id=administration_id,
                connection_id=connection.id,
                status=ConnectionStatus.FAILED,
                last_error="bank_feed_account_mismatch",
            )
            await self._record(mismatch, "complete_bank_feed", actor_user_id, AuditOutcome.FAILURE)
            return mismatch

        connection = await self._repository.update(
            administration_id=administration_id,
            connection_id=connection.id,
            status=ConnectionStatus.LINKED,
            provider_account_id=chosen,
            consent_expires_at=self._clock() + timedelta(days=self._settings.consent_days),
            last_error=None,
        )
        await self._record(connection, "complete_bank_feed", actor_user_id)
        # The first read. If the provider is down it is recorded on the connection and retried by
        # "Sync now" or the next daily run; linked stays linked.
        synced = await self.sync(
            organization_id=connection.organization_id,
            administration_id=administration_id,
            bank_account_id=connection.bank_account_id,
            actor_user_id=actor_user_id,
        )
        return synced.connection

    async def _choose_account(self, own_iban: str | None, account_ids: Sequence[str]) -> str | None:
        """The consented account whose IBAN is this bank account's. Without an IBAN on our side,
        only a single consented account is unambiguous."""
        own = normalise_iban(own_iban)
        if own is None:
            return account_ids[0] if len(account_ids) == 1 else None
        for account_id in account_ids:
            try:
                iban = await self._provider.account_iban(account_id=account_id)
            except (ProviderUnavailable, ProviderRefused) as exc:
                raise FeedProviderDown() from exc
            if normalise_iban(iban) == own:
                return account_id
        return None

    # -- sync -------------------------------------------------------------------------------------

    async def sync(
        self,
        *,
        organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        bank_account_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
    ) -> SyncResult:
        if not self._provider.is_configured:
            raise FeedUnavailable()
        connection = await self._repository.active_for_account(
            administration_id=administration_id, bank_account_id=bank_account_id
        )
        if connection is None or connection.status is not ConnectionStatus.LINKED:
            raise FeedNotLinked()
        connection = await self._expire_if_due(connection)
        if connection.status is ConnectionStatus.EXPIRED:
            return SyncResult(connection=connection, imported=None)
        assert connection.provider_account_id is not None  # 0077's check, for the type checker

        now = self._clock()
        if connection.last_synced_at is not None:
            date_from = (connection.last_synced_at - SYNC_OVERLAP).date()
        else:
            date_from = (now - timedelta(days=self._settings.history_days)).date()

        try:
            rows = await self._provider.fetch_transactions(
                account_id=connection.provider_account_id, date_from=date_from
            )
        except ProviderRefused:
            # A linked consent the bank no longer honours: revoked at the bank, or expired early.
            expired = await self._repository.update(
                administration_id=administration_id,
                connection_id=connection.id,
                status=ConnectionStatus.EXPIRED,
                last_error="bank_feed_consent_expired",
            )
            return SyncResult(connection=expired, imported=None)
        except ProviderUnavailable:
            down = await self._repository.update(
                administration_id=administration_id,
                connection_id=connection.id,
                last_error="bank_feed_provider_unavailable",
            )
            return SyncResult(connection=down, imported=None)

        imported = await self._bank.import_rows(
            organization_id=organization_id,
            administration_id=administration_id,
            bank_account_id=bank_account_id,
            rows=rows,
            filename=None,
            source_format="psd2",
            actor_user_id=actor_user_id,
        )
        connection = await self._repository.update(
            administration_id=administration_id,
            connection_id=connection.id,
            last_synced_at=now,
            last_error=None,
        )
        return SyncResult(connection=connection, imported=imported)

    # -- disconnect -------------------------------------------------------------------------------

    async def disconnect(
        self,
        *,
        administration_id: uuid.UUID,
        bank_account_id: uuid.UUID,
        actor_user_id: uuid.UUID,
    ) -> FeedConnection:
        connection = await self._repository.active_for_account(
            administration_id=administration_id, bank_account_id=bank_account_id
        )
        if connection is None:
            raise FeedNotLinked()
        if connection.provider_reference is not None and self._provider.is_configured:
            try:
                await self._provider.revoke(reference=connection.provider_reference)
            except ProviderUnavailable:
                # Revoked here regardless: Boeklite stops reading now. The consent at the provider
                # lapses on its own date; the audit event records that it could not be deleted.
                revoked = await self._revoke(connection)
                await self._record(
                    revoked, "disconnect_bank_feed", actor_user_id, AuditOutcome.FAILURE
                )
                return revoked
        revoked = await self._revoke(connection)
        await self._record(revoked, "disconnect_bank_feed", actor_user_id)
        return revoked

    async def _revoke(self, connection: FeedConnection) -> FeedConnection:
        return await self._repository.update(
            administration_id=connection.administration_id,
            connection_id=connection.id,
            status=ConnectionStatus.REVOKED,
        )

    # -- helpers ----------------------------------------------------------------------------------

    async def _account(
        self, administration_id: uuid.UUID, bank_account_id: uuid.UUID
    ) -> BankAccount:
        accounts = await self._bank.list_accounts(administration_id=administration_id)
        for account in accounts:
            if account.id == bank_account_id:
                return account
        raise BankAccountNotFound(f"bank account {bank_account_id} does not exist")

    async def _expire_if_due(self, connection: FeedConnection) -> FeedConnection:
        if (
            connection.status is ConnectionStatus.LINKED
            and connection.consent_expires_at is not None
            and connection.consent_expires_at <= self._clock()
        ):
            return await self._repository.update(
                administration_id=connection.administration_id,
                connection_id=connection.id,
                status=ConnectionStatus.EXPIRED,
                last_error="bank_feed_consent_expired",
            )
        return connection

    async def _record(
        self,
        connection: FeedConnection,
        action: str,
        actor_user_id: uuid.UUID | None,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
    ) -> None:
        await self._audit.record(
            AuditEvent(
                organization_id=connection.organization_id,
                administration_id=connection.administration_id,
                category=AuditCategory.CONFIGURATION,
                action=action,
                resource_type="bank_feed_connection",
                resource_id=connection.id,
                outcome=outcome,
                actor_type=ActorType.USER if actor_user_id is not None else ActorType.SYSTEM,
                actor_user_id=actor_user_id,
                detail={
                    "bank_account_id": str(connection.bank_account_id),
                    "provider": connection.provider,
                    "institution_id": connection.institution_id,
                    "status": connection.status.value,
                },
            )
        )
