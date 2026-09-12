"""The shipped RGS dataset itself (CMP-003, FR-ONB-005).

The dataset is data, which means it is a place where a mistake ships without a
compiler or a type checker noticing. So it gets tests: the same validator
scripts/load_rgs_version.py runs before loading anything, plus the properties
that make it usable as the seed FR-ONB-005 asks for.

Also pinned here: that it is still marked PROVISIONAL. The file is a starter
subset in RGS's shape, not the official publication, and the day someone
replaces it with the real thing they should have to change this test - which
is the moment to check that the provenance fields, the checksum, and
ledger.rgs_readiness()'s reporting all say the right thing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.ledger.chart_cases import COMMON_ACCOUNTS, DISTINCTIVE_ACCOUNTS
from tests.support.fake_chart_repository import SHIPPED_DATASET, load_document

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _loader() -> ModuleType:
    """Loads scripts/load_rgs_version.py by path - scripts/ is not a package,
    the same way tests/authz/test_role_catalogue_generation.py loads its
    generator.
    """
    spec = importlib.util.spec_from_file_location(
        "load_rgs_version", SCRIPTS / "load_rgs_version.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return load_document()


def test_the_shipped_dataset_is_valid(document: dict[str, Any]) -> None:
    """The validator the loader runs, against the file that ships.

    It checks parents resolve, every profile account names a postable element,
    VAT defaults are FR-AR-002 treatments, and each of FR-ONB-004's five legal
    forms has a profile with both control accounts.
    """
    problems = _loader().validate(document)

    assert not problems, "\n".join(problems)


def test_the_shipped_dataset_is_still_marked_provisional(
    document: dict[str, Any],
) -> None:
    """A guard, not a preference.

    Flipping `source` to 'official-publication' removes the --allow-provisional
    gate and stops ledger.rgs_readiness() reporting these administrations as
    unready to file. That is exactly right once the official dataset is loaded
    and exactly wrong before, so the change should be deliberate enough to
    break a test.
    """
    assert document["source"] == "provisional-subset"
    assert "provisional" in document["rgs_version"]
    assert any("provisional" in line.lower() for line in document["_readme"])


def test_the_dataset_names_its_own_limits(document: dict[str, Any]) -> None:
    """CMP-002 and CMP-004 are where a provisional dataset would do real harm:
    both carry RGS codes to a reader outside this system. The file has to say
    so, because the person who finds it will not have read the ADR.
    """
    readme = " ".join(document["_readme"])

    assert "CMP-002" in readme
    assert "CMP-004" in readme
    assert "referentiegrootboekschema.nl" in readme


def test_every_legal_form_seeds_the_accounts_its_form_needs(
    document: dict[str, Any],
) -> None:
    """FR-ONB-004 and FR-ONB-005 together: the profile matching the legal form
    must actually contain what that form's books need.

    Read straight out of the document rather than through the repository, so a
    failure names the file rather than the loader.
    """
    common = {account["account_code"] for account in document["common"]}

    for profile in document["profiles"]:
        form = profile["legal_form"]
        codes = common | {a["account_code"] for a in profile["accounts"]}

        assert set(COMMON_ACCOUNTS) <= codes, f"{form} is missing common accounts"
        assert set(DISTINCTIVE_ACCOUNTS[form]) <= codes, (
            f"{form} is missing {set(DISTINCTIVE_ACCOUNTS[form]) - codes}"
        )


def test_account_codes_follow_dutch_practice(document: dict[str, Any]) -> None:
    """0 fixed assets and equity, 1 current assets and liabilities, 2 suspense,
    4 operating expenses, 7 cost of sales, 8 revenue.

    A convention rather than a rule, and worth pinning: a bookkeeper reading a
    chart where revenue sits in the 4000s has to look up every account, and
    the whole point of the seed is that they do not.
    """
    expected_prefix = {
        "asset": ("0", "1", "2"),
        "liability": ("0", "1"),
        "equity": ("0",),
        "expense": ("4", "7"),
        "revenue": ("8",),
    }
    elements = {e["code"]: e for e in document["elements"]}

    for profile in document["profiles"]:
        for account in [*document["common"], *profile["accounts"]]:
            account_type = elements[account["rgs_code"]]["account_type"]
            code = account["account_code"]
            assert code.startswith(expected_prefix[account_type]), (
                f"{profile['legal_form']}/{code} is {account_type} but its code "
                f"is outside {expected_prefix[account_type]}"
            )


def test_the_checksum_is_over_the_file_bytes() -> None:
    """The database refuses a second load of the same version name from a
    different document, and this is the value it compares. Hashing the bytes
    rather than the parsed document means a whitespace-only edit is still a
    different document - the conservative direction, because it asks a human
    whether the change was intended.
    """
    loader = _loader()

    digest = loader.checksum(SHIPPED_DATASET)

    assert len(digest) == 64
    assert digest == loader.checksum(SHIPPED_DATASET), "the checksum is not stable"


def test_the_validator_actually_rejects_things(document: dict[str, Any]) -> None:
    """A validator that returned [] unconditionally would make every test
    above pass. Each mutation below is a mistake somebody could plausibly make
    while editing the file by hand.
    """
    loader = _loader()

    def mutated(**changes: Any) -> dict[str, Any]:
        return {**document, **changes}

    # A profile account naming a heading rather than a leaf.
    broken = mutated(
        common=[{**document["common"][0], "rgs_code": "BLim"}, *document["common"][1:]]
    )
    assert any("heading" in problem for problem in loader.validate(broken))

    # An element whose parent does not exist.
    broken = mutated(
        elements=[
            *document["elements"],
            {
                "code": "BOrphan",
                "description_nl": "x",
                "level": 3,
                "parent_code": "BNope",
                "postable": True,
                "account_type": "asset",
            },
        ]
    )
    assert any("parent" in problem for problem in loader.validate(broken))

    # A legal form with no profile - an administration that cannot onboard.
    broken = mutated(profiles=document["profiles"][:2])
    assert any("no profile for legal form" in problem for problem in loader.validate(broken))

    # A VAT default outside FR-AR-002's treatments.
    broken = mutated(
        common=[
            {**document["common"][0], "default_vat_code": "21%"},
            *document["common"][1:],
        ]
    )
    assert any("FR-AR-002" in problem for problem in loader.validate(broken))

    # A profile with no receivables control account (FR-GL-006).
    without_control = [
        account for account in document["common"] if account.get("control_kind") is None
    ]
    broken = mutated(common=without_control)
    problems = loader.validate(broken)
    assert any("accounts_receivable control account" in p for p in problems)
    assert any("accounts_payable control account" in p for p in problems)
