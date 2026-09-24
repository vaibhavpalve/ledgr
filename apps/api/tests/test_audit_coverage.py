"""IAM-090: a new mutating endpoint without an audit event fails the build.

The third coverage check in this suite, and deliberately the same shape as
the other two - tests/test_isolation_coverage.py for IAM-005 and
tests/test_authz_coverage.py for CLAUDE.md rule three. All three walk the
FastAPI route table at collection time and fail if a route declares nothing;
none of them need a database, so all three run on every CI invocation.

Why mutating routes specifically: IAM-090's list is overwhelmingly things
that CHANGE something (grants, revocations, exports, postings, approvals,
filings, configuration changes). Reads of financial records are on the list
too, and a read route MAY declare a category - `financial_read` exists for
exactly that - but requiring one on every GET would make the log mostly
noise about people looking at their own dashboard, and noise is how an audit
log stops being read.
"""

from __future__ import annotations

from api.audit.log import AuditCategory
from api.authz.dependencies import declared_requirements
from api.authz_middleware import FRAMEWORK_PATHS
from api.main import app

# Mirrors api.authz.dependencies.MUTATING_METHODS. Not imported from there:
# that constant exists for the active-client guard (FR-FRM-000a) and this one
# for audit coverage, and a future change to either should not silently move
# the other.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Mutating routes that genuinely record nothing. Every entry needs a reason,
# and the list is meant to stay short enough that a reviewer reads all of it.
#
#   /v1/me/language
#       FR-LOC-001b. Which of two languages a person reads the interface in is
#       presentation, and it changes nothing about the data, the books or
#       anyone's access to them - the same figures, the same permissions, the
#       same rows, in different words. There is no question in IAM-090's list
#       that this row would help answer.
#
#       Worth contrasting with the switcher, which is NOT exempt even though
#       it also "only changes the UI": which CLIENT someone was working in is
#       the context an auditor needs to read the rest of their activity.
#       Which language they read it in is not.
#
#   /v1/me/reminders (ADR-090)
#       Whether a person receives reminder e-mails about their own deadlines. The same case as
#       the language: a preference about how the product talks to this person, changing no data,
#       no books and nobody's access - nothing IAM-090 asks about.
#
#   api.auth.routes' sign-up/sign-in/MFA surface (IAM-010/IAM-011/IAM-012)
#       Every one of these runs before any permission can be evaluated - the
#       route IS how a permission-bearing identity gets established, so
#       there is no require_permission(audit=...) call to hang a
#       declaration on (declared_requirements only sees that dependency).
#       This is exactly the case api.audit.trail's module docstring
#       anticipates ("authentication - api.auth.service ... wiring those at
#       the service rather than waiting for their routes"): these handlers
#       call AuditTrail.authentication() directly - see
#       api.auth.routes._record_authentication_event and its call sites in
#       signup(), login(), logout(), mfa_totp_enroll_confirm(),
#       mfa_totp_verify(), mfa_passkey_enroll_finish(),
#       mfa_passkey_verify_finish(), login_passkey_finish(),
#       login_google_callback() and signup_google().
#
#       The "begin" half of each ceremony (mfa_totp_enroll_begin,
#       mfa_passkey_enroll_begin, mfa_passkey_verify_begin,
#       login_passkey_begin, login_google_start) is exempt for the ordinary
#       reason above instead: it only writes a short-lived, single-use
#       WebAuthn challenge or OIDC state/PKCE row (auth_ceremony) with no
#       security-relevant outcome of its own - nothing IAM-090 asks about
#       until the matching finish/confirm/callback either succeeds or is
#       abandoned.
#
#   /v1/auth/verify-email, /v1/auth/verify-email/resend (IAM-010b) and the
#   account security settings under /v1/me (IAM-017, IAM-010f, IAM-011,
#   IAM-013 - api.account.security_routes)
#       The same case as the auth routes above, for the same reason: they
#       are authorization-exempt (they act only on the caller's own account
#       - api.authz.dependencies.AUTHORIZATION_EXEMPT_PATHS) so there is no
#       require_permission(audit=...) to declare a category on, and each
#       handler records its own AuditTrail.authentication event instead:
#       verify_email() and resend_verification_email() via
#       _record_authentication_event, and revoke_session(), revoke_passkey(),
#       change_password() (including the DENIED entry for a wrong current
#       password), remove_totp() and revoke_trusted_device() (ADR-061) via
#       api.account.security_routes._record.
#       tests/integration/test_account_security.py asserts the entries land.
#
#   /v1/administrations/{administration_id}/customers/kvk-lookup (SI-03)
#       Unlike validate_customer_vat_number (which writes a verdict onto an
#       EXISTING customer row - vat_number_status, checked_at - and IS
#       audited), this route acts before any customer record exists at all:
#       it is a pure external lookup whose result a "new customer" form uses
#       to pre-fill fields locally, and nothing here is written to any
#       table. See api.customers.kvk's module docstring.
#
# A route that changes tenant data does not belong here. The bar is "this
# changes nothing an auditor would ask about".
AUDIT_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/v1/administrations/{administration_id}/customers/kvk-lookup",
        "/v1/me/language",
        "/v1/me/reminders",
        "/v1/me/sessions/{session_id}",
        "/v1/me/passkeys/{passkey_id}",
        "/v1/me/password",
        "/v1/me/mfa/totp",
        "/v1/me/trusted-devices/{device_id}",
        "/v1/auth/signup",
        "/v1/auth/signup/google",
        "/v1/auth/login",
        "/v1/auth/recover",
        "/v1/auth/logout",
        "/v1/auth/mfa/totp/enroll/begin",
        "/v1/auth/mfa/totp/enroll/confirm",
        "/v1/auth/mfa/totp/verify",
        "/v1/auth/mfa/passkey/enroll/begin",
        "/v1/auth/mfa/passkey/enroll/finish",
        "/v1/auth/mfa/passkey/verify/begin",
        "/v1/auth/mfa/passkey/verify/finish",
        "/v1/auth/login/passkey/begin",
        "/v1/auth/login/passkey/finish",
        "/v1/auth/login/google/start",
        "/v1/auth/login/google/callback",
        "/v1/auth/verify-email",
        "/v1/auth/verify-email/resend",
    }
)


