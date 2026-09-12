"""Translation completeness, enforced in CI - FR-LOC-001d, FR-LOC-001,
FR-LOC-001c, FR-UX-007.

    FR-LOC-001d  Translation completeness is enforced in CI: a build fails if
                 any user-facing string lacks a translation in either
                 language.
    FR-LOC-001   A missing translation is a release blocker, not a fallback to
                 English.

--- Why this is the load-bearing piece ---

Both runtimes RAISE on a message they cannot resolve, rather than falling back
from Dutch to English (packages/i18n/src/catalogue.ts and
apps/api/src/api/i18n/catalogue.py). That is the posture FR-LOC-001 demands and
it is only defensible because this check runs first: the build fails on a
missing string before anybody's request can.

So the ordering matters. Without this script, "raises at runtime" is reckless.
With it, a fallback would be the reckless choice - it would hide from every
environment where somebody might have noticed.

--- What it checks ---

Shape of the catalogue:

  1. Every file parses, declares its namespace, and every key in it carries
     that namespace as a prefix. Keys are therefore greppable back to a file.
  2. No key appears in two files (whichever loaded second would silently win).
  3. Every message has BOTH languages, non-empty, neither a placeholder
     (`TODO`, `FIXME`, `???`, `TBD`, `XXX`).
  4. A record whose two languages are identical says `"identical": true`.
     Legitimate for endonyms and statutory terms; indistinguishable, without
     the flag, from pasting the English into the Dutch slot to make a build
     pass. Legal but never silent.
  5. Placeholders match between the languages. A `{query}` present in one and
     absent in the other is a sentence with a hole in it or a stray brace on
     screen.
  6. Counted messages have the same shape in both languages and carry both
     CLDR categories Dutch and English use.
  7. `api.i18n.catalogue.CATALOGUE_FILES` lists exactly the files present, so
     a namespace the web app serves cannot be one the API has never loaded.

FR-LOC-001c, statutory terminology:

  8. A glossary term marked `keepDutch` and present in a message's Dutch text
     appears, spelled identically, in its English. This is what makes "do not
     translate BTW to VAT" a build failure rather than a style note.

Usage in code:

  9. Every message key referenced by a string literal in the web or API
     source exists in the catalogue. A key that does not is a raw identifier
     on somebody's screen (FR-UX-007).
 10. No hard-coded user-facing text in the web app's JSX, and none in the
     API's HTTP responses. These are the checks that keep the product from
     drifting back to "English with translations bolted on": an untranslated
     literal is not a missing translation the other checks can see, because it
     never reached the catalogue at all.

     Both apps need one, and for the same reason. A sentence a person reads is
     equally permanent whether it was typed into a component or into an
     `HTTPException(detail=...)`, and FR-UX-007 is explicit that
     developer-facing strings never reach a user. Writing the JSX check first
     and stopping there is what let three untranslated 403s survive the
     original pass, including IAM-010f's "add a passkey or password" prompt —
     which is an instruction, and an instruction a person cannot read is not
     one.

--- Not checked, and why ---

An UNUSED key is not an error. A key may legitimately be referenced from a
surface this script does not read - a mobile app, an e-mail template, a
future PDF renderer - and dynamically (`t(errorKey)`) besides. Failing on
"unused" would either be wrong or would push people to reference keys
artificially, and neither makes a translation more complete.

--- Usage ---

    python scripts/check_translations.py          # check; CI runs this
    python scripts/check_translations.py --list   # print the catalogue

Exit codes: 0 complete, 1 problems found, 2 could not run. Stdlib only and no
database, so it runs on every CI invocation - the same design as the four
route-coverage checks in apps/api/tests.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_DIR = REPO_ROOT / "packages" / "i18n" / "catalogue"
GLOSSARY_FILE = CATALOGUE_DIR / "glossary.json"
API_CATALOGUE_MODULE = REPO_ROOT / "apps" / "api" / "src" / "api" / "i18n" / "catalogue.py"

#: Where a message key might be referenced. Both apps, because the catalogue
#: is shared and a key typo is equally fatal on either side.
SOURCE_ROOTS = (
    REPO_ROOT / "apps" / "web" / "src",
    REPO_ROOT / "apps" / "api" / "src",
    REPO_ROOT / "packages" / "i18n" / "src",
)

LANGUAGES = ("nl", "en")

#: CLDR's categories for Dutch and English. Both, or neither.
PLURAL_CATEGORIES = ("one", "other")

PLACEHOLDER_TEXT = re.compile(r"\b(TODO|FIXME|XXX|TBD)\b|\?{3,}", re.IGNORECASE)
PLACEHOLDER = re.compile(r"\{(\w+)\}")

EXIT_OK, EXIT_PROBLEMS, EXIT_CANNOT_RUN = 0, 1, 2


class Problems:
    """Collects every failure rather than stopping at the first.

    A translator or an engineer fixing these wants the whole list in one
    build, not one item per CI run.
    """

    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, where: str, message: str) -> None:
        self.items.append(f"  {where}\n      {message}")

    def __bool__(self) -> bool:
        return bool(self.items)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def catalogue_files() -> list[Path]:
    return sorted(p for p in CATALOGUE_DIR.glob("*.json") if p != GLOSSARY_FILE)


def load(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read {path.relative_to(REPO_ROOT)}: {exc}") from exc


def text_forms(value: object) -> list[str]:
    """Every string a message record's language slot contains - one for a
    plain message, one per plural category for a counted one.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [v for v in value.values() if isinstance(v, str)]
    return []


