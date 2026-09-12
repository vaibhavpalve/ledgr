"""FR-AR-002's legal wording, as a sheet for a tax adviser to sign off.

    FR-AR-002  ... Correct legal wording rendered per treatment.

--- Why this script exists ---

The wording on an invoice is not a label on an amount. Art. 226(11) of the VAT
Directive makes the STATEMENT the legal content of a zero-VAT invoice: a
reverse-charged supply obliges the buyer to account for the tax, and they can
only know that because the invoice says so.

Nothing in this system can check that the words are the right ones. Only a
person qualified in Dutch VAT can, and the thing that stops that review
happening is usually not disagreement - it is that nobody has produced a page
they can read. This is that page.

Least settled first, because the entries with open questions are the ones worth
a conversation and the ones an adviser should reach before their attention
runs out. Rendered from the same file the runtime reads, so what gets signed
off is what ships - the device `check_translations.py --glossary` already uses
for FR-LOC-001c's terminology review.

    make review-invoice-wording

Exit codes: 0 rendered, 2 could not run. Deliberately NOT 1-on-unreviewed: this
is a sheet, not a gate. A CI check that blocked on a human process nobody had
scheduled would be disabled within a month, and the checks worth having would
go with it - the argument scripts/check_translations.py makes at length about
the glossary.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from api.i18n.language import Language  # noqa: E402
from api.invoicing.wording import WORDING_KEYS, reviews, wording_for  # noqa: E402

EXIT_OK, EXIT_CANNOT_RUN = 0, 2

#: Least settled first. Within a status, lowest confidence first - the two
#: together are "how much does this need somebody".
_STATUS_ORDER = {"flagged": 0, "proposed": 1, "reviewed": 2}
_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def main() -> int:
    try:
        record = reviews()
    except (OSError, KeyError, RuntimeError) as exc:
        print(f"cannot read the wording review record: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    ordered = sorted(
        record.values(),
        key=lambda review: (
            _STATUS_ORDER.get(review.status, 99),
            _CONFIDENCE_ORDER.get(review.confidence, 99),
            review.role.value,
        ),
    )

    print("FR-AR-002 - invoice legal wording, for review")
    print("=" * 78)
    print()
    print(
        "Each treatment below puts a statement on every invoice that carries it.\n"
        "For the treatments that charge no VAT, that statement IS the legal\n"
        "content: art. 226(11) requires the invoice to say why no VAT was\n"
        "charged, and a customer's own tax authority reads it.\n"
    )
    print(
        "Nothing in LEDGR can check these words. Signing one off is an edit to\n"
        "apps/api/data/invoicing/wording-review.json: set status to 'reviewed'\n"
        "and fill reviewedBy and reviewedOn. It takes effect on the next\n"
        "restart, with no code change.\n"
    )

    outstanding = 0
    for review in ordered:
        mark = "OK " if review.is_reviewed else "-- "
        print("-" * 78)
        print(
            f"{mark}{review.role.value.upper()}   ({review.status}, confidence {review.confidence})"
        )
        print()
        print(f"  basis        {review.basis}")
        print(f"  message key  {WORDING_KEYS[review.role]}")
        for language in Language:
            print(f"  {language.value.upper():<12} {wording_for(review.role, language).text}")

        if review.is_reviewed:
            print(f"  reviewed     {review.reviewed_by} on {review.reviewed_on}")
        else:
            outstanding += 1
            print("  reviewed     NOT YET")

        if review.questions:
            print()
            print("  still open:")
            for index, question in enumerate(review.questions, start=1):
                print(f"    {index}. {_wrap(question)}")
        print()

    print("=" * 78)
    reviewed = len(record) - outstanding
    print(f"{reviewed} of {len(record)} treatments reviewed; {outstanding} outstanding.")
    if outstanding:
        print(
            "\nUntil each is signed off, every invoice this system produces reports\n"
            "`wording_is_provisional: true`. That is a statement about the words,\n"
            "not about the arithmetic - the amounts are checked and tested."
        )
    return EXIT_OK


def _wrap(text: str, width: int = 70, indent: str = "       ") -> str:
    """Hanging indent, so a long question stays readable in a terminal."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return f"\n{indent}".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
