"""Pairwise judging mode: "which of these two is better?", estimated by Bradley–Terry.

Pairwise comparison is easier on judges than absolute scoring (no one has to decide
what a 3 means) and immune to judge leniency by construction. It needs more judgements
than rubric scoring, so pairs are chosen to be informative: pairs compared least so far,
and among those, projects whose current estimates are closest, because a comparison
between a clear favourite and a clear outsider teaches the model almost nothing.
"""

import random

from django.db import transaction

from audit import services as audit
from core.errors import Conflict, DomainError
from events.models import Project
from judging import stats
from judging.models import Conflict as ConflictOfInterest
from judging.models import PairwiseComparison
from judging.scoring import ensure_judging_open


def eligible_projects(event, judge_role):
    tracks = list(judge_role.tracks.values_list("pk", flat=True))
    qs = Project.objects.canonical().filter(event=event).select_related("team", "track")
    if tracks:
        qs = qs.filter(track_id__in=tracks)
    blocked = ConflictOfInterest.objects.filter(judge=judge_role).values("team_id")
    return list(qs.exclude(team_id__in=blocked).order_by("public_id"))


def _key(a, b):
    return (a, b) if a < b else (b, a)


def next_pair(event, judge_role, rng=None):
    projects = eligible_projects(event, judge_role)
    if len(projects) < 2:
        return None
    rows = list(PairwiseComparison.objects.filter(event=event).values_list(
        "winner_id", "loser_id", "judge_id"))
    seen_by_judge = {_key(win, lose) for win, lose, j in rows if j == judge_role.pk}
    counts = {}
    for win, lose, _ in rows:
        counts[_key(win, lose)] = counts.get(_key(win, lose), 0) + 1
    strengths = stats.bradley_terry([(win, lose, 1.0) for win, lose, _ in rows], items=[p.pk for p in projects])
    rng = rng or random.Random()
    best, best_cost = None, None
    for i, a in enumerate(projects):
        for b in projects[i + 1:]:
            key = _key(a.pk, b.pk)
            if key in seen_by_judge:
                continue
            cost = (counts.get(key, 0), abs(strengths.get(a.pk, 0) - strengths.get(b.pk, 0)), rng.random())
            if best_cost is None or cost < best_cost:
                best, best_cost = (a, b), cost
    if best is None:
        return None
    return best if rng.random() < 0.5 else (best[1], best[0])  # no left/right bias


def record(event, judge_role, winner_id, loser_id):
    ensure_judging_open(event)
    if winner_id == loser_id:
        raise DomainError("Pick two different projects.")
    allowed = {p.public_id: p for p in eligible_projects(event, judge_role)}
    if winner_id not in allowed or loser_id not in allowed:
        raise DomainError("Those projects are not in your pairwise pool.")
    winner, loser = allowed[winner_id], allowed[loser_id]
    already = PairwiseComparison.objects.filter(judge=judge_role).filter(
        winner__in=[winner, loser], loser__in=[winner, loser]).exists()
    if already:
        raise Conflict("You have already compared these two.", code="already_compared")
    with transaction.atomic():
        comparison = PairwiseComparison.objects.create(event=event, judge=judge_role, winner=winner, loser=loser)
        audit.record("pairwise.compared",
                     f"{judge_role.user.display_name} compared {winner.title} and {loser.title}",
                     actor=judge_role.user, event=event, target=comparison,
                     data={"winner": winner.public_id, "loser": loser.public_id})
    return comparison
