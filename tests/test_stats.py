"""The judging math, on data where the right answer is known."""

import math
import random

import pytest

from judging import stats
from judging.stats import Review


def test_weighted_total_normalises_weights():
    assert stats.weighted_total({"a": 4, "b": 2}, {"a": 3, "b": 1}) == pytest.approx(3.5)
    assert stats.weighted_total({"a": 4, "b": 2}, {"a": 30, "b": 10}) == pytest.approx(3.5)


def _exact(biases, quality, design):
    return [Review(j, p, quality[p] + biases[j]) for j, p in design]


def test_judge_effect_recovers_known_biases_without_noise():
    rng = random.Random(1)
    judges = [f"j{i}" for i in range(8)]
    projects = [f"p{i}" for i in range(20)]
    design = [(j, p) for p in projects for j in rng.sample(judges, 3)]
    biases = {j: rng.uniform(-1, 1) for j in judges}
    mean_bias = sum(biases.values()) / len(biases)
    quality = {p: rng.uniform(2, 4) for p in projects}
    result = stats.judge_effect(_exact(biases, quality, design), lam=1e-6)
    for j in judges:
        # Biases are identified up to a shared constant; the ridge centres them.
        assert result.judges[j].bias == pytest.approx(biases[j] - mean_bias, abs=1e-3)
    for p in projects:
        assert result.projects[p].score == pytest.approx(quality[p] + mean_bias, abs=1e-3)


def test_judge_effect_ranks_perfectly_when_raw_mean_cannot():
    """A lenient judge who happens to review weak projects fools the raw mean."""
    quality = {"strong": 4.0, "weak": 3.0, "mid": 3.5}
    biases = {"lenient": 1.2, "fair": 0.0, "harsh": -0.6}
    design = [("lenient", "weak"), ("fair", "weak"), ("fair", "strong"), ("harsh", "strong"),
              ("harsh", "mid"), ("lenient", "mid")]
    reviews = _exact(biases, quality, design)
    raw = stats.raw_mean(reviews).ranking()
    fitted = stats.judge_effect(reviews, lam=0.01).ranking()
    assert fitted == ["strong", "mid", "weak"]
    assert raw != fitted


def test_lambda_estimate_backs_off_when_judges_agree():
    rng = random.Random(7)
    quality = {f"p{i}": rng.uniform(2, 4.5) for i in range(30)}
    judges = [f"j{i}" for i in range(10)]
    reviews = [Review(j, p, quality[p] + rng.gauss(0, 0.4)) for p in quality for j in rng.sample(judges, 3)]
    result = stats.judge_effect(reviews)
    assert result.diagnostics["lambda"] >= 10
    raw = {p: s.score for p, s in stats.raw_mean(reviews).projects.items()}
    fitted = {p: s.score for p, s in result.projects.items()}
    assert stats.spearman(raw, fitted) > 0.97


def test_flat_judge_is_detected_and_does_not_break_zscore():
    reviews = [Review("flat", p, 4.0) for p in ("a", "b", "c")] + \
              [Review("normal", "a", 2.0), Review("normal", "b", 3.0), Review("normal", "c", 5.0)]
    profiles = stats.judge_profiles(reviews)
    assert profiles["flat"].flat and not profiles["normal"].flat
    z = stats.zscore(reviews)
    assert all(math.isfinite(s.score) for s in z.projects.values())
    assert z.ranking() == ["c", "b", "a"]


def test_single_review_judge_is_shrunk_to_the_pool():
    reviews = [Review("once", "a", 5.0)] + [Review(f"j{i}", p, 3.0 + i * 0.1)
                                             for i in range(4) for p in "abc"]
    result = stats.judge_effect(reviews, lam=3.0)
    assert abs(result.judges["once"].bias) < 0.5


def test_components_find_disconnected_judges():
    reviews = [Review("a", "p1", 3), Review("b", "p1", 4), Review("c", "p2", 3), Review("d", "p2", 2)]
    groups = stats.components(reviews)
    assert sorted(map(sorted, groups)) == [["a", "b"], ["c", "d"]]


def test_bradley_terry_matches_known_odds():
    # A beats B three times as often as B beats A: s_A / s_B should approach 3.
    comparisons = [("A", "B", 1.0)] * 300 + [("B", "A", 1.0)] * 100
    s = stats.bradley_terry(comparisons, prior=0.01)
    assert math.exp(s["A"] - s["B"]) == pytest.approx(3.0, rel=0.02)


def test_bradley_terry_stays_finite_for_undefeated_items():
    s = stats.bradley_terry([("A", "B", 1.0), ("A", "C", 1.0), ("B", "C", 1.0)])
    assert all(math.isfinite(v) for v in s.values())
    assert s["A"] > s["B"] > s["C"]


def test_implied_comparisons_ignore_a_judges_level():
    harsh = [Review("harsh", "x", 1.0), Review("harsh", "y", 2.0)]
    lenient = [Review("lenient", "x", 4.0), Review("lenient", "y", 5.0)]
    assert stats.implied_bt(harsh).ranking() == stats.implied_bt(lenient).ranking() == ["y", "x"]


def test_spearman_handles_ties():
    assert stats.spearman({"a": 1, "b": 2, "c": 3}, {"a": 10, "b": 20, "c": 30}) == pytest.approx(1.0)
    assert stats.spearman({"a": 1, "b": 2, "c": 3}, {"a": 3, "b": 2, "c": 1}) == pytest.approx(-1.0)
    assert math.isfinite(stats.spearman({"a": 1, "b": 1, "c": 2}, {"a": 1, "b": 2, "c": 3}))


def test_judge_effect_beats_raw_mean_on_the_fixture_design(fixture_data):
    """The claim in JUDGING.md, as a test: on the fixture's own judge-project graph,
    with biased judges, the default method recovers the true ranking better."""
    design = [(s["judge"], s["project"]) for s in fixture_data["scores"] if s["project"] != "prj_41"]
    summary = stats.recovery_study(design, stats.Scenario(bias_sd=0.6), runs=40, seed=11)
    assert summary["judge_effect"]["spearman_mean"] > summary["raw"]["spearman_mean"] + 0.02


def test_judge_effect_beats_zscore_when_tracks_differ(fixture_data):
    design = [(s["judge"], s["project"]) for s in fixture_data["scores"] if s["project"] != "prj_41"]
    track_of = {p["id"]: p["track"] for p in fixture_data["projects"]}
    summary = stats.recovery_study(design, stats.Scenario(bias_sd=0.3, track_sd=0.6), runs=40, seed=5,
                                   track_of=track_of)
    assert summary["judge_effect"]["spearman_mean"] > summary["zscore"]["spearman_mean"]
