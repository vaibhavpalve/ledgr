"""The FR-LOC-001d gate, checked itself.

    FR-LOC-001d  Translation completeness is enforced in CI: a build fails if
                 any user-facing string lacks a translation in either language.

scripts/check_translations.py is what makes FR-LOC-001's posture defensible.
Both runtimes RAISE on a message they cannot resolve rather than falling back
from Dutch to English, and that is only safe because the build fails first. So
the gate is load-bearing, and a gate that has quietly stopped detecting things
is worse than no gate: it reads as a guarantee.

This file is the repo's own convention applied to it -
tests/test_audit_coverage.py proves its check can fail on an undeclared route,
tests/test_idempotency_coverage.py proves its middleware would cover a new
one. Every rule below is asserted in both directions: a catalogue that
violates it FAILS, and one that does not PASSES. One-directional tests would
be satisfied by a checker that rejected everything.

The script lives at the repo root rather than under apps/api, because the
catalogue it checks is shared with the web and mobile apps. It is loaded by
path for the same reason.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "check_translations.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_translations", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging accident
        raise RuntimeError(f"cannot load the FR-LOC-001d checker from {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so the module can be introspected normally; the
    # name is not "__main__", so its `raise SystemExit(main())` does not fire.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load()


# ---------------------------------------------------------------------------
# Building a catalogue to point the checker at
# ---------------------------------------------------------------------------


def message(
    nl: Any = "Nederlandse tekst", en: Any = "English text", **extra: Any
) -> dict[str, Any]:
    return {"nl": nl, "en": en, **extra}


def review(status: str = "proposed", **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "confidence": "high",
        "reviewedBy": None,
        "reviewedOn": None,
        "questions": [],
        **extra,
    }


GLOSSARY = {
    "namespace": "glossary",
    "terms": {
        "btw": {
            "term": "BTW",
            "keepDutch": True,
            "definition": message("Belasting toegevoegde waarde", "Dutch value added tax"),
            "review": review(),
        }
    },
}


def glossary_with(**terms: Any) -> dict[str, Any]:
    return {"namespace": "glossary", "terms": terms}


@pytest.fixture
def catalogue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Points the checker at a catalogue built for one test.

    REPO_ROOT is repointed too: the source scans report paths relative to it,
    and a fixture file outside the real repo would otherwise raise rather than
    produce a finding.
    """

    def build(files: dict[str, dict[str, Any]], glossary: dict[str, Any] | None = None) -> None:
        for name, document in files.items():
            (tmp_path / name).write_text(json.dumps(document), encoding="utf-8")
        (tmp_path / "glossary.json").write_text(
            json.dumps(glossary if glossary is not None else GLOSSARY), encoding="utf-8"
        )
        # check_api_file_list compares the directory against the tuple in
        # api.i18n.catalogue; a stand-in listing exactly these files keeps
        # that rule out of the way of tests aimed at other rules.
        listed = ", ".join(f'"{name}"' for name in files)
        (tmp_path / "fake_catalogue.py").write_text(
            f"CATALOGUE_FILES = ({listed},)", encoding="utf-8"
        )

        monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(checker, "CATALOGUE_DIR", tmp_path)
        monkeypatch.setattr(checker, "GLOSSARY_FILE", tmp_path / "glossary.json")
        monkeypatch.setattr(checker, "API_CATALOGUE_MODULE", tmp_path / "fake_catalogue.py")

    return build


def run_catalogue_checks() -> list[str]:
    problems = checker.Problems()
    messages = checker.check_catalogue(problems)
    checker.check_glossary(problems, messages)
    return problems.items


def one_file(**messages: Any) -> dict[str, dict[str, Any]]:
    return {"common.json": {"namespace": "common", "messages": messages}}


# ---------------------------------------------------------------------------
# The gate, against the real catalogue
# ---------------------------------------------------------------------------


