"""Who reviews what.

The strategy, defended at length in JUDGING.md:

1. Eligibility. A judge may review a project if the judge covers its track (a judge
   with no tracks covers all of them), has not declared a conflict with its team, and
   is not already assigned to it.
2. Most constrained first. Projects with the fewest eligible judges are served first,
   so a thin track is not starved by projects that had plenty of options.
3. Round robin. Every under-covered project gets one more reviewer per round, rather
   than filling one project completely before the next, so a shortfall is spread thin
   instead of landing on whoever came last.
4. Least loaded judge wins, then the judge who has reviewed least alongside the
   project's existing reviewers. The second rule is for the statistics: the bias of a
   judge can only be estimated relative to judges they share projects with, so varied
   pairings make normalization better conditioned (see judging.stats).
5. Deterministic. Remaining ties are broken by a seeded RNG, and the seed is written to
   the audit log, so the same inputs produce the same plan and a plan can be replayed.
"""

import hashlib
import random
from collections import defaultdict
from dataclasses import dataclass, field

from django.db import transaction

from audit import services as audit
from core.errors import Conflict, DomainError
from events import access
from events.models import EventRole, Project, TeamMember
from judging.models import Assignment
from judging.models import Conflict as ConflictOfInterest


@dataclass
class Shortfall:
    project: Project
    missing: int
    reason: str


@dataclass
class AssignmentPlan:
    created: list = field(default_factory=list)       # [(EventRole, Project)]
    shortfalls: list = field(default_factory=list)    # [Shortfall]
    seed: int = 0

    def summary(self):
        return {
            "created": len(self.created),
            "shortfalls": [
                {"project": s.project.public_id, "missing": s.missing, "reason": s.reason}
                for s in self.shortfalls
            ],
            "seed": self.seed,
        }


def _seed_for(event, salt=""):
    digest = hashlib.sha256(f"{event.public_id}:{salt}".encode()).hexdigest()
    return int(digest[:12], 16)


def plan_assignments(event, *, target=None, max_load=None, seed=None) -> AssignmentPlan:
    """Compute, without saving, the assignments needed to reach `target` reviews each."""
    target = target or event.reviews_per_project
    max_load = max_load if max_load is not None else event.judge_max_load
    seed = seed if seed is not None else _seed_for(event, Assignment.objects.filter(event=event).count())
    rng = random.Random(seed)

    projects = list(Project.objects.canonical().filter(event=event).select_related("team", "track"))
    judges = list(EventRole.objects.judges().filter(event=event).prefetch_related("tracks"))
    judge_tracks = {j.pk: {t.pk for t in j.tracks.all()} for j in judges}

    existing = Assignment.objects.active().filter(event=event).values_list("judge_id", "project_id")
    reviewers = defaultdict(set)   # project -> judges
    load = defaultdict(int)        # judge -> assignments
    for judge_id, project_id in existing:
        reviewers[project_id].add(judge_id)
        load[judge_id] += 1
    blocked = set(
        ConflictOfInterest.objects.filter(judge__event=event).values_list("judge_id", "team_id")
    )
    member_of = defaultdict(set)
    for user_id, team_id in TeamMember.objects.filter(event=event).values_list("user_id", "team_id"):
        member_of[user_id].add(team_id)

    # Recused assignments stay recused: never offer that pair again.
    recused = set(
        Assignment.objects.filter(event=event, status=Assignment.Status.RECUSED)
        .values_list("judge_id", "project_id")
    )

    def eligible(judge, project):
        tracks = judge_tracks[judge.pk]
        if tracks and project.track_id not in tracks:
            return False
        if (judge.pk, project.team_id) in blocked or project.team_id in member_of[judge.user_id]:
            return False
        if (judge.pk, project.pk) in recused or judge.pk in reviewers[project.pk]:
            return False
        return not (max_load and load[judge.pk] >= max_load)

    # Co-review counts between pairs of judges, kept current as the plan grows.
    together = defaultdict(int)
    for project_judges in reviewers.values():
        for a in project_judges:
            for b in project_judges:
                if a != b:
                    together[(a, b)] += 1

    plan = AssignmentPlan(seed=seed)
    order = sorted(projects, key=lambda p: (sum(eligible(j, p) for j in judges), len(reviewers[p.pk]),
                                            p.public_id))
    tie = {j.pk: rng.random() for j in judges}
    while True:
        progressed = False
        for project in order:
            if len(reviewers[project.pk]) >= target:
                continue
            candidates = [j for j in judges if eligible(j, project)]
            if not candidates:
                continue
            best = min(candidates, key=lambda j: (
                load[j.pk],
                sum(together[(j.pk, r)] for r in reviewers[project.pk]),
                tie[j.pk],
            ))
            for r in reviewers[project.pk]:
                together[(best.pk, r)] += 1
                together[(r, best.pk)] += 1
            reviewers[project.pk].add(best.pk)
            load[best.pk] += 1
            plan.created.append((best, project))
            progressed = True
        if not progressed:
            break

    for project in order:
        missing = target - len(reviewers[project.pk])
        if missing > 0:
            eligible_total = sum(
                1 for j in judges
                if (not judge_tracks[j.pk] or project.track_id in judge_tracks[j.pk])
            )
            reason = (
                f"only {eligible_total} judge(s) cover {project.track.name if project.track else 'this track'}"
                if eligible_total < target else "remaining judges are at their load limit or conflicted"
            )
            plan.shortfalls.append(Shortfall(project, missing, reason))
    return plan


