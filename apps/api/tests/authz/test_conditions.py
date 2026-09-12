"""IAM-033: "Attribute conditions supplement roles: amount ceilings, cost
centre restriction, journal restriction, period restriction, IP allowlist."

Tested through the real AuthorizationService rather than by calling
evaluate_conditions directly wherever the point is about a grant, because
"the condition denies" and "the request is denied" are the two things that
have to line up, and only the service connects them.
"""

from __future__ import annotations

import inspect
import uuid
from decimal import Decimal

import pytest

from api.authz import conditions as conditions_module
from api.authz.conditions import CONDITION_KEYS, evaluate_conditions
from api.authz.model import (
    AdministrationScope,
    AuthorizationRequest,
    ResourceAttributes,
)
from api.authz.service import AuthorizationService
from tests.authz.helpers import World, build_world


def _approve(world: World, attributes: ResourceAttributes) -> AuthorizationRequest:
    return AuthorizationRequest(
        user_id=world.user,
        action="approve",
        resource_type="purchase_invoice",
        target=AdministrationScope(world.acme_books),
        attributes=attributes,
    )


# --- amount ceiling ---------------------------------------------------------


async def test_an_amount_at_or_below_the_ceiling_is_allowed() -> None:
    world = build_world()
    world.repository.assign(
        user_id=world.user,
        role="Approver",
        scope_id=world.acme_books,
        conditions={"amount_ceiling": "5000.00"},
    )
    service = AuthorizationService(world.repository)

    below = await service.authorize(_approve(world, ResourceAttributes(amount=Decimal("4999.99"))))
    exactly = await service.authorize(
        _approve(world, ResourceAttributes(amount=Decimal("5000.00")))
    )

    assert below.allowed
    assert exactly.allowed, "a ceiling is inclusive - it is the most you may approve"


async def test_an_amount_above_the_ceiling_is_denied_by_one_cent() -> None:
    world = build_world()
    world.repository.assign(
        user_id=world.user,
        role="Approver",
        scope_id=world.acme_books,
        conditions={"amount_ceiling": "5000.00"},
    )
    service = AuthorizationService(world.repository)

    decision = await service.authorize(
        _approve(world, ResourceAttributes(amount=Decimal("5000.01")))
    )

    assert decision.denied
    assert decision.reason == "condition_not_satisfied"
    assert decision.detail is not None and "amount_ceiling" in decision.detail


async def test_a_ceiling_comparison_is_exact_at_a_value_float_cannot_represent() -> None:
    """NFR-031 / CLAUDE.md rule four, made concrete with real invoice
    amounts. An approver capped at EUR 7,299.70 approves a batch of exactly
    that: 4335.09 + 2964.61. In binary floating point that sum is
    7299.700000000001 - over the cap - so a float implementation would
    refuse a payment it should allow, and the mirror-image error (a sum
    landing just UNDER a cap it actually exceeds) is the same bug pointed
    the other way.
    """
    world = build_world()
    world.repository.assign(
        user_id=world.user,
        role="Approver",
        scope_id=world.acme_books,
        conditions={"amount_ceiling": "7299.70"},
    )
    service = AuthorizationService(world.repository)

    total = Decimal("4335.09") + Decimal("2964.61")
    assert total == Decimal("7299.70")
    assert 4335.09 + 2964.61 > 7299.70, "the float behaviour this test exists to avoid"

    assert (await service.authorize(_approve(world, ResourceAttributes(amount=total)))).allowed


async def test_a_ceiling_with_no_amount_supplied_denies() -> None:
    """The forgetting case: a route that never declares where its amount
    comes from leaves attributes.amount unset. That must deny, not sail past
    the ceiling.
    """
    world = build_world()
    world.repository.assign(
        user_id=world.user,
        role="Approver",
        scope_id=world.acme_books,
        conditions={"amount_ceiling": "5000.00"},
    )
    service = AuthorizationService(world.repository)

    decision = await service.authorize(_approve(world, ResourceAttributes()))

    assert decision.denied
    assert decision.detail is not None and "no amount supplied" in decision.detail


async def test_a_ceiling_stored_as_a_float_denies_rather_than_being_coerced() -> None:
    outcome = evaluate_conditions(
        {"amount_ceiling": 5000.00}, ResourceAttributes(amount=Decimal("10.00"))
    )
    assert not outcome.satisfied
    assert outcome.detail is not None and "float" in outcome.detail


async def test_an_unconditioned_grant_for_the_same_action_is_not_capped() -> None:
    """An Approver capped at EUR 5,000 who is also an Accountant approves
    without a cap: the cap belongs to the Approver assignment, not to the
    person. Stated explicitly because it looks like a bug until you see that
    conditions narrow a GRANT, and the Accountant grant is a different one.
    """
    world = build_world()
    world.repository.assign(
        user_id=world.user,
        role="Approver",
        scope_id=world.acme_books,
        conditions={"amount_ceiling": "5000.00"},
    )
    world.repository.assign(user_id=world.user, role="Accountant", scope_id=world.acme_books)
    service = AuthorizationService(world.repository)

    decision = await service.authorize(
        _approve(world, ResourceAttributes(amount=Decimal("50000.00")))
    )

    assert decision.allowed


# --- cost centre / journal / period -----------------------------------------


