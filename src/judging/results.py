"""Turning scores into rankings, and freezing them when they are published.

`compute()` is a pure read: it can run on every dashboard refresh. `publish()` freezes
one computation into a ResultSnapshot, and from then on the public sees that snapshot
and nothing else, however the scores move afterwards (they cannot move: judging closes
when results are published).
"""

from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Count, Prefetch, Q

from audit import services as audit
from core import clock
from core.errors import Conflict
from events import access
from events.models import Event, Project
from judging import stats
from judging.models import Assignment, PairwiseComparison, ResultSnapshot, Score, ScoreValue

METHOD_LABELS = dict(Event.Normalization.choices)
SCALE_METHODS = {"raw", "zscore", "judge_effect"}  # scores on the rubric's own scale


def event_reviews(event):
    """The counted reviews of an event as stats.Review rows, plus the criteria."""
    criteria = list(event.criteria.all())
    weights = {c.key: float(c.weight) for c in criteria}
    scores = (
        Score.objects.counted()
        .filter(assignment__event=event)
        .select_related("assignment__judge", "assignment__project")
        .prefetch_related(Prefetch("values", queryset=ScoreValue.objects.select_related("criterion")))
    )
    reviews = []
    for s in scores:
        values = {v.criterion.key: v.value for v in s.values.all()}
        if set(values) != set(weights):
            continue  # a rubric changed after scoring; such a score cannot be weighted
        reviews.append(stats.Review(
            s.assignment.judge.public_id, s.assignment.project.public_id,
            stats.weighted_total(values, weights),
        ))
    return reviews, criteria


def _pairwise(event):
    rows = PairwiseComparison.objects.filter(event=event).values_list(
        "winner__public_id", "loser__public_id")
    return [(winner, loser, 1.0) for winner, loser in rows]


def run_method(event, method, reviews):
    if method == "pairwise":
        projects = list(Project.objects.canonical().filter(event=event).values_list("public_id", flat=True))
        return stats.pairwise_bt(_pairwise(event), projects)
    return stats.METHODS[method](reviews)


@dataclass
class Row:
    rank: int
    project: Project
    score: float
    raw_mean: float | None
    n: int
    ci: tuple | None
    track_rank: int | None = None
    shift: int | None = None  # places moved relative to the raw mean ranking

    def as_dict(self):
        p = self.project
        return {
            "rank": self.rank,
            "track_rank": self.track_rank,
            "project": p.public_id,
            "title": p.title,
            "team": p.team.name,
            "track": p.track.public_id if p.track else None,
            "track_name": p.track.name if p.track else None,
            "score": round(self.score, 4),
            "raw_mean": None if self.raw_mean is None or self.raw_mean != self.raw_mean
            else round(self.raw_mean, 4),
            "reviews": self.n,
            "ci": [round(self.ci[0], 4), round(self.ci[1], 4)] if self.ci else None,
            "shift": self.shift,
        }


@dataclass
class Results:
    event: Event
    method: str
    rows: list[Row]
    result: stats.MethodResult
    excluded: list = field(default_factory=list)
    ties: list = field(default_factory=list)
    criteria: list = field(default_factory=list)
    review_count: int = 0

    @property
    def on_scale(self):
        return self.method in SCALE_METHODS

    def by_track(self):
        tracks = {}
        for row in self.rows:
            key = row.project.track
            tracks.setdefault(key, []).append(row)
        return tracks


def compute(event, method=None) -> Results:
    method = method or event.normalization
    reviews, criteria = event_reviews(event)
    projects = {
        p.public_id: p for p in
        Project.objects.canonical().filter(event=event).select_related("team", "track")
    }
    reviews = [r for r in reviews if r.project in projects]
    result = run_method(event, method, reviews) if (reviews or method == "pairwise") else \
        stats.MethodResult(method, {})
    raw = stats.raw_mean(reviews) if reviews else stats.MethodResult("raw", {})
    raw_rank = {p: i for i, p in enumerate(raw.ranking(), start=1)}

    rows, track_counters = [], {}
    lo, hi = event.score_min, event.score_max
    for i, pid in enumerate(result.ranking(), start=1):
        ps = result.projects[pid]
        project = projects[pid]
        track_counters[project.track_id] = track_counters.get(project.track_id, 0) + 1
        # An interval is reported inside the scale: "somewhere up to 5.02" means 5.
        ci = (max(lo, ps.ci[0]), min(hi, ps.ci[1])) if ps.ci else None
        rows.append(Row(
            rank=i, project=project, score=ps.score,
            raw_mean=raw.projects[pid].score if pid in raw.projects else None,
            n=ps.n, ci=ci, track_rank=track_counters[project.track_id],
            shift=(raw_rank[pid] - i) if pid in raw_rank else None,
        ))

    excluded = [
        {"project": pid, "title": p.title, "reason": "no counted reviews"}
        for pid, p in projects.items() if pid not in result.projects
    ]
    for dup in Project.objects.filter(event=event, duplicate_of__isnull=False).select_related("duplicate_of"):
        excluded.append({"project": dup.public_id, "title": dup.title,
                         "reason": f"duplicate of {dup.duplicate_of.public_id}"})

    # Adjacent places whose 95% intervals overlap are not separated by the evidence.
    ties = []
    for a, b in zip(rows[:3], rows[1:4], strict=False):
        if a.ci and b.ci and a.ci[0] <= b.ci[1]:
            ties.append({"ranks": [a.rank, b.rank], "projects": [a.project.public_id, b.project.public_id],
                         "gap": round(a.score - b.score, 4)})
    return Results(event, method, rows, result, excluded, ties, criteria, len(reviews))


