"""Organizer operations on the event itself: creating it and changing its settings."""

from django.db import transaction
from django.utils.text import slugify

from audit import services as audit
from core.errors import Conflict, DomainError
from events import access
from events.models import Event, EventRole, Prize, Track
from judging.models import Criterion, Score

# A new event starts with a sensible rubric the organizer can reweight or replace.
DEFAULT_RUBRIC = [
    ("functionality", "Functionality", 2, "Does it work? Can a stranger run it and get the promised result?"),
    ("quality", "Quality", 1, "Code, documentation and experience you would want to maintain."),
    ("innovation", "Innovation", 1, "An idea you have not seen done this way before."),
]

# Settings that decide what already-cast judgements mean. Once scores exist, changing
# them silently would rewrite history, so they are logged with before and after.
SENSITIVE = {"submissions_close_at", "score_min", "score_max", "normalization", "voting_close_at",
             "voting_open_at", "judging_close_at"}


def _unique_slug(base):
    base = slugify(base)[:70] or "event"
    slug, n = base, 2
    while Event.objects.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def create_event(user, data: dict) -> Event:
    if not user.may_create_events:
        raise DomainError("Your account cannot create events.", status=403)
    data = dict(data)
    data["slug"] = data.get("slug") or _unique_slug(data.get("name", "event"))
    if Event.objects.filter(slug=data["slug"]).exists():
        raise Conflict("That address is taken; choose another slug.", code="slug_taken")
    with transaction.atomic():
        event = Event.objects.create(created_by=user, **data)
        EventRole.objects.create(event=event, user=user, role=EventRole.Role.ORGANIZER)
        for pos, (key, name, weight, description) in enumerate(DEFAULT_RUBRIC):
            Criterion.objects.create(event=event, key=key, name=name, weight=weight,
                                     description=description, position=pos)
        audit.record("event.created", f"{user.display_name} created {event.name}", actor=user,
                     event=event, target=event)
    access.forget_roles(user)
    return event


def update_event(event: Event, user, data: dict) -> Event:
    access.require_organizer(user, event)
    before = {k: getattr(event, k) for k in data}
    scored = Score.objects.filter(assignment__event=event).exists()
    if scored and ("score_min" in data or "score_max" in data) and (
        data.get("score_min", event.score_min) != event.score_min
        or data.get("score_max", event.score_max) != event.score_max
    ):
        raise Conflict("The score scale cannot change once judges have scored.", code="scale_locked")
    voting_fields = {"voting_mode", "voting_open_at", "voting_close_at", "votes_per_voter"}
    if event.voting_closed() and any(data.get(k, getattr(event, k)) != getattr(event, k) for k in voting_fields):
        # Otherwise an organizer could close voting for a moment, read the totals, and
        # reopen it. Closing early is allowed; it is just final.
        raise Conflict("Community voting has closed, so its settings are frozen.", code="voting_locked")
    for key, value in data.items():
        setattr(event, key, value)
    changed = {k: [str(before[k]), str(getattr(event, k))] for k in data if before[k] != getattr(event, k)}
    if not changed:
        return event
    with transaction.atomic():
        event.full_clean(exclude=["created_by"])
        event.save()
        sensitive = sorted(set(changed) & SENSITIVE)
        audit.record(
            "event.updated",
            f"{user.display_name} changed {', '.join(sorted(changed))} of {event.name}"
            + (" (affects judging)" if sensitive else ""),
            actor=user, event=event, target=event, data={"changed": changed},
        )
    return event


def add_track(event, user, name, description=""):
    access.require_organizer(user, event)
    if event.tracks.filter(name__iexact=name.strip()).exists():
        raise Conflict("A track with that name already exists.")
    with transaction.atomic():
        track = Track.objects.create(event=event, name=name.strip(), description=description,
                                     position=event.tracks.count())
        audit.record("track.created", f"{user.display_name} added track {track.name}", actor=user,
                     event=event, target=track)
    return track


def delete_track(track, user):
    event = track.event
    access.require_organizer(user, event)
    if track.projects.exists():
        raise Conflict("Projects are filed under this track; move them first.", code="track_in_use")
    with transaction.atomic():
        audit.record("track.deleted", f"{user.display_name} removed track {track.name}", actor=user,
                     event=event, target=track)
        track.delete()


def add_prize(event, user, **fields):
    access.require_organizer(user, event)
    with transaction.atomic():
        prize = Prize.objects.create(event=event, position=event.prizes.count(), **fields)
        audit.record("prize.created", f"{user.display_name} added prize {prize.name}", actor=user,
                     event=event, target=prize)
    return prize


def delete_prize(prize, user):
    access.require_organizer(user, prize.event)
    with transaction.atomic():
        audit.record("prize.deleted", f"{user.display_name} removed prize {prize.name}", actor=user,
                     event=prize.event, target=prize)
        prize.delete()
