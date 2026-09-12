"""The ledger service: the bounded context's only entry point (PRD §6.2).

CLAUDE.md's first architectural non-negotiable is "the ledger is a separate
bounded context with a narrow API. Nothing writes to posting tables except
the ledger service." That sentence is enforced in three places, and only one
of them is this file:

  1. **Privileges (migration 0020).** `ledgr_app` holds SELECT on the posting
     tables and nothing else. Application code that bypassed this service
     entirely and wrote its own INSERT would get `permission denied for table
     journal_entry`. This is the enforcement; the other two are ergonomics.

  2. **The `ledger` schema's SECURITY DEFINER functions.** They run as
     `ledgr_ledger`, which holds the only INSERT privilege in the system.
     Their signatures are the narrow API, and the code a reviewer has to
     audit for "can this write a bad posting" is exactly their bodies.

  3. **This class**, which is the only Python that may import
     `api.ledger.repository`. tests/ledger/test_bounded_context.py fails the
     build if another module does, so the mistake is caught at review time
     with a good message rather than at runtime with a privilege error.

--- Why the validation here is not the guarantee ---

`post()` checks the entry balances before sending it. That check is for the
error message. `journal_entry_balanced_trg` re-derives the same sum from the
stored rows at COMMIT and aborts the transaction if it disagrees, which is
what makes FR-GL-001's "no transaction may persist" true. If this file and
the database ever disagree, the database is right and this file has a bug.

--- The audit entry is not optional ---

IAM-090 names postings explicitly. `AuditLog` is a required constructor
argument, not an optional one, because an audit trail a caller may forget to
write is not an audit trail. It shares this service's session, so the posting
and its audit entry commit or roll back together: a posting without a
corresponding audit entry is not a state this code can reach.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING

from api.audit.log import ActorType, AuditCategory, AuditEvent, AuditLog, AuditOutcome
from api.ledger.model import (
    Account,
    AccountStatus,
    AccountType,
    ControlKind,
    EntryInput,
    Journal,
    JournalType,
    LedgerError,
    Party,
    PartyKind,
    PostedEntry,
    ReconciliationRow,
    SubledgerRow,
    TrialBalanceRow,
)
from api.ledger.repository import LedgerRepository

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncSession


class AlreadyReversed(LedgerError):
    """FR-GL-003. An entry may be reversed once; a second reversal would
    double the correction and overstate the ledger in the other direction.
    """


class EntryNotFound(LedgerError):
    pass


class MissingActor(LedgerError):
    """FR-GL-004 lists `actor` among the fields every posting carries.

    A posting with no actor is not a lesser posting, it is an unattributable
    one - and an unattributable posting defeats both the audit trail (IAM-090)
    and segregation of duties (IAM-060), which can only ask "did the same
    person do both" if it knows who did either.

    An automated posting is not an exception to that; it has a different KIND
    of actor. Bank imports, recurring templates and year-end close post as
    ActorType.SYSTEM, which the caller has to say deliberately rather than
    reach by leaving a field blank.
    """


class LedgerService:
    def __init__(self, repository: LedgerRepository, audit_log: AuditLog) -> None:
        self._repository = repository
        self._audit = audit_log

    # -- posting ----------------------------------------------------------

    async def post(
        self,
        entry: EntryInput,
        *,
        actor_user_id: uuid.UUID | None = None,
        actor_type: ActorType = ActorType.USER,
        correlation_id: str | None = None,
    ) -> PostedEntry:
        """FR-GL-001 through FR-GL-004. The only way a posting is written.

        The local balance check runs first so a caller gets a precise error
        without a round trip. It is deliberately NOT the guarantee - see the
        module docstring.
        """
        entry.assert_balanced()
        actor = self._resolve_actor(actor_user_id or entry.posted_by_user_id, actor_type)

        posted = await self._repository.post(entry)

        # Same session, therefore the same transaction as the posting. The
        # deferred balance trigger fires at COMMIT, after this: if it aborts,
        # the audit entry goes with it, which is correct - nothing was posted.
        await self._audit.record(
            AuditEvent(
                organization_id=posted.organization_id,
                administration_id=posted.administration_id,
                category=AuditCategory.POSTING,
                action="post_journal_entry",
                resource_type="journal_entry",
                resource_id=posted.id,
                outcome=AuditOutcome.SUCCESS,
                actor_type=actor_type,
                actor_user_id=actor,
                correlation_id=correlation_id,
                detail={
                    "journal_id": str(posted.journal_id),
                    "period_id": str(posted.period_id),
                    "entry_number": posted.entry_number,
                    "entry_date": posted.entry_date.isoformat(),
                    # Strings, not floats. The audit detail is jsonb and is
                    # inside the hash chain; a float here would both violate
                    # NFR-031 and make the entry's own hash depend on IEEE 754
                    # rounding.
                    "total_debit": f"{entry.total_debit:.2f}",
                    "total_credit": f"{entry.total_credit:.2f}",
                    "lines": len(entry.lines),
                    "reverses_entry_id": (
                        str(posted.reverses_entry_id) if posted.reverses_entry_id else None
                    ),
                },
            )
        )
        return posted

    async def reverse(
        self,
        *,
        entry_id: uuid.UUID,
        period_id: uuid.UUID,
        entry_date: date,
        description: str,
        actor_user_id: uuid.UUID | None = None,
        actor_type: ActorType = ActorType.USER,
        source_system: str = "api",
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> PostedEntry:
        """FR-GL-003's correction mechanism, and the only one there is.

        The mirror lines are built in the database from the original's own
        lines, so a caller cannot supply a "reversal" that quietly differs
        from what it claims to reverse. `journal_entry_reverses_trg` verifies
        the result independently, which is what binds a second write path.

        `period_id` is the caller's, not the original's: the original's period
        is usually locked by the time an error is found, and posting the
        correction into the current open period is the correct accounting
        treatment rather than a workaround for the lock.
        """
        actor = self._resolve_actor(actor_user_id, actor_type)

        original = await self._repository.entry(entry_id)
        if original is None:
            raise EntryNotFound(f"entry {entry_id} does not exist")

        # Checked for a clear error. journal_entry_reverses_once_idx is what
        # actually guarantees it, including against two concurrent reversals
        # that would both pass this check.
        existing = await self._repository.reversal_of(entry_id)
        if existing is not None:
            raise AlreadyReversed(
                f"entry {original.entry_number} was already reversed by entry "
                f"{existing.entry_number} (FR-GL-003)"
            )

        reversal = await self._repository.reverse(
            entry_id=entry_id,
            period_id=period_id,
            entry_date=entry_date,
            description=description,
            posted_by_user_id=actor,
            source_system=source_system,
            idempotency_key=idempotency_key,
        )

        await self._audit.record(
            AuditEvent(
                organization_id=reversal.organization_id,
                administration_id=reversal.administration_id,
                category=AuditCategory.POSTING,
                action="reverse_journal_entry",
                resource_type="journal_entry",
                resource_id=reversal.id,
                outcome=AuditOutcome.SUCCESS,
                actor_type=actor_type,
                actor_user_id=actor,
                correlation_id=correlation_id,
                detail={
                    "reverses_entry_id": str(entry_id),
                    "reverses_entry_number": original.entry_number,
                    "entry_number": reversal.entry_number,
                    "period_id": str(period_id),
                },
            )
        )
        return reversal

    @staticmethod
    def _resolve_actor(actor_user_id: uuid.UUID | None, actor_type: ActorType) -> uuid.UUID | None:
        """FR-GL-004's `actor`, checked before anything is written.

        AuditEvent refuses a user-attributed entry naming nobody, so without
        this the failure would arrive from the audit layer AFTER the posting
        had been sent - inside the same transaction, so nothing would be
        persisted, but the error would point at the wrong thing entirely.
        """
        if actor_type is ActorType.USER and actor_user_id is None:
            raise MissingActor(
                "every posting carries an actor (FR-GL-004): supply "
                "posted_by_user_id, or pass actor_type=ActorType.SYSTEM for an "
                "automated posting such as a bank import or a recurring template."
            )
        return actor_user_id

    async def entry(self, entry_id: uuid.UUID) -> PostedEntry | None:
        return await self._repository.entry(entry_id)

    async def reversal_of(self, entry_id: uuid.UUID) -> PostedEntry | None:
        return await self._repository.reversal_of(entry_id)

    # -- master data ------------------------------------------------------

    async def create_account(
        self,
        *,
        administration_id: uuid.UUID,
        code: str,
        name: str,
        account_type: AccountType,
        rgs_code: str | None = None,
        default_vat_code: str | None = None,
        control_kind: ControlKind | None = None,
    ) -> Account:
        """FR-GL-005, and FR-GL-006's other half.

        `control_kind` can only be set here. It is immutable afterwards
        (ledger_account_identity_immutable_trg), because flipping it on an
        account that already has postings would retroactively turn every one
        of them into a direct posting to a control account.
        """
        return await self._repository.create_account(
            administration_id=administration_id,
            code=code,
            name=name,
            account_type=account_type,
            rgs_code=rgs_code,
            default_vat_code=default_vat_code,
            control_kind=control_kind,
        )

    async def block_account(self, account_id: uuid.UUID) -> Account:
        """FR-GL-005. A blocked account keeps every posting it already has and
        takes no new ones. Deleting it is not offered and never will be: its
        history is in an append-only table.
        """
        return await self._repository.set_account_status(
            account_id=account_id, status=AccountStatus.BLOCKED
        )

    async def unblock_account(self, account_id: uuid.UUID) -> Account:
        return await self._repository.set_account_status(
            account_id=account_id, status=AccountStatus.ACTIVE
        )

    async def create_journal(
        self,
        *,
        administration_id: uuid.UUID,
        code: str,
        name: str,
        journal_type: JournalType,
    ) -> Journal:
        return await self._repository.create_journal(
            administration_id=administration_id,
            code=code,
            name=name,
            journal_type=journal_type,
        )

    async def block_journal(self, journal_id: uuid.UUID) -> Journal:
        """FR-GL-002. A blocked journal keeps its numbering series and its
        entries and accepts no new ones - the same treatment as a blocked
        account, and for the same reason: FR-GL-013's series must stay intact,
        so removal is not on offer.
        """
        return await self._repository.set_journal_status(journal_id=journal_id, status="blocked")

    async def create_party(
        self,
        *,
        administration_id: uuid.UUID,
        party_kind: PartyKind,
        name: str,
        external_reference: str | None = None,
    ) -> Party:
        return await self._repository.create_party(
            administration_id=administration_id,
            party_kind=party_kind,
            name=name,
            external_reference=external_reference,
        )

    # -- reporting --------------------------------------------------------

    async def numbering_gaps(
        self, *, journal_id: uuid.UUID, fiscal_year_id: uuid.UUID
    ) -> Sequence[int]:
        """FR-GL-013's gap detection reporting.

        Should always return an empty sequence: the allocator makes a gap
        impossible. It exists because a report that can only ever say "no
        gaps" is the check ON the allocator - and because an inspector asks to
        see the report, not the trigger (CMP-009).
        """
        return await self._repository.numbering_gaps(
            journal_id=journal_id, fiscal_year_id=fiscal_year_id
        )

    async def trial_balance(
        self, *, administration_id: uuid.UUID, fiscal_year_id: uuid.UUID
    ) -> Sequence[TrialBalanceRow]:
        return await self._repository.trial_balance(
            administration_id=administration_id, fiscal_year_id=fiscal_year_id
        )

    async def subledger_balance(
        self, *, administration_id: uuid.UUID, control_kind: ControlKind
    ) -> Sequence[SubledgerRow]:
        """FR-GL-006. These rows ARE the control account's own lines grouped by
        party - there is no separately maintained sub-ledger balance, so there
        is nothing that can drift out of step with the control account.
        """
        return await self._repository.subledger_balance(
            administration_id=administration_id, control_kind=control_kind
        )

    async def reconciliation(self, *, administration_id: uuid.UUID) -> Sequence[ReconciliationRow]:
        """FR-GL-006's "reconciled to control accounts continuously", as
        something showable. Every row's `difference` is zero by construction.
        """
        return await self._repository.reconciliation(administration_id=administration_id)


def build_ledger_service(session: AsyncSession, audit_log: AuditLog) -> LedgerService:
    """A wired LedgerService, for callers outside this bounded context.

    The context's API is `LedgerService`, and until now the only way to obtain
    one was to construct its repository - which meant importing
    `api.ledger.repository`, the module tests/ledger/test_bounded_context.py
    exists to keep private. A caller that reaches for the repository to build
    the service has reached past the narrow API even with the best intentions,
    and the next caller reaches for a query on it.

    So this is the deliberate widening that test's failure message asks for:
    one function, on a public module, that hands back the service and keeps the
    SQL where it belongs. `api.expenses.posting` uses it to confirm an expense
    claim into the books (FR-EXP-001d).

    The repository import is function-local rather than module-level so that
    the only place naming it stays the one line below, where the check can see
    it is the context's own.
    """
    from api.ledger.repository import SqlLedgerRepository

    return LedgerService(SqlLedgerRepository(session), audit_log)
