"""PRD Appendix A and §8.4, as data.

This module is the single source of truth for what every standard role can
do. The permission catalogue, the twelve standard roles, and the bundles
tying them together are all derived from the two structures below — there
is no second place where a role's capabilities are decided, and no
conditional anywhere that special-cases a role by name.

Three things are generated or checked from this module, so drift is
mechanical rather than a matter of remembering:

  - migrations/0010_role_catalogue.sql is GENERATED from it by
    scripts/generate_role_catalogue.py. Never edit that file by hand;
    tests/authz/test_role_catalogue_generation.py fails if it is stale.
  - tests/authz/test_appendix_a_conformance.py parses prd.md's Appendix A
    table and asserts MATRIX matches it cell for cell, and parses §8.4 and
    asserts ROLES matches its Scope column. Editing the PRD without
    editing this file (or the reverse) fails CI, naming the cell.
  - tests/integration/test_authorization_isolation.py asserts the live
    database's seeded rows match what this module derives.

--- How an Appendix A cell becomes permissions ---

Appendix A grades each capability per role as F (full), R (read only),
C (conditional), or — (no access). A Capability therefore declares two
permission tuples:

    read   granted at R, C and F
    full   granted additionally at C and F

C and F grant the SAME permissions. They differ in whether the individual
assignment is expected to carry IAM-033 attribute conditions — a
Bookkeeper's permitted journals, an Approver's amount ceiling. Those limits
vary per person, so they belong on the assignment, never in the role; see
CONDITIONAL_CELLS below and ADR-011.

Where a capability has an empty `full`, its F and R cells grant identical
permissions. That is not an oversight: on the three "View X" rows it
applies to, Appendix A's F marks the roles whose core area the data is,
and R marks read-only roles, but both amount to "may view it" and the PRD
names no separate write capability for that resource. Each such row carries
a `note` saying so, so the judgment is visible rather than buried.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass

from api.authz.model import ScopeType


class Access(enum.Enum):
    """An Appendix A cell. Values are the exact characters the PRD table
    uses, so the conformance test compares against the document without a
    translation layer that could itself drift.
    """

    NONE = "—"
    READ = "R"
    CONDITIONAL = "C"
    FULL = "F"


# Single-letter aliases so MATRIX below reads like the PRD's grid.
F, R, C, N = Access.FULL, Access.READ, Access.CONDITIONAL, Access.NONE


@dataclass(frozen=True, slots=True)
class Permission:
    action: str
    resource_type: str
    description: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.action, self.resource_type)


@dataclass(frozen=True, slots=True)
class Capability:
    """One row of Appendix A."""

    label: str
    scope: ScopeType
    read: tuple[Permission, ...] = ()
    full: tuple[Permission, ...] = ()
    note: str | None = None

    def permissions_at(self, access: Access) -> tuple[Permission, ...]:
        if access is Access.NONE:
            return ()
        if access is Access.READ:
            return self.read
        return self.read + self.full


@dataclass(frozen=True, slots=True)
class Role:
    """One row of §8.4."""

    name: str
    scope_type: ScopeType
    # Appendix A's column header for this role, or None for the two §8.4
    # roles the matrix has no column for (see APPENDIX_A_COLUMNS).
    matrix_column: str | None
    # Permissions held outside the Appendix A grid, by capability label.
    extra_capabilities: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Capabilities not in Appendix A
# ---------------------------------------------------------------------------
# Appendix A grades what a role may DO with an administration's contents; it
# has no row for seeing that the administration exists. Every role needs
# that to navigate at all (and /v1/administrations requires it), so it is
# declared here, plainly marked as not from the matrix, rather than smuggled
# into a matrix row where the conformance test would reject it.
VIEW_ADMINISTRATION = Capability(
    label="View an administration",
    scope="administration",
    read=(Permission("view", "administration", "View an administration"),),
    note="Not an Appendix A row - see the comment above.",
)

BASELINE_CAPABILITIES: tuple[Capability, ...] = (VIEW_ADMINISTRATION,)


# ---------------------------------------------------------------------------
# Capabilities IAM-101 names that Appendix A does not grade
# ---------------------------------------------------------------------------
# IAM-101 defines the built-in client access profiles in terms of "upload
# documents" and "customers". Appendix A has no row for either, so they
# cannot be expressed by reading a matrix column - but a "Capture only"
# profile that cannot capture anything is not that profile.
#
# These are declared here rather than added to CAPABILITIES because
# test_appendix_a_conformance.py compares CAPABILITIES to the document
# itself, and inventing rows there would be exactly the drift that test
# exists to catch. Which roles hold them IS an interpretation - stated
# below, per role, rather than implied.
#
# Row-level "own" scoping is NOT modelled anywhere: IAM-101's "view own
# submissions" and §8.4's "Expense Submitter cannot see any data belonging
# to another person" both need a per-record owner column on tables that do
# not exist yet (FR-DOC, FR-EXP). `view document` is currently
# all-or-nothing within an administration. Whoever builds FR-DOC must add
# that scoping; until then an Expense Submitter granted `view document`
# sees more than §8.4 intends.
UPLOAD_DOCUMENT = Capability(
    label="Upload a source document",
    scope="administration",
    full=(Permission("upload", "document", "Upload a source document"),),
    note="Not an Appendix A row; required by IAM-101's 'Capture only' profile.",
)

VIEW_DOCUMENT = Capability(
    label="View source documents",
    scope="administration",
    read=(Permission("view", "document", "View source documents"),),
    note=(
        "Not an Appendix A row; required by IAM-101's 'view own submissions' and by "
        "IAM-105's client rights floor ('read access to their own source documents'). "
        "F and R would be equivalent here - there is no separate write capability, "
        "uploading is its own row above."
    ),
)

MANAGE_CUSTOMER = Capability(
    label="Manage customers",
    scope="administration",
    full=(Permission("manage", "customer", "Manage customers"),),
    note=(
        "Not an Appendix A row; named by IAM-101's 'Invoice and capture' profile and "
        "by §8.4's Invoicer ('create and send sales invoices, manage customers')."
    ),
)

VIEW_VAT_RETURN = Capability(
    label="View filed returns",
    scope="administration",
    read=(Permission("view", "vat_return", "View prepared and filed VAT returns"),),
    note=(
        "Not an Appendix A row - it grades 'Prepare VAT return' and 'File VAT return' but "
        "not reading one back. Required by IAM-105's client rights floor ('read access to "
        "their own source documents and filed returns'): a client must be able to read a "
        "return filed on their behalf, and a statutory filing they cannot read is not a "
        "record they can be said to retain (CMP-001). F and R would be equivalent - "
        "preparing and filing are their own rows."
    ),
)

APPROVE_SALES_INVOICE = Capability(
    label="Approve sales invoices",
    scope="administration",
    full=(Permission("approve", "sales_invoice", "Approve sales invoices"),),
    note=(
        "Not an Appendix A row: SI-16's draft-approve-send workflow. Held by the Owner alone - "
        "the business owner approves what a bookkeeper (or a firm) drafted on their behalf. "
        "Deliberately NOT given to Accountant or Bookkeeper: the point of the workflow is that "
        "the person who drafts cannot also release. Only consulted when an administration has "
        "turned invoice approval on."
    ),
)

VIEW_FIXED_ASSET = Capability(
    label="View fixed assets",
    scope="administration",
    read=(Permission("view", "fixed_asset", "View fixed assets"),),
    note=(
        "Not an Appendix A row: the fixed-asset register (PRD §13) is ungraded there. Split "
        "from 'Manage fixed assets' the way 'View purchase invoices' is split from 'Code "
        "purchase invoices' - so a Viewer can read the register without holding the write."
    ),
)

MANAGE_FIXED_ASSET = Capability(
    label="Manage fixed assets",
    scope="administration",
    full=(Permission("manage", "fixed_asset", "Create, depreciate and dispose fixed assets"),),
    note=(
        "Not an Appendix A row. Full only, like 'Code purchase invoices' - 'View fixed assets' "
        "above is the read row for this resource."
    ),
)

EXTENSION_CAPABILITIES: tuple[Capability, ...] = (
    UPLOAD_DOCUMENT,
    VIEW_DOCUMENT,
    MANAGE_CUSTOMER,
    VIEW_VAT_RETURN,
    APPROVE_SALES_INVOICE,
    VIEW_FIXED_ASSET,
    MANAGE_FIXED_ASSET,
)


# ---------------------------------------------------------------------------
# Appendix A capabilities, in the PRD's row order
# ---------------------------------------------------------------------------
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "Manage organization settings",
        "organization",
        full=(Permission("manage", "organization_settings", "Manage organization settings"),),
    ),
    Capability(
        "Create/delete administrations",
        "organization",
        full=(Permission("manage", "administration", "Create and archive administrations"),),
    ),
    Capability(
        "Invite users, assign roles",
        "organization",
        full=(Permission("manage", "user_role", "Invite users and assign roles"),),
    ),
    Capability(
        "Manage security policy (MFA, sessions, IP)",
        "organization",
        full=(Permission("manage", "security_policy", "Manage MFA, session and IP policy"),),
    ),
    Capability(
        "Run access reviews",
        "organization",
        read=(Permission("view", "access_review", "View access review results"),),
        full=(Permission("run", "access_review", "Run access reviews"),),
    ),
    # Administration-scoped, though three organization-scoped roles hold it:
    # an organization grant reaches the administrations it owns (ADR-011), so
    # one capability covers both. The PRD names no separate organization-level
    # audit log, and inventing one would put a permission in the catalogue
    # that no Appendix A cell grants.
    Capability(
        "Read audit log",
        "administration",
        read=(Permission("read", "audit_log", "Read an administration's audit log"),),
        full=(Permission("export", "audit_log", "Export or stream the audit log"),),
    ),
    Capability(
        "Manage subscription and billing",
        "organization",
        full=(Permission("manage", "billing", "Manage subscription and billing"),),
    ),
    Capability(
        "View chart of accounts",
        "administration",
        read=(Permission("view", "chart_of_accounts", "View the chart of accounts"),),
        full=(Permission("manage", "chart_of_accounts", "Maintain the chart of accounts"),),
    ),
    Capability(
        "Post journal entries",
        "administration",
        full=(Permission("post", "journal_entry", "Post journal entries"),),
    ),
    Capability(
        "Reverse a posting",
        "administration",
        full=(Permission("reverse", "journal_entry", "Reverse a posting"),),
    ),
    Capability(
        "Lock / unlock periods",
        "administration",
        full=(Permission("lock", "period", "Lock and unlock periods"),),
    ),
    Capability(
        "Year-end close",
        "administration",
        full=(Permission("close", "fiscal_year", "Year-end close"),),
    ),
    Capability(
        "Create sales invoices",
        "administration",
        full=(Permission("create", "sales_invoice", "Create sales invoices"),),
    ),
    Capability(
        "Send sales invoices",
        "administration",
        full=(Permission("send", "sales_invoice", "Send sales invoices"),),
    ),
    Capability(
        "View purchase invoices",
        "administration",
        read=(Permission("view", "purchase_invoice", "View purchase invoices"),),
        note=(
            "F and R grant the same permission. Appendix A marks Accountant and "
            "Bookkeeper F because purchase invoices are their working area and Viewer R "
            "because it is read-only, but the PRD names no separate write capability "
            "for viewing - 'Code purchase invoices' is the write row."
        ),
    ),
    Capability(
        "Code purchase invoices",
        "administration",
        full=(Permission("code", "purchase_invoice", "Code purchase invoices"),),
    ),
    Capability(
        "Approve purchase invoices",
        "administration",
        full=(Permission("approve", "purchase_invoice", "Approve purchase invoices"),),
    ),
    Capability(
        "Create payment batch",
        "administration",
        full=(Permission("create", "payment_batch", "Create a payment batch"),),
    ),
    Capability(
        "Release payment to bank",
        "administration",
        full=(Permission("release", "payment_batch", "Release payment to the bank"),),
    ),
    Capability(
        "Connect / revoke bank consent",
        "administration",
        full=(Permission("manage", "bank_consent", "Connect and revoke bank consent"),),
    ),
    Capability(
        "View bank transactions",
        "administration",
        read=(Permission("view", "bank_transaction", "View bank transactions"),),
        note=(
            "F and R grant the same permission; 'Reconcile bank' is the write row "
            "for this resource."
        ),
    ),
    Capability(
        "Reconcile bank",
        "administration",
        full=(Permission("reconcile", "bank_transaction", "Reconcile bank transactions"),),
    ),
    Capability(
        "Submit expenses",
        "administration",
        full=(Permission("submit", "expense", "Submit expenses"),),
    ),
    Capability(
        "Approve expenses",
        "administration",
        full=(Permission("approve", "expense", "Approve expenses"),),
    ),
    Capability(
        "Prepare VAT return",
        "administration",
        full=(Permission("prepare", "vat_return", "Prepare a VAT return"),),
    ),
    Capability(
        "File VAT return",
        "administration",
        full=(Permission("file", "vat_return", "File a VAT return"),),
    ),
    Capability(
        "View reports",
        "administration",
        read=(Permission("view", "report", "View reports"),),
        note=(
            "F and R grant the same permission; 'Export data' is the separate row "
            "for doing more than reading them."
        ),
    ),
    Capability(
        "Export data",
        "administration",
        full=(Permission("export", "report_data", "Export data"),),
    ),
    Capability(
        "Manage API clients",
        "organization",
        read=(Permission("view", "api_client", "View API clients"),),
        full=(Permission("manage", "api_client", "Manage API clients"),),
    ),
    Capability(
        "Grant firm access to an administration",
        "organization",
        full=(Permission("grant", "firm_engagement", "Grant firm access to an administration"),),
    ),
    Capability(
        "Revoke firm access",
        "organization",
        full=(Permission("revoke", "firm_engagement", "Revoke firm access"),),
    ),
)


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------
# Column order is Appendix A's, left to right. The header labels are the
# PRD's own ("Org Admin", not "Organization Admin") so the conformance test
# compares against the document verbatim; ROLES below maps them to the
# canonical role names §8.4 uses.
APPENDIX_A_COLUMNS: tuple[str, ...] = (
    "Owner",
    "Org Admin",
    "Security Admin",
    "Billing Admin",
    "Accountant",
    "Bookkeeper",
    "Approver",
    "Invoicer",
    "Expense Submitter",
    "Viewer",
)

MATRIX: Mapping[str, tuple[Access, ...]] = {
    #                                             Own  OrgA Sec  Bill Acct Book Appr Invc Exp  View
    "Manage organization settings":               (F,   F,   N,   N,   N,   N,   N,   N,   N,   N),
    "Create/delete administrations":              (F,   F,   N,   N,   N,   N,   N,   N,   N,   N),
    "Invite users, assign roles":                 (F,   F,   N,   N,   N,   N,   N,   N,   N,   N),
    "Manage security policy (MFA, sessions, IP)": (F,   N,   F,   N,   N,   N,   N,   N,   N,   N),
    "Run access reviews":                         (F,   R,   F,   N,   N,   N,   N,   N,   N,   N),
    "Read audit log":                             (F,   R,   F,   N,   R,   N,   N,   N,   N,   R),
    "Manage subscription and billing":            (F,   N,   N,   F,   N,   N,   N,   N,   N,   N),
    "View chart of accounts":                     (F,   N,   N,   N,   F,   F,   R,   R,   N,   R),
    "Post journal entries":                       (F,   N,   N,   N,   F,   C,   N,   N,   N,   N),
    "Reverse a posting":                          (F,   N,   N,   N,   F,   C,   N,   N,   N,   N),
    "Lock / unlock periods":                      (F,   N,   N,   N,   F,   N,   N,   N,   N,   N),
    "Year-end close":                             (F,   N,   N,   N,   F,   N,   N,   N,   N,   N),
    "Create sales invoices":                      (F,   N,   N,   N,   F,   F,   N,   F,   N,   N),
    "Send sales invoices":                        (F,   N,   N,   N,   F,   F,   N,   F,   N,   N),
    "View purchase invoices":                     (F,   N,   N,   N,   F,   F,   C,   N,   N,   R),
    "Code purchase invoices":                     (F,   N,   N,   N,   F,   F,   N,   N,   N,   N),
    "Approve purchase invoices":                  (F,   N,   N,   N,   F,   N,   C,   N,   N,   N),
    "Create payment batch":                       (F,   N,   N,   N,   F,   F,   N,   N,   N,   N),
    "Release payment to bank":                    (F,   N,   N,   N,   C,   N,   C,   N,   N,   N),
    "Connect / revoke bank consent":              (F,   N,   N,   N,   F,   C,   N,   N,   N,   N),
    "View bank transactions":                     (F,   N,   N,   N,   F,   F,   N,   N,   N,   R),
    "Reconcile bank":                             (F,   N,   N,   N,   F,   F,   N,   N,   N,   N),
    "Submit expenses":                            (F,   F,   F,   F,   F,   F,   F,   F,   F,   N),
    "Approve expenses":                           (F,   N,   N,   N,   F,   N,   C,   N,   N,   N),
    "Prepare VAT return":                         (F,   N,   N,   N,   F,   F,   N,   N,   N,   N),
    "File VAT return":                            (F,   N,   N,   N,   F,   N,   N,   N,   N,   N),
    "View reports":                               (F,   N,   N,   N,   F,   F,   C,   N,   N,   R),
    "Export data":                                (F,   N,   N,   N,   F,   C,   N,   N,   N,   C),
    "Manage API clients":                         (F,   F,   R,   N,   N,   N,   N,   N,   N,   N),
    "Grant firm access to an administration":     (F,   F,   N,   N,   N,   N,   N,   N,   N,   N),
    "Revoke firm access":                         (F,   F,   N,   N,   N,   N,   N,   N,   N,   N),
}  # fmt: skip


# ---------------------------------------------------------------------------
# Appendix A's "Notes on the conditionals", as data
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ConditionalNote:
    """Why a cell is C, and which IAM-033 condition keys express it.

    An empty `condition_keys` means the narrowing the PRD describes is not
    expressible with the conditions built so far. Recording that as data
    rather than omitting the cell keeps the gap visible: a reader of this
    table can see which conditionals are actually enforced and which are
    only documented.
    """

    note: str
    condition_keys: tuple[str, ...]


CONDITIONAL_CELLS: Mapping[tuple[str, str], ConditionalNote] = {
    ("Post journal entries", "Bookkeeper"): ConditionalNote(
        "Bookkeeper posting is limited to permitted journals and open periods.",
        ("journal_ids", "period_ids"),
    ),
    ("Reverse a posting", "Bookkeeper"): ConditionalNote(
        "Bookkeeper posting is limited to permitted journals and open periods.",
        ("journal_ids", "period_ids"),
    ),
    ("View purchase invoices", "Approver"): ConditionalNote(
        "Approver is limited by amount ceiling and, optionally, cost centre.",
        ("amount_ceiling", "cost_centre_ids"),
    ),
    ("Approve purchase invoices", "Approver"): ConditionalNote(
        "Approver is limited by amount ceiling and, optionally, cost centre.",
        ("amount_ceiling", "cost_centre_ids"),
    ),
    ("Release payment to bank", "Accountant"): ConditionalNote(
        "Payment release is subject to IAM-061 segregation: not the same person who "
        "approved it. Not expressible with the conditions built so far - a "
        "same-actor exclusion needs the approval record, which no condition key "
        "reads. IAM-061 is unbuilt.",
        (),
    ),
    ("Release payment to bank", "Approver"): ConditionalNote(
        "Payment release is subject to IAM-061 segregation: not the same person who "
        "approved it. An Approver's own amount ceiling and cost centre also apply.",
        ("amount_ceiling", "cost_centre_ids"),
    ),
    ("Connect / revoke bank consent", "Bookkeeper"): ConditionalNote(
        "Limited to the administrations and, by extension, the bank connections a "
        "Bookkeeper's assignment covers.",
        (),
    ),
    ("Approve expenses", "Approver"): ConditionalNote(
        "Approver is limited by amount ceiling and, optionally, cost centre.",
        ("amount_ceiling", "cost_centre_ids"),
    ),
    ("View reports", "Approver"): ConditionalNote(
        "Approver is limited by amount ceiling and, optionally, cost centre.",
        ("amount_ceiling", "cost_centre_ids"),
    ),
    ("Export data", "Bookkeeper"): ConditionalNote(
        "Bookkeeper export is limited to the periods and journals their assignment covers.",
        ("journal_ids", "period_ids"),
    ),
    ("Export data", "Viewer"): ConditionalNote(
        "Viewer export is permitted but always logged as a data-access event; it can "
        "be disabled per organization. Neither the logging nor the per-organization "
        "switch is a grant-level condition - both are unbuilt (IAM-038+).",
        (),
    ),
}


# ---------------------------------------------------------------------------
# The twelve standard roles (§8.4)
# ---------------------------------------------------------------------------
# PRD §8.4 lists twelve, two of which have no Appendix A column: Firm Manager
# and Service Account. Their bundles come from §8.4's own "Core capability"
# and "Explicitly cannot" prose, named here as capability labels so they are
# still data rather than a hand-written permission list.
#
# extra_capabilities also carries the EXTENSION_CAPABILITIES above, per role.
# Those assignments are interpretation, not transcription: Appendix A grades
# neither documents nor customers, so each is placed by the role's §8.4
# description ("Full bookkeeping", "manage customers", "Submit own expenses
# and mileage") rather than read off the matrix.
ROLES: tuple[Role, ...] = (
    Role(
        "Owner",
        "organization",
        "Owner",
        extra_capabilities=(
            "Upload a source document",
            "View source documents",
            "Manage customers",
            "View filed returns",
            "Approve sales invoices",
            "View fixed assets",
            "Manage fixed assets",
        ),
    ),
    Role("Organization Admin", "organization", "Org Admin"),
    Role("Security Admin", "organization", "Security Admin"),
    Role("Billing Admin", "organization", "Billing Admin"),
    Role(
        "Firm Manager",
        "organization",
        None,
        extra_capabilities=(
            # §8.4: "Client portfolio management, client onboarding, assigning
            # firm staff to clients, setting client access profiles."
            "Create/delete administrations",
            "Invite users, assign roles",
            "Grant firm access to an administration",
            "Revoke firm access",
            "Submit expenses",
            # "Explicitly cannot: post to a client's ledger without also holding
            # Accountant on it" holds because no ledger capability is listed
            # here, not because of a check elsewhere.
        ),
    ),
    Role(
        "Accountant",
        "administration",
        "Accountant",
        extra_capabilities=(
            "Upload a source document",
            "View source documents",
            "Manage customers",
            "View filed returns",
            "View fixed assets",
            "Manage fixed assets",
        ),
    ),
    Role(
        "Bookkeeper",
        "administration",
        "Bookkeeper",
        extra_capabilities=(
            "Upload a source document",
            "View source documents",
            "Manage customers",
            "View filed returns",
            "View fixed assets",
            "Manage fixed assets",
        ),
    ),
    # Approver reads the documents behind what they approve; they do not
    # capture or maintain customers.
    Role("Approver", "administration", "Approver", extra_capabilities=("View source documents",)),
    Role(
        "Invoicer",
        "administration",
        "Invoicer",
        extra_capabilities=(
            "Upload a source document",
            "View source documents",
            "Manage customers",
        ),
    ),
    # §8.4 scopes this role to their OWN submissions. See the note on
    # EXTENSION_CAPABILITIES: that row-level scoping does not exist yet, so
    # `view document` is currently broader here than §8.4 intends.
    Role(
        "Expense Submitter",
        "administration",
        "Expense Submitter",
        extra_capabilities=("Upload a source document", "View source documents"),
    ),
    Role(
        "Viewer",
        "administration",
        "Viewer",
        extra_capabilities=("View source documents", "View filed returns", "View fixed assets"),
    ),
    # §8.4: "Scoped API access to named endpoints", "Explicitly cannot:
    # interactive login; broaden its own scope." Named endpoints are per
    # integration and none exist yet, so this holds only the baseline.
    Role("Service Account", "administration", None),
)

# §8.4 spells this one "Viewer / Auditor"; the catalogue uses the shorter
# name Appendix A's column header uses. Kept here so the §8.4 conformance
# test can reconcile the two without guessing.
SECTION_8_4_NAMES: Mapping[str, str] = {"Viewer / Auditor": "Viewer"}


# ---------------------------------------------------------------------------
# Derived views - everything downstream reads these, never the tables above
# ---------------------------------------------------------------------------
ALL_CAPABILITIES: tuple[Capability, ...] = (
    CAPABILITIES + BASELINE_CAPABILITIES + EXTENSION_CAPABILITIES
)

_BY_LABEL: Mapping[str, Capability] = {c.label: c for c in ALL_CAPABILITIES}


def capability(label: str) -> Capability:
    return _BY_LABEL[label]


def permission_catalogue() -> tuple[tuple[Permission, ScopeType], ...]:
    """Every distinct permission, with the resource scope of the capability
    it belongs to. Deduplicated by (action, resource_type) - the catalogue is
    a set of triples, and two capabilities naming the same one would be the
    same permission.
    """
    seen: dict[tuple[str, str], tuple[Permission, ScopeType]] = {}
    for cap in ALL_CAPABILITIES:
        for permission in cap.read + cap.full:
            seen.setdefault(permission.key, (permission, cap.scope))
    return tuple(seen.values())


def permissions_for_role(role: Role) -> frozenset[tuple[str, str]]:
    """The role's bundle: its Appendix A row read across the matrix, plus
    any §8.4-only capabilities, plus the baseline every role holds.
    """
    held: set[tuple[str, str]] = set()

    if role.matrix_column is not None:
        column = APPENDIX_A_COLUMNS.index(role.matrix_column)
        for cap in CAPABILITIES:
            for permission in cap.permissions_at(MATRIX[cap.label][column]):
                held.add(permission.key)

    for label in role.extra_capabilities:
        for permission in capability(label).permissions_at(Access.FULL):
            held.add(permission.key)

    for cap in BASELINE_CAPABILITIES:
        for permission in cap.permissions_at(Access.FULL):
            held.add(permission.key)

    return frozenset(held)


def role_bundles() -> Mapping[str, frozenset[tuple[str, str]]]:
    return {role.name: permissions_for_role(role) for role in ROLES}
