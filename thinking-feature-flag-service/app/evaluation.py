"""The flag evaluation engine.

Deliberately pure: no database, no cache, no clock, no randomness. Everything it
needs arrives as arguments, so it is exhaustively unit-testable and any instance of
the service computes the same answer for the same inputs. That property is the whole
product — see `bucket_for`.
"""

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any

BUCKET_SPACE = 100


def bucket_for(flag_key: str, user_id: str) -> int:
    """Map (flag_key, user_id) onto a stable bucket in [0, 100).

    This is the spec's `hash(flag_key + user_id)` and it is the reason percentage
    rollouts work at all. Because it is a pure function of two strings, every process
    on every instance computes the same bucket forever — a user cannot be inside the
    rollout on the cart page and outside it at payment.

    Widening a rollout is therefore purely additive: a user at bucket 67 is at 67
    whether the rollout is 10% or 70%, so raising the percentage only ever adds people
    and never takes the feature away from someone mid-session.

    Three details that are easy to get wrong:

    * The ``:`` separator is load-bearing. Without it ``("ab", "c")`` and ``("a", "bc")``
      hash identically and one flag silently serves another flag's cohort.
    * ``flag_key`` is in the digest so each flag gets an independent partition of the
      user base. Hashing ``user_id`` alone would put the same unlucky cohort in the
      first 10% of *every* rollout, making them permanent guinea pigs and confounding
      every experiment against every other.
    * SHA-256 rather than a faster non-cryptographic hash, and a big-endian uint32
      rather than a language-native hash, so that an SDK computing buckets client-side
      in Go or Python arrives at the same number. Uniformity and reproducibility are
      what matter here, not cryptographic strength.

    Modulo bias is present and ignored on purpose: 2**32 % 100 == 96, so buckets 0-95
    are more likely than 96-99 by roughly 0.000002%. Correcting it costs more code than
    the error costs anyone.
    """
    digest = sha256(f"{flag_key}:{user_id}".encode()).digest()
    return int.from_bytes(digest[:4], byteorder="big") % BUCKET_SPACE


class Reason(StrEnum):
    """Why the engine returned what it returned.

    Not exposed on the API response — the spec asks only for values — but carried on
    the result so the structured logs and the unit tests can assert on *why* a user
    got a value, not merely that they got one.
    """

    ARCHIVED = "archived"
    NO_CONFIG = "no_config"
    DISABLED = "disabled"
    TARGETING_MATCH = "targeting_match"
    OUTSIDE_ROLLOUT = "outside_rollout"
    IN_ROLLOUT = "in_rollout"


@dataclass(frozen=True)
class FlagSnapshot:
    """A flag plus its config for one environment, flattened.

    This is exactly the row shape the compiled cache holds, which is why the engine
    takes it rather than ORM objects: evaluation never touches the database.
    """

    key: str
    type: str
    default_value: Any
    enabled: bool
    value: Any
    rollout_percentage: int
    targeting_rules: list[dict]
    archived: bool = False


@dataclass(frozen=True)
class Evaluation:
    key: str
    value: Any
    reason: Reason


def _matches(rule: dict, user_id: str, context: dict[str, Any]) -> bool:
    """Evaluate one targeting rule against a user context.

    Unknown attributes and unknown operators return False rather than raising: a
    malformed rule must not take down evaluation for every other flag in the request.
    A flag that fails to match falls through to the rollout, which is the safe
    direction — it can only serve `default_value`, never leak a feature on.
    """
    attribute = rule.get("attribute")
    operator = rule.get("operator")
    expected = rule.get("values", [])

    actual = user_id if attribute == "user_id" else context.get(attribute)
    if actual is None:
        return False

    match operator:
        case "in":
            return actual in expected
        case "not_in":
            return actual not in expected
        case "eq":
            return bool(expected) and actual == expected[0]
        case "neq":
            return bool(expected) and actual != expected[0]
        case "contains":
            return isinstance(actual, str) and any(str(v) in actual for v in expected)
        case "starts_with":
            return isinstance(actual, str) and any(actual.startswith(str(v)) for v in expected)
        case "ends_with":
            return isinstance(actual, str) and any(actual.endswith(str(v)) for v in expected)
        case _:
            return False


def evaluate(
    snapshot: FlagSnapshot, user_id: str, context: dict[str, Any] | None = None
) -> Evaluation:
    """Resolve one flag for one user.

    Precedence, in order — the ordering is the design, not an implementation detail:

    1. Archived              -> default_value
    2. Disabled              -> default_value.  The kill switch outranks targeting: an
                                operator turning a flag off during an incident must not
                                be second-guessed by a rule someone wrote last month.
    3. First matching rule   -> the rule's value.  Explicit targeting outranks the
                                rollout, otherwise a QA user pinned to `true` would sit
                                at their hash bucket and test a coin flip.
    4. Outside the rollout   -> default_value
    5. Otherwise             -> value
    """
    context = context or {}

    if snapshot.archived:
        return Evaluation(snapshot.key, snapshot.default_value, Reason.ARCHIVED)

    if not snapshot.enabled:
        return Evaluation(snapshot.key, snapshot.default_value, Reason.DISABLED)

    for rule in snapshot.targeting_rules:
        if _matches(rule, user_id, context):
            return Evaluation(snapshot.key, rule.get("value"), Reason.TARGETING_MATCH)

    if bucket_for(snapshot.key, user_id) >= snapshot.rollout_percentage:
        return Evaluation(snapshot.key, snapshot.default_value, Reason.OUTSIDE_ROLLOUT)

    return Evaluation(snapshot.key, snapshot.value, Reason.IN_ROLLOUT)


def evaluate_all(
    snapshots: list[FlagSnapshot], user_id: str, context: dict[str, Any] | None = None
) -> list[Evaluation]:
    """Bulk path. Same engine, one pass — no per-flag I/O to amortise."""
    return [evaluate(s, user_id, context) for s in snapshots]
