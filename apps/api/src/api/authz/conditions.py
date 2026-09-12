"""IAM-033: "Attribute conditions supplement roles: amount ceilings, cost
centre restriction, journal restriction, period restriction, IP allowlist,
device trust."

A condition NARROWS a grant the role already carries. It can never widen
one: there is no condition that adds a permission, only conditions that
withhold one the role would otherwise have allowed. That asymmetry is why
conditions are safe to store per assignment - an attacker who could somehow
write to role_assignment.conditions could restrict their own access, not
extend it.

Three rules make this module fail closed:

  1. An unknown condition key DENIES. It is never ignored. A typo'd key on
     a grant ("amount_cieling") would otherwise silently mean "no ceiling
     applies," turning a misspelling into unlimited approval authority.
     migrations/0009_authorization.sql refuses to store an unknown key at
     all; this is the second line, for a grant that reached memory some
     other way (a test fake, a future import path).
  2. A missing ATTRIBUTE denies. If a grant carries an amount ceiling and
     the caller supplied no amount, the answer is no - not "unrestricted."
     This is the property that makes a route which forgets to declare where
     its amount comes from fail safely; see api.authz.dependencies.
  3. A malformed condition VALUE denies. A ceiling that will not parse as a
     decimal, or an allowlist entry that is not a network, is treated as
     unsatisfied rather than raised - a broken grant must not be able to
     take an endpoint down, and must not be able to grant anything either.

Device trust is the one item in IAM-033's list with no implementation here.
There is no device registry in this codebase to check against (sessions
record a user agent, not an attested device key), so rather than ship a
condition that silently passes, `device_trust` is simply not a recognized
key - and by rule 1 above, a grant carrying it is denied until the registry
and this evaluator's branch for it exist together.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from api.authz.model import ResourceAttributes

# The closed set, mirroring the CHECK in role_assignment_conditions_guard().
# Adding a member here without adding its evaluator below is caught by
# tests/authz/test_conditions.py, which asserts the two stay in step.
CONDITION_KEYS = frozenset(
    {
        "amount_ceiling",
        "cost_centre_ids",
        "journal_ids",
        "period_ids",
        "ip_allowlist",
    }
)


@dataclass(frozen=True, slots=True)
class ConditionOutcome:
    satisfied: bool
    failed_key: str | None = None
    detail: str | None = None


def _amount_within_ceiling(raw_ceiling: Any, attributes: ResourceAttributes) -> ConditionOutcome:
    if attributes.amount is None:
        return ConditionOutcome(False, "amount_ceiling", "no amount supplied for this request")

    # Rejecting float here is not defensive over-checking: a float that
    # reached this point would mean some caller parsed JSON with the stdlib
    # default, and NFR-031 forbids a float anywhere in the calculation path.
    # Silently coercing it would launder the very bug this catches.
    if isinstance(raw_ceiling, float):
        return ConditionOutcome(
            False, "amount_ceiling", "ceiling stored as a float (NFR-031 requires decimal)"
        )

    try:
        ceiling = Decimal(str(raw_ceiling))
    except (InvalidOperation, ValueError):
        return ConditionOutcome(False, "amount_ceiling", f"unparseable ceiling {raw_ceiling!r}")

    if attributes.amount > ceiling:
        return ConditionOutcome(
            False, "amount_ceiling", f"amount {attributes.amount} exceeds ceiling {ceiling}"
        )
    return ConditionOutcome(True)


def _id_in_allowed_set(key: str, raw_allowed: Any, supplied: uuid.UUID | None) -> ConditionOutcome:
    if supplied is None:
        return ConditionOutcome(False, key, f"no {key[:-4]} supplied for this request")

    if not isinstance(raw_allowed, list | tuple | set | frozenset):
        return ConditionOutcome(False, key, f"{key} is not a list")

    allowed = {str(item).lower() for item in raw_allowed}
    if str(supplied).lower() not in allowed:
        return ConditionOutcome(False, key, f"{supplied} is not among the permitted values")
    return ConditionOutcome(True)


def _ip_in_allowlist(raw_allowlist: Any, attributes: ResourceAttributes) -> ConditionOutcome:
    if attributes.source_ip is None:
        return ConditionOutcome(False, "ip_allowlist", "request has no resolvable source address")

    if not isinstance(raw_allowlist, list | tuple | set | frozenset):
        return ConditionOutcome(False, "ip_allowlist", "ip_allowlist is not a list")

    try:
        address = ipaddress.ip_address(attributes.source_ip)
    except ValueError:
        return ConditionOutcome(
            False, "ip_allowlist", f"unparseable source address {attributes.source_ip!r}"
        )

    for entry in raw_allowlist:
        try:
            # strict=False so a plain address ("203.0.113.7") and a network
            # ("203.0.113.0/24") are both accepted as allowlist entries.
            network = ipaddress.ip_network(str(entry), strict=False)
        except ValueError:
            # A malformed entry is skipped rather than fatal, but it can
            # only ever cost access, never grant it - if no other entry
            # matches, the outcome below is still a denial.
            continue
        if address in network:
            return ConditionOutcome(True)

    return ConditionOutcome(
        False, "ip_allowlist", f"{attributes.source_ip} is outside the permitted networks"
    )


def evaluate_conditions(
    conditions: Mapping[str, Any], attributes: ResourceAttributes
) -> ConditionOutcome:
    """Every condition on the grant must hold. An empty condition map is
    satisfied - a grant with no attribute conditions is narrowed by nothing,
    which is the ordinary case.
    """
    for key, value in conditions.items():
        if key not in CONDITION_KEYS:
            return ConditionOutcome(False, key, "unrecognized attribute condition")

        outcome: ConditionOutcome
        if key == "amount_ceiling":
            outcome = _amount_within_ceiling(value, attributes)
        elif key == "cost_centre_ids":
            outcome = _id_in_allowed_set(key, value, attributes.cost_centre_id)
        elif key == "journal_ids":
            outcome = _id_in_allowed_set(key, value, attributes.journal_id)
        elif key == "period_ids":
            outcome = _id_in_allowed_set(key, value, attributes.period_id)
        else:  # key == "ip_allowlist"
            outcome = _ip_in_allowlist(value, attributes)

        if not outcome.satisfied:
            return outcome

    return ConditionOutcome(True)