def test_the_real_catalogue_is_complete() -> None:
    """The check CI runs, run here too.

    Not redundant with the `translation-completeness` job: this one fails in
    the suite a developer runs locally, next to the code that broke it,
    rather than several minutes later in a separate job.
    """
    problems = checker.Problems()
    messages = checker.check_catalogue(problems)
    checker.check_glossary(problems, messages)
    sources = checker.source_files()
    checker.check_keys_used_exist(problems, messages, sources)
    checker.check_no_hardcoded_jsx_text(problems, sources)
    checker.check_no_hardcoded_api_text(problems, sources)

    assert problems.items == [], "\n".join(problems.items)
    assert messages, "the checker found no messages - it would pass vacuously"
    assert sources, "the checker found no source files to scan"


# ---------------------------------------------------------------------------
# Both languages, present and real
# ---------------------------------------------------------------------------


def test_a_complete_record_passes(catalogue: Any) -> None:
    """The other direction of every test below. Without this, a checker that
    rejected everything would satisfy all of them.
    """
    catalogue(one_file(**{"common.a": message()}))
    assert run_catalogue_checks() == []


@pytest.mark.parametrize("missing", ["nl", "en"])
def test_a_missing_language_fails(catalogue: Any, missing: str) -> None:
    record = message()
    del record[missing]
    catalogue(one_file(**{"common.a": record}))

    assert any(f"has no `{missing}`" in item for item in run_catalogue_checks())


@pytest.mark.parametrize("empty", ["", "   "])
def test_an_empty_translation_fails(catalogue: Any, empty: str) -> None:
    catalogue(one_file(**{"common.a": message(nl=empty)}))
    assert any("is empty" in item for item in run_catalogue_checks())


@pytest.mark.parametrize("text", ["TODO", "todo: vertalen", "FIXME later", "XXX", "???"])
def test_a_placeholder_translation_fails(catalogue: Any, text: str) -> None:
    """ "I will do the Dutch later" is the specific way FR-LOC-001 gets
    violated by somebody acting in good faith, so it is named rather than left
    to the empty-string rule.
    """
    catalogue(one_file(**{"common.a": message(nl=text)}))
    assert any("placeholder" in item for item in run_catalogue_checks())


# ---------------------------------------------------------------------------
# Identical languages: legal, never silent
# ---------------------------------------------------------------------------


def test_identical_languages_without_the_flag_fail(catalogue: Any) -> None:
    """The rule that makes "paste the English into the Dutch slot" impossible
    to do quietly.
    """
    catalogue(one_file(**{"common.a": message(nl="Same", en="Same")}))
    assert any("identical" in item for item in run_catalogue_checks())


def test_identical_languages_with_the_flag_pass(catalogue: Any) -> None:
    # Endonyms, proper nouns and statutory terms are legitimately identical.
    catalogue(one_file(**{"common.a": message(nl="BTW", en="BTW", identical=True)}))
    assert run_catalogue_checks() == []


def test_a_stale_identical_flag_fails(catalogue: Any) -> None:
    # Left behind after the two languages diverged. It would let the next
    # divergence go unremarked, which is the whole point of the flag.
    catalogue(one_file(**{"common.a": message(nl="Een", en="One", identical=True)}))
    assert any("declares `identical: true` but" in item for item in run_catalogue_checks())


# ---------------------------------------------------------------------------
# Placeholders and plurals
# ---------------------------------------------------------------------------


def test_a_placeholder_present_in_one_language_only_fails(catalogue: Any) -> None:
    """One language renders with a hole in the sentence, or a literal brace on
    screen. Both runtimes raise on the missing parameter, so this would be a
    500 in front of a user.
    """
    catalogue(one_file(**{"common.a": message(nl="Hallo {name}", en="Hello")}))
    assert any("placeholders differ" in item for item in run_catalogue_checks())


