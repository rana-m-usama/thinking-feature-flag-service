"""Unit tests for the evaluation engine.

The engine is pure, so these need no database, no cache and no event loop — which is
exactly why it was written that way. This is where the test weight goes: a bug here
serves the wrong feature to real users silently, whereas a bug in the CRUD layer
returns a 500 that someone notices.
"""

import pytest

from app.evaluation import BUCKET_SPACE, FlagSnapshot, Reason, bucket_for, evaluate


def snapshot(**overrides) -> FlagSnapshot:
    base = dict(
        key="checkout.new_flow",
        type="boolean",
        default_value=False,
        enabled=True,
        value=True,
        rollout_percentage=100,
        targeting_rules=[],
        archived=False,
    )
    return FlagSnapshot(**{**base, **overrides})


# --- Determinism ---------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_always_same_bucket(self):
        """The property the whole product rests on."""
        results = {bucket_for("checkout.new_flow", "user_2c91") for _ in range(1000)}
        assert len(results) == 1

    def test_buckets_are_known_constants(self):
        """Pin the exact values.

        This is a change-detector on purpose. If someone swaps the hash, reorders the
        digest bytes, or drops the ':' separator, every existing rollout silently
        reshuffles and users lose features mid-session. That must fail loudly in CI, not
        in production. These values are also reproduced by an independent Node
        implementation — see the README — which is what makes cross-language SDKs viable.
        """
        assert bucket_for("checkout.new_flow", "qa-bot") == 17
        assert bucket_for("checkout.new_flow", "user_2c91") == 67
        assert bucket_for("checkout.new_flow", "user_8f3a") == 82
        assert bucket_for("search.new_ranking", "qa-bot") == 75
        assert bucket_for("search.new_ranking", "user_2c91") == 6
        assert bucket_for("search.new_ranking", "user_8f3a") == 41

    def test_bucket_always_in_range(self):
        for i in range(10_000):
            assert 0 <= bucket_for("flag", f"user_{i}") < BUCKET_SPACE

    def test_separator_prevents_collision(self):
        """('ab', 'c') and ('a', 'bc') must not collide.

        Without the ':' separator these concatenate identically and one flag serves
        another flag's cohort — a silent correctness bug with no symptom.
        """
        assert bucket_for("ab", "c") != bucket_for("a", "bc")


# --- Distribution --------------------------------------------------------------


class TestDistribution:
    @pytest.mark.parametrize("percentage", [1, 10, 25, 50, 75, 99])
    def test_rollout_percentage_is_accurate(self, percentage):
        """A rollout of N% must actually reach ~N% of users, or the number is a lie.

        1% tolerance over 20k users. A non-uniform hash would still be perfectly
        deterministic and pass every test above while quietly making "10%" mean 3%.
        """
        users = [f"user_{i}" for i in range(20_000)]
        snap = snapshot(rollout_percentage=percentage)
        included = sum(1 for u in users if evaluate(snap, u).value is True)
        actual = included / len(users) * 100
        assert abs(actual - percentage) < 1.0, f"{percentage}% rollout reached {actual:.2f}%"

    def test_flags_are_decorrelated(self):
        """Different flags must partition the user base independently.

        If flag_key were not in the hash, the same users would land in the first 10% of
        every rollout: permanent guinea pigs, and every experiment confounded by every
        other. Overlap between two 10% cohorts should be ~1% (0.1 * 0.1), not ~10%.
        """
        users = [f"user_{i}" for i in range(20_000)]
        a = {u for u in users if bucket_for("flag.a", u) < 10}
        b = {u for u in users if bucket_for("flag.b", u) < 10}
        overlap = len(a & b) / len(users) * 100
        assert 0.5 < overlap < 1.5, f"expected ~1% overlap, got {overlap:.2f}%"


# --- Monotonicity --------------------------------------------------------------