async def test_a_cost_centre_restriction_admits_only_the_listed_centres() -> None:
    world = build_world()
    permitted, other = uuid.uuid4(), uuid.uuid4()
    world.repository.assign(
        user_id=world.user,
        role="Approver",
        scope_id=world.acme_books,
        conditions={"cost_centre_ids": [str(permitted)]},
    )
    service = AuthorizationService(world.repository)

    inside = await service.authorize(_approve(world, ResourceAttributes(cost_centre_id=permitted)))
    outside = await service.authorize(_approve(world, ResourceAttributes(cost_centre_id=other)))
    unspecified = await service.authorize(_approve(world, ResourceAttributes()))

    assert inside.allowed
    assert outside.denied
    assert unspecified.denied, "an unstated cost centre is not a permitted one"


async def test_a_journal_restriction_limits_a_bookkeeper_to_permitted_journals() -> None:
    """Appendix A: "Bookkeeper posting is limited to permitted journals and
    open periods." Both halves are conditions on the assignment, not a
    weaker permission in the role.
    """
    world = build_world()
    sales, bank = uuid.uuid4(), uuid.uuid4()
    open_period = uuid.uuid4()
    world.repository.assign(
        user_id=world.user,
        role="Bookkeeper",
        scope_id=world.acme_books,
        conditions={"journal_ids": [str(sales)], "period_ids": [str(open_period)]},
    )
    service = AuthorizationService(world.repository)

    def post(journal: uuid.UUID, period: uuid.UUID) -> AuthorizationRequest:
        return AuthorizationRequest(
            user_id=world.user,
            action="post",
            resource_type="journal_entry",
            target=AdministrationScope(world.acme_books),
            attributes=ResourceAttributes(journal_id=journal, period_id=period),
        )

    assert (await service.authorize(post(sales, open_period))).allowed
    # Right period, forbidden journal.
    assert (await service.authorize(post(bank, open_period))).denied
    # Right journal, a period this grant does not cover.
    assert (await service.authorize(post(sales, uuid.uuid4()))).denied


async def test_every_condition_on_a_grant_must_hold_not_just_one() -> None:
    outcome = evaluate_conditions(
        {"amount_ceiling": "100.00", "cost_centre_ids": [str(uuid.uuid4())]},
        # Amount passes; cost centre does not.
        ResourceAttributes(amount=Decimal("50.00"), cost_centre_id=uuid.uuid4()),
    )

    assert not outcome.satisfied
    assert outcome.failed_key == "cost_centre_ids"


# --- IP allowlist -----------------------------------------------------------


@pytest.mark.parametrize(
    ("allowlist", "source_ip", "expected"),
    [
        (["203.0.113.0/24"], "203.0.113.7", True),
        (["203.0.113.0/24"], "203.0.114.7", False),
        (["203.0.113.7"], "203.0.113.7", True),  # bare address, not a network
        (["203.0.113.7"], "203.0.113.8", False),
        (["10.0.0.0/8", "203.0.113.0/24"], "10.4.5.6", True),  # any entry may match
        (["2001:db8::/32"], "2001:db8::1", True),
        (["2001:db8::/32"], "2001:db9::1", False),
        ([], "203.0.113.7", False),  # an empty allowlist permits nothing
        (["not-a-network"], "203.0.113.7", False),  # malformed entries cannot admit
        (["203.0.113.0/24"], "definitely-not-an-ip", False),
    ],
)
def test_ip_allowlist_membership(allowlist: list[str], source_ip: str, expected: bool) -> None:
    outcome = evaluate_conditions(
        {"ip_allowlist": allowlist}, ResourceAttributes(source_ip=source_ip)
    )
    assert outcome.satisfied is expected


async def test_an_ip_allowlist_denies_when_the_source_address_is_unknown() -> None:
    """A request whose client address could not be resolved is outside every
    allowlist, not inside all of them.
    """
    world = build_world()
    world.repository.assign(
        user_id=world.user,
        role="Approver",
        scope_id=world.acme_books,
        conditions={"ip_allowlist": ["203.0.113.0/24"]},
    )
    service = AuthorizationService(world.repository)

    decision = await service.authorize(_approve(world, ResourceAttributes(source_ip=None)))

    assert decision.denied


# --- the closed set ---------------------------------------------------------


def test_an_unrecognized_condition_key_denies_rather_than_being_ignored() -> None:
    """A typo'd key must not mean "no condition applies." This is the
    single most dangerous failure mode available to this module: silently
    ignoring "amount_cieling" would turn a misspelling into unlimited
    approval authority.
    """
    outcome = evaluate_conditions(
        {"amount_cieling": "5000.00"}, ResourceAttributes(amount=Decimal("1000000.00"))
    )

    assert not outcome.satisfied
    assert outcome.failed_key == "amount_cieling"


def test_device_trust_is_not_silently_accepted_while_unimplemented() -> None:
    """IAM-033 names device trust; there is no device registry to check it
    against yet. It is therefore not a recognized key, which by the rule
    above means a grant carrying it is denied - never quietly satisfied.
    """
    assert "device_trust" not in CONDITION_KEYS
    assert not evaluate_conditions({"device_trust": True}, ResourceAttributes()).satisfied


def test_every_declared_condition_key_has_an_evaluator() -> None:
    """Guards against adding a key to CONDITION_KEYS (and to the migration's
    matching CHECK) without adding its branch to evaluate_conditions - which
    would make the key storable and then permanently denying, a confusing
    half-implemented state.
    """
    source = inspect.getsource(conditions_module.evaluate_conditions)
    for key in CONDITION_KEYS:
        assert f'"{key}"' in source, f"{key} has no branch in evaluate_conditions"


def test_a_grant_with_no_conditions_is_narrowed_by_nothing() -> None:
    assert evaluate_conditions({}, ResourceAttributes()).satisfied
