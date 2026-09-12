"""An in-memory IntegrityRepository that REIMPLEMENTS migration 0023's checks
over InMemoryLedgerRepository's state.

Same bargain tests/support/fake_ledger_repository.py makes with 0020, for the
same reason. A fake that returned canned findings would let
tests/ledger/test_integrity.py prove that the job calls the repository and
nothing about whether the checks find anything - and the checks are the
requirement. So this one actually computes them, over the fake ledger's own
rows, with the same deviation keys and the same ordering as the SQL.

The risk that creates is drift, and the answer is the one 0020's fake uses:
tests/ledger/integrity_cases.py is a single table of corruptions, and
tests/integration/test_ledger_integrity.py runs the SAME table against a real
Postgres. A deviation this file reports and 0023 does not - or the reverse -
fails there.

One check has no in-memory counterpart and says so rather than pretending:
`orphan_line`. The fake stores lines INSIDE their PostedEntry, so a line whose
entry does not exist is not a state this data structure can hold. The case
table marks that case database-only and tests/ledger/test_integrity.py asserts
the marking is honest, so the gap is visible in the suite rather than being an
absence nobody notices.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from api.ledger.integrity import Deviation, IntegrityCheck, IntegrityScope
from api.ledger.model import ZERO, Account, Party, PostedEntry, PostedLine
from tests.support.fake_ledger_repository import InMemoryLedgerRepository


def money(value: Decimal) -> str:
    """ledger.money_text() in Python: two decimals, a leading zero, a sign
    where the difference runs the other way, and never a float.
    """
    return f"{value:.2f}"


def _in_scope(administration_id: uuid.UUID, requested: uuid.UUID | None) -> bool:
    return requested is None or administration_id == requested


class InMemoryIntegrityRepository:
    """Reads the fake ledger's tables the way 0023 reads the real ones."""

    def __init__(self, ledger: InMemoryLedgerRepository) -> None:
        self._ledger = ledger

    # -- scope ------------------------------------------------------------

    async def scope(self, *, administration_id: uuid.UUID | None = None) -> IntegrityScope:
        ledger = self._ledger
        administrations = {
            *(a.administration_id for a in ledger.accounts.values()),
            *(j.administration_id for j in ledger.journals.values()),
            *(e.administration_id for e in ledger.entries.values()),
        }
        if administration_id is not None:
            administrations &= {administration_id}

        entries = self._entries(administration_id)
        return IntegrityScope(
            administrations=len(administrations),
            journals=sum(
                1
                for j in ledger.journals.values()
                if _in_scope(j.administration_id, administration_id)
            ),
            accounts=sum(
                1
                for a in ledger.accounts.values()
                if _in_scope(a.administration_id, administration_id)
            ),
            entries=len(entries),
            lines=sum(len(e.lines) for e in entries),
        )

    # -- findings ---------------------------------------------------------

    async def findings(self, *, administration_id: uuid.UUID | None = None) -> Sequence[Deviation]:
        found = [
            *self._balance(administration_id),
            *self._control_accounts(administration_id),
            *self._numbering(administration_id),
        ]
        # ledger.integrity_findings() orders by check, administration,
        # deviation, subject. Matched here so that two runs over unchanged
        # books produce identical reports through either implementation.
        return sorted(
            found,
            key=lambda d: (
                d.check.value,
                str(d.administration_id),
                d.deviation,
                str(d.subject_id),
            ),
        )

    # -- FR-GL-001 --------------------------------------------------------

    def _balance(self, administration_id: uuid.UUID | None) -> list[Deviation]:
        found: list[Deviation] = []
        per_year: dict[tuple[uuid.UUID, uuid.UUID], list[PostedEntry]] = {}

        for entry in self._entries(administration_id):
            per_year.setdefault((entry.administration_id, entry.fiscal_year_id), []).append(entry)

            debit = sum((line.debit for line in entry.lines), ZERO)
            credit = sum((line.credit for line in entry.lines), ZERO)
            count = len(entry.lines)
            if count >= 2 and debit == credit:
                continue

            code = self._journal_code(entry.journal_id)
            if count == 0:
                deviation = "entry_without_lines"
                summary = f"entry {entry.entry_number} in journal {code} has no posting lines"
            elif count == 1:
                deviation = "entry_with_one_line"
                summary = (
                    f"entry {entry.entry_number} in journal {code} has a single line; "
                    "double-entry requires at least two"
                )
            else:
                deviation = "entry_unbalanced"
                summary = (
                    f"entry {entry.entry_number} in journal {code} is unbalanced: "
                    f"debits {money(debit)} <> credits {money(credit)}"
                )

            found.append(
                Deviation(
                    check=IntegrityCheck.BALANCE,
                    requirement="FR-GL-001",
                    deviation=deviation,
                    organization_id=entry.organization_id,
                    administration_id=entry.administration_id,
                    subject_type="journal_entry",
                    subject_id=entry.id,
                    summary=summary,
                    detail={
                        "journal_id": str(entry.journal_id),
                        "journal_code": code,
                        "entry_number": entry.entry_number,
                        "fiscal_year_id": str(entry.fiscal_year_id),
                        "line_count": count,
                        "total_debit": money(debit),
                        "total_credit": money(credit),
                        "difference": money(debit - credit),
                    },
                )
            )

        for (admin, year), entries in per_year.items():
            debit = sum((line.debit for e in entries for line in e.lines), ZERO)
            credit = sum((line.credit for e in entries for line in e.lines), ZERO)
            if debit == credit:
                continue
            found.append(
                Deviation(
                    check=IntegrityCheck.BALANCE,
                    requirement="FR-GL-001",
                    deviation="administration_unbalanced",
                    organization_id=entries[0].organization_id,
                    administration_id=admin,
                    subject_type="fiscal_year",
                    subject_id=year,
                    summary=(
                        f"the books do not balance for fiscal year {year}: debits "
                        f"{money(debit)} <> credits {money(credit)} across "
                        f"{len(entries)} entries"
                    ),
                    detail={
                        "fiscal_year_id": str(year),
                        "entries": len(entries),
                        "total_debit": money(debit),
                        "total_credit": money(credit),
                        "difference": money(debit - credit),
                    },
                )
            )
        return found

    # -- FR-GL-006 --------------------------------------------------------

    def _control_accounts(self, administration_id: uuid.UUID | None) -> list[Deviation]:
        found: list[Deviation] = []
        #: control account id -> [control total, sub-ledger total]. The second
        #: only counts lines that reach a real party in the same administration,
        #: which is what makes the two sides independent enough to disagree.
        totals: dict[uuid.UUID, list[Decimal]] = {
            account.id: [ZERO, ZERO]
            for account in self._ledger.accounts.values()
            if account.control_kind is not None
            and _in_scope(account.administration_id, administration_id)
        }

        for entry in self._entries(administration_id):
            for line in entry.lines:
                account = self._ledger.accounts.get(line.account_id)
                if account is None:
                    continue
                party = (
                    self._ledger.parties.get(line.subledger_party_id)
                    if line.subledger_party_id is not None
                    else None
                )

                if account.control_kind is not None and account.id in totals:
                    signed = line.debit - line.credit
                    totals[account.id][0] += signed
                    if party is not None and party.administration_id == account.administration_id:
                        totals[account.id][1] += signed

                deviation = self._line_deviation(entry, line, account, party)
                if deviation is not None:
                    found.append(deviation)

        for account_id, (control, subledger) in totals.items():
            if control == subledger:
                continue
            account = self._ledger.accounts[account_id]
            assert account.control_kind is not None
            found.append(
                Deviation(
                    check=IntegrityCheck.CONTROL_ACCOUNT,
                    requirement="FR-GL-006",
                    deviation="control_subledger_difference",
                    organization_id=self._ledger.organization_id,
                    administration_id=account.administration_id,
                    subject_type="ledger_account",
                    subject_id=account.id,
                    summary=(
                        f"control account {account.code} carries {money(control)} but "
                        f"its sub-ledger accounts for {money(subledger)}; "
                        f"{money(control - subledger)} is unattributed"
                    ),
                    detail={
                        "account_code": account.code,
                        "control_kind": account.control_kind.value,
                        "control_balance": money(control),
                        "subledger_balance": money(subledger),
                        "difference": money(control - subledger),
                    },
                )
            )
        return found

    def _line_deviation(
        self,
        entry: PostedEntry,
        line: PostedLine,
        account: Account,
        party: Party | None,
    ) -> Deviation | None:
        """FR-GL-006's biconditional, and the two ways a named party can still
        be wrong. At most one finding per line: a line on the wrong account
        with the wrong party is one mistake, not two.
        """
        # Local names, so the branches below read as the requirement does.
        control_kind = account.control_kind
        code = account.code
        amount = money(max(line.debit, line.credit))

        def build(deviation: str, summary: str, detail: dict[str, object]) -> Deviation:
            return Deviation(
                check=IntegrityCheck.CONTROL_ACCOUNT,
                requirement="FR-GL-006",
                deviation=deviation,
                organization_id=entry.organization_id,
                administration_id=entry.administration_id,
                subject_type="journal_line",
                subject_id=line.id,
                summary=summary,
                detail={"journal_entry_id": str(entry.id), **detail},
            )

        if control_kind is not None and line.subledger_party_id is None:
            return build(
                "control_line_without_party",
                f"line {line.line_number} posts {amount} to the {control_kind.value} "
                f"control account {code} directly, naming no sub-ledger party",
                {
                    "account_code": code,
                    "control_kind": control_kind.value,
                    "debit": money(line.debit),
                    "credit": money(line.credit),
                },
            )

        if control_kind is None and line.subledger_party_id is not None:
            named = party.name if party is not None else str(line.subledger_party_id)
            return build(
                "party_line_off_control",
                f"line {line.line_number} names sub-ledger party {named} on account "
                f"{code}, which is not a control account; the amount is attributed to "
                "a party the control account cannot see",
                {
                    "account_code": code,
                    "subledger_party_id": str(line.subledger_party_id),
                    "party_name": party.name if party is not None else None,
                    "debit": money(line.debit),
                    "credit": money(line.credit),
                },
            )

        if control_kind is None or party is None:
            return None

        if party.administration_id != entry.administration_id:
            return build(
                "party_from_other_administration",
                f"line {line.line_number} attributes {amount} to party {party.name}, "
                f"which belongs to administration {party.administration_id} rather "
                f"than {entry.administration_id}",
                {
                    "account_code": code,
                    "subledger_party_id": str(party.id),
                    "party_administration_id": str(party.administration_id),
                    "line_administration_id": str(entry.administration_id),
                },
            )

        if party.party_kind is not control_kind.party_kind:
            return build(
                "party_kind_mismatch",
                f"line {line.line_number} posts {party.party_kind.value} party "
                f"{party.name} to the {control_kind.value} control account {code}",
                {
                    "account_code": code,
                    "control_kind": control_kind.value,
                    "subledger_party_id": str(party.id),
                    "party_kind": party.party_kind.value,
                    "party_name": party.name,
                },
            )
        return None

    # -- FR-GL-013 --------------------------------------------------------

    def _numbering(self, administration_id: uuid.UUID | None) -> list[Deviation]:
        found: list[Deviation] = []
        series: dict[tuple[uuid.UUID, uuid.UUID], list[int]] = {}
        first: dict[tuple[uuid.UUID, uuid.UUID], PostedEntry] = {}

        for entry in self._entries(administration_id):
            key = (entry.journal_id, entry.fiscal_year_id)
            series.setdefault(key, []).append(entry.entry_number)
            first.setdefault(key, entry)

        for (journal_id, year_id), numbers in series.items():
            entry = first[(journal_id, year_id)]
            code = self._journal_code(journal_id)
            highest = max(numbers)
            count = len(numbers)

            # count == highest holds if and only if the series is exactly
            # 1..highest, because numbers are unique per (journal, year) and
            # CHECKed >= 1. The same cheap test 0023 uses to avoid expanding
            # healthy series.
            if count != highest:
                present = set(numbers)
                missing = [n for n in range(1, highest + 1) if n not in present]
                found.append(
                    Deviation(
                        check=IntegrityCheck.NUMBERING,
                        requirement="FR-GL-013",
                        deviation="numbering_gap",
                        organization_id=entry.organization_id,
                        administration_id=entry.administration_id,
                        subject_type="ledger_journal",
                        subject_id=journal_id,
                        summary=(
                            f"journal {code}, fiscal year {year_id}: {count} entries "
                            f"numbered 1..{highest}, so {highest - count} number(s) "
                            "are missing from the series"
                        ),
                        detail={
                            "journal_code": code,
                            "fiscal_year_id": str(year_id),
                            "entries": count,
                            "lowest_number": min(numbers),
                            "highest_number": highest,
                            "missing_count": highest - count,
                            "missing_sample": missing[:50],
                        },
                    )
                )

            next_number = self._ledger.sequences.get((journal_id, year_id))
            if next_number is None:
                summary = (
                    f"journal {code}, fiscal year {year_id}: {count} entries numbered "
                    f"up to {highest}, but the series has no allocator row - the next "
                    "posting would restart at 1"
                )
            elif next_number > highest + 1:
                summary = (
                    f"journal {code}, fiscal year {year_id}: the allocator has issued "
                    f"up to {next_number - 1} but the highest entry is {highest} - "
                    f"{next_number - 1 - highest} number(s) were issued to entries "
                    "that are no longer there"
                )
            elif next_number < highest + 1:
                summary = (
                    f"journal {code}, fiscal year {year_id}: the allocator will issue "
                    f"{next_number} but the highest entry is already {highest} - the "
                    "next posting would collide with an existing number"
                )
            else:
                continue

            found.append(
                Deviation(
                    check=IntegrityCheck.NUMBERING,
                    requirement="FR-GL-013",
                    deviation=("sequence_missing" if next_number is None else "sequence_drift"),
                    organization_id=entry.organization_id,
                    administration_id=entry.administration_id,
                    subject_type="ledger_journal",
                    subject_id=journal_id,
                    summary=summary,
                    detail={
                        "journal_code": code,
                        "fiscal_year_id": str(year_id),
                        "entries": count,
                        "highest_number": highest,
                        "next_number": next_number,
                        "expected_next": highest + 1,
                    },
                )
            )

        # The allocator that outlived its entries. This is what catches a whole
        # series being removed, which the gap check structurally cannot: with
        # every entry gone there is no series left to find a hole in.
        for (journal_id, year_id), next_number in self._ledger.sequences.items():
            if (journal_id, year_id) in series or next_number <= 1:
                continue
            journal = self._ledger.journals.get(journal_id)
            if journal is None or not _in_scope(journal.administration_id, administration_id):
                continue
            found.append(
                Deviation(
                    check=IntegrityCheck.NUMBERING,
                    requirement="FR-GL-013",
                    deviation="series_missing",
                    organization_id=self._ledger.organization_id,
                    administration_id=journal.administration_id,
                    subject_type="ledger_journal",
                    subject_id=journal_id,
                    summary=(
                        f"journal {journal.code}, fiscal year {year_id}: the allocator "
                        f"has issued {next_number - 1} number(s) but the series holds "
                        "no entries at all"
                    ),
                    detail={
                        "journal_code": journal.code,
                        "fiscal_year_id": str(year_id),
                        "next_number": next_number,
                        "issued": next_number - 1,
                        "entries": 0,
                    },
                )
            )
        return found

    # -- helpers ----------------------------------------------------------

    def _entries(self, administration_id: uuid.UUID | None) -> list[PostedEntry]:
        return [
            entry
            for entry in self._ledger.entries.values()
            if _in_scope(entry.administration_id, administration_id)
        ]

    def _journal_code(self, journal_id: uuid.UUID) -> str:
        journal = self._ledger.journals.get(journal_id)
        return journal.code if journal is not None else str(journal_id)
