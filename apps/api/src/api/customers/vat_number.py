"""EU VAT identification numbers - shape only, never existence (FR-ONB-003).

    FR-ONB-003  Capture and validate BTW-identificatienummer and OB-nummer;
                validate EU VAT numbers via VIES.

--- This module answers one question, and it is the cheap one ---

    "Could this string be a VAT number issued by that member state?"

It does NOT answer "does this number exist", "is it registered for
intra-Community supplies", or "does it belong to this customer". Only VIES
answers those, and `api.customers.vies` is where that call lives.

The split is worth stating because the two are constantly confused. A
syntactically perfect number can be entirely fictional; a number VIES confirms
today can be deregistered tomorrow. What this module buys is the ability to
refuse an obvious typo without a network round trip, and - more usefully - to
avoid burning a VIES call, and a rate-limit slot, on a string that could not
possibly be valid.

--- Why no checksums ---

Several member states' numbers carry a check digit, and the Dutch one famously
used to: the old BTW-identificatienummer was the holder's BSN plus a modulus-11
digit. Implementing that would be actively wrong now. Since 2020 the Dutch
number issued to a sole trader is RANDOM precisely so it no longer discloses a
BSN (a privacy change, and PRIV-012's argument in the wild), and it does not
satisfy the eleven-proef. A checksum here would reject exactly the numbers the
Belastingdienst has been issuing for years.

The same trap exists elsewhere in different form, so the rule is applied
uniformly: format only, and the registry is the authority on existence.

--- Normalisation happens once, here ---

VIES compares on the compact upper-case form. Humans write `NL 8123.45.678.B01`
and `nl8123 45 678b01`. If normalisation lived at the call site, two spellings
of one number would become two customer records and two different VIES answers,
so `normalise` is applied before the value reaches the database - migration
0039's `vat_number` column stores the normalised form and nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "VatNumber",
    "normalise",
    "parse",
    "is_eu_member_state",
    "EU_VAT_COUNTRY_CODES",
]

#: Characters stripped before matching: everything a person might use to make a
#: long number readable. Removed rather than rejected, because a customer who
#: pasted the number off a letterhead has not made a mistake.
_SEPARATORS = re.compile(r"[\s.\-/]+")


def normalise(value: str) -> str:
    """The compact upper-case form VIES compares on. Never raises.

    Returns the input stripped of separators and upper-cased, whatever it is.
    Deciding whether the result is a VAT number is `parse`'s job - a function
    that both cleaned and validated would have to return two kinds of failure
    and callers would check the wrong one.
    """
    return _SEPARATORS.sub("", value).strip().upper()


#: Per-member-state formats, as published by the European Commission's VIES
#: service. Two things about this table are deliberate:
#:
#:   * `EL` is Greece, not `GR`. VIES uses the ISO 639 code for the language,
#:     not the ISO 3166 code for the country, and a lookup keyed on `GR` fails
#:     with a confusing "invalid country" rather than "not found".
#:   * `XI` is Northern Ireland, which remains inside the EU VAT area for goods
#:     under the Windsor Framework and is queryable through VIES. `GB` is not:
#:     Great Britain left, and a `GB` number has to be checked against HMRC's
#:     own service, which this system does not integrate.
#:
#: The patterns match the NATIONAL part only - the two-letter prefix is
#: stripped before matching, so a member state that changes its format is one
#: line here rather than a migration (0039's CHECK is deliberately loose for
#: this reason).
_NATIONAL_FORMATS: dict[str, re.Pattern[str]] = {
    "AT": re.compile(r"^U[0-9]{8}$"),
    "BE": re.compile(r"^[01][0-9]{9}$"),
    "BG": re.compile(r"^[0-9]{9,10}$"),
    "CY": re.compile(r"^[0-9]{8}[A-Z]$"),
    "CZ": re.compile(r"^[0-9]{8,10}$"),
    "DE": re.compile(r"^[0-9]{9}$"),
    "DK": re.compile(r"^[0-9]{8}$"),
    "EE": re.compile(r"^[0-9]{9}$"),
    "EL": re.compile(r"^[0-9]{9}$"),
    "ES": re.compile(r"^[0-9A-Z][0-9]{7}[0-9A-Z]$"),
    "FI": re.compile(r"^[0-9]{8}$"),
    "FR": re.compile(r"^[0-9A-Z]{2}[0-9]{9}$"),
    "HR": re.compile(r"^[0-9]{11}$"),
    "HU": re.compile(r"^[0-9]{8}$"),
    "IE": re.compile(r"^([0-9]{7}[A-Z]{1,2}|[0-9][A-Z+*][0-9]{5}[A-Z])$"),
    "IT": re.compile(r"^[0-9]{11}$"),
    "LT": re.compile(r"^([0-9]{9}|[0-9]{12})$"),
    "LU": re.compile(r"^[0-9]{8}$"),
    "LV": re.compile(r"^[0-9]{11}$"),
    "MT": re.compile(r"^[0-9]{8}$"),
    # NL: nine digits, a literal B, then two more. See the module docstring for
    # why there is no eleven-proef here.
    "NL": re.compile(r"^[0-9]{9}B[0-9]{2}$"),
    "PL": re.compile(r"^[0-9]{10}$"),
    "PT": re.compile(r"^[0-9]{9}$"),
    "RO": re.compile(r"^[0-9]{2,10}$"),
    "SE": re.compile(r"^[0-9]{12}$"),
    "SI": re.compile(r"^[0-9]{8}$"),
    "SK": re.compile(r"^[0-9]{10}$"),
    "XI": re.compile(r"^([0-9]{9}|[0-9]{12}|(GD|HA)[0-9]{3})$"),
}

#: The prefixes VIES will answer about. Frozen so a caller cannot mutate the
#: table by holding the keys view.
EU_VAT_COUNTRY_CODES: frozenset[str] = frozenset(_NATIONAL_FORMATS)


@dataclass(frozen=True, slots=True)
class VatNumber:
    """A syntactically well-formed EU VAT number, in VIES's own form."""

    #: The two-letter VIES prefix (`NL`, `EL`, `XI`). NOT always the ISO 3166
    #: country code - see _NATIONAL_FORMATS.
    country_code: str
    #: Everything after the prefix, normalised.
    national_number: str

    @property
    def value(self) -> str:
        """What migration 0039's `vat_number` column stores."""
        return f"{self.country_code}{self.national_number}"

    def __str__(self) -> str:
        return self.value