def test_matching_placeholders_pass_regardless_of_word_order(catalogue: Any) -> None:
    # The reason messages interpolate rather than concatenate: the placeholder
    # sits in a different place in each language and that is fine.
    catalogue(
        one_file(**{"common.a": message(nl="{count} regels geboekt", en="Posted {count} lines")})
    )
    assert run_catalogue_checks() == []


def test_a_counted_message_in_one_language_only_fails(catalogue: Any) -> None:
    catalogue(one_file(**{"common.a": message(nl={"one": "regel", "other": "regels"}, en="lines")}))
    assert any("counted" in item for item in run_catalogue_checks())


def test_a_missing_plural_category_fails(catalogue: Any) -> None:
    catalogue(
        one_file(
            **{"common.a": message(nl={"one": "regel", "other": "regels"}, en={"other": "lines"})}
        )
    )
    assert any("missing plural form" in item for item in run_catalogue_checks())


def test_a_category_neither_language_has_fails(catalogue: Any) -> None:
    """Dutch and English use exactly `one` and `other`. A `few` here is
    somebody copying a catalogue from a language that has one.
    """
    forms = {"one": "a", "other": "b", "few": "c"}
    catalogue(one_file(**{"common.a": message(nl=forms, en=forms)}))
    assert any("plural categor" in item for item in run_catalogue_checks())


def test_a_well_formed_counted_message_passes(catalogue: Any) -> None:
    catalogue(
        one_file(
            **{
                "common.a": message(
                    nl={"one": "{count} regel", "other": "{count} regels"},
                    en={"one": "{count} line", "other": "{count} lines"},
                )
            }
        )
    )
    assert run_catalogue_checks() == []


# ---------------------------------------------------------------------------
# Keys, namespaces and files
# ---------------------------------------------------------------------------


def test_a_key_outside_its_namespace_fails(catalogue: Any) -> None:
    """Keys carry their file's namespace so a message on screen can be
    grepped back to the file that owns it.
    """
    catalogue(one_file(**{"client.a": message()}))
    assert any("namespace" in item for item in run_catalogue_checks())


def test_a_duplicate_key_across_files_fails(catalogue: Any) -> None:
    """Whichever file loaded second would silently win, making the message
    depend on load order.
    """
    catalogue(
        {
            "common.json": {"namespace": "common", "messages": {"common.a": message()}},
            "extra.json": {"namespace": "common", "messages": {"common.a": message()}},
        }
    )
    assert any("already defined in" in item for item in run_catalogue_checks())


def test_a_file_the_api_never_loads_fails(catalogue: Any, tmp_path: Path) -> None:
    """api.i18n.catalogue names its files rather than globbing, so a file on
    disk and absent from that tuple is a namespace the web app serves and the
    API raises on. Only a comparison makes naming them safe.
    """
    catalogue(one_file(**{"common.a": message()}))
    (tmp_path / "fake_catalogue.py").write_text("CATALOGUE_FILES = ()", encoding="utf-8")

    assert any("CATALOGUE_FILES" in item for item in run_catalogue_checks())


# ---------------------------------------------------------------------------
# FR-LOC-001c: statutory terminology
# ---------------------------------------------------------------------------


def test_translating_a_statutory_term_fails(catalogue: Any) -> None:
    """The named example: BTW must not become VAT. An English reader who sees
    "VAT" reasons about it using their own country's rules.
    """
    catalogue(one_file(**{"common.a": message(nl="BTW aangifte", en="VAT return")}))
    assert any("statutory term" in item for item in run_catalogue_checks())


def test_keeping_a_statutory_term_passes(catalogue: Any) -> None:
    catalogue(one_file(**{"common.a": message(nl="BTW-aangifte", en="BTW return")}))
    assert run_catalogue_checks() == []


def test_a_term_inside_a_longer_word_is_not_a_false_match(catalogue: Any) -> None:
    # Word-boundary matching, so a term is not "found" inside an unrelated
    # word and demanded in the other language.
    catalogue(one_file(**{"common.a": message(nl="Afgebtwd woord", en="Unrelated word")}))
    assert run_catalogue_checks() == []