# ---------------------------------------------------------------------------
# Checks 1-7: the catalogue's shape
# ---------------------------------------------------------------------------


def check_catalogue(problems: Problems) -> dict[str, dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    origin: dict[str, str] = {}

    for path in catalogue_files():
        name = path.name
        document = load(path)

        namespace = document.get("namespace")
        if not isinstance(namespace, str) or not namespace:
            problems.add(name, "no `namespace` field; keys cannot be traced to a file")
            continue

        messages = document.get("messages")
        if not isinstance(messages, dict):
            problems.add(name, "no `messages` object")
            continue

        for key, record in messages.items():
            where = f"{name} :: {key}"

            if not key.startswith(f"{namespace}."):
                problems.add(
                    where,
                    f"key does not start with its namespace ({namespace}.). Keys are "
                    f"prefixed so a message on screen can be grepped back to the "
                    f"file that owns it.",
                )
            if key in merged:
                problems.add(
                    where,
                    f"already defined in {origin[key]}. Whichever file loaded second "
                    f"would silently win, making the message depend on load order.",
                )
                continue

            merged[key], origin[key] = record, name

            if not isinstance(record, dict):
                problems.add(where, "not an object")
                continue

            check_record(problems, where, record)

    check_api_file_list(problems)
    return merged


def check_record(problems: Problems, where: str, record: dict[str, object]) -> None:
    # --- 3: both languages, present and real ------------------------------
    for language in LANGUAGES:
        if language not in record:
            problems.add(
                where,
                f"has no `{language}`. A message is ONE record holding both "
                f"languages (FR-LOC-001); there is no file to add the other to "
                f"later, and no fallback that would hide this.",
            )
            continue

        forms = text_forms(record[language])
        if not forms:
            problems.add(where, f"`{language}` is not a string or a {{one, other}} object")
        for form in forms:
            if not form.strip():
                problems.add(where, f"`{language}` is empty")
            elif PLACEHOLDER_TEXT.search(form):
                problems.add(
                    where,
                    f"`{language}` still reads {form.strip()!r}. A placeholder is how "
                    f'"I will do the Dutch later" ships; write the string or leave the '
                    f"message out.",
                )

    if not all(language in record for language in LANGUAGES):
        return

    dutch, english = record["nl"], record["en"]

    # --- 6: the two languages have the same shape -------------------------
    if isinstance(dutch, str) != isinstance(english, str):
        problems.add(
            where,
            "one language is a plain string and the other is a counted "
            "{one, other} object. Dutch and English share CLDR's categories, so a "
            "message is counted in both or in neither.",
        )
        return

    if isinstance(dutch, dict) and isinstance(english, dict):
        for language, forms in (("nl", dutch), ("en", english)):
            missing = [c for c in PLURAL_CATEGORIES if c not in forms]
            if missing:
                problems.add(where, f"`{language}` is missing plural form(s): {missing}")
            extra = [c for c in forms if c not in PLURAL_CATEGORIES]
            if extra:
                problems.add(
                    where,
                    f"`{language}` declares plural categor(ies) {extra}, which neither "
                    f"Dutch nor English has. Both use exactly {list(PLURAL_CATEGORIES)}.",
                )

    # --- 4: identical languages are legal, never silent -------------------
    declared_identical = record.get("identical") is True
    actually_identical = dutch == english
    if actually_identical and not declared_identical:
        problems.add(
            where,
            'the two languages are identical but the record does not say `"identical": '
            "true`. That is legitimate for an endonym (Nederlands), a proper noun or a "
            "statutory term that keeps its Dutch form - and it is exactly what pasting "
            "the English into the Dutch slot looks like. Say so explicitly.",
        )
    elif declared_identical and not actually_identical:
        problems.add(
            where,
            "declares `identical: true` but the two languages differ. Remove the flag - "
            "a stale one would let a later divergence go unremarked.",
        )

    # --- 5: the same placeholders on both sides ---------------------------
    dutch_names = {n for form in text_forms(dutch) for n in PLACEHOLDER.findall(form)}
    english_names = {n for form in text_forms(english) for n in PLACEHOLDER.findall(form)}
    if dutch_names != english_names:
        problems.add(
            where,
            f"placeholders differ: nl has {sorted(dutch_names)}, en has "
            f"{sorted(english_names)}. One of the two renders with a hole in the "
            f"sentence or a literal brace on screen.",
        )


def check_api_file_list(problems: Problems) -> None:
    """Check 7. `api.i18n.catalogue.CATALOGUE_FILES` names its files rather
    than globbing, so that a file present on disk and absent from the list is
    a namespace the web app serves and the API has never heard of. That is
    only safe if something compares the two.
    """
    try:
        source = API_CATALOGUE_MODULE.read_text(encoding="utf-8")
    except OSError as exc:
        problems.add("apps/api/.../i18n/catalogue.py", f"cannot read: {exc}")
        return

    match = re.search(r"CATALOGUE_FILES\s*=\s*\(([^)]*)\)", source)
    if match is None:
        problems.add("apps/api/.../i18n/catalogue.py", "cannot find CATALOGUE_FILES")
        return

    listed = set(re.findall(r'"([^"]+)"', match.group(1)))
    present = {p.name for p in catalogue_files()}
    if listed != present:
        problems.add(
            "apps/api/.../i18n/catalogue.py",
            f"CATALOGUE_FILES is {sorted(listed)} but the catalogue holds "
            f"{sorted(present)}. A file the API does not load is a namespace the web "
            f"app serves and the API raises on.",
        )


# ---------------------------------------------------------------------------
# Check 8: statutory terminology (FR-LOC-001c)
# ---------------------------------------------------------------------------


REVIEW_STATUSES = ("proposed", "flagged", "reviewed")
REVIEW_CONFIDENCES = ("high", "medium", "low")


def check_term_review(problems: Problems, where: str, entry: dict[str, object]) -> None:
    """FR-LOC-001c's other half.

    The requirement is two claims, not one: the terminology is authoritative,
    AND it is reviewed by a practising Dutch accountant. Only the first is
    expressible as a string in a file. This makes the second visible in the
    data — every term records whether it has been reviewed and what it still
    needs decided — instead of in a comment nobody re-reads.

    The build does NOT fail on an unreviewed term. It cannot: a CI gate that
    blocks on a human process that has not been scheduled just gets disabled,
    and then the far more valuable completeness checks go with it. What it
    fails on is a term with no review block at all, or a flagged term with no
    question attached, because both mean the uncertainty was recorded nowhere.
    """
    review = entry.get("review")
    if not isinstance(review, dict):
        problems.add(
            where,
            "has no `review` block. FR-LOC-001c requires terminology reviewed by a "
            "practising Dutch accountant, so every term records where it stands: "
            "status, confidence, who reviewed it, and what is still open.",
        )
        return

    status = review.get("status")
    if status not in REVIEW_STATUSES:
        problems.add(where, f"review.status is {status!r}; expected one of {REVIEW_STATUSES}")
    if review.get("confidence") not in REVIEW_CONFIDENCES:
        problems.add(
            where,
            f"review.confidence is {review.get('confidence')!r}; expected one of "
            f"{REVIEW_CONFIDENCES}",
        )

    questions = review.get("questions")
    if not isinstance(questions, list):
        problems.add(where, "review.questions must be a list, empty if nothing is open")
    elif status == "flagged" and not questions:
        problems.add(
            where,
            "is flagged but asks nothing. A flag without a question is a worry that "
            "cannot be answered, and it will be cleared by whoever tires of seeing it.",
        )

    if status == "reviewed" and not review.get("reviewedBy"):
        problems.add(
            where,
            "claims to be reviewed but names no reviewer. FR-LOC-001c's review is by a "
            "specific person, and an unattributable sign-off is not one.",
        )


def check_glossary(problems: Problems, messages: dict[str, dict[str, object]]) -> None:
    document = load(GLOSSARY_FILE)
    terms = document.get("terms")
    if not isinstance(terms, dict):
        problems.add(GLOSSARY_FILE.name, "no `terms` object")
        return

    kept: list[str] = []
    for term_id, entry in terms.items():
        where = f"{GLOSSARY_FILE.name} :: {term_id}"
        if not isinstance(entry, dict):
            problems.add(where, "not an object")
            continue

        term = entry.get("term")
        if not isinstance(term, str) or not term:
            problems.add(where, "no `term`")
            continue

        definition = entry.get("definition")
        if isinstance(definition, dict):
            check_record(problems, f"{where} :: definition", definition)
        else:
            problems.add(
                where,
                "no bilingual `definition`. FR-LOC-001c asks for a hover or tap "
                "definition, which is itself user-facing text and needs both languages.",
            )

        check_term_review(problems, where, entry)

        if entry.get("keepDutch") is True:
            kept.append(term)

    # A term cannot be half-decided in two places: `candidates` is the list of
    # terms with no treatment chosen, so an id appearing in both would mean the
    # decision was made and the open question left standing beside it.
    candidates = document.get("candidates")
    if isinstance(candidates, dict):
        overlap = sorted(set(candidates.get("terms", {})) & set(terms))
        if overlap:
            problems.add(
                GLOSSARY_FILE.name,
                f"{overlap} appear both as decided terms and as undecided candidates. "
                f"A term is one or the other.",
            )

    for key, record in messages.items():
        if not isinstance(record, dict) or not all(lang in record for lang in LANGUAGES):
            continue
        dutch = " ".join(text_forms(record["nl"]))
        english = " ".join(text_forms(record["en"]))
        for term in kept:
            boundary = rf"(?<!\w){re.escape(term)}(?!\w)"
            if re.search(boundary, dutch) and not re.search(boundary, english):
                problems.add(
                    f"{key}",
                    f"the Dutch uses the statutory term {term!r} and the English does "
                    f"not. FR-LOC-001c keeps such terms in their Dutch form in the "
                    f"English UI - translating BTW to VAT makes an English reader "
                    f"reason about their own country's rules. If an accurate "
                    f"equivalent really does exist, drop keepDutch from the glossary "
                    f"entry and say why there.",
                )


# ---------------------------------------------------------------------------
# Checks 9-10: how the code uses it
# ---------------------------------------------------------------------------


def source_files() -> list[Path]:
    files: list[Path] = []
    for root in SOURCE_ROOTS:
        if not root.is_dir():
            continue
        for pattern in ("*.ts", "*.tsx", "*.py"):
            files.extend(
                path
                for path in root.rglob(pattern)
                if "node_modules" not in path.parts and "__pycache__" not in path.parts
            )
    return sorted(files)


def is_test(path: Path) -> bool:
    return (
        path.name.startswith("test_")
        or ".test." in path.name
        or ".spec." in path.name
        or "tests" in path.parts
    )


def check_keys_used_exist(
    problems: Problems, messages: dict[str, dict[str, object]], sources: list[Path]
) -> None:
    """Check 9.

    Rather than matching call shapes - `t(...)`, `translate(...)`,
    `problem(request, 403, ...)`, `message(...)` - this matches any string
    literal that LOOKS like a message key: dotted, lower case, and beginning
    with a namespace the catalogue actually declares. That covers every call
    shape at once, including ones nobody has written yet, and the namespace
    requirement is what keeps it from flagging ordinary dotted strings.
    """
    namespaces = {key.split(".")[0] for key in messages}
    if not namespaces:
        return

    pattern = re.compile(
        r"""["'](?P<key>(?:%s)\.[a-z0-9_]+(?:\.[a-z0-9_]+)*)["']""" % "|".join(sorted(namespaces))
    )

    for path in sources:
        # The catalogue loader itself names namespaces as data.
        if path.name in {"catalogue.py", "catalogue.ts"}:
            continue
        # Tests are excluded, and the reason is specific rather than general:
        # both runtimes are expected to RAISE on an unknown key, so the test
        # that proves it has to reference one that does not exist. Flagging
        # that would make the check demand the removal of the test that makes
        # the runtime's posture safe.
        if is_test(path):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line_number, line in enumerate(source.splitlines(), start=1):
            for match in pattern.finditer(line):
                key = match.group("key")
                if key not in messages:
                    problems.add(
                        f"{path.relative_to(REPO_ROOT)}:{line_number}",
                        f"references {key!r}, which is not in the catalogue. Both "
                        f"runtimes raise on it rather than falling back, so this is a "
                        f"raw key or a 500 in front of a user (FR-UX-007).",
                    )


#: Attributes whose value is read aloud or displayed. Precise, because a
#: literal in one of these positions is unambiguously user-facing.
USER_FACING_ATTRIBUTES = ("aria-label", "aria-description", "placeholder", "title", "alt")

ATTRIBUTE_LITERAL = re.compile(
    r"""\b(?P<attr>%s)\s*=\s*"(?P<text>[^"]*[A-Za-z]{2,}[^"]*)\"""" % "|".join(USER_FACING_ATTRIBUTES)
)

#: Text sitting between a JSX tag and whatever ends it - the next tag, or an
#: interpolation. Ending on `{` as well as `<` is what catches the common
#: shape `<li>No clients match “{query}”</li>`, whose text would otherwise be
#: invisible because it is broken by the placeholder.
#:
#: Starting only at `>` is deliberate: allowing `}` to open a match would read
#: `} else {` as the user-facing text "else". The trailing fragment after an
#: interpolation is given up in exchange, which costs nothing - it is `”` or a
#: full stop, never a sentence.
#:
#: `[^<>{}]` inside is what keeps this off TypeScript generics and arrow
#: functions, which carry a brace or a further angle bracket before the next
#: tag.
JSX_TEXT = re.compile(r">(?P<text>[^<>{}]*[A-Za-z]{2,}[^<>{}]*)[<{]")

#: Code fragments that survive the pattern above. Real interface copy does not
#: contain these; `=> entry.name` does.
CODE_ISH = re.compile(r"[(){}=;]|=>|\.\w")

#: Text that is genuinely not translatable, by file and exact string. An entry
#: is a deliberate, reviewable act - the point of the check is that
#: untranslated text cannot be SILENT, not that it can never exist.
#:
#: The bar is "this string is the same in every language because it is a
#: name", not "translating this is inconvenient".
LITERAL_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # The wordmark. A brand name reads LEDGR in Dutch and in English, and
        # putting it in the catalogue would invite somebody to translate it.
        #
        # One entry, not one per screen: apps/web/src/Wordmark.tsx exists so
        # that the product's name is written once. An allowlist that grew
        # every time a header was added would stop being read, and an
        # allowlist nobody reads is where an untranslated sentence hides.
        ("apps/web/src/Wordmark.tsx", "LEDGR"),
    }
)


