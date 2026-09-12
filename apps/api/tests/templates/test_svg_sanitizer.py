"""FR-TPL-018's adversarial test suite - the most important tests in the logo
upload feature.

Every test here asserts the dangerous content is ABSENT from the sanitised
output bytes, not merely that no exception was raised - a sanitiser that
silently no-ops would pass a "does not raise" test while doing nothing.
"""

from __future__ import annotations

import pytest

from api.templates.svg_sanitizer import (
    MAX_DEPTH,
    MAX_ELEMENT_COUNT,
    MAX_SVG_BYTES,
    SvgContainsDoctypeOrEntity,
    SvgInvalidRoot,
    SvgMalformed,
    SvgSanitizationError,
    SvgTooComplex,
    SvgTooLarge,
    sanitize_svg,
)

SVG_NS = 'xmlns="http://www.w3.org/2000/svg"'
XLINK_NS = 'xmlns:xlink="http://www.w3.org/1999/xlink"'

#: A tiny, valid 1x1 transparent PNG, for the one <image> case that must
#: SURVIVE.
_TINY_PNG_DATA_URI = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
    "2mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _svg(body: str, *, xlink: bool = False) -> bytes:
    namespaces = f"{SVG_NS} {XLINK_NS}" if xlink else SVG_NS
    return f"<svg {namespaces}>{body}</svg>".encode()


class TestScriptElement:
    def test_script_element_is_stripped_entirely(self) -> None:
        out = sanitize_svg(_svg("<script>alert(1)</script>"))
        assert b"script" not in out
        assert b"alert" not in out


class TestEventHandlerAttributes:
    def test_onload_and_onclick_are_stripped_but_the_element_survives(self) -> None:
        out = sanitize_svg(
            _svg('<path d="M0 0 L1 1" onclick="alert(1)"/>').replace(
                b"<svg ", b'<svg onload="alert(1)" '
            )
        )
        assert b"onload" not in out
        assert b"onclick" not in out
        assert b"alert" not in out
        # The element itself is not thrown away merely for carrying a bad
        # attribute.
        assert b"<path" in out
        assert b'd="M0 0 L1 1"' in out


class TestForeignObject:
    def test_foreign_object_subtree_is_gone_entirely(self) -> None:
        out = sanitize_svg(
            _svg(
                '<foreignObject><body xmlns="http://www.w3.org/1999/xhtml">'
                "<script>alert(1)</script></body></foreignObject>"
            )
        )
        assert b"foreignObject" not in out
        assert b"script" not in out
        assert b"alert" not in out
        assert b"body" not in out


class TestUseHref:
    def test_external_use_href_never_survives(self) -> None:
        out = sanitize_svg(_svg('<use xlink:href="http://evil.example/steal.svg#x"/>', xlink=True))
        assert b"evil.example" not in out
        assert b"href" not in out

    def test_same_document_use_reference_survives(self) -> None:
        """The one case that MUST survive: a same-document `use` reference is
        a completely ordinary SVG pattern, and refusing it would make this
        sanitiser too aggressive to be useful.
        """
        out = sanitize_svg(
            _svg(
                '<defs><path id="legitimate-local-id" d="M0 0 L1 1"/></defs>'
                '<use xlink:href="#legitimate-local-id"/>',
                xlink=True,
            )
        )
        assert b'id="legitimate-local-id"' in out
        assert b'href="#legitimate-local-id"' in out


class TestImageHref:
    def test_external_image_href_is_dropped(self) -> None:
        out = sanitize_svg(_svg('<image href="http://evil.example/tracker.png"/>'))
        assert b"evil.example" not in out

    def test_valid_embedded_png_survives(self) -> None:
        out = sanitize_svg(_svg(f'<image href="{_TINY_PNG_DATA_URI}"/>'))
        assert b"data:image/png;base64," in out

    def test_svg_in_svg_recursion_is_refused(self) -> None:
        """`data:image/svg+xml` must never survive - it would let a
        sanitised SVG re-embed another, unsanitised one."""
        nested = "data:image/svg+xml;base64,PHN2Zz48c2NyaXB0PmFsZXJ0KDEpPC9zY3JpcHQ+PC9zdmc+"
        out = sanitize_svg(_svg(f'<image href="{nested}"/>'))
        assert b"svg+xml" not in out
        assert b"href" not in out


class TestStyle:
    def test_style_attribute_is_dropped_entirely(self) -> None:
        out = sanitize_svg(_svg('<path d="M0 0 L1 1" style="fill:url(http://evil.example/x)"/>'))
        assert b"style" not in out
        assert b"evil.example" not in out
        # The element itself survives; only the attribute is lost.
        assert b"<path" in out

    def test_style_element_is_dropped_entirely(self) -> None:
        out = sanitize_svg(_svg("<style>@import url(http://evil.example/x.css);</style>"))
        assert b"<style" not in out
        assert b"evil.example" not in out