def test_a_glossary_definition_needs_both_languages(catalogue: Any) -> None:
    """FR-LOC-001c asks for a hover or tap definition, which is itself
    user-facing text.
    """
    catalogue(
        one_file(**{"common.a": message()}),
        glossary=glossary_with(
            btw={
                "term": "BTW",
                "keepDutch": True,
                "definition": {"nl": "Alleen"},
                "review": review(),
            }
        ),
    )
    assert any("has no `en`" in item for item in run_catalogue_checks())


# ---------------------------------------------------------------------------
# FR-LOC-001c's other half: the review itself
# ---------------------------------------------------------------------------


def test_a_term_with_no_review_block_fails(catalogue: Any) -> None:
    """FR-LOC-001c is two claims - the terminology is authoritative, AND it is
    reviewed by a practising Dutch accountant. Only the first is a string in a
    file; this makes the second visible in the data instead of in a comment.
    """
    catalogue(
        one_file(**{"common.a": message()}),
        glossary=glossary_with(btw={"term": "BTW", "keepDutch": True, "definition": message()}),
    )
    assert any("has no `review` block" in item for item in run_catalogue_checks())


def test_a_flagged_term_with_no_question_fails(catalogue: Any) -> None:
    """A flag without a question is a worry nobody can answer, so it gets
    cleared by whoever tires of seeing it rather than by a decision.
    """
    catalogue(
        one_file(**{"common.a": message()}),
        glossary=glossary_with(
            btw={
                "term": "BTW",
                "keepDutch": True,
                "definition": message(),
                "review": review("flagged"),
            }
        ),
    )
    assert any("flagged but asks nothing" in item for item in run_catalogue_checks())


def test_a_flagged_term_that_asks_something_passes(catalogue: Any) -> None:
    catalogue(
        one_file(**{"common.a": message()}),
        glossary=glossary_with(
            btw={
                "term": "BTW",
                "keepDutch": True,
                "definition": message(),
                "review": review("flagged", confidence="low", questions=["Keep or translate?"]),
            }
        ),
    )
    assert run_catalogue_checks() == []


def test_an_unreviewed_term_does_not_fail_the_build(catalogue: Any) -> None:
    """Deliberate, and the most important assertion in this section.

    A gate that blocked on a human review nobody had scheduled would be
    disabled inside a week, and the far more valuable completeness checks
    would go with it. Unreviewed is recorded, not enforced.
    """
    catalogue(one_file(**{"common.a": message()}))
    assert run_catalogue_checks() == []


def test_claiming_a_review_without_naming_the_reviewer_fails(catalogue: Any) -> None:
    """FR-LOC-001c's review is by a specific person. An unattributable
    sign-off is not one.
    """
    catalogue(
        one_file(**{"common.a": message()}),
        glossary=glossary_with(
            btw={
                "term": "BTW",
                "keepDutch": True,
                "definition": message(),
                "review": review("reviewed"),
            }
        ),
    )
    assert any("names no reviewer" in item for item in run_catalogue_checks())


def test_a_term_cannot_be_decided_and_undecided_at_once(catalogue: Any, tmp_path: Path) -> None:
    """`candidates` lists terms with no treatment chosen. An id in both places
    means the decision was made and the open question left standing beside it.
    """
    catalogue(one_file(**{"common.a": message()}))
    document = json.loads((tmp_path / "glossary.json").read_text(encoding="utf-8"))
    document["candidates"] = {"terms": {"btw": "still open?"}}
    (tmp_path / "glossary.json").write_text(json.dumps(document), encoding="utf-8")

    assert any(
        "decided terms and as undecided candidates" in item for item in run_catalogue_checks()
    )


