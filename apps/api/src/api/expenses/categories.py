"""The categories a receipt can be filed under at capture - FR-EXP-001b.

    FR-EXP-001b  The expense form asks for the minimum - date, supplier, gross
                 amount, VAT rate, category - ...

The capture screen asks for the category FIRST, before a file is sent, so that
the receipt arrives already filed. This is the list it offers.

--- The server owns the list ---

A client sends a KEY and the server decides whether it is one. The label stored
on the expense comes from here, not from the request, so a client cannot file a
receipt under a category this product does not have, and cannot invent the
string a ledger account mapping is later matched against
(`expense_posting_account.category_key`, migration 0034).

--- The stored value is the English label, not the key ---

`expense.category` has always been free text, and three things read it as text:
the form's category box, `expenses.suggest_category`'s history, and the posting
account mapping. Storing `office_supplies` would put a slug in front of a
person; storing "Office supplies" is readable in all three, and is what an
administrator types when they map a category to an account. The label is the
English one on purpose: the stored value is a lookup key for accounting
configuration, and it must not change with the language somebody happens to be
reading.

--- Why the VAT treatment is here but is not applied at capture ---

`vat_treatment` is on each entry because it is what the picker shows and what a
later step may pre-select. It is NOT written to the expense when the receipt is
captured, and that is not an oversight: migration 0033's
`expense_treatment_and_rate_together` requires a treatment and a rate to be set
together, the rate is a fact about the treatment on the receipt's DATE (CMP-014),
and at capture there is no date yet. A treatment with no rate is refused by the
database.

`rgs_code` is the account code the picker shows beside each category: the one
the default mapping (migration 0067, ADR-086) books it to in a seeded chart.
Which account an administration actually posts to is that administration's own
mapping and is not decided by this file - but a new administration's mapping is
0067's, and the picker used to show codes it did not use ("Office supplies 4400"
booked to 4100), so tests/integration/test_posting_defaults.py now asserts the
two agree.

`apps/web` / `packages/shared-types` carries the same list for the picker, and
`tests/expenses/test_categories.py` fails if the two drift.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.expenses.model import CaptureError, VatTreatment


@dataclass(frozen=True, slots=True)
class ExpenseCategory:
    key: str
    #: What is stored on the expense. English, and stable - see the module docstring.
    label: str
    rgs_code: str
    vat_treatment: VatTreatment


class UnknownExpenseCategory(CaptureError):
    """The category key is not one this product offers."""


EXPENSE_CATEGORIES: tuple[ExpenseCategory, ...] = (
    ExpenseCategory("inventory_stock", "Inventory & stock", "7000", VatTreatment.BTW_21),
    ExpenseCategory("car_transport", "Car & transport", "4300", VatTreatment.BTW_21),
    ExpenseCategory("travel_lodging", "Travel & lodging", "4200", VatTreatment.BTW_9),
    ExpenseCategory("lunch", "Lunch", "4200", VatTreatment.BTW_9),
    ExpenseCategory("dining_out", "Dining out", "4200", VatTreatment.BTW_9),
    ExpenseCategory("entertainment_gifts", "Entertainment & gifts", "4200", VatTreatment.BTW_21),
    ExpenseCategory("office_supplies", "Office supplies", "4100", VatTreatment.BTW_21),
    ExpenseCategory("rent_premises", "Rent & premises", "4000", VatTreatment.BTW_21),
    ExpenseCategory("utilities", "Utilities", "4000", VatTreatment.BTW_21),
    ExpenseCategory("phone_internet", "Phone & internet", "4100", VatTreatment.BTW_21),
    ExpenseCategory("marketing_ads", "Marketing & ads", "4200", VatTreatment.BTW_21),
    ExpenseCategory(
        "software_subscriptions", "Software & subscriptions", "4100", VatTreatment.BTW_21
    ),
    # Insurance and banking are VAT-EXEMPT in the Netherlands rather than
    # zero-rated: nothing is charged and nothing can be reclaimed, which is a
    # different treatment from a 0% supply even though both show "0%".
    ExpenseCategory("insurance", "Insurance", "4400", VatTreatment.BTW_VRIJGESTELD),
    ExpenseCategory("professional_services", "Professional services", "4400", VatTreatment.BTW_21),
    ExpenseCategory("training_education", "Training & education", "4400", VatTreatment.BTW_21),
    ExpenseCategory("staff_costs", "Staff costs", "4600", VatTreatment.BTW_21),
    ExpenseCategory("bank_interest", "Bank & interest", "4900", VatTreatment.BTW_VRIJGESTELD),
    ExpenseCategory("capital_asset", "Capital asset", "0250", VatTreatment.BTW_21),
    ExpenseCategory("other", "Other", "4400", VatTreatment.BTW_21),
)

_BY_KEY = {category.key: category for category in EXPENSE_CATEGORIES}
_BY_LABEL = {category.label.strip().lower(): category for category in EXPENSE_CATEGORIES}


def category_for_label(label: str | None) -> ExpenseCategory | None:
    """The category a stored expense label belongs to, or None.

    None is a normal answer: `expense.category` is free text, so a label typed
    into the form (or stored before this list existed) matches nothing, and the
    caller shows it as it is rather than inventing a category for it.
    """
    if not label:
        return None
    return _BY_LABEL.get(label.strip().lower())


def category_for_key(key: str) -> ExpenseCategory:
    """The category, or a refusal. Never a guess at what was meant."""
    try:
        return _BY_KEY[key.strip()]
    except KeyError:
        raise UnknownExpenseCategory(f"{key!r} is not an expense category") from None