def strip_comments(source: str) -> str:
    """Comments blanked out, with offsets preserved so line numbers survive.

    A doc comment describing a screen quotes the copy on it, and a scan that
    read those would report the documentation as the defect. Blanking rather
    than deleting keeps every later offset pointing at the line it came from.
    """
    out = list(source)
    for match in re.finditer(r"/\*.*?\*/|//[^\n]*", source, re.DOTALL):
        for index in range(match.start(), match.end()):
            if out[index] != "\n":
                out[index] = " "
    return "".join(out)


def strip_python_comments(source: str) -> str:
    """`#` comments blanked out, offsets preserved.

    A comment explaining a message quotes it, and a scan that read those would
    report the documentation as the defect. Docstrings are left alone on
    purpose: a `detail=` inside one is code being illustrated, and no
    heuristic tells that apart from the real thing better than the
    two-word-prose rule already does.
    """
    out = list(source)
    for match in re.finditer(r"#[^\n]*", source):
        for index in range(match.start(), match.end()):
            out[index] = " "
    return "".join(out)


def line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


#: Response fields a person reads. `detail` is FastAPI's own, and `message` is
#: the one this codebase puts inside a structured detail body (see
#: api.i18n.http.problem). `reason` is deliberately absent: it is a stable
#: machine token and must NOT be translated.
USER_FACING_RESPONSE_FIELDS = ("detail", "message")

