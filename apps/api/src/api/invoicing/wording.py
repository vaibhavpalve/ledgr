"""The legal wording a treatment puts on the invoice - FR-AR-002.

    FR-AR-002  VAT handling: 21%, 9%, 0%, exempt (vrijgesteld), reverse charge
               domestic (verlegd), intra-Community supply, export, margin
               scheme. CORRECT LEGAL WORDING RENDERED PER TREATMENT.

--- Wording is a legal obligation, not a label ---

Article 226 of the EU VAT Directive, and Article 35a of the Dutch Wet OB that
implements it, require an invoice to STATE the reason no VAT has been charged.
An invoice showing 0.00 with no explanation is not a compliant invoice, and the
customer cannot use it: a reverse-charged supply obliges the BUYER to account
for the VAT, and they can only know that because the invoice says so.

So the wording is not decoration on the amount. For the five treatments that
charge nothing it is the part that carries the legal meaning, and it is the
reason `api.invoicing.vat` refuses to collapse them into "rate 0".

--- The words come from the catalogue, in the RECIPIENT's language ---

FR-TPL-013: "Language of the rendered document follows the recipient,
independent of the designer's UI language." A Dutch bookkeeper invoicing a
German customer in English gets English wording on the document while their own
screen stays Dutch, so the language is a PARAMETER here and never the request's
own.

Both languages live in packages/i18n/catalogue/invoice.json, which the API and
the web app read from the same file (FR-LOC-001). The check in
scripts/check_translations.py is what keeps a missing one from shipping.

--- PROVENANCE: THIS TEXT NEEDS A TAX ADVISER BEFORE AN INVOICE IS SENT ---

The MECHANISM here is complete: a treatment resolves to a statement, in both
languages, and an invoice cannot be issued without one. The TEXT is drafted
from the Directive's own language and ordinary Dutch invoicing practice, and it
has not been reviewed by a Dutch tax adviser.

That is the same posture data/vat/nl-vat-rules.json takes about its
treatment-to-rubriek mapping, and for the same reason: getting the wording
wrong produces an invoice that looks entirely normal and is not compliant, so
it is exactly the kind of error that survives every other check in this system.
FR-LOC-001c already requires Dutch accounting terminology to be reviewed by a
practising accountant; this is that obligation, applied to the text a customer
receives.

The state is DATA, in apps/api/data/invoicing/wording-review.json, shaped like
the `review` blocks in the glossary and read here. Signing a treatment off is
an edit to that file - a status, a name and a date - and takes effect on the
next restart with no code change and no release, the same property
`scripts/load_vat_rules.py` gives a rate change. `make review-invoice-wording`
renders it as a sheet to review.

Nothing here blocks an issue, and that is deliberate rather than lax. The
glossary check makes the argument at length: a gate that blocks on a human
process nobody has scheduled gets disabled, and the checks worth having go
with it. What this does instead is make the state impossible to lose - every
invoice carries `wording_is_provisional`, and `unreviewed_wording()` is what a
release check reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from api.i18n.catalogue import translate
from api.i18n.language import Language
from api.vat.rules import TreatmentRole

__all__ = [
    "WORDING_KEYS",
    "REVIEWED",
    "WordingReview",
    "review_for",
    "reviews",
    "wording_for",
    "requires_customer_vat_number",
    "unreviewed_wording",
]

#: One catalogue key per treatment role. Every role has an entry - including
#: the rate-bearing ones, whose "wording" is the rate description that sits
#: beside the amount - so no branch has to decide whether a statement exists.
WORDING_KEYS: dict[TreatmentRole, str] = {
    TreatmentRole.STANDARD: "invoice.vat.standard",
    TreatmentRole.REDUCED: "invoice.vat.reduced",
    TreatmentRole.ZERO: "invoice.vat.zero",
    TreatmentRole.EXEMPT: "invoice.vat.exempt",
    TreatmentRole.REVERSE_CHARGE: "invoice.vat.reverse_charge",
    TreatmentRole.INTRA_COMMUNITY: "invoice.vat.intra_community",
    TreatmentRole.EXPORT: "invoice.vat.export",
    TreatmentRole.MARGIN: "invoice.vat.margin",
}

_REVIEW_FILENAME = "wording-review.json"


def _review_file() -> Path:
    """Where the review record is, most specific first.

    Two real locations, and they are not redundant - the split
    `api.i18n.catalogue` already makes for the message catalogue:

      * the packaged copy, `api/invoicing/data`, placed there by the
        `force-include` in pyproject.toml. This is what a deployed API reads.
      * the checkout, `apps/api/data/invoicing`. This is what a developer and
        the test suite read.
    """
    here = Path(__file__).resolve()
    candidates = (
        here.parent / "data" / _REVIEW_FILENAME,
        here.parents[3] / "data" / "invoicing" / _REVIEW_FILENAME,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "no invoice wording review record found. Looked in: "
        + ", ".join(str(path) for path in candidates)
        + ". FR-AR-002's legal wording is only defensible alongside a record of "
        "whether anybody qualified has checked it, so the API does not serve "
        "invoices without one."
    )


@dataclass(frozen=True, slots=True)
class WordingReview:
    """Where one treatment's statement stands.

    `basis` is the statutory provision the statement answers to. It is the
    thing an adviser actually checks the words against, so it is carried
    rather than left in a comment.
    """

    role: TreatmentRole
    basis: str
    status: str
    confidence: str
    reviewed_by: str | None
    reviewed_on: str | None
    questions: tuple[str, ...]

    @property
    def is_reviewed(self) -> bool:
        """Reviewed means a NAMED person on a DATE, not a status alone.

        A status set to 'reviewed' with nobody against it records a decision
        with no author, which is the state this whole file exists to prevent.
        """
        return self.status == "reviewed" and bool(self.reviewed_by) and bool(self.reviewed_on)


@cache
def reviews() -> dict[TreatmentRole, WordingReview]:
    """The review record, read once.

    A missing or malformed file is a startup failure rather than a silent
    "nothing is reviewed": the second would look identical to an honest
    unreviewed state, and the difference matters.
    """
    document = json.loads(_review_file().read_text(encoding="utf-8"))
    entries = document["treatments"]

    parsed: dict[TreatmentRole, WordingReview] = {}
    for role in TreatmentRole:
        entry = entries.get(role.value)
        if entry is None:
            raise RuntimeError(
                f"{_REVIEW_FILENAME} has no entry for the {role.value} treatment. "
                f"Every treatment records where its legal wording stands "
                f"(FR-AR-002); an omission is the uncertainty going unrecorded."
            )
        review = entry["review"]
        parsed[role] = WordingReview(
            role=role,
            basis=entry["basis"],
            status=review["status"],
            confidence=review["confidence"],
            reviewed_by=review.get("reviewedBy"),
            reviewed_on=review.get("reviewedOn"),
            questions=tuple(review.get("questions", ())),
        )
    return parsed


def review_for(role: TreatmentRole) -> WordingReview:
    return reviews()[role]


#: Roles whose statement a named person has checked against the statute.
#: DERIVED from the review file, so signing one off is a data edit rather than
#: a code change - see the module docstring.
REVIEWED: frozenset[TreatmentRole] = frozenset(
    role for role, review in reviews().items() if review.is_reviewed
)

#: Art. 226(11a) and the intra-Community rules: where the CUSTOMER is liable
#: for the VAT, the invoice must carry the customer's VAT identification
#: number. Without it the buyer cannot make the reverse charge and the supply
#: cannot be zero-rated as intra-Community - so this is a hard requirement on
#: the document, checked in statutory.py rather than left to a renderer.
_NEEDS_CUSTOMER_VAT_NUMBER: frozenset[TreatmentRole] = frozenset(
    {TreatmentRole.REVERSE_CHARGE, TreatmentRole.INTRA_COMMUNITY}
)


@dataclass(frozen=True, slots=True)
class Wording:
    role: TreatmentRole
    text: str
    #: False while the text has not been through a tax adviser. Carried on the
    #: value rather than looked up separately, so anything rendering an invoice
    #: can see it without knowing to ask.
    reviewed: bool


def wording_for(role: TreatmentRole, language: Language) -> Wording:
    """The statement this treatment must put on the invoice.

    `language` is the RECIPIENT's (FR-TPL-013), not the caller's. Raises for an
    unknown role rather than returning an empty string: an invoice that silently
    dropped its reverse-charge statement would be non-compliant in a way nobody
    would notice until a buyer failed to account for the VAT.
    """
    try:
        key = WORDING_KEYS[role]
    except KeyError as exc:
        raise KeyError(
            f"no invoice wording is defined for the {role.value} treatment "
            f"(FR-AR-002); an invoice cannot be issued without one"
        ) from exc
    return Wording(role=role, text=translate(key, language), reviewed=review_for(role).is_reviewed)


def requires_customer_vat_number(role: TreatmentRole) -> bool:
    """Whether this treatment makes the customer's VAT number statutory."""
    return role in _NEEDS_CUSTOMER_VAT_NUMBER


def unreviewed_wording() -> tuple[TreatmentRole, ...]:
    """Treatments whose legal statement no tax adviser has signed off.

    Exposed so the fact is checkable rather than buried in a comment - the
    device `VatRulesService.provisional_rulesets` uses for the same class of
    problem.
    """
    return tuple(role for role in WORDING_KEYS if role not in REVIEWED)
