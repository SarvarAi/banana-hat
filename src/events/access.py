"""Who may do what inside an event.

This module is the only place role questions are answered. HTML views, API views and
services all call it, so a rule changed here changes everywhere at once. It never looks
at the request's URL or template; it looks at the user and the object.
"""

from django.core.exceptions import PermissionDenied

from events.models import Event, EventRole, TeamMember

Role = EventRole.Role


def _roles(user, event) -> frozenset[str]:
    """The set of role names `user` holds in `event`, cached on the user object."""
    if user is None or not user.is_authenticated:
        return frozenset()
    cache = user.__dict__.setdefault("_event_roles_cache", {})
    if event.pk not in cache:
        cache[event.pk] = frozenset(
            EventRole.objects.filter(event=event, user=user).values_list("role", flat=True)
        )
    return cache[event.pk]


def forget_roles(user):
    """Drop the per-user cache after roles change within the same request."""
    user.__dict__.pop("_event_roles_cache", None)


def is_admin(user) -> bool:
    return bool(user and user.is_authenticated and user.is_active and user.is_staff)


def is_organizer(user, event) -> bool:
    return is_admin(user) or Role.ORGANIZER in _roles(user, event)


def is_judge(user, event) -> bool:
    """Judges are never admins by implication: judging needs an explicit role."""
    return Role.JUDGE in _roles(user, event)


def is_participant(user, event) -> bool:
    return Role.PARTICIPANT in _roles(user, event)


def judge_role(user, event) -> EventRole | None:
    if not is_judge(user, event):
        return None
    return EventRole.objects.filter(event=event, user=user, role=Role.JUDGE).first()


def team_of(user, event):
    if user is None or not user.is_authenticated:
        return None
    membership = (
        TeamMember.objects.select_related("team").filter(event=event, user=user).first()
    )
    return membership.team if membership else None


def can_view_event(user, event: Event) -> bool:
    if event.is_listed:
        return True
    return bool(_roles(user, event)) or is_admin(user)


def require_organizer(user, event):
    if not is_organizer(user, event):
        raise PermissionDenied("Only this event's organizers can do that.")


def require_judge(user, event) -> EventRole:
    role = judge_role(user, event)
    if role is None:
        raise PermissionDenied("Only this event's judges can do that.")
    return role


def require_view_event(user, event):
    if not can_view_event(user, event):
        raise PermissionDenied("This event is not public.")
