"""The judging math. Pure Python, no Django, no numpy: every function here takes plain
data and returns plain data, so it can be tested, audited and re-run by anyone.

JUDGING.md explains and defends every choice. In brief:

- `raw_mean`          the average weighted score. The baseline everything is compared to.
- `zscore`            each judge's scores standardised against that judge's own mean and
                      spread, shrunk toward the pool for judges with few reviews.
- `judge_effect`      the default. Fits score = project quality + judge bias + noise by
                      ridge-regularised least squares. Unlike z-scores it knows WHICH
                      projects each judge saw, so a judge who drew a strong batch is not
                      mistaken for a lenient one.
- `implied_bt`        Bradley–Terry on the pairwise orderings implied by each judge's
                      scores: a judge's absolute level cannot matter, only their ranking.
- `bradley_terry`     the estimator itself, also used for pairwise-mode judging.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field

Z95 = 1.959963984540054


@dataclass(frozen=True)
class Review:
    judge: str
    project: str
    value: float  # the review's weighted total, on the event's scale


@dataclass
class ProjectScore:
    project: str
    score: float
    n: int
    raw_mean: float
    se: float | None = None

    @property
    def ci(self):
        if self.se is None:
            return None
        return (self.score - Z95 * self.se, self.score + Z95 * self.se)


@dataclass
class JudgeEffect:
    judge: str
    n: int
    mean: float
    sd: float
    bias: float | None = None     # judge_effect: estimated leniency (+) or harshness (-)
    flat: bool = False            # gave (almost) the same total to everything they scored


@dataclass
class MethodResult:
    method: str
    projects: dict[str, ProjectScore]
    judges: dict[str, JudgeEffect] = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)

    def ranking(self):
        """Project ids, best first. Ties broken by more reviews, then by id, so the
        order is total and reproducible."""
        return sorted(self.projects, key=lambda p: (-self.projects[p].score, -self.projects[p].n, p))


# --- building blocks -----------------------------------------------------------------

def weighted_total(values: dict, weights: dict) -> float:
    """The weighted mean of a review's criterion values. Weights need not sum to one."""
    total_weight = sum(float(weights[k]) for k in values)
    if total_weight <= 0:
        raise ValueError("weights must be positive")
    return sum(float(weights[k]) * float(v) for k, v in values.items()) / total_weight


def _mean(xs):
    return sum(xs) / len(xs)


def _pvar(xs):
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / len(xs)


def _group(reviews, key):
    out = defaultdict(list)
    for r in reviews:
        out[getattr(r, key)].append(r)
    return out


def judge_profiles(reviews, flat_tolerance=1e-9) -> dict[str, JudgeEffect]:
    out = {}
    for judge, rs in _group(reviews, "judge").items():
        values = [r.value for r in rs]
        sd = math.sqrt(_pvar(values))
        out[judge] = JudgeEffect(judge, len(rs), _mean(values), sd,
                                 flat=len(rs) >= 2 and sd <= flat_tolerance)
    return out