def compare_methods(event):
    """Every method's top ten side by side, and how far each disagrees with raw means."""
    reviews, _ = event_reviews(event)
    live = set(Project.objects.canonical().filter(event=event).values_list("public_id", flat=True))
    reviews = [r for r in reviews if r.project in live]
    if not reviews:
        return {}
    out = {}
    raw_scores = {p: s.score for p, s in stats.raw_mean(reviews).projects.items()}
    for name in stats.METHODS:
        result = stats.METHODS[name](reviews)
        scores = {p: s.score for p, s in result.projects.items()}
        out[name] = {
            "label": METHOD_LABELS.get(name, name),
            "top": result.ranking()[:10],
            "spearman_vs_raw": stats.spearman(raw_scores, scores),
            "diagnostics": result.diagnostics,
        }
    return out


def progress(event):
    """The organizer dashboard's numbers. Cheap enough to poll."""
    assignments = Assignment.objects.filter(event=event, project__status="submitted",
                                            project__duplicate_of__isnull=True)
    total = assignments.exclude(status=Assignment.Status.RECUSED).count()
    done = assignments.filter(status=Assignment.Status.DONE, score__voided_at__isnull=True).count()
    recused = assignments.filter(status=Assignment.Status.RECUSED).count()

    per_track = []
    for track in event.tracks.all():
        a = assignments.filter(project__track=track).exclude(status=Assignment.Status.RECUSED)
        t_total = a.count()
        t_done = a.filter(status=Assignment.Status.DONE).count()
        per_track.append({"track": track, "total": t_total, "done": t_done,
                          "pct": round(100 * t_done / t_total) if t_total else 100})

    reviews, _ = event_reviews(event)
    profiles = stats.judge_profiles(reviews) if reviews else {}
    effect = stats.judge_effect(reviews) if len(reviews) >= 3 else None
    judges = []
    judge_rows = (
        event.roles.judges().select_related("user")
        .annotate(
            total=Count("assignments", filter=~Q(assignments__status=Assignment.Status.RECUSED)
                        & Q(assignments__project__duplicate_of__isnull=True)),
            done=Count("assignments", filter=Q(assignments__status=Assignment.Status.DONE)
                       & Q(assignments__project__duplicate_of__isnull=True)),
        ).order_by("user__name")
    )
    for j in judge_rows:
        profile = profiles.get(j.public_id)
        flags = []
        if profile and profile.flat:
            flags.append(("flat", "gave every project the same total"))
        if profile and profile.n == 1:
            flags.append(("single", "only one review, so their bias cannot be estimated"))
        bias = effect.judges[j.public_id].bias if effect and j.public_id in effect.judges else None
        if bias is not None and effect.diagnostics["judge_sd"] > 0.15 and abs(bias) > 0.3:
            flags.append(("lenient" if bias > 0 else "harsh", f"estimated bias {bias:+.2f}"))
        if j.total and j.done < j.total:
            flags.append(("pending", f"{j.total - j.done} review(s) outstanding"))
        judges.append({"role": j, "total": j.total, "done": j.done, "profile": profile, "bias": bias,
                       "flags": flags})

    coverage = (
        Project.objects.canonical().filter(event=event)
        .annotate(n=Count("assignments", filter=Q(assignments__status=Assignment.Status.DONE)
                          & Q(assignments__score__voided_at__isnull=True)))
        .select_related("track").order_by("n", "title")
    )
    under = [p for p in coverage if p.n < event.reviews_per_project]
    return {
        "total": total, "done": done, "recused": recused,
        "pct": round(100 * done / total) if total else 0,
        "per_track": per_track, "judges": judges, "under_covered": under,
        "components": len(stats.components(reviews)) if reviews else 0,
        "diagnostics": effect.diagnostics if effect else {},
        "projects": coverage.count(),
    }


# --- publishing --------------------------------------------------------------------

