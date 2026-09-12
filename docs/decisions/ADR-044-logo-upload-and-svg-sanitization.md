# ADR-044: Logo upload and SVG sanitisation

- **Status**: Accepted
- **Date**: 2026-09-12
- **Implements**: FR-TPL-001 (logo upload, position, size, transparent-background preview),
  FR-TPL-018 (SVG sanitisation)
- **Serves**: SEC-005 (file upload posture), SEC-006 (parser isolation reasoning, applied by
  analogy)
- **Constrained by**: CLAUDE.md non-negotiable #4 (integrations behind adapters), ADR-030 (the
  document archive's upload pipeline shape and SEC-005 posture, mirrored here), ADR-041/ADR-042
  (the `InvoiceTemplate`/`TemplatedPdfRenderer` this closes a gap in), the hard constraint that no
  XML/SVG/image library is available in this project's dependency set
- **Related**: [ADR-030](ADR-030-document-storage.md) — the upload pipeline shape and
  `BlobStore`/`MalwareScanner` this reuses directly; [ADR-041](ADR-041-invoice-template-model.md) —
  the template model and migration 0042's `template_asset` table; [ADR-042](ADR-042-invoice-pdf-rendering.md)
  — the one renderer (FR-TPL-008) this wires a real `resolve_logo` into

## Context

> **FR-TPL-001.** Logo upload (PNG, JPEG, SVG), with position (left, centre, right), size control
> and a transparent-background preview against the paper colour.
>
> **FR-TPL-018.** Uploaded logos are validated and sanitised — SVG is stripped of scripts and
> external references before storage or rendering.
>
> **SEC-005.** File uploads: type verified by content not extension, size-capped, malware-scanned,
> stored outside the web root, served from a separate origin with `Content-Disposition: attachment`.

Migration 0042 (ADR-041) already created `template_asset` and modelled `Logo.asset_id` on
`InvoiceTemplate`, and ADR-042 already gave `TemplatedPdfRenderer` a `resolve_logo` seam - but
named both as gaps: no upload endpoint existed, and nothing called `resolve_logo` in production.
This ADR closes both, and adds the one genuinely new, security-critical piece neither prior ADR
needed: accepting SVG at all. `api.documents.content_type` refuses SVG outright, calling it "a
script host wearing an image's name" - correct for a scanned receipt, wrong for FR-TPL-001, which
explicitly asks for it. The difference is what happens next: a document is stored and shown as-is;
a logo SVG is sanitised first.

Also settled here, after asking the user directly: **SVG-to-PDF vector rendering is out of scope
for this pass.** `api.invoicing.pdf` has zero vector-drawing capability - only text, lines, and
raster PNG/JPEG image XObjects - and building a full SVG-to-PDF converter (parsing path data,
gradients, transforms, and re-emitting them as PDF content-stream operators) is a different, much
larger piece of work than a logo upload feature. Scope is: PNG/JPEG logos are fully embedded in the
real issued PDF; an SVG logo is fully uploaded, sanitised, stored, and shown correctly in the
designer's own `<img>`/object-URL preview (browsers render SVG natively - no backend vector
support needed for that); at PDF-issue time and in the FR-TPL-008 preview, a template whose logo is
an SVG is refused with a specific, actionable message, not silently skipped.

## Decision

### 1. A parallel, narrower content-type module - not a change to `api.documents`

`api.templates.asset_content_type.TemplateAssetContentType` accepts exactly PNG, JPEG and SVG,
mirroring `api.documents.content_type`'s conventions (an enum of accepted types, a `sniff()` that
never looks at a filename, D5-shaped refusal messages) without reusing its code. `api.documents`
keeps refusing SVG outright, unconditionally correctly, for source documents; `api.templates`
accepts it because FR-TPL-001 asks for it and FR-TPL-018 exists to make that safe.

SVG has no fixed byte signature (it is XML text, and a conforming file may legally open with a
BOM, an XML declaration, a comment or a DOCTYPE before `<svg>` itself), so `sniff()` looks for the
*shape* of an XML/SVG opening among a small set of legal prefixes rather than a single magic
sequence - still "verified by content, not extension," just over a text format's real grammar
instead of a fixed byte string. A DOCTYPE-laden or hostile SVG-shaped file is still sniffed as SVG
here; the DOCTYPE refusal belongs to the sanitiser (below), and duplicating a weaker version of it
here would only create a second place for the real gate to drift.

