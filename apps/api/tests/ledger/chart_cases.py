"""RGS mapping integrity, as data (FR-ONB-005).

    FR-ONB-005  ... user may extend but not break RGS mapping integrity.

"May extend" and "not break" are two claims and both need cases, so the table
below holds accepted mappings as well as refused ones. A rule set that refused
everything would satisfy the second half and destroy the first.

Each case is an account somebody tries to add, plus what must happen. Written
as a table because it is executed twice: against the in-memory chart
(tests/ledger/test_chart.py, every `make test-api`) and against Postgres with
migration 0024 applied (tests/integration/test_chart_of_accounts.py). A rule
one enforces and the other does not fails there rather than diverging quietly
- the same bargain tests/ledger/cases.py makes for the posting invariants.

The `expect` substrings are chosen to appear in BOTH the fake's message and
the trigger's, so a case that passes for the wrong reason - a typo'd RGS code
refused as "does not exist" rather than as the type mismatch under test -
fails instead of passing quietly.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from api.ledger.model import AccountType

#: Codes from the shipped dataset (apps/api/data/rgs/rgs-3.8-mkb.json). Named
#: here so a case reads as an intention and a dataset change that removes one
#: fails in one place.
CASH = "BLimKas"  # postable, asset
BANK = "BLimBan"  # postable, asset
REVENUE = "WOmzNeoBin"  # postable, revenue
OFFICE = "WBedKan"  # postable, expense
LIQUID_HEADING = "BLim"  # a heading: level 2, not postable
UNKNOWN = "BLimNotAThing"  # defined by no version


@dataclass(frozen=True, slots=True)
class MappingCase:
    name: str
    requirement: str
    account_code: str
    account_type: AccountType
    rgs_code: str | None
    #: None means the mapping must be ACCEPTED. Otherwise, a substring the
    #: refusal has to contain.
    expect: str | None = None

    @property
    def is_accepted(self) -> bool:
        return self.expect is None


MAPPING_CASES: tuple[MappingCase, ...] = (
    # -- "may extend" ------------------------------------------------------
    MappingCase(
        name="an asset account mapped to an asset element",
        requirement="FR-ONB-005",
        account_code="1150",
        account_type=AccountType.ASSET,
        rgs_code=BANK,
    ),
    MappingCase(
        name="an expense account mapped to an expense element",
        requirement="FR-ONB-005",
        account_code="4150",
        account_type=AccountType.EXPENSE,
        rgs_code=OFFICE,
    ),
    MappingCase(
        # 0020 made rgs_code nullable deliberately: a customer may carry an
        # account with no RGS equivalent. It is reported as unmapped by
        # ledger.rgs_readiness() rather than refused - refusing would make
        # FR-ONB-005's "may extend" false for exactly the accounts that need
        # it most.
        name="an account with no RGS code at all",
        requirement="FR-ONB-005",
        account_code="4160",
        account_type=AccountType.EXPENSE,
        rgs_code=None,
    ),
    # -- "not break RGS mapping integrity" ---------------------------------
    MappingCase(
        # The one that matters most in practice. It balances, it reports, and
        # it files wrongly - nothing downstream would notice.
        name="an expense account mapped to a revenue element",
        requirement="FR-ONB-005",
        account_code="4170",
        account_type=AccountType.EXPENSE,
        rgs_code=REVENUE,
        expect="remapped across types",
    ),
    MappingCase(
        name="a revenue account mapped to an asset element",
        requirement="FR-ONB-005",
        account_code="8300",
        account_type=AccountType.REVENUE,
        rgs_code=CASH,
        expect="remapped across types",
    ),
    MappingCase(
        # A heading holds a subtree. An account mapped to one puts an amount
        # where the taxonomy expects the sum of its children, which double
        # counts it in every report built from the hierarchy.
        name="an account mapped to a heading rather than a leaf",
        requirement="FR-ONB-005",
        account_code="1160",
        account_type=AccountType.ASSET,
        rgs_code=LIQUID_HEADING,
        expect="heading",
    ),
    MappingCase(
        name="an account mapped to a code the version does not define",
        requirement="FR-ONB-005",
        account_code="1170",
        account_type=AccountType.ASSET,
        rgs_code=UNKNOWN,
        expect="does not exist",
    ),
)


#: What each legal form's seeded chart must contain, beyond the common core.
#: FR-ONB-004 says the legal form "drives the default chart of accounts", so
#: the forms have to actually DIFFER - a profile table that returned the same
#: chart for all five would satisfy every other test in the suite.
#:
#: Account codes, not names, because the name is display text and the code is
#: what a bookkeeper types.
DISTINCTIVE_ACCOUNTS: dict[str, tuple[str, ...]] = {
    #: Capital and drawings; no payroll by default.
    "eenmanszaak": ("0500", "0550", "0560"),
    #: Capital and drawings per partner, and payroll.
    "vof": ("0500", "0510", "0550", "0560", "4600"),
    #: Share capital, reserves, the director's current account, corporate
    #: income tax - none of which an eenmanszaak has.
    "bv": ("0500", "0510", "0520", "0530", "1500", "1760", "4950"),
    #: Designated reserves, grants and donations rather than trading revenue
    #: alone.
    "stichting": ("0500", "0520", "8200", "8210"),
    #: As stichting, plus membership fees.
    "vereniging": ("0500", "0520", "8200", "8210", "8220"),
}

#: Accounts every profile must seed regardless of form. The two control
#: accounts are the load-bearing ones: without them FR-GL-006's sub-ledgers
#: have nothing to reconcile to and the first sales invoice has nowhere to go.
COMMON_ACCOUNTS: tuple[str, ...] = ("1000", "1100", "1300", "1600", "1700", "8000")

#: Accounts no OTHER form may have, so that "the legal form drives the chart"
#: is asserted in both directions.
EXCLUSIVE_ACCOUNTS: dict[str, tuple[str, ...]] = {
    "bv": ("1500", "1760", "4950"),
    "vereniging": ("8220",),
}


def successor(
    document: dict[str, Any],
    *,
    version: str = "4.0-test",
    changes: dict[str, tuple[str | None, str]] | None = None,
) -> dict[str, Any]:
    """Build the next RGS release from this one.

    `changes` maps a code to (new code or None, change_kind); everything not
    named is carried over unchanged. That is what a real successor file
    contains - CMP-003's upgrade path is these mapping rows and nothing else,
    which is the property the upgrade tests are actually checking.

    Lives here rather than in a test module because both the in-memory suite
    and the DB-backed one build their successor the same way; a second
    implementation would let the two disagree about what a release looks like.
    """
    changes = changes or {}
    result = copy.deepcopy(document)
    result["rgs_version"] = version
    result["supersedes"] = document["rgs_version"]

    renames = {old: new for old, (new, kind) in changes.items() if kind == "renamed" and new}
    withdrawn = {old for old, (_, kind) in changes.items() if kind == "withdrawn"}

    elements = []
    for element in result["elements"]:
        if element["code"] in withdrawn:
            continue
        if element["code"] in renames:
            element = {**element, "code": renames[element["code"]]}
        elements.append(element)
    result["elements"] = elements

    # The profile has to follow the elements. A renamed code is renamed here
    # too; a WITHDRAWN one takes its profile accounts with it, because
    # rgs_profile_account has a composite foreign key into rgs_element and a
    # profile naming a code the version no longer defines cannot be loaded at
    # all. The in-memory chart does not enforce that key, so this is a rule the
    # database taught the test rather than the other way round.
    result["common"] = [
        account for account in (result.get("common") or []) if account["rgs_code"] not in withdrawn
    ]
    for profile in result["profiles"]:
        profile["accounts"] = [
            account
            for account in (profile.get("accounts") or [])
            if account["rgs_code"] not in withdrawn
        ]

    for group in (
        result.get("common") or [],
        *[profile.get("accounts") or [] for profile in result["profiles"]],
    ):
        for account in group:
            if account["rgs_code"] in renames:
                account["rgs_code"] = renames[account["rgs_code"]]

    result["mappings"] = [
        {
            "from_code": code,
            "to_code": None if kind == "withdrawn" else new,
            "change_kind": kind,
        }
        for code, (new, kind) in changes.items()
    ] + [
        {
            "from_code": element["code"],
            "to_code": element["code"],
            "change_kind": "unchanged",
        }
        for element in document["elements"]
        if element["code"] not in changes
    ]
    return result