class TestDoctypeAndEntity:
    def test_doctype_with_external_entity_is_refused_outright(self) -> None:
        payload = (
            b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b'<svg xmlns="http://www.w3.org/2000/svg">&xxe;</svg>'
        )
        with pytest.raises(SvgContainsDoctypeOrEntity):
            sanitize_svg(payload)

    def test_a_bare_doctype_with_no_entity_is_also_refused(self) -> None:
        payload = b'<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>'
        with pytest.raises(SvgContainsDoctypeOrEntity):
            sanitize_svg(payload)

    def test_refusal_happens_before_any_parsing(self) -> None:
        """Even XML the parser could never make sense of is refused for the
        DOCTYPE reason specifically, proving the check runs first."""
        payload = b"<!DOCTYPE svg [<!ENTITY x 'y'>this is not even XML"
        with pytest.raises(SvgContainsDoctypeOrEntity):
            sanitize_svg(payload)


class TestNamespaceSmuggling:
    def test_a_non_svg_namespaced_element_with_a_dangerous_name_is_dropped(self) -> None:
        out = sanitize_svg(
            _svg('<x:script xmlns:x="http://www.w3.org/1999/xhtml">alert(1)</x:script>')
        )
        assert b"alert" not in out
        assert b"script" not in out


class TestAnimationElements:
    def test_animate_and_set_are_entirely_absent(self) -> None:
        out = sanitize_svg(
            _svg(
                '<animate attributeName="onmouseover" to="alert(1)" />'
                '<set attributeName="href" to="javascript:alert(1)" />'
            )
        )
        assert b"animate" not in out
        assert b"<set" not in out
        assert b"alert" not in out


class TestJavascriptValueDefenseInDepth:
    def test_javascript_value_on_a_non_href_attribute_is_dropped(self) -> None:
        out = sanitize_svg(_svg('<path d="M0 0 L1 1" fill="javascript:alert(1)"/>'))
        assert b"javascript" not in out
        # The element and its other, legitimate attribute survive.
        assert b"<path" in out
        assert b'd="M0 0 L1 1"' in out


class TestSizeCap:
    def test_a_file_over_the_size_cap_is_refused(self) -> None:
        huge_path = "M0 0 " + "L1 1 " * (MAX_SVG_BYTES // 4)
        payload = _svg(f'<path d="{huge_path}"/>')
        assert len(payload) > MAX_SVG_BYTES
        with pytest.raises(SvgTooLarge):
            sanitize_svg(payload)


class TestComplexityCap:
    def test_pathological_nesting_is_refused_by_the_depth_guard(self) -> None:
        nested = "<g>" * (MAX_DEPTH + 50) + "</g>" * (MAX_DEPTH + 50)
        payload = _svg(nested)
        with pytest.raises(SvgTooComplex):
            sanitize_svg(payload)

    def test_a_wide_but_shallow_tree_is_refused_by_the_element_count_guard(self) -> None:
        many_siblings = "<rect/>" * (MAX_ELEMENT_COUNT + 100)
        payload = _svg(many_siblings)
        with pytest.raises(SvgTooComplex):
            sanitize_svg(payload)


class TestMalformedAndInvalidRoot:
    def test_malformed_xml_is_refused(self) -> None:
        with pytest.raises(SvgMalformed):
            sanitize_svg(b"<svg><path d='M0 0'></svg>")

    def test_a_non_svg_root_is_refused(self) -> None:
        with pytest.raises(SvgInvalidRoot):
            sanitize_svg(b'<html xmlns="http://www.w3.org/1999/xhtml"></html>')

    def test_a_root_with_no_namespace_is_refused(self) -> None:
        """A bare, unnamespaced <svg> is refused too - a logo SVG must
        properly declare its namespace."""
        with pytest.raises(SvgInvalidRoot):
            sanitize_svg(b"<svg><path d='M0 0'/></svg>")


class TestCleanRealisticLogo:
    def test_a_genuinely_clean_logo_passes_through_intact(self) -> None:
        payload = _svg(
            "<title>Acme B.V.</title>"
            "<defs>"
            '<linearGradient id="brand" x1="0" y1="0" x2="1" y2="1">'
            '<stop offset="0" stop-color="#0f172a"/>'
            '<stop offset="1" stop-color="#1d4ed8"/>'
            "</linearGradient>"
            "</defs>"
            '<g transform="translate(4,4)">'
            '<path d="M0 0 L40 0 L40 40 L0 40 Z" fill="url(#brand)"/>'
            '<circle cx="20" cy="20" r="8" fill="#ffffff" opacity="0.9"/>'
            '<text x="4" y="52" font-family="Inter" font-size="10">Acme</text>'
            "</g>",
        )
        out = sanitize_svg(payload)

        for expected in (
            b"<title>Acme B.V.</title>",
            b'id="brand"',
            b'stop-color="#0f172a"',
            b'stop-color="#1d4ed8"',
            b'd="M0 0 L40 0 L40 40 L0 40 Z"',
            b'fill="url(#brand)"',
            b'cx="20"',
            b'r="8"',
            b'font-family="Inter"',
            b">Acme<",
        ):
            assert expected in out, f"{expected!r} missing from sanitised output: {out!r}"


class TestExceptionsAreSpecific:
    def test_every_refusal_is_a_subclass_of_the_common_base(self) -> None:
        for exc_type in (
            SvgContainsDoctypeOrEntity,
            SvgTooLarge,
            SvgTooComplex,
            SvgMalformed,
            SvgInvalidRoot,
        ):
            assert issubclass(exc_type, SvgSanitizationError)