class TestMonotonicity:
    def test_widening_never_removes_a_user(self):
        """The property that makes gradual rollout usable.

        A user at bucket 67 is at 67 whether the rollout is 10% or 70%. Raising the
        percentage only ever adds people. If this fails, real users lose a feature
        mid-session because someone ramped a rollout — a cart in the new flow and a
        payment in the old one.
        """
        users = [f"user_{i}" for i in range(5_000)]
        previous: set[str] = set()
        for pct in range(0, 101, 5):
            snap = snapshot(rollout_percentage=pct)
            current = {u for u in users if evaluate(snap, u).value is True}
            assert previous <= current, f"users dropped out when widening to {pct}%"
            previous = current

    def test_rollback_removes_exactly_the_added_cohort(self):
        """A rollback must be the precise inverse of the ramp that preceded it.

        This is what makes rolling back a valid diagnostic rather than a prayer: the
        error rate should return to exactly where it was, because the population does.
        """
        users = [f"user_{i}" for i in range(5_000)]
        at_25 = {u for u in users if evaluate(snapshot(rollout_percentage=25), u).value is True}
        at_70 = {u for u in users if evaluate(snapshot(rollout_percentage=70), u).value is True}
        back_to_25 = {
            u for u in users if evaluate(snapshot(rollout_percentage=25), u).value is True
        }
        assert back_to_25 == at_25
        assert at_25 < at_70

    def test_zero_and_hundred_are_absolute(self):
        """0% must reach nobody and 100% must reach everybody — no off-by-one at the edges.

        An off-by-one here means a "fully disabled" flag leaks to one user in a hundred.
        """
        users = [f"user_{i}" for i in range(2_000)]
        assert not any(evaluate(snapshot(rollout_percentage=0), u).value is True for u in users)
        assert all(evaluate(snapshot(rollout_percentage=100), u).value is True for u in users)


# --- Precedence ----------------------------------------------------------------


class TestPrecedence:
    def test_archived_serves_default(self):
        result = evaluate(snapshot(archived=True), "user_1")
        assert result.value is False
        assert result.reason is Reason.ARCHIVED

    def test_disabled_serves_default(self):
        result = evaluate(snapshot(enabled=False), "user_1")
        assert result.value is False
        assert result.reason is Reason.DISABLED

    def test_kill_switch_beats_targeting(self):
        """Disabling must outrank a matching rule.

        An operator turning a flag off during an incident cannot be second-guessed by a
        targeting rule someone wrote last month. If this inverts, the kill switch is not
        a kill switch.
        """
        snap = snapshot(
            enabled=False,
            targeting_rules=[
                {"attribute": "plan", "operator": "in", "values": ["enterprise"], "value": True}
            ],
        )
        result = evaluate(snap, "user_1", {"plan": "enterprise"})
        assert result.value is False
        assert result.reason is Reason.DISABLED

    def test_targeting_beats_rollout(self):
        """qa-bot sits at bucket 17, so a 1% rollout excludes it — the rule must not.

        Otherwise QA is pinned to a hash bucket and tests a coin flip instead of the
        feature they were asked to verify.
        """
        assert bucket_for("checkout.new_flow", "qa-bot") == 17  # outside a 1% rollout
        snap = snapshot(
            rollout_percentage=1,
            targeting_rules=[
                {"attribute": "user_id", "operator": "in", "values": ["qa-bot"], "value": True}
            ],
        )
        result = evaluate(snap, "qa-bot")
        assert result.value is True
        assert result.reason is Reason.TARGETING_MATCH

    def test_first_matching_rule_wins(self):
        snap = snapshot(
            type="string",
            default_value="control",
            value="treatment",
            targeting_rules=[
                {"attribute": "plan", "operator": "in", "values": ["enterprise"], "value": "first"},
                {
                    "attribute": "plan",
                    "operator": "in",
                    "values": ["enterprise"],
                    "value": "second",
                },
            ],
        )
        assert evaluate(snap, "user_1", {"plan": "enterprise"}).value == "first"

    def test_outside_rollout_serves_default(self):
        result = evaluate(snapshot(rollout_percentage=0), "user_1")
        assert result.value is False
        assert result.reason is Reason.OUTSIDE_ROLLOUT


# --- Targeting rules -----------------------------------------------------------