### 2. The sanitiser: allowlist, hand-rolled against `xml.etree.ElementTree`

No SVG/XML/image library exists in this project's dependency set (verified: Pillow, cairosvg,
defusedxml, lxml and svglib are all absent from `apps/api/.venv-check`). `api.templates.
svg_sanitizer.sanitize_svg` is therefore hand-rolled against the standard library's
`xml.etree.ElementTree` - consistent with how `api.invoicing.pdf`/`truetype` already hand-roll PDF
and TrueType rather than pulling in a library for a narrow, well-understood format.

**Posture: allowlist, not denylist** - the standard, correct shape for this exact problem (the same
one OWASP's guidance and DOMPurify's SVG mode take), because a denylist has to be exhaustively
complete to be correct and SVG's own history of new elements and browser quirks means it never is.

In order:

1. **Pre-parse refusal, on the raw bytes.** Any `<!DOCTYPE` or `<!ENTITY` (case-insensitive,
   whitespace-tolerant) is refused OUTRIGHT before `ET.fromstring` ever sees the bytes. This is XXE
   defence-in-depth: Python's expat does not resolve external entities by default, but a hostile
   DOCTYPE is never legitimate in a logo file, and refusing outright is simpler and safer than
   relying on a default staying true forever.
2. **Size cap: 2 MiB** (`MAX_SVG_BYTES`), checked on the raw upload and, defensively, again on the
   sanitised output. A logo is a small, simple vector graphic - real exports run from a few KB to a
   few hundred; 2 MiB is generous headroom while bounding the cost of parsing a hostile file.
3. **Complexity caps: 5000 elements, 100 levels of nesting** (`MAX_ELEMENT_COUNT`, `MAX_DEPTH`),
   checked with an ITERATIVE walk (a stack, not recursion) before anything recursive touches the
   tree - so the DoS guard itself cannot be defeated by the nesting it exists to catch. A
   hand-authored or exported logo uses tens to a few hundred elements and nests a handful of levels
   deep; both caps give roughly 10x headroom over genuinely complex real artwork.
4. **Root element check**: local name `svg`, namespace exactly `http://www.w3.org/2000/svg` -
   refusing a bare/no-namespace root too.
5. **Comments and processing instructions**: never reach the tree at all - `ET.fromstring`'s
   default `TreeBuilder` does not add them (no `insert_comments`/`insert_pis` requested), so there
   is nothing further to strip.
6. **Element allowlist** (namespace-aware AND namespace-STRICT - an element must actually be in the
   SVG namespace, not merely share a locally-recognised name, which is what defeats a
   namespace-prefix-smuggled `<script>`): `svg, g, path, rect, circle, ellipse, line, polyline,
   polygon, defs, linearGradient, radialGradient, stop, clipPath, title, desc, symbol, marker,
   pattern, use, image, text, tspan`. Anything else - `script`, `foreignObject`, every SMIL
   animation element (`animate`, `set`, ...), `style`, `a`, `video`, `audio`, `iframe`, `object`,
   `embed` and anything simply not on the list - is dropped ENTIRELY with its whole subtree; the
   walk never recurses into a dropped element looking for anything to save. Character data
   (`.text`) on a surviving element IS copied (needed for `<title>`/`<desc>`/`<text>`/`<tspan>` to
   mean anything; harmless for a shape element, which has none).
7. **Attribute allowlist**: `id, width, height, viewBox, xmlns, xmlns:xlink, version,
   preserveAspectRatio, x, y, x1, y1, x2, y2, cx, cy, r, rx, ry, d, points, transform, fill,
   fill-opacity, fill-rule, stroke, stroke-width, stroke-linecap, stroke-linejoin,
   stroke-dasharray, stroke-opacity, opacity, offset, stop-color, stop-opacity, gradientUnits,
   gradientTransform, spreadMethod, clip-path, font-family, font-size, font-weight, text-anchor,
   dx, dy`. Every other attribute is dropped (the element survives). `on*` (case-insensitive) is
   dropped UNCONDITIONALLY, on every element, regardless of the allowlist above. `style` is
   dropped ENTIRELY, always - no CSS support at all, which is what removes
   `style="...url(...)..."` and the whole `@import`/`expression()` class of attack in one cut. Any
   attribute value containing `javascript:` (case-insensitive) is dropped regardless of which
   attribute carries it, as defence in depth beyond the href-specific handling below.