def auto_assign(event, *, actor=None, target=None, max_load=None, seed=None) -> AssignmentPlan:
    plan = plan_assignments(event, target=target, max_load=max_load, seed=seed)
    with transaction.atomic():
        Assignment.objects.bulk_create([
            Assignment(event=event, judge=judge, project=project, source=Assignment.Source.AUTO,
                       created_by=actor if getattr(actor, "is_authenticated", False) else None)
            for judge, project in plan.created
        ])
        audit.record(
            "assignment.auto",
            f"Automatic assignment created {len(plan.created)} reviews "
            f"({len(plan.shortfalls)} project(s) still short)",
            actor=actor, event=event, target=event, data=plan.summary(),
        )
    return plan


def assign(event, judge_role: EventRole, project: Project, *, actor) -> Assignment:
    access.require_organizer(actor, event)
    if judge_role.event_id != event.pk or project.event_id != event.pk:
        raise DomainError("Judge and project must belong to this event.")
    if project.team.memberships.filter(user=judge_role.user).exists() or \
            ConflictOfInterest.objects.filter(judge=judge_role, team=project.team).exists():
        raise Conflict(f"{judge_role.user.display_name} has a conflict of interest with {project.team.name}.",
                       code="conflict_of_interest")
    with transaction.atomic():
        assignment, created = Assignment.objects.get_or_create(
            judge=judge_role, project=project,
            defaults={"event": event, "source": Assignment.Source.MANUAL, "created_by": actor},
        )
        if not created:
            raise Conflict("That judge is already assigned to this project.", code="already_assigned")
        audit.record("assignment.created",
                     f"{actor.display_name} assigned {judge_role.user.display_name} to {project.title}",
                     actor=actor, event=event, target=assignment)
    return assignment


def unassign(assignment: Assignment, *, actor):
    event = assignment.event
    access.require_organizer(actor, event)
    if hasattr(assignment, "score"):
        raise Conflict("This review has been scored. Void the score instead of deleting the assignment.",
                       code="has_score")
    with transaction.atomic():
        audit.record("assignment.removed",
                     f"{actor.display_name} removed {assignment.judge.user.display_name} "
                     f"from {assignment.project.title}",
                     actor=actor, event=event, target=assignment)
        assignment.delete()


def recuse(assignment: Assignment, *, actor, reason: str):
    """A judge declares a conflict of interest. Recorded, never silently dropped."""
    judge = assignment.judge
    if actor.pk != judge.user_id and not access.is_organizer(actor, assignment.event):
        raise DomainError("Only the judge or an organizer can recuse.", status=403)
    reason = (reason or "").strip() or "conflict of interest"
    with transaction.atomic():
        assignment.status = Assignment.Status.RECUSED
        assignment.recusal_reason = reason[:300]
        assignment.save(update_fields=["status", "recusal_reason"])
        ConflictOfInterest.objects.get_or_create(
            judge=judge, team=assignment.project.team,
            defaults={"reason": reason[:300], "declared_by": actor},
        )
        if hasattr(assignment, "score") and assignment.score.voided_at is None:
            from judging import scoring

            scoring.void_score(assignment.score, actor=actor, reason=f"recused: {reason}", check=False)
        audit.record("assignment.recused",
                     f"{judge.user.display_name} recused from {assignment.project.title}: {reason}",
                     actor=actor, event=assignment.event, target=assignment)
