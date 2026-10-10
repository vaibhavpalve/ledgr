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
from api.firm.rules import RuleApplier
from api.firm.rules_repository import SqlRuleRepository
from api.invoicing.payments import SalesPaymentService
from api.invoicing.payments_repository import SqlPaymentRepository
from api.ledger.service import build_ledger_service


def build_bank_service(
    session: AsyncSession, authorization: AuthorizationService | None = None
) -> BankService:
    service, _ = _wire(session, SqlBankRepository(session), authorization)
    return service


def build_proposal_hook(session: AsyncSession, repository: SqlBankRepository) -> ProposalHook:
    """ADR-110's booking proposals, on the same tenant-scoped session as the import or
    reconciliation that triggers them - with ADR-113's rules applied to what it proposes, booked
    through a BankService on that same session."""
    _, hook = _wire(session, repository, None)
    return hook


def _wire(
    session: AsyncSession,
    repository: SqlBankRepository,
    authorization: AuthorizationService | None,
) -> tuple[BankService, ProposalHook]:
    audit_log = AuditLog(SqlAuditRepository(session))
    authorization = authorization or AuthorizationService(SqlAuthorizationRepository(session))
    proposals = SqlProposalRepository(session)
    generator = ProposalGenerator(proposals=proposals, matching=repository)
    hook = ProposalHook(generator=generator, proposals=proposals)
    service = BankService(
        repository,
        build_ledger_service(session, audit_log),
        SalesPaymentService(
            repository=SqlPaymentRepository(session),
            ledger=build_ledger_service(session, audit_log),
            authorization=authorization,
            audit_log=audit_log,
        ),
        audit_log,
        hook,
    )
    # ADR-113: a rule approves through exactly this BankService - the wave-1 approve path. Wired
    # after construction because the service and the generator each need the other.
    generator.auto_approve = RuleApplier(
        store=SqlRuleRepository(session),
        authorization=authorization,
        bank=service,
        audit_log=audit_log,
    )
    return service, hook


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