def _mutating_routes() -> list[tuple[str, str, object]]:
    routes: list[tuple[str, str, object]] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None or path in FRAMEWORK_PATHS:
            continue
        for method in sorted(set(methods) & MUTATING_METHODS):
            routes.append((method, path, route))
    return routes


def test_the_check_actually_has_routes_to_check() -> None:
    """Without this, every assertion below passes vacuously if
    `_mutating_routes` ever stops finding anything - a refactor of the route
    table, a changed method set, an early return. A coverage check that
    silently checks nothing is worse than no check, because it reads as a
    guarantee.
    """
    found = {(method, path) for method, path, _ in _mutating_routes()}

    assert found, "no mutating routes found - the coverage check is inert"
    assert ("PUT", "/v1/switcher/{administration_id}") in found


def test_every_mutating_route_records_an_audit_event() -> None:
    missing: list[str] = []

    for method, path, route in _mutating_routes():
        if path in AUDIT_EXEMPT_PATHS:
            continue
        declared = [r for r in declared_requirements(route) if r.audit_category is not None]
        if not declared:
            missing.append(f"  {method} {path}")

    assert not missing, (
        "These routes change something and record no audit event (IAM-090). Add "
        "`audit=AuditCategory.<...>` to the route's require_permission(...), or add "
        "its path to AUDIT_EXEMPT_PATHS in this file with a reason:\n" + "\n".join(missing)
    )


def test_a_route_declares_at_most_one_audit_category() -> None:
    """Two categories on one route would mean two entries per request saying
    different things about the same action, and no way to tell which is the
    one an auditor should believe.
    """
    for method, path, route in _mutating_routes():
        categories = {
            r.audit_category for r in declared_requirements(route) if r.audit_category is not None
        }
        assert len(categories) <= 1, f"{method} {path} declares {categories}"


def test_every_exempt_path_actually_exists() -> None:
    """An exemption for a route that has since been renamed or removed is
    dead weight that quietly widens the opt-out list.
    """
    registered = {getattr(route, "path", None) for route in app.routes}
    stale = sorted(path for path in AUDIT_EXEMPT_PATHS if path not in registered)

    assert not stale, f"AUDIT_EXEMPT_PATHS names routes that no longer exist: {stale}"


def test_declared_categories_are_real_ones() -> None:
    """Guards against a category being declared as a bare string that happens
    to typecheck somewhere - only IAM-090's nine exist.
    """
    for _, _, route in _mutating_routes():
        for requirement in declared_requirements(route):
            if requirement.audit_category is not None:
                assert isinstance(requirement.audit_category, AuditCategory)


def test_the_check_would_actually_fail_on_an_undeclared_route() -> None:
    """The check is only worth having if it can fail. Builds a route with no
    audit declaration and asserts the same predicate rejects it - so a
    refactor that made `declared_requirements` always return a category, or
    the loop never execute, is caught.
    """
    import uuid

    from fastapi import Depends, FastAPI

    from api.authz.dependencies import (
        administration_from_path,
        require_permission,
    )
    from api.authz.model import AuthorizationDecision

    probe = FastAPI()

    @probe.post("/v1/undeclared/{administration_id}")
    def undeclared(
        administration_id: uuid.UUID,
        _: AuthorizationDecision = Depends(
            require_permission(
                "post",
                "journal_entry",
                scope=administration_from_path("administration_id"),
            )
        ),
    ) -> dict[str, str]:
        return {}

    route = next(
        r
        for r in probe.routes
        if str(getattr(r, "path", "")).endswith("/v1/undeclared/{administration_id}")
    )
    declared = [r for r in declared_requirements(route) if r.audit_category is not None]

    assert declared == [], "an undeclared route must be detectable as undeclared"
