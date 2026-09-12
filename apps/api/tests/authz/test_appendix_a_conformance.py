"""Catches drift between the PRD and the code.

api.authz.matrix is the source of truth for what every standard role can
do, but the PRD is the source of truth for api.authz.matrix. These tests
parse prd.md itself - Appendix A's capability matrix and §8.4's standard
roles table - and assert the module matches the document exactly. Editing
one without the other fails CI, naming the cell or the role.

Deliberately parses the markdown rather than comparing against a second
hand-transcribed copy: a transcription is one more thing that can drift, and
the point of these tests is to have exactly two artifacts (the PRD and the
module) with a mechanical check between them.

No database, so these run on every CI invocation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.authz.conditions import CONDITION_KEYS
from api.authz.matrix import (
    ALL_CAPABILITIES,
    APPENDIX_A_COLUMNS,
    CAPABILITIES,
    CONDITIONAL_CELLS,
    MATRIX,
    ROLES,
    SECTION_8_4_NAMES,
    Access,
    permission_catalogue,
    permissions_for_role,
)


def _prd() -> str:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "prd.md"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    raise AssertionError("prd.md not found in any parent directory")


def _cells(line: str) -> list[str]:
    """Splits one markdown table row into its cells, stripping the bold
    markers §8.4 uses on role names.
    """
    return [cell.strip().replace("**", "") for cell in line.strip().strip("|").split("|")]


def _table_after(heading: str) -> list[list[str]]:
    """The first markdown table following `heading`, as rows of cells, with
    the header and the |---|---| separator dropped.
    """
    text = _prd()
    start = text.index(heading)
    rows: list[list[str]] = []
    for line in text[start:].splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if rows:
                break  # the table ended
            continue
        if set(stripped) <= set("|- "):
            continue  # the |---|---| separator
        rows.append(_cells(stripped))
    return rows


def _appendix_a() -> tuple[list[str], dict[str, list[str]]]:
    rows = _table_after("## Appendix A")
    header = rows[0][1:]
    body = {row[0]: row[1:] for row in rows[1:]}
    return header, body


# --- Appendix A -------------------------------------------------------------


def test_the_matrix_has_the_columns_appendix_a_has_in_the_same_order() -> None:
    header, _ = _appendix_a()
    assert list(APPENDIX_A_COLUMNS) == header


def test_the_matrix_has_exactly_appendix_as_capability_rows_in_order() -> None:
    _, body = _appendix_a()

    assert [c.label for c in CAPABILITIES] == list(body), (
        "CAPABILITIES must list every Appendix A row, in the PRD's order, with identical labels"
    )
    assert list(MATRIX) == list(body)


def test_every_matrix_cell_matches_the_prd() -> None:
    """The test the whole module exists for. One assertion per cell, so a
    failure names the capability and the role rather than dumping two grids.
    """
    header, body = _appendix_a()

    mismatches: list[str] = []
    for label, prd_row in body.items():
        for column, prd_value in zip(header, prd_row, strict=True):
            index = APPENDIX_A_COLUMNS.index(column)
            code_value = MATRIX[label][index].value
            if code_value != prd_value:
                mismatches.append(
                    f"  {label!r} x {column!r}: prd.md says {prd_value!r}, "
                    f"api.authz.matrix says {code_value!r}"
                )

    assert not mismatches, (
        "api.authz.matrix.MATRIX has drifted from prd.md's Appendix A:\n" + "\n".join(mismatches)
    )


def test_every_conditional_cell_is_documented_and_no_note_is_orphaned() -> None:
    """Appendix A's "Notes on the conditionals" are data too
    (CONDITIONAL_CELLS). A C cell with no note, or a note for a cell that is
    no longer C, is drift in the same way a wrong cell is.
    """
    conditional_in_matrix = {
        (label, APPENDIX_A_COLUMNS[i])
        for label, row in MATRIX.items()
        for i, access in enumerate(row)
        if access is Access.CONDITIONAL
    }

    assert conditional_in_matrix == set(CONDITIONAL_CELLS), (
        "CONDITIONAL_CELLS must have exactly one entry per C cell.\n"
        f"  undocumented C cells: {sorted(conditional_in_matrix - set(CONDITIONAL_CELLS))}\n"
        f"  notes with no C cell:  {sorted(set(CONDITIONAL_CELLS) - conditional_in_matrix)}"
    )


def test_every_condition_key_named_in_a_note_actually_exists() -> None:
    """A note naming a condition this system does not implement would read
    as an enforced limit while enforcing nothing.
    """
    for cell, note in CONDITIONAL_CELLS.items():
        unknown = set(note.condition_keys) - CONDITION_KEYS
        assert not unknown, f"{cell} names condition keys that do not exist: {sorted(unknown)}"


def test_appendix_as_conditional_notes_are_reproduced_faithfully() -> None:
    """The four bullets under "Notes on the conditionals" are the PRD's own
    words about why those cells are C. Checks the substance of each appears
    in at least one note, so rewording the PRD's rationale is noticed.
    """
    text = _prd()
    notes_block = text[text.index("Notes on the conditionals") :].split("---")[0]
    prd_bullets = [line for line in notes_block.splitlines() if line.strip().startswith("- **")]
    assert len(prd_bullets) == 5, (
        f"expected 5 conditional notes in Appendix A, found {len(prd_bullets)}"
    )

    recorded = " ".join(note.note for note in CONDITIONAL_CELLS.values()).lower()
    for fragment in (
        "permitted journals",
        "amount ceiling",
        "cost centre",
        "not the same person who approved it",
        "disabled per organization",
    ):
        assert fragment in recorded, (
            f"Appendix A's conditional notes mention {fragment!r} but no "
            "CONDITIONAL_CELLS entry does"
        )


# --- §8.4 -------------------------------------------------------------------


def test_every_standard_role_in_section_8_4_is_implemented() -> None:
    rows = _table_after("### 8.4 Standard roles")[1:]
    prd_names = [SECTION_8_4_NAMES.get(row[0], row[0]) for row in rows]

    assert [role.name for role in ROLES] == prd_names, (
        f"ROLES must list every §8.4 role, in the PRD's order. §8.4 has {len(prd_names)} roles."
    )


def test_every_roles_scope_matches_section_8_4() -> None:
    """§8.4's Scope column spells out qualifiers this model does not need as
    separate scope kinds - "Firm organization" is still an organization,
    "Per administration, optional cost centre" is still an administration
    (the cost centre is an IAM-033 condition, not a scope). The reduction is
    one line, here, rather than a judgment made silently in matrix.py.
    """
    rows = _table_after("### 8.4 Standard roles")[1:]
    by_name = {role.name: role for role in ROLES}

    for row in rows:
        name = SECTION_8_4_NAMES.get(row[0], row[0])
        scope_text = row[1]
        expected = (
            "administration" if scope_text.startswith("Per administration") else "organization"
        )
        assert by_name[name].scope_type == expected, (
            f"{name}: §8.4 says {scope_text!r} -> {expected}, "
            f"matrix.py says {by_name[name].scope_type}"
        )


def test_the_two_roles_without_a_matrix_column_are_the_expected_ones() -> None:
    """Firm Manager and Service Account appear in §8.4 but have no Appendix
    A column, so their bundles come from §8.4's prose instead. Pinned so a
    future PRD revision that adds a column for either is noticed.
    """
    _, body = _appendix_a()
    without = {role.name for role in ROLES if role.matrix_column is None}

    assert without == {"Firm Manager", "Service Account"}
    assert "Firm Manager" not in str(list(body.values()))


# --- internal consistency ---------------------------------------------------


def test_a_read_only_cell_always_grants_something() -> None:
    """An R cell on a capability with no `read` permissions would grant
    nothing at all, silently - the role would appear in the matrix with
    access and hold none.
    """
    for label, row in MATRIX.items():
        cap = next(c for c in CAPABILITIES if c.label == label)
        if Access.READ in row:
            assert cap.read, f"{label!r} has an R cell but declares no read permission"


def test_a_capability_granting_nothing_at_full_is_documented_as_stylistic() -> None:
    """Where F and R grant identical permissions, the capability must say
    why in its note - otherwise it looks like a missing permission.
    """
    for cap in ALL_CAPABILITIES:
        if not cap.full:
            assert cap.note, (
                f"{cap.label!r} grants nothing extra at F; add a note explaining "
                "why F and R are equivalent for this row"
            )


def test_no_administration_scope_role_holds_an_organization_scope_permission() -> None:
    """The invariant role_permission_scope_guard_trg enforces in the
    database (IAM-032). Checked here too, against the derived bundles, so a
    mis-declared capability scope fails without needing Postgres.
    """
    scopes = {permission.key: scope for permission, scope in permission_catalogue()}

    for role in ROLES:
        if role.scope_type != "administration":
            continue
        offending = sorted(
            key for key in permissions_for_role(role) if scopes[key] == "organization"
        )
        assert not offending, (
            f"{role.name} is administration-scoped but holds organization-scope "
            f"permissions {offending}; the database would reject this bundle"
        )


def test_every_permission_in_the_catalogue_is_held_by_at_least_one_role() -> None:
    """A permission no role grants is dead weight in the catalogue - either
    a capability was mis-transcribed, or a role's column was."""
    held: set[tuple[str, str]] = set()
    for role in ROLES:
        held |= permissions_for_role(role)

    orphans = sorted(
        permission.key for permission, _ in permission_catalogue() if permission.key not in held
    )
    assert not orphans, f"permissions no standard role grants: {orphans}"