#: The two constructors that put a body in front of a person. Scanning only
#: INSIDE these calls is what keeps the rule precise: `detail` is also the name
#: of an internal diagnostic field on AuthorizationDecision and on several
#: domain errors, and those are machine-read, correctly English, and would
#: otherwise drown this check in false reports.
RESPONSE_CONSTRUCTORS = ("HTTPException", "JSONResponse")

RESPONSE_CALL = re.compile(r"\b(?:%s)\s*\(" % "|".join(RESPONSE_CONSTRUCTORS))

#: `detail="..."` or `"message": "..."` holding prose rather than a token.
#: Two words minimum, because a single bare word in one of these positions is
#: almost always a machine value and a sentence never is.
PYTHON_RESPONSE_LITERAL = re.compile(
    r"""["']?\b(?P<field>%s)\b["']?\s*[=:]\s*"""
    r"""(?P<quote>["'])(?P<text>(?=[^"']*[A-Za-z]{2})[^"']*?\s[^"']*?)(?P=quote)"""
    % "|".join(USER_FACING_RESPONSE_FIELDS)
)


def call_spans(source: str) -> list[tuple[int, int]]:
    """The argument list of every response constructor, paren-balanced.

    Balanced rather than "to the next close paren", because these calls nest —
    a `detail={...}` dict, an f-string with a call in it — and stopping at the
    first `)` would read only part of the body.
    """
    spans: list[tuple[int, int]] = []
    for match in RESPONSE_CALL.finditer(source):
        depth, index = 0, match.end() - 1
        while index < len(source):
            if source[index] == "(":
                depth += 1
            elif source[index] == ")":
                depth -= 1
                if depth == 0:
                    spans.append((match.end(), index))
                    break
            index += 1
    return spans

