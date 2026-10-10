"""ADR-111: the client e-mail's content, and how the question routes are declared.

The sweep itself (who is mailed, once) runs against Postgres as ledgr_ops and is covered in
tests/integration/test_question_threads.py.
"""

from __future__ import annotations

from api.audit.log import AuditCategory
from api.authz.dependencies import declared_requirements
from api.i18n.language import Language
from api.main import app
from api.questions.notifications import build_message

_BASE = "/v1/administrations/{administration_id}/questions"


def test_the_mail_names_the_books_but_never_the_question() -> None:
    nl = build_message(
        recipient_email="eigenaar@example.com",
        language=Language.NL,
        administration_name="Bakker B.V.",
    )
    assert nl.subject == "Uw boekhouder heeft een vraag over Bakker B.V."
    assert "Bakker B.V." in nl.body
    assert "/questions\n" in nl.body  # the client's question list (contract-wave2.md)
    assert "Met vriendelijke groet," in nl.body

    en = build_message(
        recipient_email="owner@example.com",
        language=Language.EN,
        administration_name="Bakker B.V.",
    )
    assert en.subject == "Your accountant has a question about Bakker B.V."
    assert "Kind regards," in en.body


def _route(path: str, method: str) -> object:
    return next(
        r
        for r in app.routes
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or set())
    )


def test_every_thread_route_is_authorized_against_the_administration_it_names() -> None:
    for path, method in (
        (_BASE, "POST"),
        (_BASE, "GET"),
        (f"{_BASE}/{{thread_id}}", "GET"),
        (f"{_BASE}/{{thread_id}}/messages", "POST"),
        (f"{_BASE}/{{thread_id}}/resolve", "POST"),
    ):
        [requirement] = declared_requirements(_route(path, method))
        assert (requirement.action, requirement.resource_type) == ("view", "administration")
        assert requirement.scope.kind == "administration"
        assert requirement.scope.path_param == "administration_id"
        if method == "POST":
            assert requirement.audit_category is AuditCategory.CONFIGURATION


def test_the_inbox_declares_a_portfolio_requirement() -> None:
    [requirement] = declared_requirements(_route("/v1/firm/inbox", "GET"))
    assert (requirement.action, requirement.resource_type) == ("view", "administration")
    assert requirement.scope.kind == "administration"
    assert requirement.scope.path_param is None  # each portfolio administration, none named