def test_owner_holds_every_permission_in_the_catalogue() -> None:
    """Appendix A gives Owner F on all 31 rows; §8.4 says "Everything,
    including deleting the organization." If the derivation is right, that
    falls out - it is not asserted anywhere in matrix.py.
    """
    owner = next(role for role in ROLES if role.name == "Owner")
    catalogue = {permission.key for permission, _ in permission_catalogue()}

    assert permissions_for_role(owner) == catalogue


@pytest.mark.parametrize(
    ("role_name", "action", "resource_type"),
    [
        # §8.4 "Explicitly cannot" claims, asserted against the derived
        # bundles rather than trusted.
        ("Organization Admin", "post", "journal_entry"),
        ("Firm Manager", "post", "journal_entry"),
        ("Firm Manager", "reverse", "journal_entry"),
        ("Security Admin", "view", "chart_of_accounts"),
        ("Billing Admin", "view", "report"),
        ("Approver", "post", "journal_entry"),
        ("Approver", "create", "sales_invoice"),
        ("Invoicer", "view", "purchase_invoice"),
        ("Invoicer", "view", "bank_transaction"),
        ("Bookkeeper", "file", "vat_return"),
        ("Bookkeeper", "lock", "period"),
        ("Viewer", "post", "journal_entry"),
        ("Accountant", "manage", "user_role"),
        ("Service Account", "post", "journal_entry"),
        ("Expense Submitter", "view", "report"),
    ],
)
def test_section_8_4_explicitly_cannot(role_name: str, action: str, resource_type: str) -> None:
    role = next(r for r in ROLES if r.name == role_name)
    assert (action, resource_type) not in permissions_for_role(role)