def test_the_shipped_glossary_records_that_no_review_has_happened() -> None:
    """Keeps ADR-029's claim honest. The moment an accountant signs off, this
    fails and the ADR gets updated with it - which is the point of recording
    the review in the data rather than in prose.
    """
    document = json.loads(checker.GLOSSARY_FILE.read_text(encoding="utf-8"))
    reviewers = {
        term_id: entry["review"]["reviewedBy"] for term_id, entry in document["terms"].items()
    }

    assert set(reviewers.values()) == {None}, (
        f"a term now names a reviewer: {reviewers}. Update ADR-029's 'FR-LOC-001c's "
        f"review has not happened' note, and this test."
    )


def test_every_flagged_term_in_the_shipped_glossary_asks_something() -> None:
    document = json.loads(checker.GLOSSARY_FILE.read_text(encoding="utf-8"))
    flagged = {
        term_id: entry["review"]
        for term_id, entry in document["terms"].items()
        if entry["review"]["status"] == "flagged"
    }

    # The two the PRD's own appendix glossary has no entry for, which is not a
    # coincidence: a term nobody wrote a meaning for is a term nobody decided.
    assert set(flagged) == {"grootboek", "kolommenbalans"}
    for term_id, entry in flagged.items():
        assert entry["questions"], f"{term_id} is flagged and asks nothing"


# ---------------------------------------------------------------------------
# How the code uses the catalogue
# ---------------------------------------------------------------------------


def source(tmp_path: Path, name: str, body: str) -> list[Path]:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return [path]


def test_a_key_that_does_not_exist_fails(catalogue: Any, tmp_path: Path) -> None:
    """Both runtimes raise on it, so this is a raw identifier or a 500 in
    front of a user (FR-UX-007).
    """
    catalogue(one_file(**{"common.a": message()}))
    problems = checker.Problems()

    checker.check_keys_used_exist(
        problems, {"common.a": {}}, source(tmp_path, "screen.tsx", 't("common.invented")')
    )

    assert any("not in the catalogue" in item for item in problems.items)


def test_a_key_that_exists_passes(catalogue: Any, tmp_path: Path) -> None:
    catalogue(one_file(**{"common.a": message()}))
    problems = checker.Problems()

    checker.check_keys_used_exist(
        problems, {"common.a": {}}, source(tmp_path, "screen.tsx", 't("common.a")')
    )

    assert problems.items == []


# ---------------------------------------------------------------------------
# Hard-coded text: the check that keeps the catalogue from being bypassed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "<p>No client selected</p>",
        '<input aria-label="Search clients by name" />',
        '<input placeholder="Search clients" />',
        "<li>\n  No clients match “{query}”\n</li>",
    ],
    ids=["text-node", "aria-label", "placeholder", "multi-line-with-placeholder"],
)
def test_hardcoded_jsx_text_fails(catalogue: Any, tmp_path: Path, body: str) -> None:
    """The check that keeps FR-LOC-001 true over time. A literal typed into a
    component is not a MISSING translation - it never reached the catalogue at
    all, so no completeness check can see it.
    """
    catalogue(one_file(**{"common.a": message()}))
    problems = checker.Problems()

    checker.check_no_hardcoded_jsx_text(problems, source(tmp_path, "screen.tsx", body))

    assert any("written into the component" in item for item in problems.items)


@pytest.mark.parametrize(
    "body",
    [
        '<p>{t("common.a")}</p>',
        '<input aria-label={t("common.a")} />',
        "<span>{entry.displayName}</span>",
        "const x = useMemo<Record<string, number>>(() => ({}), []);",
        "const f = (a: number) => a > 1 && a < 9;",
        "// <p>No client selected</p>",
        "/* <p>No client selected</p> */",
    ],
    ids=[
        "translated-text",
        "translated-attribute",
        "interpolated-data",
        "generics",
        "comparisons",
        "line-comment",
        "block-comment",
    ],
)
def test_legitimate_jsx_passes(catalogue: Any, tmp_path: Path, body: str) -> None:
    """The false positives that would make this check unusable. A gate that
    cries wolf gets disabled, and then it protects nothing.
    """
    catalogue(one_file(**{"common.a": message()}))
    problems = checker.Problems()

    checker.check_no_hardcoded_jsx_text(problems, source(tmp_path, "screen.tsx", body))

    assert problems.items == []