def components(reviews) -> list[set[str]]:
    """Connected components of the judge graph (judges linked by a shared project).

    Judge biases are only comparable within a component. If there is more than one,
    normalization can even out judges inside each group but cannot compare the groups'
    overall generosity; the dashboard says so.
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for rs in _group(reviews, "project").values():
        judges = [r.judge for r in rs]
        for j in judges:
            find(j)
        for a, b in zip(judges, judges[1:], strict=False):
            parent[find(a)] = find(b)
    groups = defaultdict(set)
    for j in parent:
        groups[find(j)].add(j)
    return sorted(groups.values(), key=lambda g: (-len(g), sorted(g)))


# --- methods -------------------------------------------------------------------------

def raw_mean(reviews) -> MethodResult:
    by_project = _group(reviews, "project")
    all_values = [r.value for r in reviews]
    pooled_sd = math.sqrt(_pvar(all_values)) if len(all_values) > 1 else 0.0
    projects = {}
    for p, rs in by_project.items():
        values = [r.value for r in rs]
        m = _mean(values)
        projects[p] = ProjectScore(p, m, len(values), m, se=pooled_sd / math.sqrt(len(values)))
    return MethodResult("raw", projects, judge_profiles(reviews))


def zscore(reviews, prior=3.0) -> MethodResult:
    """Per-judge standardisation with shrinkage.

    A judge's mean and variance are pulled toward the pooled mean and variance as if the
    judge had also scored `prior` perfectly average projects. With one or two reviews
    the pool dominates; with ten the judge's own numbers do. It also keeps a flat judge
    (variance zero) from dividing by zero. Standardised values are mapped back onto the
    pooled scale so scores stay readable as "out of 5".
    """
    values = [r.value for r in reviews]
    mu, var = _mean(values), _pvar(values)
    sd = math.sqrt(var) or 1.0
    profiles = judge_profiles(reviews)
    by_judge = _group(reviews, "judge")
    adjusted = []
    for judge, rs in by_judge.items():
        xs = [r.value for r in rs]
        n = len(xs)
        m_star = (n * _mean(xs) + prior * mu) / (n + prior)
        v_star = (n * _pvar(xs) + prior * var) / (n + prior)
        s_star = math.sqrt(v_star) or sd
        for r in rs:
            adjusted.append(Review(r.judge, r.project, mu + sd * (r.value - m_star) / s_star))
        profiles[judge].bias = m_star - mu
    by_project = _group(adjusted, "project")
    raw = _group(reviews, "project")
    projects = {
        p: ProjectScore(p, _mean([r.value for r in rs]), len(rs), _mean([r.value for r in raw[p]]),
                        se=sd / math.sqrt(len(rs)))
        for p, rs in by_project.items()
    }
    return MethodResult("zscore", projects, profiles, {"prior": prior})


def _solve(matrix, vector):
    """Gaussian elimination with partial pivoting. Small dense systems only (one row
    per judge), which is all judging ever needs."""
    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("singular system")
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(col + 1, n):
            f = a[r][col] / a[col][col]
            if f:
                for c in range(col, n + 1):
                    a[r][c] -= f * a[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (a[r][n] - sum(a[r][c] * x[c] for c in range(r + 1, n))) / a[r][r]
    return x


def _fit_judge_effect(reviews, lam):
    """Exact ridge solution of  y = theta_project + b_judge + e,  penalty lam * sum(b^2).

    Setting the gradient to zero gives theta_p = mean_p(y) - mean_p(b), and substituting
    that back leaves one linear equation per judge:

        (n_j + lam) b_j - sum over j's projects p of (1/n_p) sum over p's judges k of b_k
            = sum over j's reviews of (y - mean_p(y))

    The matrix is symmetric positive definite for lam > 0, so the solution is unique
    even when some judges share no projects at all.
    """
    judges = sorted({r.judge for r in reviews})
    index = {j: i for i, j in enumerate(judges)}
    by_project = _group(reviews, "project")
    ybar = {p: _mean([r.value for r in rs]) for p, rs in by_project.items()}
    n = len(judges)
    a = [[0.0] * n for _ in range(n)]
    c = [0.0] * n
    for i in range(n):
        a[i][i] = lam
    for p, rs in by_project.items():
        inv = 1.0 / len(rs)
        for r in rs:
            i = index[r.judge]
            a[i][i] += 1.0
            c[i] += r.value - ybar[p]
            for other in rs:
                a[i][index[other.judge]] -= inv
    b = dict(zip(judges, _solve(a, c), strict=True))
    theta = {p: ybar[p] - _mean([b[r.judge] for r in rs]) for p, rs in by_project.items()}
    residuals = [r.value - theta[r.project] - b[r.judge] for r in reviews]
    return theta, b, residuals


def judge_effect(reviews, lam=None, lam_bounds=(0.5, 50.0), max_iter=100) -> MethodResult:
    """The default method. See the module docstring and JUDGING.md.

    `lam` is the prior strength, measured in reviews: a judge's bias is shrunk toward
    zero as if they had also written `lam` perfectly unbiased reviews. If not given it
    is estimated from the data by empirical Bayes (an EM iteration on the variance of
    the judge effects), so when judges genuinely do not differ, lam grows, the biases
    vanish, and the method returns the raw means rather than inventing corrections.
    """
    by_judge = _group(reviews, "judge")
    by_project = _group(reviews, "project")
    n_total, n_projects, n_judges = len(reviews), len(by_project), len(by_judge)
    lo, hi = lam_bounds
    estimated = lam is None
    current = 3.0 if lam is None else float(lam)
    sigma2 = tau2 = None
    for _ in range(max_iter if estimated else 1):
        theta, b, residuals = _fit_judge_effect(reviews, current)
        df_b = sum(len(rs) / (len(rs) + current) for rs in by_judge.values())
        dof = max(n_total - n_projects - df_b, 1.0)
        sigma2 = max(sum(e * e for e in residuals) / dof, 1e-6)
        tau2 = (sum(v * v for v in b.values())
                + sum(sigma2 / (len(rs) + current) for rs in by_judge.values())) / n_judges
        if not estimated:
            break
        new = min(max(sigma2 / max(tau2, 1e-9), lo), hi)
        if abs(new - current) < 1e-6:
            current = new
            break
        current = new
    theta, b, residuals = _fit_judge_effect(reviews, current)

    profiles = judge_profiles(reviews)
    for j, effect in profiles.items():
        effect.bias = b[j]
    sigma = math.sqrt(sigma2)
    projects = {
        p: ProjectScore(p, theta[p], len(rs), _mean([r.value for r in rs]), se=sigma / math.sqrt(len(rs)))
        for p, rs in by_project.items()
    }
    return MethodResult("judge_effect", projects, profiles, {
        "lambda": current,
        "lambda_estimated": estimated,
        "residual_sd": sigma,
        "judge_sd": math.sqrt(tau2),
        "components": len(components(reviews)),
    })


# --- Bradley–Terry ------------------------------------------------------------------

def bradley_terry(comparisons, items=None, prior=1.0, max_iter=10000, tol=1e-10):
    """Bradley–Terry strengths by the MM algorithm (Hunter, 2004).

    `comparisons` is an iterable of (winner, loser, weight). P(i beats j) = s_i / (s_i + s_j).

    Every item also plays `prior` virtual wins and `prior` virtual losses against a
    fixed reference item of strength 1. That is a weak Bayesian prior: it keeps an item
    that won (or lost) every comparison at a finite strength, makes items with no
    comparisons sit at exactly 1, and anchors the scale so no renormalisation is needed.

    Returns {item: log strength}; 0 is the reference (an average item), +1 means e
    times the odds of beating it.
    """
    wins = defaultdict(float)
    pair = defaultdict(float)
    names = set(items or [])
    for winner, loser, weight in comparisons:
        if winner == loser:
            continue
        wins[winner] += weight
        key = (winner, loser) if winner < loser else (loser, winner)
        pair[key] += weight
        names.update((winner, loser))
    opponents = defaultdict(list)
    for (a, b_), n in pair.items():
        opponents[a].append((b_, n))
        opponents[b_].append((a, n))
    s = dict.fromkeys(names, 1.0)
    for _ in range(max_iter):
        delta = 0.0
        new = {}
        for i in names:
            denom = sum(n / (s[i] + s[j]) for j, n in opponents[i]) + 2 * prior / (s[i] + 1.0)
            new[i] = (wins[i] + prior) / denom
            delta = max(delta, abs(new[i] - s[i]) / s[i])
        s = new
        if delta < tol:
            break
    return {i: math.log(v) for i, v in s.items()}


def implied_comparisons(reviews):
    """Each judge's scores, read as a ranking of the projects that judge saw.

    For every pair a judge scored, the higher total wins; a tie counts half each way.
    Pairs are weighted 1/(n-1) for a judge with n reviews, so each review carries the
    same total weight however many projects its judge scored.
    """
    out = []
    for rs in _group(reviews, "judge").values():
        if len(rs) < 2:
            continue
        weight = 1.0 / (len(rs) - 1)
        for i, a in enumerate(rs):
            for b_ in rs[i + 1:]:
                if a.value > b_.value:
                    out.append((a.project, b_.project, weight))
                elif b_.value > a.value:
                    out.append((b_.project, a.project, weight))
                else:
                    out.append((a.project, b_.project, weight / 2))
                    out.append((b_.project, a.project, weight / 2))
    return out


def implied_bt(reviews, prior=1.0) -> MethodResult:
    by_project = _group(reviews, "project")
    strengths = bradley_terry(implied_comparisons(reviews), items=by_project, prior=prior)
    projects = {
        p: ProjectScore(p, strengths[p], len(rs), _mean([r.value for r in rs]))
        for p, rs in by_project.items()
    }
    return MethodResult("implied_bt", projects, judge_profiles(reviews), {"prior": prior})


def pairwise_bt(comparisons, projects, prior=1.0) -> MethodResult:
    counts = defaultdict(int)
    for winner, loser, _ in comparisons:
        counts[winner] += 1
        counts[loser] += 1
    strengths = bradley_terry(comparisons, items=projects, prior=prior)
    return MethodResult("pairwise", {
        p: ProjectScore(p, strengths.get(p, 0.0), counts[p], float("nan")) for p in projects
    }, {}, {"prior": prior, "comparisons": len(comparisons)})


METHODS = {
    "raw": raw_mean,
    "zscore": zscore,
    "judge_effect": judge_effect,
    "implied_bt": implied_bt,
}


# --- comparing rankings --------------------------------------------------------------

def _ranks(values: dict) -> dict:
    """Average ranks (1 = smallest), ties sharing the mean of their positions."""
    order = sorted(values, key=values.get)
    ranks, i = {}, 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(a: dict, b: dict) -> float:
    keys = sorted(set(a) & set(b))
    if len(keys) < 2:
        return float("nan")
    ra, rb = _ranks({k: a[k] for k in keys}), _ranks({k: b[k] for k in keys})
    ma, mb = _mean(list(ra.values())), _mean(list(rb.values()))
    cov = sum((ra[k] - ma) * (rb[k] - mb) for k in keys)
    va = sum((ra[k] - ma) ** 2 for k in keys)
    vb = sum((rb[k] - mb) ** 2 for k in keys)
    return cov / math.sqrt(va * vb) if va and vb else float("nan")


def top_k_overlap(truth: dict, estimate: dict, k: int) -> float:
    best = set(sorted(truth, key=lambda p: -truth[p])[:k])
    picked = set(sorted(estimate, key=lambda p: -estimate[p])[:k])
    return len(best & picked) / k


# --- simulation, for the proof in JUDGING.md -----------------------------------------------

@dataclass
class Scenario:
    quality_sd: float = 0.6
    bias_sd: float = 0.5
    noise_sd: float = 0.6
    track_sd: float = 0.0        # real differences in quality between tracks
    flat_judges: int = 0         # judges who give everyone the same score
    criteria: int = 3
    scale: tuple = (1, 5)


def simulate(design, scenario: Scenario, rng: random.Random, track_of=None):
    """Synthetic scores on a real assignment design.

    `design` is a list of (judge, project) pairs, e.g. the fixture's own assignment
    graph. Each criterion value is a rounded, clipped integer, like a real scorecard.
    Returns (reviews, true_quality).
    """
    projects = sorted({p for _, p in design})
    judges = sorted({j for j, _ in design})
    tracks = sorted({track_of[p] for p in projects}) if track_of else []
    track_effect = {t: rng.gauss(0, scenario.track_sd) for t in tracks}
    quality = {p: 3.3 + rng.gauss(0, scenario.quality_sd) + (track_effect[track_of[p]] if track_of else 0)
               for p in projects}
    bias = {j: rng.gauss(0, scenario.bias_sd) for j in judges}
    flat = set(rng.sample(judges, scenario.flat_judges)) if scenario.flat_judges else set()
    flat_value = {j: rng.choice([2, 3, 4]) for j in flat}
    lo, hi = scenario.scale
    reviews = []
    for judge, project in design:
        if judge in flat:
            value = float(flat_value[judge])
        else:
            parts = [min(hi, max(lo, round(quality[project] + bias[judge] + rng.gauss(0, scenario.noise_sd))))
                     for _ in range(scenario.criteria)]
            value = sum(parts) / len(parts)
        reviews.append(Review(judge, project, value))
    return reviews, quality


def recovery_study(design, scenario: Scenario, runs=200, seed=2026, track_of=None, top_k=5):
    """How well does each method recover the true ranking? Mean Spearman correlation
    and top-k overlap over `runs` simulated events."""
    rng = random.Random(seed)
    results = {name: {"spearman": [], "top_k": []} for name in METHODS}
    for _ in range(runs):
        reviews, truth = simulate(design, scenario, rng, track_of=track_of)
        for name, method in METHODS.items():
            estimate = {p: ps.score for p, ps in method(reviews).projects.items()}
            results[name]["spearman"].append(spearman(truth, estimate))
            results[name]["top_k"].append(top_k_overlap(truth, estimate, top_k))
    summary = {}
    for name, r in results.items():
        rho = [x for x in r["spearman"] if not math.isnan(x)]
        summary[name] = {
            "spearman_mean": _mean(rho),
            "spearman_sd": math.sqrt(_pvar(rho)),
            "top_k_mean": _mean(r["top_k"]),
        }
    return summary
