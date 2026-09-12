r"""Sanitising an uploaded SVG logo - FR-TPL-018, SEC-005, SEC-006.

    FR-TPL-018  Uploaded logos are validated and sanitised - SVG is stripped
                of scripts and external references before storage or
                rendering.

--- The threat model, briefly ---

An SVG file is not a picture format the way PNG or JPEG is. It is an XML
document that a browser (and this product's own designer preview, which
renders it with a plain `<img>`/object-URL, see `TemplateDesigner.tsx`) will
parse and execute parts of: `<script>`, event-handler attributes
(`onload`, `onclick`, ...), `<foreignObject>` carrying arbitrary HTML,
`<style>`/`style="..."` carrying CSS (`url(...)`, `@import`, and older
`expression()` tricks), and `href`/`xlink:href` attributes that can fetch an
external resource or re-embed another SVG recursively. Every one of these is a
well-documented class of SVG-based XSS, the same ground the OWASP XSS
Filter Evasion cheatsheet and libraries like DOMPurism/DOMPurify's SVG mode
exist to cover - this module hand-rolls the same posture DOMPurify takes
(strip to an allowlist, never try to enumerate every dangerous tag) because no
XML/SVG-sanitising library is available in this project's dependency set (see
ADR-044): the only tool on hand is the Python standard library's
`xml.etree.ElementTree`.

--- Allowlist, not denylist - and why ---

A denylist ("strip `<script>`, strip `on*`, strip `javascript:`...") has to be
exhaustively complete to be correct, and SVG's own history of new elements,
attributes and browser-specific quirks means a denylist is never finished.
This module instead decides what is PERMITTED - a short, fixed list of
elements and attributes a business logo plausibly needs - and drops
EVERYTHING else, including things nobody has thought to attack yet. This is
the standard, correct shape for exactly this problem.

--- What is deliberately NOT supported, and why that is fine for a logo ---

  * No `<script>`, no event-handler attributes, no `<foreignObject>`, no
    `<animate>`/`<set>`/other SMIL animation elements, no `<style>` element
    and no `style` attribute (CSS support at all - `style="fill:url(...)"` is
    an exfiltration vector and `@import`/`expression()` a scripting one; a
    logo needing CSS styling should use presentation attributes instead,
    which this sanitiser keeps). No `<a>`, `<video>`, `<audio>`, `<iframe>`,
    `<object>`, `<embed>`. A business logo is static vector art - paths,
    shapes, gradients, text - and needs none of these.
  * `href`/`xlink:href` is permitted only in the two shapes a logo plausibly
    needs: a same-document `#fragment` reference on `<use>`, and an embedded
    `data:image/png` or `data:image/jpeg` URI on `<image>`. Every other use of
    href, on every other element, is dropped - which is what closes the
    external-fetch and SVG-in-SVG-recursion holes without losing the ordinary
    "reuse a shape defined once" pattern real logos use.
  * XXE (XML external entity) is defended twice: a raw, pre-parse refusal of
    any `<!DOCTYPE` or `<!ENTITY` declaration (this module's first check,
    on the raw bytes, before `xml.etree.ElementTree` ever sees them), and,
    independently, the fact that `xml.etree.ElementTree`/expat does not
    resolve external entities by default. The pre-parse refusal is not
    redundant with that default: a logo file has no legitimate reason to
    declare a DOCTYPE at all, and refusing outright is simpler and safer than
    trying to verify that today's default stays true of tomorrow's Python.

--- Comments and processing instructions ---

FR-TPL-018 asks for scripts and external references to be stripped; comments
and processing instructions carry neither on their own, but they are also
never meaningful in a rendered logo. `xml.etree.ElementTree.fromstring`'s
default parser already does not add them to the tree at all (no
`insert_comments`/`insert_pis` was requested), so this module does not need a
separate stripping step for them - the output of `ET.tostring` on the tree
this module builds can never contain one.

--- What this module does not do ---

It does not rasterise, validate visual correctness, or guarantee the output
"looks the same" as the input beyond what the allowlist preserves - an SVG
using CSS classes for its colours, for instance, loses that styling entirely
(see the `style` note above) because there is no supported way to carry it
through safely. That is a real, named trade-off, not an oversight.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

__all__ = [
    "SVG_NAMESPACE",
    "MAX_SVG_BYTES",
    "MAX_ELEMENT_COUNT",
    "MAX_DEPTH",
    "SvgSanitizationError",
    "SvgContainsDoctypeOrEntity",
    "SvgTooLarge",
    "SvgTooComplex",
    "SvgMalformed",
    "SvgInvalidRoot",
    "sanitize_svg",
]

SVG_NAMESPACE = "http://www.w3.org/2000/svg"

#: A logo is a small, simple vector graphic - a handful of paths, maybe a
#: gradient. A few hundred KB covers essentially every real-world exported
#: logo with generous headroom; 2 MiB is chosen as a round upper bound that
#: still bounds the cost of parsing and walking a hostile file to something
#: trivial, while never refusing a legitimate one.
MAX_SVG_BYTES = 2 * 1024 * 1024

#: A hand-authored or typically-exported logo (a few paths, a gradient with a
#: handful of stops, some groups) uses on the order of tens to a few hundred
#: elements. 5000 gives roughly 10x headroom over a genuinely complex
#: illustration while still bounding a pathological generator's element count
#: to something the allowlist walk finishes in well under a second.
MAX_ELEMENT_COUNT = 5000

#: Real logos nest a handful of `<g>` levels at most - single digits in
#: practice. 100 gives generous headroom for an unusually structured file
#: while keeping the RECURSIVE allowlist-rebuild walk (see `_clean_children`)
#: safely within Python's default recursion limit (1000), with room to spare
#: for the interpreter's own call stack above this module.
MAX_DEPTH = 100


class SvgSanitizationError(ValueError):
    """Base for every refusal `sanitize_svg` raises. Each subclass below
    carries its own D5-shaped message (what happened, why, what to do) rather
    than a generic 'invalid SVG' - see this module's docstring and
    `api.templates.assets`/`api.templates.routes` for how a caller turns one
    into a 422.
    """


class SvgContainsDoctypeOrEntity(SvgSanitizationError):
    """XXE defence-in-depth: refused before any XML parsing touches the
    bytes."""


class SvgTooLarge(SvgSanitizationError):
    """Over `MAX_SVG_BYTES`, either as uploaded or (defensively) after
    sanitisation."""


class SvgTooComplex(SvgSanitizationError):
    """Over `MAX_ELEMENT_COUNT` elements or `MAX_DEPTH` nesting - a DoS guard
    against a pathologically shaped but otherwise well-formed file."""


class SvgMalformed(SvgSanitizationError):
    """Not parseable XML at all."""


class SvgInvalidRoot(SvgSanitizationError):
    """The root element is not a namespaced `<svg>` element."""


# =============================================================================
# 1. Pre-parse refusals - on the RAW bytes, before xml.etree ever sees them
# =============================================================================

#: Case-insensitive, tolerant of the whitespace a real file might have between
#: `<!` and the keyword (`<!  DOCTYPE`, `<!\nENTITY`, ...). This is a
#: string-level check on purpose - see the module docstring on why this is not
#: redundant with expat's own default XXE posture.
_DOCTYPE_RE = re.compile(rb"<!\s*DOCTYPE", re.IGNORECASE)
_ENTITY_RE = re.compile(rb"<!\s*ENTITY", re.IGNORECASE)


def _refuse_doctype_or_entity(data: bytes) -> None:
    if _DOCTYPE_RE.search(data) or _ENTITY_RE.search(data):
        raise SvgContainsDoctypeOrEntity(
            "this SVG contains a <!DOCTYPE or <!ENTITY declaration, which is never "
            "legitimate in a logo file and is a classic XML external-entity (XXE) "
            "attack vector. The upload is refused outright, before any part of it "
            "is parsed. Re-export the logo without a DOCTYPE, or open it in a text "
            "editor and remove the declaration yourself."
        )


def _refuse_if_too_large(data: bytes) -> None:
    if len(data) > MAX_SVG_BYTES:
        raise SvgTooLarge(
            f"this SVG is {len(data)} bytes, over the {MAX_SVG_BYTES}-byte limit for "
            f"a logo. Simplify the artwork (fewer paths/points, no embedded raster "
            f"images) or export it at a smaller size."
        )


# =============================================================================
# 2. Parsing, and the DoS guards that run on the parsed tree before any
#    recursive walk touches it
# =============================================================================


def _parse(data: bytes) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise SvgMalformed(
            f"this file is not well-formed XML ({exc}), so it cannot be a valid "
            f"SVG. Re-export the logo from your design tool."
        ) from exc


def _refuse_if_too_complex(root: ET.Element) -> None:
    """Iterative (never recursive) so this guard itself cannot be defeated by
    the very pathological nesting it exists to catch - a recursive count
    would hit Python's own recursion limit on a sufficiently deep, entity-free
    file before it ever got to report anything.
    """
    count = 0
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_ELEMENT_COUNT:
            raise SvgTooComplex(
                f"this SVG has more than {MAX_ELEMENT_COUNT} elements, which is far "
                f"more than a business logo needs and is refused as a precaution "
                f"against a maliciously oversized file. Simplify the artwork."
            )
        if depth > MAX_DEPTH:
            raise SvgTooComplex(
                f"this SVG nests elements more than {MAX_DEPTH} levels deep, which "
                f"no ordinary logo does and is refused as a precaution against a "
                f"maliciously constructed file. Flatten the artwork's group "
                f"structure and re-export it."
            )
        for child in node:
            stack.append((child, depth + 1))


def _split(tag: str) -> tuple[str | None, str]:
    """A `{namespace}local` ElementTree tag (or a bare, unnamespaced one) as
    its two parts. Used for both elements and namespaced attribute keys.
    """
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return None, tag


def _refuse_if_wrong_root(root: ET.Element) -> None:
    namespace, local = _split(root.tag)
    if local != "svg" or namespace != SVG_NAMESPACE:
        raise SvgInvalidRoot(
            f"this file's root element is {root.tag!r}, not a <svg> element "
            f"properly declaring the {SVG_NAMESPACE!r} namespace. A logo must be a "
            f"genuine, namespaced SVG document - export it again from your design "
            f"tool rather than editing it by hand."
        )


# =============================================================================
# 3. The allowlist itself
# =============================================================================

#: Real SVG spec casing matters: `linearGradient`/`radialGradient`/`clipPath`
#: are camelCase in the specification, and SVG element names are
#: case-sensitive.
ALLOWED_ELEMENTS: frozenset[str] = frozenset(
    {
        "svg",
        "g",
        "path",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "defs",
        "linearGradient",
        "radialGradient",
        "stop",
        "clipPath",
        "title",
        "desc",
        "symbol",
        "marker",
        "pattern",
        "use",
        "image",
        "text",
        "tspan",
    }
)

#: Presentation/geometry attributes only - no `style`, no event handlers (both
#: are enforced by the checks in `_clean_attributes`, not by their absence
#: here, which is defence in depth rather than the only guard). `xmlns`/
#: `xmlns:xlink` are listed for documentation fidelity with FR-TPL-018's own
#: spec even though `xml.etree.ElementTree` never surfaces a namespace
#: declaration as a regular attribute - see `sanitize_svg`, which sets the
#: output root's `xmlns` explicitly regardless.
ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "id",
        "width",
        "height",
        "viewBox",
        "xmlns",
        "xmlns:xlink",
        "version",
        "preserveAspectRatio",
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "d",
        "points",
        "transform",
        "fill",
        "fill-opacity",
        "fill-rule",
        "stroke",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-dasharray",
        "stroke-opacity",
        "opacity",
        "offset",
        "stop-color",
        "stop-opacity",
        "gradientUnits",
        "gradientTransform",
        "spreadMethod",
        "clip-path",
        "font-family",
        "font-size",
        "font-weight",
        "text-anchor",
        "dx",
        "dy",
    }
)

_JAVASCRIPT_VALUE_RE = re.compile(r"javascript\s*:", re.IGNORECASE)

#: A strict same-document fragment reference for `<use>` - `#id`, where `id`
#: is a legal-looking XML name. Deliberately does not permit a bare `id` with
#: no `#` (that is not a fragment reference at all) or anything with a scheme,
#: host or path in front of the `#` (that would be an external document).
_USE_FRAGMENT_RE = re.compile(r"^#[A-Za-z][\w.:-]*$")

#: `data:image/png;base64,...` / `data:image/jpeg;base64,...` only. Strict
#: about the MIME type so `data:image/svg+xml` - which would let an `<image>`
#: re-embed another SVG and defeat everything this module just did - is
#: refused exactly like an external URL would be.
_DATA_RASTER_RE = re.compile(r"^data:image/(?:png|jpeg);base64,[A-Za-z0-9+/=\s]+$", re.IGNORECASE)


def _clean_href(tag: str, value: str) -> str | None:
    """`href`'s bespoke, per-element rule - see the module docstring's
    "What is deliberately NOT supported" section. Returns the value to keep,
    or `None` to drop the attribute entirely.
    """
    if tag == "use":
        return value if _USE_FRAGMENT_RE.match(value) else None
    if tag == "image":
        return value if _DATA_RASTER_RE.match(value) else None
    # Every other element: dropped unconditionally, never on the generic
    # attribute allowlist at all.
    return None


def _clean_attributes(source: ET.Element, dest: ET.Element, tag: str) -> None:
    for key, value in source.attrib.items():
        _namespace, local = _split(key)

        # Defence in depth, checked before anything else and regardless of
        # attribute name: an event handler or a `javascript:` value is wrong
        # everywhere, on any element, on any attribute.
        if local.lower().startswith("on"):
            continue
        if _JAVASCRIPT_VALUE_RE.search(value):
            continue

        if local == "href":
            cleaned = _clean_href(tag, value)
            if cleaned is not None:
                dest.set("href", cleaned)
            continue

        if local == "style":
            # No CSS support at all - see the module docstring's trade-off
            # note. This is what eliminates style="...url(...)..." and the
            # whole @import/expression() class of CSS attacks.
            continue

        if local not in ALLOWED_ATTRIBUTES:
            continue

        dest.set(local, value)


def _clean_children(source: ET.Element, dest: ET.Element) -> None:
    """Rebuilds `dest`'s children from `source`'s, keeping only what the
    element and attribute allowlists permit. An element that fails the
    allowlist is dropped ENTIRELY along with its whole subtree - this
    function never recurses into a dropped element looking for anything to
    save, which is what makes `<foreignObject><body>...<script>...`
    disappear as one unit rather than leaking its inner text content.

    Recursive, safely: `_refuse_if_too_complex` has already bounded the
    source tree's depth to `MAX_DEPTH` (100) before this is ever called.
    """
    for child in source:
        namespace, local = _split(child.tag)
        # Namespace-aware AND namespace-strict: an element must be an actual
        # member of the SVG namespace, not merely have a locally-recognised
        # name. This is what defeats namespace-prefix smuggling - an
        # `xhtml`-namespaced `<script>` declared under an `svg`-prefixed
        # document is rejected here regardless of its local name, and would
        # be rejected by the element-name check below anyway since "script"
        # is not on the allowlist either.
        if namespace != SVG_NAMESPACE or local not in ALLOWED_ELEMENTS:
            continue

        new_child = ET.Element(local)
        # Character data - the actual visible content of `<title>`, `<desc>`,
        # `<text>` and `<tspan>` (a shape element like `<path>` has none to
        # speak of, so this is a harmless no-op for those). Plain text, never
        # interpreted as markup by an SVG renderer, so copying it verbatim
        # carries none of the risk copying an ATTRIBUTE or a whole subtree
        # would.
        new_child.text = child.text
        _clean_attributes(child, new_child, local)

        if local == "image" and "href" not in new_child.attrib:
            # An <image> with no valid source draws nothing useful - see the
            # module's FR-TPL-018 spec note. Dropped along with any (
            # meaningless, per the SVG spec) children it might carry.
            continue

        dest.append(new_child)
        _clean_children(child, new_child)


def sanitize_svg(data: bytes) -> bytes:
    """Strip an uploaded SVG to its allowlisted, safe subset - FR-TPL-018.

    Raises a specific `SvgSanitizationError` subclass, with a D5-shaped
    message, for every refusal case; returns clean, UTF-8 SVG bytes on
    success. See the module docstring for the full threat model and what is
    deliberately out of scope.
    """
    _refuse_doctype_or_entity(data)
    _refuse_if_too_large(data)

    root = _parse(data)
    _refuse_if_too_complex(root)
    _refuse_if_wrong_root(root)

    cleaned_root = ET.Element("svg")
    _clean_attributes(root, cleaned_root, "svg")
    # Always set explicitly: xml.etree.ElementTree never surfaces a source
    # document's `xmlns` declaration as a regular attribute (the parser
    # consumes it for namespace resolution instead), so the allowlist walk
    # above can never have copied one - the output would otherwise be an
    # <svg> element that is not actually in the SVG namespace, which
    # `_refuse_if_wrong_root` would itself refuse if this sanitiser were run
    # on its own output.
    cleaned_root.set("xmlns", SVG_NAMESPACE)
    _clean_children(root, cleaned_root)

    # `encoding="utf-8"` (any encoding other than the literal string
    # `"unicode"`) is what makes `tostring` return `bytes` rather than `str`
    # - true at runtime, but the stub's return type is the union of both
    # overloads, hence the explicit narrowing here for mypy.
    output = ET.tostring(cleaned_root, encoding="utf-8")
    assert isinstance(output, bytes)

    # Defensive, not expected to ever fire: sanitisation only removes content,
    # so the output should never exceed the (already size-checked) input. Kept
    # anyway per FR-TPL-018/SEC-005's "size-capped" - a size check that trusts
    # its own function to behave correctly is not a check.
    _refuse_if_too_large(output)

    return output