8. **`href`/`xlink:href`: bespoke, not generic.** On `<use>`, only a strict local fragment
   (`^#[A-Za-z][\w.:-]*$`) survives - an external `xlink:href` is dropped. On `<image>`, only a
   `data:image/png;base64,...` or `data:image/jpeg;base64,...` URI survives -
   `data:image/svg+xml` is refused explicitly (it would let a sanitised SVG re-embed another,
   unsanitised one), and an `<image>` left with no valid `href` is dropped entirely (it draws
   nothing useful). On every other element, `href` is dropped unconditionally.
9. Serialised with `ET.tostring(..., encoding="utf-8")`, with an explicit `xmlns` set on the output
   root (ElementTree never surfaces a source document's namespace declaration as a regular
   attribute, so the allowlist walk can never have copied one).

Named out of scope, explicitly: no CSS support at all (a logo needing style should use presentation
attributes), no animation, no filters/masks, no scripting surface of any kind. A logo needs none of
these, and each is a well-documented SVG XSS vector in its own right.

### 3. Reuse of the document pipeline's storage and scanning, not its content-type or service code

`api.templates.assets.TemplateAssetService` runs the SAME pipeline shape ADR-030 established for
documents - `authorize -> cap size -> sniff -> (SVG only) sanitize -> scan -> store (encrypted) ->
record -> audit` - reusing `api.documents.storage` (`BlobStore`/`EncryptedBlobStore`/
`new_storage_key`) and `api.documents.scanning` (`MalwareScanner`/`build_scanner`) directly, since
both are already tenant-scoped and provider-agnostic. It does NOT reuse `api.documents.
content_type` (refuses SVG, by design) or `api.documents.service` (owns retention/`scan_status`
concepts a logo does not have).

The sanitiser runs BEFORE the malware scan and BEFORE storage: the bytes that get scanned and
stored are the SANITISED bytes, never the raw upload - scanning the raw upload and storing the
sanitised result would mean the thing judged and the thing kept are not the same thing, and the
`template_asset.sanitized` column would be lying about what happened.

`template_asset` (0042) carries `sanitized boolean`, not `scan_status` the way `document` does -
there is no quarantine state for a logo, because a logo is not evidence FR-DOC-005 requires
retaining. A non-clean scan or an unsanitisable SVG simply means nothing is stored at all.

### 4. Upload endpoint: a raw body, not multipart - mirroring `api.documents.routes` exactly

