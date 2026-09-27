"""Changing an event's rubric."""

from decimal import Decimal

from django.db import transaction
from django.utils.text import slugify

from audit import services as audit
from core.errors import Conflict, DomainError
from events import access
from judging.models import Criterion, ScoreValue


def replace_rubric(event, user, rows):
    """Replace the whole rubric: [{"key", "name", "weight", "description"}].

    Weights can change at any time, because a weight is applied when results are
    computed, not when a judge scores; the ranking simply recomputes (and the change is
    in the audit log). Adding or removing criteria is refused once anyone has scored,
    because existing scorecards would no longer match the rubric.
    """
    access.require_organizer(user, event)
    if not rows:
        raise DomainError("A rubric needs at least one criterion.")
    wanted = []
    for pos, row in enumerate(rows):
        key = slugify(row.get("key") or row.get("name", ""))[:40].replace("-", "_")
        if not key:
            raise DomainError("Every criterion needs a key or a name.")
        weight = Decimal(str(row.get("weight", 1)))
        if weight <= 0:
            raise DomainError(f"{key}: weights must be positive.")
        wanted.append((key, row.get("name") or key.replace("_", " ").title(), weight,
                       row.get("description", ""), pos))
    keys = [w[0] for w in wanted]
    if len(set(keys)) != len(keys):
        raise DomainError("Criterion keys must be unique.")

    existing = {c.key: c for c in event.criteria.all()}
    scored = ScoreValue.objects.filter(criterion__event=event).exists()
    if scored and set(keys) != set(existing):
        raise Conflict("Judges have already scored against this rubric. You can change weights, "
                       "names and descriptions, but not add or remove criteria.", code="rubric_locked")
    before = {k: float(c.weight) for k, c in existing.items()}
    with transaction.atomic():
        for key, name, weight, description, pos in wanted:
            criterion = existing.get(key) or Criterion(event=event, key=key)
            criterion.name, criterion.weight, criterion.description, criterion.position = \
                name, weight, description, pos
            criterion.save()
        event.criteria.exclude(key__in=keys).delete()
        after = {k: float(w) for k, _, w, _, _ in wanted}
        audit.record(
            "rubric.updated",
            f"{user.display_name} updated the rubric: "
            + ", ".join(
                f"{k} ×{after[k]:g}" + (f" (was ×{before[k]:g})" if k in before and before[k] != after[k] else "")
                for k in keys
            ),
            actor=user, event=event, target=event, data={"before": before, "after": after},
        )