#: Literals in a `detail=`/`message` position that are NOT user-facing text,
#: by file and exact string. Every entry needs a reason, and the bar is "no
#: person can reach this", not "it is inconvenient to translate".
RESPONSE_LITERAL_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        # A 500 raised only by constructing ScopeSpec("administration") by
        # hand instead of via administration_from_path - a misdeclared route,
        # caught in CI by tests/test_authz_coverage.py long before a request
        # reaches it. It is addressed to whoever wrote the route.
        (
            "apps/api/src/api/authz/dependencies.py",
            "administration scope declared without a path parameter",
        ),
    }
)


def check_no_hardcoded_api_text(problems: Problems, sources: list[Path]) -> None:
    """Check 10, API side.

    Deliberately narrow, in three ways, because a noisy gate gets disabled:

      * only inside an `HTTPException(...)` or `JSONResponse(...)` call, so
        `AuthorizationDecision.detail` and the domain errors' own `detail`
        fields — internal, machine-read, correctly English — are out of scope;
      * only `detail` and `message`, never `reason`, which is a stable token;
      * only literals containing whitespace, since a single bare word in one
        of these positions is a machine value and a sentence never is.

    Scoping to the call also drops docstrings that quote an example body,
    which is what three of the four first reports turned out to be.
    """
    for path in sources:
        if path.suffix != ".py" or is_test(path):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue

        relative = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        cleaned = strip_python_comments(source)
        for start, end in call_spans(cleaned):
            for match in PYTHON_RESPONSE_LITERAL.finditer(cleaned, start, end):
                text = " ".join(match.group("text").split())
                if not text or (relative, text) in RESPONSE_LITERAL_ALLOWLIST:
                    continue
                problems.add(
                    f"{relative}:{line_of(source, match.start())}",
                    f"user-facing text {text!r} is written into a response. Every "
                    f"sentence a person reads comes from the catalogue (FR-LOC-001, "
                    f"FR-UX-007) - a literal here is permanently English and "
                    f"invisible to every completeness check. Use "
                    f'api.i18n.http.problem(request, <status>, "<key>", '
                    f'reason="...").',
                )