def snapshot_payload(results: Results):
    event = results.event
    from voting.services import tally_if_closed

    rows = [r.as_dict() for r in results.rows]
    community = tally_if_closed(event)
    # Every competing project, not only those with judge reviews: a community prize can
    # go to a project the judges never ranked.
    by_public_id = {p.public_id: p for p in Project.objects.canonical().filter(event=event)}
    winners = {}
    prizes = []
    # Judged prizes are filled from the frozen ranking, community prizes from the vote
    # tally, in the organizer's order, and no project takes two prizes.
    for pz in event.prizes.select_related("track").order_by("position"):
        taken = set(winners.values())
        if pz.source == pz.Source.COMMUNITY:
            ranked = [by_public_id[c["project"]] for c in (community or [])
                      if c["votes"] > 0 and c["project"] in by_public_id]
        else:
            ranked = [r.project for r in results.rows]
        pool = [p for p in ranked if (pz.track_id is None or p.track_id == pz.track_id) and p.pk not in taken]
        winner = pool[0] if pool else None
        if winner:
            winners[pz.pk] = winner.pk
        prizes.append({
            "prize": pz.public_id, "name": pz.name, "value": pz.value, "source": pz.source,
            "track": pz.track.name if pz.track else None,
            "winner": winner.public_id if winner else None,
            "winner_title": winner.title if winner else None,
        })
    tracks = []
    for track in event.tracks.all():
        tracks.append({
            "track": track.public_id, "name": track.name,
            "ranking": [r for r in rows if r["track"] == track.public_id],
        })
    return {
        "event": {"id": event.public_id, "name": event.name},
        "method": results.method,
        "method_label": METHOD_LABELS.get(results.method, results.method),
        "on_scale": results.on_scale,
        "scale": [event.score_min, event.score_max],
        "criteria": [{"key": c.key, "name": c.name, "weight": float(c.weight)} for c in results.criteria],
        "diagnostics": {k: v for k, v in results.result.diagnostics.items()
                        if isinstance(v, (int, float, bool, str))},
        "reviews": results.review_count,
        "overall": rows,
        "tracks": tracks,
        "prizes": prizes,
        "ties": results.ties,
        "excluded": results.excluded,
        "community": community,
        "audit_head": audit.head_digest(),
    }, winners


def publish(event, actor, method=None):
    access.require_organizer(actor, event)
    if event.results_published_at:
        raise Conflict("Results are already published.", code="already_published")
    now = clock.now()
    if now < event.submissions_close_at:
        raise Conflict("Results cannot be published while submissions are open.", code="too_early")
    if event.voting_enabled() and event.voting_open(now):
        raise Conflict("Community voting is still open; publish after it closes.", code="voting_open")
    results = compute(event, method)
    with transaction.atomic():
        payload, winners = snapshot_payload(results)
        payload["published_at"] = now.isoformat()
        snapshot = ResultSnapshot.objects.create(event=event, method=results.method, payload=payload,
                                                 published_by=actor, published_at=now)
        for prize in event.prizes.all():
            prize.winner_id = winners.get(prize.pk)
            prize.save(update_fields=["winner"])
        event.results_published_at = now
        event.save(update_fields=["results_published_at", "updated_at"])
        audit.record(
            "results.published",
            f"{actor.display_name} published results ({payload['method_label']}, "
            f"{len(payload['overall'])} ranked projects)",
            actor=actor, event=event, target=event,
            data={"method": results.method, "snapshot": snapshot.pk},
        )
        from integrations import webhooks
        from records import services as records

        records.sign_results(snapshot)
        webhooks.emit(event, "results.published", {
            "event": event.public_id, "method": results.method,
            "top": payload["overall"][:10], "prizes": payload["prizes"],
        })
    return snapshot


def unpublish(event, actor, reason):
    """Take results down (a mistake found after publishing). The snapshot is kept, the
    audit log says why, and judging reopens."""
    access.require_organizer(actor, event)
    if not event.results_published_at:
        raise Conflict("Results are not published.")
    with transaction.atomic():
        event.results_published_at = None
        event.save(update_fields=["results_published_at", "updated_at"])
        event.prizes.update(winner=None)
        audit.record("results.unpublished", f"{actor.display_name} unpublished results: {reason}",
                     actor=actor, event=event, target=event, data={"reason": reason})
        from integrations import webhooks
        from records import services as records

        records.revoke_event_records(event, actor=actor, reason=f"results unpublished: {reason}")
        webhooks.emit(event, "results.unpublished", {"event": event.public_id, "reason": reason})


def published_results(event):
    """The frozen, published results payload, or None while results are private."""
    if not event.results_published_at:
        return None
    snapshot = ResultSnapshot.objects.filter(event=event).order_by("-published_at").first()
    return snapshot.payload if snapshot else None
