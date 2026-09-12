"""An in-memory LedgerRepository that REIMPLEMENTS migration 0020's
invariants rather than stubbing them out.

A fake that accepted anything would make every pure test in tests/ledger/
meaningless: they would prove the service calls the repository, not that the
ledger holds. So this one refuses what Postgres refuses, in the same order,
with errors naming the same requirement:

    FR-GL-001  balance and the empty-entry case
    FR-GL-002  journal and period belong to the administration
    FR-GL-003  entries are sealed; reversals mirror; reverse-once
    FR-GL-005  blocked accounts take no postings
    FR-GL-006  the control-account biconditional
    FR-GL-007  postings only into an open period
    FR-GL-013  gapless numbering per journal per year
    NFR-032    an idempotency key returns the first entry, never a second

The risk this creates is drift: a fake that has quietly stopped matching
Postgres keeps its tests green while testing something production does not
do. tests/integration/test_ledger_invariants.py runs the SAME scenario table
(_INVARIANT_CASES, imported from tests/ledger/cases.py) against a real
database, so a divergence fails there. That mirrors what 0019's
`test_the_in_memory_fake_computes_the_same_hash_as_postgres` does for the
audit chain.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal

from api.ledger.model import (
    ZERO,
    Account,
    AccountStatus,
    AccountType,
    ControlKind,
    EntryInput,
    Journal,
    JournalType,
    LedgerError,
    LineInput,
    Party,
    PartyKind,
    PostedEntry,
    PostedLine,
    ReconciliationRow,
    SubledgerRow,
    TrialBalanceRow,
    amount_to_string,
)

#: (account, party) -> (debit, credit). The grouping the reversal check
#: compares by, so a reversal may consolidate two lines to the same account
#: into one as long as the totals mirror.
_Totals = dict[tuple[uuid.UUID, uuid.UUID | None], tuple[Decimal, Decimal]]


@dataclass
class FakePeriod:
    id: uuid.UUID
    administration_id: uuid.UUID
    organization_id: uuid.UUID
    fiscal_year_id: uuid.UUID
    period_number: int
    start_date: date
    end_date: date
    status: str = "open"


@dataclass
class FakeFiscalYear:
    id: uuid.UUID
    administration_id: uuid.UUID
    organization_id: uuid.UUID
    status: str = "open"


@dataclass
class InMemoryLedgerRepository:
    """Mirrors 0020's tables. Nothing here is a Mapping of convenience - the
    field names match the columns, so a reader comparing this to the migration
    can do it line by line.
    """

    organization_id: uuid.UUID = field(default_factory=uuid.uuid4)
    administration_id: uuid.UUID = field(default_factory=uuid.uuid4)

    accounts: dict[uuid.UUID, Account] = field(default_factory=dict)
    journals: dict[uuid.UUID, Journal] = field(default_factory=dict)
    parties: dict[uuid.UUID, Party] = field(default_factory=dict)
    periods: dict[uuid.UUID, FakePeriod] = field(default_factory=dict)
    fiscal_years: dict[uuid.UUID, FakeFiscalYear] = field(default_factory=dict)

    entries: dict[uuid.UUID, PostedEntry] = field(default_factory=dict)
    #: (journal_id, fiscal_year_id) -> next number. The allocator, not a
    #: SEQUENCE: a rolled-back post must not consume a number (FR-GL-013).
    sequences: dict[tuple[uuid.UUID, uuid.UUID], int] = field(default_factory=dict)

    # -- seeding ----------------------------------------------------------

    def add_fiscal_year(self, *, status: str = "open") -> FakeFiscalYear:
        year = FakeFiscalYear(
            id=uuid.uuid4(),
            administration_id=self.administration_id,
            organization_id=self.organization_id,
            status=status,
        )
        self.fiscal_years[year.id] = year
        return year

    def add_period(
        self,
        fiscal_year: FakeFiscalYear,
        *,
        period_number: int = 1,
        status: str = "open",
        start_date: date = date(2026, 1, 1),
        end_date: date = date(2026, 1, 31),
    ) -> FakePeriod:
        period = FakePeriod(
            id=uuid.uuid4(),
            administration_id=fiscal_year.administration_id,
            organization_id=fiscal_year.organization_id,
            fiscal_year_id=fiscal_year.id,
            period_number=period_number,
            start_date=start_date,
            end_date=end_date,
            status=status,
        )
        self.periods[period.id] = period
        return period

    # -- writes -----------------------------------------------------------

    async def post(self, entry: EntryInput) -> PostedEntry:
        # NFR-032, checked before a number is allocated so a retry does not
        # consume one - the same ordering as ledger.post_entry().
        if entry.idempotency_key is not None:
            for existing in self.entries.values():
                if (
                    existing.administration_id == entry.administration_id
                    and existing.idempotency_key == entry.idempotency_key
                ):
                    return existing

        period = self.periods.get(entry.period_id)
        if period is None:
            raise LedgerError(f"period {entry.period_id} does not exist")
        if period.administration_id != entry.administration_id:
            raise LedgerError(
                f"period {entry.period_id} belongs to another administration (FR-GL-002)"
            )

        journal = self.journals.get(entry.journal_id)
        if journal is None:
            raise LedgerError(f"journal {entry.journal_id} does not exist")
        if journal.administration_id != entry.administration_id:
            raise LedgerError(
                f"journal {entry.journal_id} belongs to another administration (FR-GL-002)"
            )
        if journal.status != "active":
            raise LedgerError(f"journal {journal.code} is blocked")

        year = self.fiscal_years[period.fiscal_year_id]

        # FR-GL-007
        if period.status == "vat_filed":
            raise LedgerError(
                f"period {period.period_number} is VAT-filed and hard-locked; a "
                "change requires a suppletie (FR-GL-007)"
            )
        if period.status != "open":
            raise LedgerError(
                f"period {period.period_number} is {period.status} and cannot be "
                "posted to (FR-GL-007)"
            )
        if year.status != "open":
            raise LedgerError("fiscal year is closed (FR-GL-007/FR-GL-008)")
        if not period.start_date <= entry.entry_date <= period.end_date:
            raise LedgerError(
                f"entry date {entry.entry_date} falls outside period {period.period_number}"
            )

        lines = [
            self._validate_line(entry, index, line) for index, line in enumerate(entry.lines, 1)
        ]

        # FR-GL-001. Postgres defers this to COMMIT; the fake has no commit, so
        # it runs here - which is the same observable behaviour for a caller,
        # because post() is atomic either way. The empty case is checked
        # separately for the same reason it is in SQL: summing nothing gives
        # zero on both sides and would pass.
        if len(lines) == 0:
            raise LedgerError("a journal entry with no lines is not a balanced entry (FR-GL-001)")
        if len(lines) < 2:
            raise LedgerError("double-entry requires at least two lines (FR-GL-001)")
        total_debit = sum((line.debit for line in lines), ZERO)
        total_credit = sum((line.credit for line in lines), ZERO)
        if total_debit != total_credit:
            raise LedgerError(
                f"entry is unbalanced: debits {total_debit} <> credits {total_credit} (FR-GL-001)"
            )

        if entry.reverses_entry_id is not None:
            self._assert_mirrors(entry.reverses_entry_id, lines, entry.administration_id)

        # FR-GL-013. Allocated last, so every rejection above leaves the series
        # untouched - which is exactly what "gapless" needs from a failure.
        key = (entry.journal_id, year.id)
        number = self.sequences.get(key, 1)
        self.sequences[key] = number + 1

        posted = PostedEntry(
            id=uuid.uuid4(),
            organization_id=period.organization_id,
            administration_id=entry.administration_id,
            fiscal_year_id=year.id,
            period_id=period.id,
            journal_id=entry.journal_id,
            entry_number=number,
            entry_date=entry.entry_date,
            description=entry.description,
            document_reference=entry.document_reference,
            posted_by_user_id=entry.posted_by_user_id,
            source_system=entry.source_system,
            posted_at=datetime.now(UTC),
            reverses_entry_id=entry.reverses_entry_id,
            idempotency_key=entry.idempotency_key,
            lines=tuple(lines),
        )
        self.entries[posted.id] = posted
        return posted

    def _validate_line(self, entry: EntryInput, index: int, line: LineInput) -> PostedLine:
        account = self.accounts.get(line.account_id)
        if account is None:
            raise LedgerError(f"account {line.account_id} does not exist")
        if account.administration_id != entry.administration_id:
            raise LedgerError("account belongs to another administration")

        # FR-GL-005
        if account.status is not AccountStatus.ACTIVE:
            raise LedgerError(
                f"account {account.code} is blocked and cannot be posted to (FR-GL-005)"
            )

        party_id = line.subledger_party_id

        # FR-GL-006, as a biconditional. Both halves matter: a control account
        # without a party is a direct posting; a party on an ordinary account
        # is a receivable the control account cannot see.
        if account.control_kind is not None and party_id is None:
            raise LedgerError(
                f"account {account.code} is the {account.control_kind.value} control "
                "account and cannot be posted to directly; the line must name a "
                "sub-ledger party (FR-GL-006)"
            )
        if account.control_kind is None and party_id is not None:
            raise LedgerError(
                f"account {account.code} is not a control account and cannot carry "
                "a sub-ledger party (FR-GL-006)"
            )

        if party_id is not None:
            party = self.parties.get(party_id)
            if party is None:
                raise LedgerError(f"sub-ledger party {party_id} does not exist")
            if party.administration_id != entry.administration_id:
                raise LedgerError("sub-ledger party belongs to another administration")
            assert account.control_kind is not None
            if party.party_kind is not account.control_kind.party_kind:
                raise LedgerError(
                    f"a {party.party_kind.value} party cannot be posted to the "
                    f"{account.control_kind.value} control account (FR-GL-006)"
                )

        # Re-runs the type and scale checks so a line built by bypassing
        # LineInput.__post_init__ is still rejected (NFR-031).
        amount_to_string(line.debit)
        amount_to_string(line.credit)

        return PostedLine(
            id=uuid.uuid4(),
            line_number=index,
            account_id=line.account_id,
            debit=line.debit,
            credit=line.credit,
            subledger_party_id=party_id,
            cost_centre_id=line.cost_centre_id,
            description=line.description,
        )

    def _assert_mirrors(
        self,
        original_id: uuid.UUID,
        lines: Sequence[PostedLine],
        administration_id: uuid.UUID,
    ) -> None:
        original = self.entries.get(original_id)
        if original is None:
            raise LedgerError(f"entry {original_id} does not exist and cannot be reversed")
        if original.administration_id != administration_id:
            raise LedgerError("a reversal must belong to the same administration")
        if any(e.reverses_entry_id == original_id for e in self.entries.values()):
            raise LedgerError(
                f"entry {original.entry_number} has already been reversed (FR-GL-003)"
            )

        def totals(rows: Sequence[PostedLine]) -> _Totals:
            out: _Totals = {}
            for row in rows:
                key = (row.account_id, row.subledger_party_id)
                debit, credit = out.get(key, (ZERO, ZERO))
                out[key] = (debit + row.debit, credit + row.credit)
            return out

        source = totals(original.lines)
        mirror = totals(lines)
        if set(source) != set(mirror):
            raise LedgerError(
                "the reversal does not touch the same accounts as the entry it reverses (FR-GL-003)"
            )
        for key, (debit, credit) in source.items():
            if mirror[key] != (credit, debit):
                raise LedgerError(
                    f"the reversal does not mirror the entry it reverses at "
                    f"account {key[0]}: original D{debit}/C{credit}, reversal "
                    f"D{mirror[key][0]}/C{mirror[key][1]} (FR-GL-003)"
                )

    async def reverse(
        self,
        *,
        entry_id: uuid.UUID,
        period_id: uuid.UUID,
        entry_date: date,
        description: str,
        posted_by_user_id: uuid.UUID | None,
        source_system: str,
        idempotency_key: str | None = None,
    ) -> PostedEntry:
        original = self.entries.get(entry_id)
        if original is None:
            raise LedgerError(f"entry {entry_id} does not exist")

        # Built from the original's own lines, exactly as ledger.reverse_entry
        # does: a caller that cannot supply the mirror cannot get it wrong.
        mirrored = [
            LineInput(
                account_id=line.account_id,
                debit=line.credit,
                credit=line.debit,
                subledger_party_id=line.subledger_party_id,
                cost_centre_id=line.cost_centre_id,
                description=line.description,
            )
            for line in original.lines
        ]
        return await self.post(
            EntryInput(
                administration_id=original.administration_id,
                journal_id=original.journal_id,
                period_id=period_id,
                entry_date=entry_date,
                description=description,
                lines=mirrored,
                document_reference=original.document_reference,
                posted_by_user_id=posted_by_user_id,
                source_system=source_system,
                reverses_entry_id=entry_id,
                idempotency_key=idempotency_key,
            )
        )

    # -- reads ------------------------------------------------------------

    async def entry(self, entry_id: uuid.UUID) -> PostedEntry | None:
        return self.entries.get(entry_id)

    async def reversal_of(self, entry_id: uuid.UUID) -> PostedEntry | None:
        for candidate in self.entries.values():
            if candidate.reverses_entry_id == entry_id:
                return candidate
        return None

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
        if any(
            a.administration_id == administration_id and a.code == code
            for a in self.accounts.values()
        ):
            raise LedgerError(f"account code {code} already exists")
        if control_kind is not None and any(
            a.administration_id == administration_id and a.control_kind is control_kind
            for a in self.accounts.values()
        ):
            raise LedgerError(f"administration already has a {control_kind.value} control account")
        account = Account(
            id=uuid.uuid4(),
            administration_id=administration_id,
            code=code,
            name=name,
            account_type=account_type,
            rgs_code=rgs_code,
            default_vat_code=default_vat_code,
            control_kind=control_kind,
        )
        self.accounts[account.id] = account
        return account

    async def set_account_status(self, *, account_id: uuid.UUID, status: AccountStatus) -> Account:
        account = self.accounts.get(account_id)
        if account is None:
            raise LedgerError(f"account {account_id} does not exist")
        updated = replace(account, status=status)
        self.accounts[account_id] = updated
        return updated

    async def create_journal(
        self,
        *,
        administration_id: uuid.UUID,
        code: str,
        name: str,
        journal_type: JournalType,
    ) -> Journal:
        journal = Journal(
            id=uuid.uuid4(),
            administration_id=administration_id,
            code=code,
            name=name,
            journal_type=journal_type,
        )
        self.journals[journal.id] = journal
        return journal

    async def set_journal_status(self, *, journal_id: uuid.UUID, status: str) -> Journal:
        journal = self.journals.get(journal_id)
        if journal is None:
            raise LedgerError(f"journal {journal_id} does not exist")
        updated = replace(journal, status=status)
        self.journals[journal_id] = updated
        return updated

    async def create_party(
        self,
        *,
        administration_id: uuid.UUID,
        party_kind: PartyKind,
        name: str,
        external_reference: str | None = None,
    ) -> Party:
        party = Party(
            id=uuid.uuid4(),
            administration_id=administration_id,
            party_kind=party_kind,
            name=name,
            external_reference=external_reference,
        )
        self.parties[party.id] = party
        return party

    # -- reporting --------------------------------------------------------

    async def numbering_gaps(
        self, *, journal_id: uuid.UUID, fiscal_year_id: uuid.UUID
    ) -> Sequence[int]:
        issued = sorted(
            e.entry_number
            for e in self.entries.values()
            if e.journal_id == journal_id and e.fiscal_year_id == fiscal_year_id
        )
        if not issued:
            return []
        return [n for n in range(1, issued[-1] + 1) if n not in set(issued)]

    async def trial_balance(
        self, *, administration_id: uuid.UUID, fiscal_year_id: uuid.UUID
    ) -> Sequence[TrialBalanceRow]:
        totals: dict[uuid.UUID, tuple[Decimal, Decimal]] = {}
        for entry in self.entries.values():
            if entry.administration_id != administration_id:
                continue
            if entry.fiscal_year_id != fiscal_year_id:
                continue
            for line in entry.lines:
                debit, credit = totals.get(line.account_id, (ZERO, ZERO))
                totals[line.account_id] = (debit + line.debit, credit + line.credit)

        rows = []
        for account in self.accounts.values():
            if account.administration_id != administration_id:
                continue
            debit, credit = totals.get(account.id, (ZERO, ZERO))
            rows.append(
                TrialBalanceRow(
                    account_id=account.id,
                    account_code=account.code,
                    account_name=account.name,
                    account_type=account.account_type,
                    total_debit=debit,
                    total_credit=credit,
                    balance=debit - credit,
                )
            )
        return sorted(rows, key=lambda r: r.account_code)

    async def subledger_balance(
        self, *, administration_id: uuid.UUID, control_kind: ControlKind
    ) -> Sequence[SubledgerRow]:
        totals: dict[uuid.UUID, tuple[Decimal, Decimal]] = {}
        for entry in self.entries.values():
            if entry.administration_id != administration_id:
                continue
            for line in entry.lines:
                if line.subledger_party_id is None:
                    continue
                if self.accounts[line.account_id].control_kind is not control_kind:
                    continue
                debit, credit = totals.get(line.subledger_party_id, (ZERO, ZERO))
                totals[line.subledger_party_id] = (debit + line.debit, credit + line.credit)

        return sorted(
            (
                SubledgerRow(
                    party_id=party_id,
                    party_name=self.parties[party_id].name,
                    total_debit=debit,
                    total_credit=credit,
                    balance=debit - credit,
                )
                for party_id, (debit, credit) in totals.items()
            ),
            key=lambda r: r.party_name,
        )

    async def reconciliation(self, *, administration_id: uuid.UUID) -> Sequence[ReconciliationRow]:
        rows = []
        for account in self.accounts.values():
            if account.administration_id != administration_id:
                continue
            if account.control_kind is None:
                continue
            attributed = ZERO
            unattributed = ZERO
            for entry in self.entries.values():
                for line in entry.lines:
                    if line.account_id != account.id:
                        continue
                    signed = line.debit - line.credit
                    if line.subledger_party_id is None:
                        unattributed += signed
                    else:
                        attributed += signed
            rows.append(
                ReconciliationRow(
                    control_kind=account.control_kind,
                    control_account_code=account.code,
                    control_balance=attributed + unattributed,
                    subledger_balance=attributed,
                    difference=unattributed,
                )
            )
        return rows