def check_no_hardcoded_jsx_text(problems: Problems, sources: list[Path]) -> None:
    """Check 10, and the one that keeps FR-LOC-001 true over time.

    The other checks can only see strings that reached the catalogue. A
    literal typed straight into a component never did - so it is not a missing
    translation, it is a string that is permanently English, and no amount of
    catalogue completeness will reveal it.

    This is a heuristic and says so. Its escape hatch is LITERAL_ALLOWLIST
    above, which is a code change somebody reviews, rather than silence.
    """
    for path in sources:
        if path.suffix != ".tsx" or path.name.endswith((".test.tsx", ".spec.tsx")):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue

        relative = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        # Scanned whole-file rather than line by line, because JSX text
        # routinely sits on its own line with its tags above and below it -
        # which is exactly the formatting Prettier produces for any string
        # long enough to be worth translating.
        cleaned = strip_comments(source)

        found: list[tuple[int, str]] = []
        for match in ATTRIBUTE_LITERAL.finditer(cleaned):
            found.append((line_of(cleaned, match.start()), match.group("text")))
        for match in JSX_TEXT.finditer(cleaned):
            text = match.group("text")
            if not CODE_ISH.search(text):
                found.append((line_of(cleaned, match.start()), text))

        for line_number, raw in found:
            text = " ".join(raw.split())
            if not text or (relative, text) in LITERAL_ALLOWLIST:
                continue
            problems.add(
                f"{relative}:{line_number}",
                f"user-facing text {text!r} is written into the component. Every "
                f"string comes from the catalogue (FR-LOC-001) - a literal here is "
                f"permanently English and invisible to every completeness check. "
                f'Use t("namespace.key").',
            )