@pytest.mark.parametrize(
    "body",
    [
        'raise HTTPException(status_code=403, detail="You may not do that.")',
        'return JSONResponse(status_code=429, content={"detail": "Too many attempts."})',
        'raise HTTPException(status_code=403, detail={"message": "Switch client first."})',
    ],
    ids=["detail-kwarg", "json-response", "nested-message"],
)
def test_hardcoded_api_text_fails(catalogue: Any, tmp_path: Path, body: str) -> None:
    """The API mirror of the JSX check. A sentence is equally permanent
    whether it was typed into a component or into a response body, and
    omitting this side is what let three untranslated 403s and IAM-019's
    rate-limit message survive the first pass.
    """
    catalogue(one_file(**{"common.a": message()}))
    problems = checker.Problems()

    checker.check_no_hardcoded_api_text(problems, source(tmp_path, "routes.py", body))

    assert any("written into a response" in item for item in problems.items)


@pytest.mark.parametrize(
    "body",
    [
        'raise HTTPException(status_code=403, detail=message(request, "common.a"))',
        'AuthorizationDecision(allowed=False, detail="no grant covered this request")',
        '"""Returns {"detail": "Tenant context is required."} on refusal."""',
        'raise HTTPException(status_code=403, detail={"reason": "no_matching_grant"})',
        'raise HTTPException(status_code=404, detail="notfound")',
    ],
    ids=[
        "goes-through-the-catalogue",
        "internal-decision-detail",
        "docstring-example",
        "machine-readable-reason",
        "single-token",
    ],
)
def test_legitimate_api_code_passes(catalogue: Any, tmp_path: Path, body: str) -> None:
    """Each of these was a real false positive before the rule was scoped to
    response constructors, prose-only literals, and never `reason`.
    """
    catalogue(one_file(**{"common.a": message()}))
    problems = checker.Problems()

    checker.check_no_hardcoded_api_text(problems, source(tmp_path, "routes.py", body))

    assert problems.items == []


def test_a_test_file_may_reference_a_key_that_does_not_exist(
    catalogue: Any, tmp_path: Path
) -> None:
    """Both runtimes are expected to RAISE on an unknown key, so the test
    proving it must reference one. Flagging that would make the gate demand
    the removal of the test that makes the runtime's posture safe.
    """
    catalogue(one_file(**{"common.a": message()}))
    problems = checker.Problems()

    checker.check_keys_used_exist(
        problems, {"common.a": {}}, source(tmp_path, "screen.test.tsx", 't("common.invented")')
    )

    assert problems.items == []


# ---------------------------------------------------------------------------
# The gate's own exit behaviour
# ---------------------------------------------------------------------------


def test_the_allowlists_are_short_enough_to_be_read() -> None:
    """Both escape hatches exist so untranslated text cannot be SILENT, not so
    it can be common. They are asserted rather than trusted, because an
    allowlist that grows unremarked is where a sentence eventually hides.
    """
    assert len(checker.LITERAL_ALLOWLIST) <= 3, sorted(checker.LITERAL_ALLOWLIST)
    assert len(checker.RESPONSE_LITERAL_ALLOWLIST) <= 3, sorted(checker.RESPONSE_LITERAL_ALLOWLIST)


def test_the_script_fails_the_build_rather_than_reporting_quietly() -> None:
    """A gate is only a gate if a problem is a non-zero exit. The exit codes
    are asserted here so a refactor cannot turn the check into a report
    nothing acts on.
    """
    assert checker.EXIT_OK == 0
    assert checker.EXIT_PROBLEMS == 1
    assert checker.EXIT_CANNOT_RUN == 2
