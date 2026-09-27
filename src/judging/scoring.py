"""Writing scores. The judge who owns an assignment is the only person who can."""

from django.db import transaction

from audit import services as audit
from core import clock
from core.errors import Conflict, DomainError, Forbidden
from events import access
from judging.models import Assignment, Score, ScoreValue


class JudgingClosed(Forbidden):
    code = "judging_closed"


def ensure_judging_open(event, at=None):
    at = at or clock.now()
    if event.results_published_at:
        raise JudgingClosed("Results for this event are published; scores are final.")
    if at < event.submissions_close_at:
        raise JudgingClosed("Judging opens when submissions close.")
    if event.judging_close_at and at >= event.judging_close_at:
        raise JudgingClosed("Judging for this event has closed; scores are final.")


def submit_score(assignment: Assignment, user, values: dict, comment: str = "") -> Score:
    """Create or replace the score for an assignment.

    `values` maps criterion key to an integer on the event's scale. Every criterion of
    the rubric must be present: a half-filled scorecard is not a review.
    """
    event = assignment.event
    if assignment.judge.user_id != user.pk:
        # Not even organizers: a score is the judge's own statement.
        raise Forbidden("Only the assigned judge can score this project.")
    ensure_judging_open(event)
    if assignment.status == Assignment.Status.RECUSED:
        raise Conflict("You recused yourself from this project.", code="recused")
    project = assignment.project
    if project.status != project.Status.SUBMITTED or project.duplicate_of_id:
        raise Conflict("This project is not in competition any more.", code="not_competing")

    criteria = {c.key: c for c in event.criteria.all()}
    if not criteria:
        raise DomainError("This event has no rubric yet.", code="no_rubric")
    values = {str(k): v for k, v in (values or {}).items()}
    unknown = set(values) - set(criteria)
    missing = set(criteria) - set(values)
    if unknown:
        raise DomainError(f"Unknown criteria: {', '.join(sorted(unknown))}.", code="invalid")
    if missing:
        raise DomainError(f"Score every criterion. Missing: {', '.join(sorted(missing))}.",
                          code="incomplete")
    clean = {}
    for key, raw in values.items():
        try:
            number = int(raw)
        except (TypeError, ValueError):
            raise DomainError(f"{criteria[key].name}: {raw!r} is not a whole number.", code="invalid") from None
        if not event.score_min <= number <= event.score_max or str(number) != str(raw).strip():
            raise DomainError(
                f"{criteria[key].name}: scores run from {event.score_min} to {event.score_max}.",
                code="out_of_range",
            )
        clean[key] = number
    comment = (comment or "").strip()[:4000]

    with transaction.atomic():
        # Lock the assignment so a double submit cannot create two scores.
        Assignment.objects.select_for_update().get(pk=assignment.pk)
        score = Score.objects.filter(assignment=assignment).first()
        before = score.as_dict() if score else None
        now = clock.now()
        if score is None:
            score = Score.objects.create(assignment=assignment, comment=comment, submitted_at=now, updated_at=now)
        else:
            if score.voided_at:
                raise Conflict("This score was voided by an organizer.", code="voided")
            score.comment = comment
            score.updated_at = now
            score.save(update_fields=["comment", "updated_at"])
            score.values.all().delete()
        ScoreValue.objects.bulk_create(
            ScoreValue(score=score, criterion=criteria[k], value=v) for k, v in clean.items()
        )
        if assignment.status != Assignment.Status.DONE:
            assignment.status = Assignment.Status.DONE
            assignment.save(update_fields=["status"])

        judge_name = user.display_name
        if before is None:
            summary = f"{judge_name} scored {project.title}"
        else:
            changes = [f"{criteria[k].name} {before.get(k)}→{v}" for k, v in clean.items() if before.get(k) != v]
            summary = f"{judge_name} changed their score for {project.title}" + (
                f": {', '.join(changes)}" if changes else " (comment only)"
            )
        audit.record(
            "score.updated" if before else "score.created", summary,
            actor=user, event=event, target=score,
            data={"project": project.public_id, "judge": assignment.judge.public_id,
                  "before": before, "after": clean},
        )
        if before is None:
            # Counts only. A webhook receiver is outside the building; scores stay in.
            active = Assignment.objects.filter(event=event).exclude(status=Assignment.Status.RECUSED)
            from integrations import webhooks

            webhooks.emit(event, "judging.progress", {
                "reviews_done": active.filter(status=Assignment.Status.DONE).count(),
                "reviews_total": active.count(),
            })
    return score


def void_score(score: Score, *, actor, reason: str, check=True):
    """An organizer strikes a score out (a conflict found late, a judge who scored the
    wrong project). The row stays; it just stops counting."""
    event = score.assignment.event
    if check:
        access.require_organizer(actor, event)
        if event.results_published_at:
            raise Conflict("Results are published; unpublish them before changing scores.")
    reason = (reason or "").strip()
    if not reason:
        raise DomainError("Say why the score is being voided.")
    with transaction.atomic():
        score.voided_at = clock.now()
        score.voided_reason = reason[:300]
        score.save(update_fields=["voided_at", "voided_reason"])
        audit.record(
            "score.voided",
            f"{actor.display_name} voided {score.assignment.judge.user.display_name}'s score for "
            f"{score.assignment.project.title}: {reason}",
            actor=actor, event=event, target=score,
        )
