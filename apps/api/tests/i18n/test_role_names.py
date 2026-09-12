"""Every system role has a name in both languages - FR-LOC-001, FR-LOC-001d.

The role catalogue is generated from PRD Appendix A and §8.4 (api.authz.matrix
-> migration 0010, ADR-012), so the set of role names is DATA rather than
something anyone types twice. This walks that same data and fails if a role's
display name is missing from the message catalogue.

That is what makes `roleLabel`'s fallback in packages/i18n/src/roles.ts safe.
It renders the raw role name when a translation is missing, rather than
throwing and taking the client switcher down over a database row - and a
fallback nobody checks is how a permanently-English label ships. Checked here,
in CI, against the real list.

The reverse direction is checked too: a `roles.*` key with no role behind it
is a role that was renamed or removed, and a stale label is how a screen ends
up naming something that no longer exists.
"""

from __future__ import annotations

import re

from api.authz.matrix import ROLES
from api.i18n.catalogue import MESSAGES
from api.i18n.language import Language


#: Mirrors roleMessageKey in packages/i18n/src/roles.ts. Restated rather than
#: shared, for the reason tests/test_audit_coverage.py restates MUTATING_METHODS:
#: the two exist in different languages and neither can import the other. The
#: keys they produce are compared against the same catalogue, which is what
#: keeps the duplication honest - a divergence makes one of the two sides fail.
def role_message_key(role_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", role_name.strip().lower()).strip("_")
    return f"roles.{slug}"


def test_the_check_has_roles_to_check() -> None:
    """A sweep that finds nothing passes completely and guarantees nothing -
    the same trap tests/test_audit_coverage.py guards against.
    """
    assert len(ROLES) == 12, "PRD §8.4 lists twelve roles"


def test_every_system_role_has_a_name_in_both_languages() -> None:
    missing = [
        f"  {role.name}  ->  {role_message_key(role.name)}"
        for role in ROLES
        if role_message_key(role.name) not in MESSAGES
    ]

    assert not missing, (
        "These §8.4 roles have no display name in the message catalogue, so a "
        "user sees the raw identifier (FR-LOC-001). Add each to "
        "packages/i18n/catalogue/roles.json in BOTH languages:\n" + "\n".join(missing)
    )


def test_no_role_label_outlives_its_role() -> None:
    expected = {role_message_key(role.name) for role in ROLES}
    stale = sorted(key for key in MESSAGES if key.startswith("roles.") and key not in expected)

    assert not stale, (
        "packages/i18n/catalogue/roles.json names roles that PRD §8.4 does not: "
        f"{stale}. A label that outlives its role is how a screen ends up naming "
        "something nobody can be granted."
    )


def test_the_english_label_is_the_role_name_itself() -> None:
    """The English label and the identifier are the same string, and that is
    worth pinning rather than leaving to coincidence.

    Appendix A's column headers ARE the English names, so an English label
    that drifted from the identifier would mean the UI and the permission
    matrix calling one role two things - and §8.4 conformance
    (tests/authz/test_appendix_a_conformance.py) would still pass, because it
    never looks at the catalogue.

    `Viewer` is the one to watch: §8.4 spells it "Viewer / Auditor" and
    matrix.SECTION_8_4_NAMES already records that reconciliation, so the label
    follows the catalogue's shorter name rather than reopening it here.
    """
    for role in ROLES:
        record = MESSAGES[role_message_key(role.name)]
        assert record["en"] == role.name, (
            f"the English label for {role.name!r} is {record['en']!r}. The two are "
            f"the same string by design; renaming the role is a matrix change, not "
            f"a translation."
        )


def test_dutch_role_names_are_written_rather_than_copied() -> None:
    """FR-LOC-001c prohibits machine translation of accounting terms, and the
    cheapest way to violate FR-LOC-001 is to paste the English across.

    A record whose two languages match must declare `identical: true`
    (scripts/check_translations.py enforces that for the whole catalogue).
    Here the point is narrower: exactly one role is legitimately identical.
    `Accountant` is the Dutch word as well as the English one, and it is a
    protected title in the Netherlands - translating it into something else
    would be inventing a term.
    """
    identical = sorted(
        role.name
        for role in ROLES
        if MESSAGES[role_message_key(role.name)].get("identical") is True
    )

    assert identical == ["Accountant"], (
        "Exactly one §8.4 role reads the same in Dutch and English. A new entry "
        f"here means a role was left untranslated: {identical}"
    )


def test_every_role_renders_in_every_language() -> None:
    """The labels are plain strings with no placeholders, so this is a smoke
    pass rather than a subtle one - and it is what would catch a record whose
    two languages disagree about their shape.
    """
    from api.i18n.catalogue import translate

    for role in ROLES:
        for language in Language:
            label = translate(role_message_key(role.name), language)
            assert label.strip(), f"{role.name} renders empty in {language.value}"