# ---------------------------------------------------------------------------


def print_glossary() -> None:
    """The terminology sheet FR-LOC-001c's review is done against.

    Rendered from glossary.json rather than kept as a second document, for the
    reason the catalogue itself is one file: a review sheet that drifts from
    the terms actually shipping is worse than none, because it gets signed
    off. Flagged terms come first — those are the ones the review exists for.
    """
    document = load(GLOSSARY_FILE)
    terms: dict[str, Any] = document.get("terms", {})  # type: ignore[assignment]

    print("FR-LOC-001c — Dutch accounting terminology, for review\n")
    print(
        "Every term below keeps its Dutch form in the English UI unless said "
        "otherwise.\nNone has been reviewed by a practising Dutch accountant, "
        "which is what FR-LOC-001c\nrequires and what this sheet is for.\n"
    )

    # Least settled first: a reviewer's attention is the scarce resource, and
    # the high-confidence entries mostly need confirming rather than deciding.
    order = {"low": 0, "medium": 1, "high": 2}
    for term_id, entry in sorted(
        terms.items(), key=lambda item: order.get(item[1].get("review", {}).get("confidence"), 3)
    ):
        review = entry.get("review", {})
        flag = "  ** FLAGGED **" if review.get("status") == "flagged" else ""
        term = str(entry.get("term", term_id))
        # The id is only worth showing when it differs from the term itself,
        # which it does for the abbreviations (kvk -> KvK) and not otherwise.
        shown = term if term.lower() == term_id.lower() else f"{term}   ({term_id})"
        print(f"{'=' * 72}\n{shown}{flag}")
        print(f"  confidence   {review.get('confidence', 'unstated')}")
        print(f"  keeps Dutch  {'yes' if entry.get('keepDutch') else 'no'}")
        print(f"  PRD glossary {review.get('prdAppendix') or 'no entry — meaning not settled there'}")
        reviewer = review.get("reviewedBy")
        print(f"  reviewed by  {reviewer or 'NOT YET REVIEWED'}")

        definition = entry.get("definition", {})
        print(f"\n  NL  {definition.get('nl', '')}")
        print(f"  EN  {definition.get('en', '')}")
        if definition.get("note"):
            print(f"\n  Why: {definition['note']}")

        questions = review.get("questions") or []
        if questions:
            print("\n  To decide:")
            for index, question in enumerate(questions, start=1):
                print(f"    {index}. {question}")
        print()

    candidates: dict[str, Any] = document.get("candidates", {})  # type: ignore[assignment]
    entries: dict[str, str] = candidates.get("terms", {})
    if entries:
        print(f"{'=' * 72}\nNOT YET DECIDED — no treatment chosen, no definition written\n")
        for term_id, why in entries.items():
            print(f"  {term_id}\n      {why}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list", action="store_true", help="print every key with both languages"
    )
    parser.add_argument(
        "--glossary",
        action="store_true",
        help="print the FR-LOC-001c terminology sheet for accountant review",
    )
    args = parser.parse_args()

    if args.glossary:
        print_glossary()
        return EXIT_OK

    if not CATALOGUE_DIR.is_dir():
        print(f"no catalogue at {CATALOGUE_DIR}", file=sys.stderr)
        return EXIT_CANNOT_RUN

    problems = Problems()
    messages = check_catalogue(problems)
    check_glossary(problems, messages)

    sources = source_files()
    check_keys_used_exist(problems, messages, sources)
    check_no_hardcoded_jsx_text(problems, sources)
    check_no_hardcoded_api_text(problems, sources)

    if args.list:
        for key in sorted(messages):
            record = messages[key]
            print(f"{key}\n  nl  {record.get('nl')!r}\n  en  {record.get('en')!r}")

    if not messages and not problems:
        # A check that silently checks nothing reads as a guarantee and is not
        # one - the same trap tests/test_audit_coverage.py guards against.
        print("no messages found; the check would pass vacuously", file=sys.stderr)
        return EXIT_CANNOT_RUN

    if problems:
        print(
            f"\nFR-LOC-001d: {len(problems.items)} problem(s) in the message "
            f"catalogue.\n",
            file=sys.stderr,
        )
        for item in problems.items:
            print(item + "\n", file=sys.stderr)
        print(
            "A missing translation is a release blocker, not a fallback to English "
            "(FR-LOC-001).",
            file=sys.stderr,
        )
        return EXIT_PROBLEMS

    print(
        f"FR-LOC-001d: {len(messages)} messages, complete in Dutch and English; "
        f"{len(sources)} source files checked."
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