class TestTargeting:
    @pytest.mark.parametrize(
        "operator,values,context_value,expected",
        [
            ("in", ["enterprise", "pro"], "enterprise", True),
            ("in", ["enterprise"], "free", False),
            ("not_in", ["free"], "enterprise", True),
            ("not_in", ["free"], "free", False),
            ("eq", ["DE"], "DE", True),
            ("eq", ["DE"], "FR", False),
            ("neq", ["DE"], "FR", True),
            ("contains", ["bizscout"], "user@bizscout.com", True),
            ("contains", ["acme"], "user@bizscout.com", False),
            ("starts_with", ["admin_"], "admin_jane", True),
            ("starts_with", ["admin_"], "user_jane", False),
            ("ends_with", ["@bizscout.com"], "jane@bizscout.com", True),
            ("ends_with", ["@bizscout.com"], "jane@acme.com", False),
        ],
    )
    def test_operators(self, operator, values, context_value, expected):
        snap = snapshot(
            rollout_percentage=0,  # isolate the rule: without a match, this serves default
            targeting_rules=[
                {"attribute": "attr", "operator": operator, "values": values, "value": True}
            ],
        )
        result = evaluate(snap, "user_1", {"attr": context_value})
        assert (result.reason is Reason.TARGETING_MATCH) is expected

    def test_user_id_is_targetable_without_being_in_context(self):
        """`user_id` is a first-class attribute — it arrives on the request, not in context."""
        snap = snapshot(
            rollout_percentage=0,
            targeting_rules=[
                {"attribute": "user_id", "operator": "in", "values": ["user_7"], "value": True}
            ],
        )
        assert evaluate(snap, "user_7").reason is Reason.TARGETING_MATCH
        assert evaluate(snap, "user_8").reason is Reason.OUTSIDE_ROLLOUT

    def test_missing_attribute_does_not_match(self):
        snap = snapshot(
            rollout_percentage=0,
            targeting_rules=[
                {"attribute": "plan", "operator": "in", "values": ["enterprise"], "value": True}
            ],
        )
        assert evaluate(snap, "user_1", {}).reason is Reason.OUTSIDE_ROLLOUT

    def test_malformed_rule_fails_safe(self):
        """A bad rule must not take down evaluation for every other flag in the request.

        It falls through to the rollout, which can only ever serve default_value — a
        malformed rule can fail to turn a feature on, never fail it on.
        """
        snap = snapshot(
            rollout_percentage=0,
            targeting_rules=[
                {"attribute": "plan", "operator": "oprator_typo", "values": ["x"], "value": True},
                {},
            ],
        )
        result = evaluate(snap, "user_1", {"plan": "x"})
        assert result.value is False
        assert result.reason is Reason.OUTSIDE_ROLLOUT


# --- Types ---------------------------------------------------------------------


class TestFlagTypes:
    def test_boolean(self):
        assert evaluate(snapshot(), "user_1").value is True

    def test_string_variant_selection(self):
        """The spec's "string (variant selection)", done with the rollout.

        Two values and a percentage is what makes a string flag mean anything: a flag
        carrying only default_value returns it to everyone and the rollout has nothing
        to select between.
        """
        snap = snapshot(
            type="string", default_value="control", value="treatment", rollout_percentage=50
        )
        users = [f"user_{i}" for i in range(2_000)]
        values = {u: evaluate(snap, u).value for u in users}
        assert set(values.values()) == {"control", "treatment"}
        treatment_share = sum(1 for v in values.values() if v == "treatment") / len(users) * 100
        assert abs(treatment_share - 50) < 2.0

    def test_number(self):
        snap = snapshot(type="number", default_value=10, value=25, rollout_percentage=100)
        assert evaluate(snap, "user_1").value == 25

    def test_falsy_values_are_not_conflated_with_absence(self):
        """0 and "" are legitimate flag values, not "no value".

        A truthiness check anywhere in the engine would serve the default here and be
        almost impossible to spot in production.
        """
        assert evaluate(snapshot(type="number", default_value=5, value=0), "user_1").value == 0
        assert evaluate(snapshot(type="string", default_value="x", value=""), "user_1").value == ""