`POST /v1/administrations/{administration_id}/template-assets` takes the raw file bytes with
`Content-Type` describing them, the identical shape `api.documents.routes.upload_document` takes
and for the identical reason (see that route's own docstring): multipart needs a parser running
over attacker-controlled bytes before anything has decided to accept them, and a raw body costs a
client nothing (`fetch(url, {method: 'POST', body: file, headers: {'Content-Type': file.type}})`).
Idempotency is enforced globally by `IdempotencyMiddleware` for every mutating method; the route
needs no opt-in.

`GET .../template-assets/{asset_id}` mirrors `api.documents.routes.download_document`'s SEC-005
posture exactly, including reusing `verify_separate_origin_configured` directly rather than a
second, independently-drifting copy of the same enforcement.

Both routes reuse the IDENTICAL `require_permission("create", "sales_invoice", ...)` every other
template-designer route declares (ADR-041's reasoning: a logo is part of what "shaping what a
sales invoice looks like" already means).

### 5. Wiring `resolve_logo` for real, and refusing an SVG logo with a specific message

`api.templates.assets.build_resolve_logo(administration_id, session)` builds the hook
`TemplatedPdfRenderer` has always accepted (ADR-042) but nothing supplied in production. Both call
sites that construct a real renderer - `api.invoicing.routes.get_invoicing_service` (issue) and
`api.templates.routes.get_template_service` (preview) - now build one and pass it through
`build_invoice_renderer`'s new optional `resolve_logo` parameter, so FR-TPL-008's "one engine"
extends to the logo path too.

For PNG/JPEG: fetch the `template_asset` row, decrypt the blob, and call `api.invoicing.pdf.
load_image` - `Image.fit()` sizes into a box chosen from `LogoSize` (small 28pt / medium 42pt /
large 64pt tall, aspect-preserved; already implemented in `_lay_out`), positioned by `LogoPosition`
against the existing `_MARGIN`/`_RIGHT` constants (also already implemented). Nothing new to build
here - the layout math has existed since ADR-042; the gap was only that `resolve_logo` was never
wired to anything real.

For SVG: `LogoNotRenderable` is raised, with the message **"this template's logo is an SVG, which
cannot yet be embedded in a rendered PDF; upload a PNG or JPEG version to include a logo on issued
invoices"** - D5-shaped (what happened, why, what to do). Caught at BOTH call sites that actually
render a document (`issue_invoice`, `preview_template`) and turned into a 409 with reason
`invoice_logo_not_renderable` - never silently skipped (which would misrepresent a real,
user-visible gap as success) and never a generic error.

## Alternatives considered

| Option | Rejected because |
|---|---|
| Build SVG-to-PDF vector rendering now | A different, much larger piece of work (path/gradient/transform parsing and re-emission as PDF operators) than a logo upload feature; explicitly descoped after asking the user. |
| Reuse `api.documents.content_type` and add SVG to its allowlist | That module's whole point is refusing SVG outright for source documents ("a script host wearing an image's name"); weakening it would weaken every OTHER upload that goes through it, for a need only logos have. |
| A denylist sanitiser (strip known-bad tags/attributes) | Has to be exhaustively complete to be correct, and SVG's attack surface (new elements, browser quirks, CSS vectors) keeps growing. The allowlist is the standard, correct shape for this problem. |
| Trust expat's default XXE posture and skip the pre-parse DOCTYPE check | The pre-parse check is required regardless of what a parser does by default (the task's own explicit instruction) - a logo file has no legitimate reason to declare a DOCTYPE at all, and refusing outright is simpler than depending on a default staying true forever. |
| Store the raw upload, sanitise lazily at render/preview time | Store-then-sanitise leaves a window where unsanitised bytes exist and could be served before anything has judged them - the same "store-then-scan leaves a window" argument ADR-030 already makes about malware scanning, applied to sanitisation. |
| Add `scan_status`/quarantine to `template_asset`, matching `document` | A logo is not evidence FR-DOC-005 requires retaining; quarantining an infected or unsanitisable logo would add a state with nothing to do once entered. Refuse outright, store nothing. |
| Multipart form upload | Needs a parser running over attacker-controlled bytes before anything has decided to accept it - the identical reasoning ADR-030 already recorded for documents, reused here rather than re-argued. |
| A pip-installed sanitiser (e.g. `bleach`, a defusedxml-based tool) | None is available in this project's dependency set today, and adding one could not be locked/verified in this pass - consistent with the codebase's established "hand-roll narrow, well-understood formats" ethos (`api.invoicing.pdf`, `api.invoicing.truetype`). |

## Consequences

**Easier.** A logo upload is now a real, sanitised, scanned, encrypted-at-rest asset with its own
tenant-scoped table, reachable from the designer and from the real issued PDF (for PNG/JPEG). Adding
a fourth accepted raster format later is one signature in `asset_content_type.py`.

**Harder.** Two upload pipelines now exist with almost-identical shape (`api.documents.service`,
`api.templates.assets`) that must be kept in step by hand rather than by sharing code, because their
accepted-type and retention semantics are genuinely different. This mirrors a trade-off ADR-030
already accepted for its own reasons (two retention-rule implementations); the same argument applies
here.

**Known gaps.**

- **SVG-to-PDF vector rendering.** The chief, deliberate gap this ADR exists to name honestly.
  Closing it needs a real path/gradient/transform-to-PDF-operator converter (or a rasterisation
  step at a fixed DPI), a different and larger piece of work than this pass, and is not started
  here beyond the refusal message telling a user what to do instead.
- **No re-sanitisation of already-stored assets.** A future change to the allowlist does not
  retroactively re-run against rows sanitised under an older version of this module. Not a
  regression risk today (nothing loosens between versions), but worth naming for the day the
  allowlist changes.
- **The designer's own preview trusts the browser's native SVG renderer.** `<img>`/object-URL
  rendering of a sanitised SVG is safe because the SANITISER already removed everything a browser
  could execute - this is not a second layer of sandboxing, it is the same guarantee the sanitiser
  already gives, observed a second way.
- **No thumbnailing or re-encoding of PNG/JPEG logos.** A very large but under-the-cap raster logo
  is stored and embedded exactly as uploaded; FR-TPL-001 does not ask for optimisation, and adding
  it would be new scope.
