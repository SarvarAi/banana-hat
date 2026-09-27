"""Reproduce the numbers in JUDGING.md.

    python manage.py normalization_report                  # the fixture event, 200 runs
    python manage.py normalization_report --runs 1000      # tighter simulation estimates
    python manage.py normalization_report --event evt_01

Part one analyses the real scores. Part two is a simulation study run on the event's
own judge-project assignment graph: synthetic "true" project quality is generated,
scored by synthetic judges with known biases through exactly the assignments the real
judges had, and each method is graded on how well it recovers the truth. Seeded, so
the output is identical every time.
"""

import math
from collections import Counter

from django.core.management.base import BaseCommand

from events.models import Event
from judging import results, stats

SCENARIOS = [
    ("No judge bias at all", stats.Scenario(bias_sd=0.0)),
    ("Moderate judge bias (sd 0.5)", stats.Scenario(bias_sd=0.5)),
    ("Strong judge bias (sd 0.8)", stats.Scenario(bias_sd=0.8)),
    ("Bias, and tracks that genuinely differ (sd 0.5)", stats.Scenario(bias_sd=0.5, track_sd=0.5)),
    ("Strong bias plus two flat judges", stats.Scenario(bias_sd=0.8, flat_judges=2)),
]


class Command(BaseCommand):
    help = "Analyse an event's scores with every normalization method and run the simulation study."

    def add_arguments(self, parser):
        parser.add_argument("--event", default="evt_01")
        parser.add_argument("--runs", type=int, default=200)
        parser.add_argument("--seed", type=int, default=2026)

    def handle(self, *args, event, runs, seed, **options):
        ev = Event.objects.get(public_id=event)
        reviews, criteria = results.event_reviews(ev)
        live = set(ev.projects.canonical().values_list("public_id", flat=True))
        reviews = [r for r in reviews if r.project in live]
        out = self.stdout.write
        weights = ", ".join(f"{c.key} ×{c.weight:g}" for c in criteria)

        out(f"## {ev.name}: {len(reviews)} counted reviews of {len(live)} projects by "
            f"{len({r.judge for r in reviews})} judges\n")
        out(f"Rubric: {weights}.\n")
        per_project = Counter(r.project for r in reviews)
        out("Reviews per project: " + ", ".join(
            f"{n} reviews × {k}" for n, k in sorted(Counter(per_project.values()).items())) + ".\n")
        per_judge = Counter(r.judge for r in reviews)
        profiles = stats.judge_profiles(reviews)
        out(f"Reviews per judge: min {min(per_judge.values())}, max {max(per_judge.values())}, "
            f"single-review judges: {', '.join(sorted(j for j, n in per_judge.items() if n == 1)) or 'none'}.\n")
        out(f"Flat judges (same total for every project): "
            f"{', '.join(sorted(j for j, p in profiles.items() if p.flat)) or 'none'}.\n")
        out(f"Connected components of the judge graph: {len(stats.components(reviews))}.\n")

        methods = {name: fn(reviews) for name, fn in stats.METHODS.items()}
        je = methods["judge_effect"]
        d = je.diagnostics
        out(f"\nJudge-effect model: estimated prior strength lambda = {d['lambda']:.2f}"
            f"{' (at the upper bound)' if d['lambda'] >= 49.99 else ''}, judge-bias sd = {d['judge_sd']:.3f}, "
            f"residual sd = {d['residual_sd']:.3f}.\n")
        fixed = stats.judge_effect(reviews, lam=3.0)
        biggest = sorted(fixed.judges.values(), key=lambda e: e.bias)
        out("With a fixed lambda = 3 for comparison, the most extreme judges are: "
            + ", ".join(f"{e.judge} {e.bias:+.2f} (n={e.n})" for e in biggest[:3] + biggest[-3:]) + ".\n")

        out("\n| method | top 5 | Spearman vs raw |\n|---|---|---|")
        raw_scores = {p: s.score for p, s in methods["raw"].projects.items()}
        for name, result in methods.items():
            scores = {p: s.score for p, s in result.projects.items()}
            out(f"| {name} | {', '.join(result.ranking()[:5])} | {stats.spearman(raw_scores, scores):.3f} |")

        design = [(r.judge, r.project) for r in reviews]
        track_of = dict(ev.projects.values_list("public_id", "track__public_id"))
        out(f"\n## Simulation on this event's assignment graph ({runs} runs per scenario, seed {seed})\n")
        out("Mean Spearman correlation between the true and the estimated ranking "
            "(higher is better), and in brackets the share of the true top 5 that the method put in its top 5.\n")
        out("| scenario | " + " | ".join(stats.METHODS) + " |")
        out("|---|" + "---|" * len(stats.METHODS))
        worst_gap = dict.fromkeys(stats.METHODS, 0.0)
        for label, scenario in SCENARIOS:
            summary = stats.recovery_study(design, scenario, runs=runs, seed=seed, track_of=track_of)
            cells = []
            best = max(summary[m]["spearman_mean"] for m in stats.METHODS)
            for m in stats.METHODS:
                worst_gap[m] = max(worst_gap[m], best - summary[m]["spearman_mean"])
                cell = f"{summary[m]['spearman_mean']:.3f} ({summary[m]['top_k_mean']:.2f})"
                cells.append(f"**{cell}**" if math.isclose(summary[m]["spearman_mean"], best) else cell)
            out(f"| {label} | " + " | ".join(cells) + " |")
        # The regret row: how far behind the best method each one falls in its worst
        # scenario. An organizer cannot know in advance which scenario they are in.
        out("| **Worst-case shortfall vs the best method** | "
            + " | ".join(f"{worst_gap[m]:.3f}" for m in stats.METHODS) + " |")
