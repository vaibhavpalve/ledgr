"""EU VAT number cases, as data - FR-ONB-003.

A shared table rather than assertions inline, the same shape
`tests/expenses/vat_cases.py` and `tests/vat/rule_cases.py` use: the interesting
content is WHICH strings are numbers and which are not, and that reads far
better as a list than as forty near-identical test bodies.

Every real number below is a published example from the European Commission's
own VIES documentation or a member state's format specification - not a number
belonging to an actual business. They exercise the format, and the format is
all `api.customers.vat_number` claims to check.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParseCase:
    #: What somebody typed.
    raw: str
    #: The expected normalised value, or None if this is not a VAT number.
    expected: str | None
    why: str


#: Well-formed numbers, one per member state VIES answers about, plus the
#: spellings a person actually types.
WELL_FORMED: tuple[ParseCase, ...] = (
    ParseCase("NL123456789B01", "NL123456789B01", "the Dutch shape: 9 digits, B, 2 digits"),
    ParseCase(
        "nl 1234.56.789.b01",
        "NL123456789B01",
        "off a letterhead: spaces, dots and lower case all normalise away",
    ),
    ParseCase("  NL123456789B01  ", "NL123456789B01", "pasted with surrounding whitespace"),
    ParseCase("NL-123456789-B01", "NL123456789B01", "hyphenated"),
    ParseCase("BE0123456789", "BE0123456789", "Belgium: 10 digits starting 0 or 1"),
    ParseCase("BE1234567891", "BE1234567891", "Belgium: the 1-prefix range"),
    ParseCase("DE123456789", "DE123456789", "Germany: 9 digits"),
    ParseCase("FR12345678901", "FR12345678901", "France: 2 alphanumerics then 9 digits"),
    ParseCase("FRXX123456789", "FRXX123456789", "France: the key may be letters"),
    ParseCase("ATU12345678", "ATU12345678", "Austria: a literal U then 8 digits"),
    ParseCase("IT12345678901", "IT12345678901", "Italy: 11 digits"),
    ParseCase("ES A12345678", "ESA12345678", "Spain: letter, 7 digits, alphanumeric"),
    ParseCase("PL1234567890", "PL1234567890", "Poland: 10 digits"),
    ParseCase("SE123456789012", "SE123456789012", "Sweden: 12 digits"),
    ParseCase("DK12345678", "DK12345678", "Denmark: 8 digits"),
    ParseCase("FI12345678", "FI12345678", "Finland: 8 digits"),
    ParseCase("IE1234567A", "IE1234567A", "Ireland: 7 digits then a letter"),
    ParseCase("IE1234567AB", "IE1234567AB", "Ireland: the two-letter variant"),
    ParseCase("IE1A23456B", "IE1A23456B", "Ireland: the old digit/letter form"),
    ParseCase("PT123456789", "PT123456789", "Portugal: 9 digits"),
    ParseCase("LU12345678", "LU12345678", "Luxembourg: 8 digits"),
    ParseCase("CZ12345678", "CZ12345678", "Czechia: 8, 9 or 10 digits"),
    ParseCase("CZ1234567890", "CZ1234567890", "Czechia: the 10-digit form"),
    ParseCase("RO12", "RO12", "Romania: as few as 2 digits, and that is correct"),
    ParseCase("RO1234567890", "RO1234567890", "Romania: up to 10"),
    ParseCase("LT123456789", "LT123456789", "Lithuania: 9 digits"),
    ParseCase("LT123456789012", "LT123456789012", "Lithuania: or 12"),
    ParseCase("HR12345678901", "HR12345678901", "Croatia: 11 digits"),
    ParseCase("CY12345678X", "CY12345678X", "Cyprus: 8 digits then a letter"),
    ParseCase("BG123456789", "BG123456789", "Bulgaria: 9 or 10 digits"),
    ParseCase("EE123456789", "EE123456789", "Estonia: 9 digits"),
    ParseCase("LV12345678901", "LV12345678901", "Latvia: 11 digits"),
    ParseCase("MT12345678", "MT12345678", "Malta: 8 digits"),
    ParseCase("SI12345678", "SI12345678", "Slovenia: 8 digits"),
    ParseCase("SK1234567890", "SK1234567890", "Slovakia: 10 digits"),
    ParseCase("HU12345678", "HU12345678", "Hungary: 8 digits"),
    ParseCase(
        "EL123456789",
        "EL123456789",
        "Greece is EL in VIES, not GR - the language code, not the country code",
    ),
    ParseCase(
        "XI123456789",
        "XI123456789",
        "Northern Ireland stays in the EU VAT area for goods (Windsor Framework)",
    ),
    ParseCase("XIGD123", "XIGD123", "Northern Ireland: the government-department form"),
)

#: Strings that are not EU VAT numbers, and why each one matters.
MALFORMED: tuple[ParseCase, ...] = (
    ParseCase("", None, "empty"),
    ParseCase("   ", None, "whitespace only"),
    ParseCase("NL", None, "a prefix and nothing else"),
    ParseCase("123456789B01", None, "no country prefix at all"),
    ParseCase("NL12345678B01", None, "Dutch number one digit short - the common typo"),
    ParseCase("NL1234567890B01", None, "Dutch number one digit long"),
    ParseCase("NL123456789A01", None, "Dutch number without the literal B"),
    ParseCase("NL123456789B0", None, "Dutch suffix one digit short"),
    ParseCase("DE12345678", None, "German number one digit short"),
    ParseCase("BE2123456789", None, "Belgian number must start 0 or 1"),
    ParseCase("ATX12345678", None, "Austrian number needs a literal U"),
    ParseCase(
        "GB123456789",
        None,
        "Great Britain left the EU VAT area; VIES will not answer, and calling it "
        "invalid would be a wrong verdict about a real number",
    ),
    ParseCase(
        "GR123456789",
        None,
        "the ISO 3166 code for Greece. VIES uses EL, and silently rewriting it "
        "would hide the one case where the difference matters",
    ),
    ParseCase("CH123456789", None, "Switzerland is not in the EU VAT area"),
    ParseCase("US123456789", None, "not a member state prefix"),
    ParseCase("ZZ123456789", None, "not a country at all"),
    ParseCase("NLABCDEFGHIB01", None, "letters where the Dutch format wants digits"),
    ParseCase("RO1", None, "Romania's minimum is two digits"),
    ParseCase("RO12345678901", None, "Romania's maximum is ten"),
)

ALL_CASES: tuple[ParseCase, ...] = WELL_FORMED + MALFORMED