def is_eu_member_state(country_code: str) -> bool:
    """Whether VIES will answer about this prefix at all.

    Takes the VIES prefix, so `EL` is true and `GR` is false. Callers holding
    an ISO 3166 country code from `customer.country` must map Greece
    themselves; there is deliberately no silent alias, because a lookup that
    quietly rewrote the country would hide the one case where it mattered.
    """
    return country_code.strip().upper() in EU_VAT_COUNTRY_CODES


def parse(value: str | None) -> VatNumber | None:
    """A `VatNumber`, or None if the string is not one.

    None rather than an exception, and never a partial result. Every caller of
    this has a real branch for "not a VAT number" - a customer without one is
    ordinary, a domestic consumer has none, and a typo is a validation message
    - so a raise would be an exception used for a control flow that is not
    exceptional.

    A number whose PREFIX is not an EU member state returns None too. That is
    the right answer for this module: a Swiss or British number is not
    something VIES can be asked about, and pretending otherwise would produce
    an `invalid` verdict for a number that is perfectly real.
    """
    if value is None:
        return None

    compact = normalise(value)
    if len(compact) < 3:
        return None

    country_code, national_number = compact[:2], compact[2:]
    pattern = _NATIONAL_FORMATS.get(country_code)
    if pattern is None or not pattern.match(national_number):
        return None

    return VatNumber(country_code=country_code, national_number=national_number)
