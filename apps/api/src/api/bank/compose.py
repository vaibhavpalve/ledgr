"""How a `BankService` and a `BankFeedService` are put together, shared by the routes and by the
daily feed sync (scripts/sync_bank_feeds.py), so the job runs exactly the code a request does."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from api.audit.log import AuditLog
from api.audit.repository import SqlAuditRepository
from api.authz.repository import SqlAuthorizationRepository
from api.authz.service import AuthorizationService
from api.bank.adapters import BankFeedProvider
from api.bank.feed import BankFeedService, FeedSettings
from api.bank.feed_repository import SqlBankFeedRepository
from api.bank.repository import SqlBankRepository
from api.bank.service import BankService
from api.firm.proposals import ProposalGenerator, ProposalHook
from api.firm.proposals_repository import SqlProposalRepository
from api.invoicing.payments import SalesPaymentService
from api.invoicing.payments_repository import SqlPaymentRepository
from api.ledger.service import build_ledger_service


def build_bank_service(
    session: AsyncSession, authorization: AuthorizationService | None = None
) -> BankService:
    audit_log = AuditLog(SqlAuditRepository(session))
    repository = SqlBankRepository(session)
    return BankService(
        repository,
        build_ledger_service(session, audit_log),
        SalesPaymentService(
            repository=SqlPaymentRepository(session),
            ledger=build_ledger_service(session, audit_log),
            authorization=authorization
            or AuthorizationService(SqlAuthorizationRepository(session)),
            audit_log=audit_log,
        ),
        audit_log,
        build_proposal_hook(session, repository),
    )


def build_proposal_hook(session: AsyncSession, repository: SqlBankRepository) -> ProposalHook:
    """ADR-110's booking proposals, on the same tenant-scoped session as the import or
    reconciliation that triggers them."""
    proposals = SqlProposalRepository(session)
    return ProposalHook(
        generator=ProposalGenerator(proposals=proposals, matching=repository),
        proposals=proposals,
    )


def build_bank_feed_service(
    session: AsyncSession,
    *,
    bank: BankService,
    provider: BankFeedProvider,
    settings: FeedSettings,
) -> BankFeedService:
    return BankFeedService(
        repository=SqlBankFeedRepository(session),
        bank=bank,
        provider=provider,
        audit_log=AuditLog(SqlAuditRepository(session)),
        settings=settings,
    )
