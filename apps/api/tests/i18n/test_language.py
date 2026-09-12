"""Which language the API answers in - FR-LOC-001b, FR-UX-007.

The client-side counterpart is packages/i18n/src/language.test.ts, which tests
a different question: IAM-010g's pre-login resolution from a device choice, a
browser's languages and the host. The server never sees any of those. It sees
one header, and this is about reading it correctly.
"""

from __future__ import annotations

import pytest

from api.i18n.language import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGE_CODES,
    Language,
    negotiate_language,
    parse_language,
)


class TestNegotiation:
    def test_a_plain_language_tag(self) -> None:
        assert negotiate_language("nl") is Language.NL
        assert negotiate_language("en") is Language.EN

    def test_a_regional_variant_matches_on_the_primary_subtag(self) -> None:
        # A Flemish accountant's browser sends nl-BE. Falling through to the
        # default over a region code would be an accident of string equality,
        # and would be the wrong answer for exactly the users most likely to
        # send it.
        assert negotiate_language("nl-BE") is Language.NL
        assert negotiate_language("en-US") is Language.EN

    def test_quality_weights_are_honoured_over_header_order(self) -> None:
        # `en` appears first and is wanted less. A naive "take the first tag"
        # implementation gets this backwards, and it is the shape a browser
        # actually sends when someone reorders their language preferences.
        assert negotiate_language("en;q=0.5, nl;q=0.9") is Language.NL
        assert negotiate_language("nl;q=0.4, en;q=0.8") is Language.EN

    def test_header_order_breaks_a_tie(self) -> None:
        assert negotiate_language("nl, en") is Language.NL
        assert negotiate_language("en, nl") is Language.EN
        assert negotiate_language("en;q=0.9, nl;q=0.9") is Language.EN

    def test_languages_we_do_not_ship_are_skipped_rather_than_matched(self) -> None:
        assert negotiate_language("fr-FR, de-DE, nl") is Language.NL
        assert negotiate_language("fr-FR, de-DE") is DEFAULT_LANGUAGE

    def test_q_zero_means_not_acceptable(self) -> None:
        # RFC 9110: `nl;q=0` is a client asking NOT to be given Dutch. Serving
        # it anyway - which every "just match the first supported tag"
        # implementation does - is worse than falling through to the default,
        # because the client said something specific and was overruled.
        assert negotiate_language("nl;q=0, en") is Language.EN
        assert negotiate_language("en;q=0, nl;q=0") is DEFAULT_LANGUAGE

    def test_a_wildcard_asks_for_anything_and_gets_the_default(self) -> None:
        # `*` expresses no preference between our two, so it must not be read
        # as a vote for whichever happens to sort first.
        assert negotiate_language("*") is DEFAULT_LANGUAGE
        assert negotiate_language("en, *") is Language.EN

    def test_a_missing_or_malformed_header_falls_through_rather_than_failing(self) -> None:
        # A bad header is a reason to answer in the default language, never a
        # reason to fail a request that was otherwise fine. FR-UX-007 is about
        # the words in an error, not about manufacturing one.
        for header in (None, "", "   ", ";;;", "nl;q=banana", "\x00"):
            assert negotiate_language(header) is DEFAULT_LANGUAGE

    def test_whitespace_and_case_are_tolerated(self) -> None:
        assert negotiate_language("  NL-nl ;  q = 0.8 ") is Language.NL
        assert negotiate_language("EN") is Language.EN

    def test_an_unparseable_entry_does_not_discard_the_rest(self) -> None:
        # A single bad range must not cost the caller their actual preference.
        assert negotiate_language("!!!, en") is Language.EN


class TestParsing:
    def test_only_the_languages_we_ship(self) -> None:
        assert parse_language("nl") is Language.NL
        assert parse_language(" EN ") is Language.EN
        assert parse_language("de") is None
        assert parse_language("nl-NL") is None, "a stored preference is a bare code"

    def test_none_means_never_chosen_rather_than_the_default(self) -> None:
        """FR-LOC-001b / IAM-010g. `users.language` is nullable and NULL is a
        real state - "this person has never told us" - distinct from having
        chosen Dutch.

        Returning DEFAULT_LANGUAGE here instead would erase that difference at
        the one point that needs it: the first-login step that seeds the
        account from the device's pre-login choice has to know whether it is
        filling a gap or overwriting a decision.
        """
        assert parse_language(None) is None
        assert parse_language("") is None


def test_the_supported_set_is_exactly_dutch_and_english() -> None:
    assert set(Language) == {Language.NL, Language.EN}
    # Alphabetical by endonym (English, Nederlands), so a picker rendering
    # this order presents neither as the primary language and the other as a
    # translation of it. Mirrors SUPPORTED_LANGUAGES in
    # packages/i18n/src/language.ts, which the same reasoning applies to.
    assert SUPPORTED_LANGUAGE_CODES == ("en", "nl")


def test_the_default_is_stated_once() -> None:
    """PRD Q12 is open, and DEFAULT_LANGUAGE is the answer shipped until it is
    resolved. This asserts what that answer currently is, so that changing it
    is a deliberate edit with a failing test attached rather than a drift
    nobody notices.
    """
    assert DEFAULT_LANGUAGE is Language.NL


@pytest.mark.parametrize("language", list(Language))
def test_every_language_can_answer_every_request(language: Language) -> None:
    """A language in the enum with no strings behind it would be a code the
    API accepts and then crashes on. tests/i18n/test_catalogue.py asserts the
    catalogue covers both; this asserts the enum has not grown past it.
    """
    from api.i18n.catalogue import MESSAGES

    for key, record in MESSAGES.items():
        assert language.value in record, f"{key} has nothing for {language.value}"
