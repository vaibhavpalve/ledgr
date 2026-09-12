"""IAM-110: what happens when a client revokes a firm's access.

    "On revocation of firm access, firm sessions for that administration
     terminate immediately, the administration disappears from the firm
     switcher, and the client retains all data including entries the firm
     made."

Three clauses, and they are three different kinds of guarantee - which is
why this module does three quite different things rather than one thing three
times.

--- 1. "Firm sessions for that administration terminate immediately" ---

Two halves, and only one of them needs code.

The half that needs none: authorization is evaluated per request against live
grants (IAM-034), so the moment the grants are revoked the firm's next request
is denied. Nothing is cached, so there is nothing to invalidate and no window
to close. "Immediately" is already true and would be true even if this module
did not exist.

The half that needs code: a session carries an active_administration_id (the
thing a firm switcher sets), and a session sitting inside a revoked
administration must not stay there. That column is what makes "sessions FOR
THAT ADMINISTRATION" name specific rows - without it the only options would be
terminating a firm employee's entire login, which also cuts them off from
every other client, or terminating nothing.

Terminating is deliberately narrower than revoking. `revoke_session` would
sign the user out of LEDGR entirely; `clear_administration_context` puts them
back at the switcher, which is what losing access to one client means when
they still have four others. A firm employee whose only client revokes them
ends up at an empty switcher, which is correct and is not the same as being
signed out.

--- 2. "The administration disappears from the firm switcher" ---

The switcher is not a stored list, and that is the design. `switchable_administrations`
derives it from live grants every time it is called, so an administration
disappears because the grant that put it there is gone - not because a
revocation remembered to remove it from somewhere. A cached switcher list
would be one more place a revocation could be forgotten.

--- 3. "The client retains all data including entries the firm made" ---

A pure non-action, and the hardest of the three to test meaningfully, because
what it forbids is a cascade nobody wrote. It holds structurally: no table in
this schema grants DELETE to ledgr_app, no foreign key uses ON DELETE CASCADE,
and the ledger is append-only by CLAUDE.md's second rule. Revocation writes
`revoked_at` and nothing else.

`RevocationReceipt.retained` states it positively rather than leaving the
absence of deletion to be inferred - the client asking "what happens to my
books if I fire my accountant" deserves an answer, not silence.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from api.audit.log import AuditOutcome
from api.audit.trail import AuditTrail


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NotPermittedToRevokeError(Exception):
    """IAM-105 puts "the ability to revoke the firm's access" in the client
    rights floor, so this is a client decision. A firm cannot revoke its own
    engagement on a client's behalf, and nobody else can revoke it for them.
    """


class NoSuchEngagementError(Exception):
    """No active engagement between this firm and this administration."""


@dataclass(frozen=True, slots=True)
class SwitchableAdministration:
    """One entry in the firm switcher, derived from a live grant."""

    administration_id: uuid.UUID
    legal_name: str
    role_name: str
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RevocationReceipt:
    """What actually happened, in terms the client can be shown.

    Deliberately a value rather than a log line: a client revoking their
    accountant's access is entitled to see the effect confirmed, and a
    caller that wants to record it (IAM-111's audit log, not built) has
    something structured to record.
    """

    administration_id: uuid.UUID
    firm_organization_id: uuid.UUID
    revoked_at: datetime
    grants_revoked: int
    sessions_cleared: int
    users_affected: tuple[uuid.UUID, ...] = ()
    # IAM-110's third clause, stated rather than implied. Nothing sets this
    # to False; it exists so the receipt says what was KEPT as well as what
    # was withdrawn.
    retained: str = (
        "All data in this administration is retained, including entries and documents "
        "the firm created. Revoking access withdraws the firm's permission to read or "
        "write; it removes nothing."
    )


class EngagementRevocationRepository(Protocol):
    async def active_engagement_exists(
        self, *, firm_organization_id: uuid.UUID, administration_id: uuid.UUID
    ) -> bool: ...

    async def owning_organization(self, administration_id: uuid.UUID) -> uuid.UUID | None: ...

    async def revoke_engagement(
        self,
        *,
        firm_organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        revoked_by_user_id: uuid.UUID,
        at: datetime,
    ) -> None: ...

    async def revoke_firm_grants_on(
        self,
        *,
        firm_organization_id: uuid.UUID,
        administration_id: uuid.UUID,
        at: datetime,
    ) -> Sequence[uuid.UUID]:
        """Revokes every live grant this firm made on this administration and
        returns the users who held them. Keyed on provenance (0016), so the
        client's OWN users - who hold identically-shaped rows on the same
        administration - are untouched.
        """
        ...

    async def clear_administration_context(
        self, *, administration_id: uuid.UUID, user_ids: Sequence[uuid.UUID], at: datetime
    ) -> int:
        """Puts the named users' sessions back at the switcher if they were
        working inside this administration. Returns how many were cleared.
        """
        ...

    async def switchable_administrations(
        self, *, user_id: uuid.UUID, now: datetime
    ) -> Sequence[SwitchableAdministration]: ...

    async def set_active_administration(
        self,
        *,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        administration_id: uuid.UUID | None,
    ) -> bool:
        """Writes sessions.active_administration_id. Scoped to user_id as
        well as session_id so a stolen or malformed session identifier
        cannot move somebody else's session; returns whether a row matched.
        """
        ...


class NoSessionError(Exception):
    """IAM-110's session clause needs to know WHICH session to move. A
    request whose token carries no `sid` cannot be switched - writing to all
    of the user's sessions would move devices the person is not holding.
    """


class EngagementRevocationService:
    def __init__(
        self,
        repository: EngagementRevocationRepository,
        *,
        clock: Callable[[], datetime] = _utcnow,
        audit: AuditTrail | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._audit = audit

    async def revoke_firm_access(
        self,
        *,
        administration_id: uuid.UUID,
        firm_organization_id: uuid.UUID,
        revoked_by_user_id: uuid.UUID,
        acting_organization_id: uuid.UUID,
    ) -> RevocationReceipt:
        """The client withdrawing a firm's access.

        Ordering is load-bearing. The grants go first, because that is the
        step that actually stops access - every subsequent request is denied
        from that moment by live evaluation. Clearing session context comes
        after, as housekeeping: it moves a firm user out of a screen they can
        no longer read anything on. If the process died between the two, the
        firm would be locked out with a stale screen, which is the failure
        direction that costs nothing.
        """
        owner = await self._repository.owning_organization(administration_id)
        if owner is None:
            raise NoSuchEngagementError(f"administration {administration_id} is not visible")

        # IAM-105: revoking the firm's access is the CLIENT's right. A firm
        # cannot exercise it on the client's behalf - not because a firm
        # revoking itself would be harmful, but because "the ability to
        # revoke" belongs to a party whose access nobody else controls, and
        # letting the firm do it would make the audit trail lie about who
        # ended the relationship.
        if acting_organization_id != owner:
            raise NotPermittedToRevokeError(
                f"organization {acting_organization_id} does not own administration "
                f"{administration_id}; revoking a firm's access is the client's own "
                "right (IAM-105)"
            )

        if not await self._repository.active_engagement_exists(
            firm_organization_id=firm_organization_id,
            administration_id=administration_id,
        ):
            raise NoSuchEngagementError(
                f"organization {firm_organization_id} has no active engagement on "
                f"administration {administration_id}"
            )

        now = self._clock()

        affected = await self._repository.revoke_firm_grants_on(
            firm_organization_id=firm_organization_id,
            administration_id=administration_id,
            at=now,
        )

        await self._repository.revoke_engagement(
            firm_organization_id=firm_organization_id,
            administration_id=administration_id,
            revoked_by_user_id=revoked_by_user_id,
            at=now,
        )

        cleared = await self._repository.clear_administration_context(
            administration_id=administration_id, user_ids=affected, at=now
        )

        receipt = RevocationReceipt(
            administration_id=administration_id,
            firm_organization_id=firm_organization_id,
            revoked_at=now,
            grants_revoked=len(affected),
            sessions_cleared=cleared,
            users_affected=tuple(affected),
        )

        # Recorded last, once the outcome is known - and against the client's
        # own organization, because ending an engagement is the client's
        # decision (IAM-105) and belongs in the log they can read (IAM-094).
        if self._audit is not None:
            await self._audit.permission_change(
                organization_id=owner,
                administration_id=administration_id,
                actor_user_id=revoked_by_user_id,
                action="revoke",
                resource_type="firm_engagement",
                outcome=AuditOutcome.SUCCESS,
                detail={
                    "firm_organization_id": str(firm_organization_id),
                    "grants_revoked": receipt.grants_revoked,
                    "sessions_cleared": receipt.sessions_cleared,
                },
            )

        return receipt

    async def switchable_administrations(
        self, user_id: uuid.UUID
    ) -> Sequence[SwitchableAdministration]:
        """The firm switcher, computed from live grants rather than stored.

        This is what makes IAM-110's "the administration disappears from the
        firm switcher" a consequence rather than a step: there is no list to
        remove it from.
        """
        return await self._repository.switchable_administrations(user_id=user_id, now=self._clock())

    async def switch_to(
        self,
        *,
        session_id: uuid.UUID | None,
        user_id: uuid.UUID,
        administration_id: uuid.UUID,
    ) -> SwitchableAdministration:
        """Moves this session into an administration - the write that makes
        IAM-110's "sessions for that administration" refer to anything.

        Deliberately re-checks the switcher rather than trusting the caller's
        authorization alone. The route already required `view administration`
        on the target, but the switcher is the list of administrations the
        user holds a LIVE administration-scoped grant on, and those are not
        the same set: an organization Owner passes the permission check on
        every administration their organization owns without necessarily
        appearing in a firm switcher. Writing a context the switcher would
        not offer would put a session somewhere the UI cannot represent.

        Setting the context grants nothing. Every subsequent request still
        evaluates live grants (IAM-034), so a session pointing at an
        administration whose grants were revoked is denied exactly as if the
        column were empty.
        """
        if session_id is None:
            raise NoSessionError(
                "this request carries no session identifier, so there is no session to "
                "switch; writing to every session the user holds would move devices "
                "they are not holding"
            )

        available = {
            entry.administration_id: entry
            for entry in await self._repository.switchable_administrations(
                user_id=user_id, now=self._clock()
            )
        }
        entry = available.get(administration_id)
        if entry is None:
            raise NoSuchEngagementError(
                f"administration {administration_id} is not in this user's switcher"
            )

        if not await self._repository.set_active_administration(
            session_id=session_id, user_id=user_id, administration_id=administration_id
        ):
            raise NoSessionError(f"session {session_id} is not a live session for this user")

        return entry

    async def leave_administration(
        self, *, session_id: uuid.UUID | None, user_id: uuid.UUID
    ) -> bool:
        """Back to the switcher, without signing out. The same write
        revocation performs (api.authz.engagement_revocation clears the
        column rather than revoking the session), reached deliberately
        instead of as a consequence.
        """
        if session_id is None:
            raise NoSessionError("this request carries no session identifier")
        return await self._repository.set_active_administration(
            session_id=session_id, user_id=user_id, administration_id=None
        )
