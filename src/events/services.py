"""Teams, submissions and roles: the write side of an event.

Every way of changing a project comes through here: the HTML forms, the REST API, and
the organizer console. That is what makes the deadline hold. There is one function
that decides whether a team may still edit (`ensure_team_can_edit`), and nothing that
writes a project skips it. The only exception is `integrations.interchange`, which
loads historical data and says so in the audit log.
"""

from dataclasses import dataclass
from datetime import UTC

from django.db import IntegrityError, transaction

from audit import services as audit
from core import clock
from core.errors import Conflict, DomainError, Forbidden
from events import access
from events.models import Event, EventRole, Project, SubmissionExtension, Team, TeamMember, Track

Role = EventRole.Role


class SubmissionsClosed(Forbidden):
    code = "submissions_closed"


class SubmissionsNotOpen(Forbidden):
    code = "submissions_not_open"


def _fmt(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


# --- roles ------------------------------------------------------------------------

INCOMPATIBLE = {
    Role.JUDGE: {Role.PARTICIPANT, Role.ORGANIZER},
    Role.PARTICIPANT: {Role.JUDGE, Role.ORGANIZER},
    Role.ORGANIZER: {Role.JUDGE, Role.PARTICIPANT},
}


def grant_role(event, user, role, *, actor=None, tracks=None) -> EventRole:
    """Give `user` a role in `event`, refusing combinations that break judging integrity."""
    held = set(EventRole.objects.filter(event=event, user=user).values_list("role", flat=True))
    clash = held & INCOMPATIBLE[role]
    if clash:
        raise Conflict(
            f"{user.display_name} is already a {', '.join(sorted(clash))} of {event.name} and "
            f"cannot also be a {role}: judges and organizers may not compete, and organizers can "
            "read every score.",
            code="role_conflict",
        )
    with transaction.atomic():
        obj, created = EventRole.objects.get_or_create(event=event, user=user, role=role)
        if tracks is not None:
            obj.tracks.set(tracks)
        if created:
            audit.record(
                "role.granted", f"{user.display_name} became a {role} of {event.name}",
                actor=actor, event=event, target=obj, data={"role": role, "user": user.email},
            )
    access.forget_roles(user)
    return obj


def revoke_role(role_obj: EventRole, *, actor=None):
    with transaction.atomic():
        audit.record(
            "role.revoked",
            f"{role_obj.user.display_name} is no longer a {role_obj.role} of {role_obj.event.name}",
            actor=actor, event=role_obj.event, target=role_obj,
            data={"role": role_obj.role, "user": role_obj.user.email},
        )
        role_obj.delete()
    access.forget_roles(role_obj.user)


# --- deadlines ------------------------------------------------------------------------

def deadline_for(event: Event, team: Team | None):
    """When this team loses the ability to edit: the event deadline, or its extension."""
    if team is not None:
        extension = SubmissionExtension.objects.filter(team=team).first()
        if extension and extension.until > event.submissions_close_at:
            return extension.until
    return event.submissions_close_at


def ensure_team_can_edit(event: Event, team: Team | None, at=None):
    at = at or clock.now()
    if at < event.submissions_open_at:
        raise SubmissionsNotOpen(
            f"Submissions for {event.name} open at {_fmt(event.submissions_open_at)}."
        )
    deadline = deadline_for(event, team)
    if at >= deadline:
        raise SubmissionsClosed(
            f"Submissions for {event.name} closed at {_fmt(deadline)}. "
            "Projects can no longer be created or changed."
        )


# --- teams -------------------------------------------------------------------------

def _ensure_may_compete(event, user):
    if access.is_judge(user, event):
        raise Forbidden("Judges of this event cannot join a team in it.", code="role_conflict")
    if EventRole.objects.filter(event=event, user=user, role=Role.ORGANIZER).exists():
        raise Forbidden("Organizers of this event cannot compete in it.", code="role_conflict")
    if TeamMember.objects.filter(event=event, user=user).exists():
        raise Conflict("You are already on a team in this event.", code="already_on_team")


def create_team(event: Event, user, name: str) -> Team:
    ensure_team_can_edit(event, None)
    _ensure_may_compete(event, user)
    name = (name or "").strip()[:120]
    if not name:
        raise DomainError("Give your team a name.", code="invalid")
    if Team.objects.filter(event=event, name__iexact=name).exists():
        raise Conflict(f"A team called “{name}” already exists in this event. Pick another name, "
                       "or ask that team for their invite link.", code="team_name_taken")
    try:
        with transaction.atomic():
            team = Team.objects.create(event=event, name=name, created_by=user)
            TeamMember.objects.create(team=team, user=user, is_captain=True)
            grant_role(event, user, Role.PARTICIPANT, actor=user)
            audit.record("team.created", f"{user.display_name} created team {team.name}",
                         actor=user, event=event, target=team)
    except IntegrityError as exc:
        raise Conflict("You are already on a team in this event.", code="already_on_team") from exc
    return team


def join_team(code: str, user) -> Team:
    team = Team.objects.select_related("event").filter(invite_code=code).first()
    if team is None:
        raise DomainError("This invite link is not valid. Ask the team for a fresh one.",
                          code="invalid_invite", status=404)
    event = team.event
    ensure_team_can_edit(event, team)
    _ensure_may_compete(event, user)
    with transaction.atomic():
        # Lock the team row so two people clicking the last seat cannot both get it.
        Team.objects.select_for_update().get(pk=team.pk)
        if team.memberships.count() >= event.max_team_size:
            raise Conflict(f"{team.name} is full ({event.max_team_size} members).", code="team_full")
        try:
            TeamMember.objects.create(team=team, user=user)
        except IntegrityError as exc:
            raise Conflict("You are already on a team in this event.", code="already_on_team") from exc
        grant_role(event, user, Role.PARTICIPANT, actor=user)
        audit.record("team.joined", f"{user.display_name} joined team {team.name}",
                     actor=user, event=event, target=team)
    return team


def leave_team(team: Team, user):
    event = team.event
    ensure_team_can_edit(event, team)
    membership = TeamMember.objects.filter(team=team, user=user).first()
    if membership is None:
        raise Forbidden("You are not on this team.")
    with transaction.atomic():
        membership.delete()
        remaining = list(team.memberships.order_by("joined_at"))
        if membership.is_captain and remaining:
            remaining[0].is_captain = True
            remaining[0].save(update_fields=["is_captain"])
        audit.record("team.left", f"{user.display_name} left team {team.name}",
                     actor=user, event=event, target=team)
        if not remaining:
            if team.projects.filter(status=Project.Status.SUBMITTED).exists():
                raise Conflict("You are the last member of a team with a submitted project. "
                               "Withdraw the project first.", code="team_has_submission")
            team.delete()
        EventRole.objects.filter(event=event, user=user, role=Role.PARTICIPANT).delete()
    access.forget_roles(user)


def rotate_invite(team: Team, user):
    if not TeamMember.objects.filter(team=team, user=user, is_captain=True).exists():
        raise Forbidden("Only the team captain can reset the invite link.")
    with transaction.atomic():
        team.rotate_invite()
        audit.record("team.invite_rotated", f"{user.display_name} reset the invite link of {team.name}",
                     actor=user, event=team.event, target=team)


# --- projects -----------------------------------------------------------------------

EDITABLE_FIELDS = ("title", "summary", "description", "repo_url", "demo_url", "video_url", "track")


@dataclass
class ProjectInput:
    title: str | None = None
    summary: str | None = None
    description: str | None = None
    repo_url: str | None = None
    demo_url: str | None = None
    video_url: str | None = None
    track: Track | None = None

    def changes(self):
        return {k: getattr(self, k) for k in EDITABLE_FIELDS if getattr(self, k) is not None}


def _team_for_edit(project: Project, user) -> Team:
    if not TeamMember.objects.filter(team=project.team, user=user).exists():
        raise Forbidden("Only members of this project's team can change it.")
    return project.team


def _check_track(event, track):
    if track is not None and track.event_id != event.pk:
        raise DomainError("That track belongs to another event.", code="invalid_track")


def create_project(event: Event, user, data: ProjectInput, *, submit=False) -> Project:
    """Start a project for the user's team. The deadline is checked first, before
    anything else about the request, so a late submission is refused as late."""
    team = access.team_of(user, event)
    ensure_team_can_edit(event, team)
    if team is None:
        raise Forbidden("Create or join a team in this event before submitting.", code="no_team")
    if team.projects.exclude(status=Project.Status.WITHDRAWN).filter(duplicate_of__isnull=True).exists():
        raise Conflict("Your team already has a project in this event. Edit it instead.",
                       code="project_exists")
    if not (data.title or "").strip():
        raise DomainError("A project needs a title.", code="invalid")
    _check_track(event, data.track)
    with transaction.atomic():
        project = Project(event=event, team=team, **data.changes())
        project.title = project.title.strip()
        project.save()
        audit.record("project.created", f"{user.display_name} started project {project.title}",
                     actor=user, event=event, target=project)
        if submit:
            _submit(project, user)
    return project


def update_project(project: Project, user, data: ProjectInput) -> Project:
    team = _team_for_edit(project, user)
    ensure_team_can_edit(project.event, team)
    if project.status == Project.Status.WITHDRAWN:
        raise Conflict("This project was withdrawn.", code="withdrawn")
    changes = data.changes()
    _check_track(project.event, changes.get("track"))
    if "title" in changes and not changes["title"].strip():
        raise DomainError("A project needs a title.", code="invalid")
    before = {k: str(getattr(project, k) or "") for k in changes}
    for key, value in changes.items():
        setattr(project, key, value)
    changed = {k: [before[k], str(getattr(project, k) or "")]
               for k in changes if before[k] != str(getattr(project, k) or "")}
    if not changed:
        return project
    with transaction.atomic():
        project.save()
        audit.record(
            "project.updated",
            f"{user.display_name} edited {project.title} ({', '.join(sorted(changed))})",
            actor=user, event=project.event, target=project, data={"changed": changed},
        )
    return project


def _submit(project, user):
    missing = [f for f in ("title", "summary") if not getattr(project, f)]
    if project.event.tracks.exists() and project.track_id is None:
        missing.append("track")
    if missing:
        raise DomainError(f"Fill in {', '.join(missing)} before submitting.", code="incomplete")
    project.status = Project.Status.SUBMITTED
    project.submitted_at = clock.now()
    project.save(update_fields=["status", "submitted_at", "updated_at"])
    audit.record("project.submitted", f"{user.display_name} submitted {project.title}",
                 actor=user, event=project.event, target=project)
    from integrations import webhooks

    webhooks.emit(project.event, "project.submitted", {
        "project": project.public_id, "title": project.title, "team": project.team.name,
        "track": project.track.public_id if project.track else None,
        "submitted_at": project.submitted_at.isoformat(),
    })


def submit_project(project: Project, user) -> Project:
    team = _team_for_edit(project, user)
    ensure_team_can_edit(project.event, team)
    if project.status == Project.Status.WITHDRAWN:
        raise Conflict("This project was withdrawn.", code="withdrawn")
    with transaction.atomic():
        _submit(project, user)
    return project


def unsubmit_project(project: Project, user) -> Project:
    """Back to draft, e.g. to hide it while making big changes. Only before the deadline."""
    team = _team_for_edit(project, user)
    ensure_team_can_edit(project.event, team)
    with transaction.atomic():
        project.status = Project.Status.DRAFT
        project.submitted_at = None
        project.save(update_fields=["status", "submitted_at", "updated_at"])
        audit.record("project.unsubmitted", f"{user.display_name} moved {project.title} back to draft",
                     actor=user, event=project.event, target=project)
    return project


def withdraw_project(project: Project, actor) -> Project:
    """A team withdraws before the deadline, or an organizer withdraws at any time."""
    event = project.event
    if not access.is_organizer(actor, event):
        team = _team_for_edit(project, actor)
        ensure_team_can_edit(event, team)
    with transaction.atomic():
        project.status = Project.Status.WITHDRAWN
        project.save(update_fields=["status", "updated_at"])
        audit.record("project.withdrawn", f"{actor.display_name} withdrew {project.title}",
                     actor=actor, event=event, target=project)
        from integrations import webhooks

        webhooks.emit(event, "project.withdrawn", {"project": project.public_id, "title": project.title})
    return project


def resolve_duplicate(project: Project, actor, *, make_canonical: bool):
    """Organizer decision on a flagged duplicate: swap which copy competes, or confirm."""
    access.require_organizer(actor, project.event)
    original = project.duplicate_of
    if original is None:
        raise Conflict("This project is not flagged as a duplicate.")
    with transaction.atomic():
        if make_canonical:
            # Point the old original at the new one first, so at no moment are there two
            # live projects for the team and the partial unique index stays satisfied.
            Project.objects.filter(pk=original.pk).update(duplicate_of=project)
            Project.objects.filter(duplicate_of=original).exclude(pk=project.pk).update(duplicate_of=project)
            Project.objects.filter(pk=project.pk).update(duplicate_of=None)
            summary = f"{actor.display_name} made {project.public_id} the competing copy of {project.title}"
        else:
            summary = f"{actor.display_name} confirmed {project.public_id} as a duplicate of {original.public_id}"
        audit.record("project.duplicate_resolved", summary, actor=actor, event=project.event,
                     target=project, data={"original": original.public_id, "swapped": make_canonical})


def grant_extension(event, team, until, reason, *, actor) -> SubmissionExtension:
    access.require_organizer(actor, event)
    if until <= event.submissions_close_at:
        raise DomainError("An extension must end after the event deadline.", code="invalid")
    with transaction.atomic():
        ext, _ = SubmissionExtension.objects.update_or_create(
            team=team, defaults={"event": event, "until": until, "reason": reason, "granted_by": actor},
        )
        audit.record(
            "team.extension_granted",
            f"{actor.display_name} let {team.name} edit until {_fmt(until)}: {reason}",
            actor=actor, event=event, target=team, data={"until": until.isoformat(), "reason": reason},
        )
    return ext
