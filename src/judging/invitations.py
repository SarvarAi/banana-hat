"""Inviting judges by email."""

import hashlib
import secrets
from datetime import timedelta

from django.core.mail import send_mail
from django.db import transaction
from django.urls import reverse

from accounts import verification
from accounts.models import User
from audit import services as audit
from core import clock
from core.errors import Conflict, DomainError, Forbidden
from events import access
from events import services as event_services
from events.models import EventRole
from judging.models import JudgeInvitation

INVITE_TTL = timedelta(days=14)


def _digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def invite(event, actor, *, email, name="", tracks=(), base_url) -> JudgeInvitation:
    access.require_organizer(actor, event)
    email = User.objects.normalize_email(email)
    if not email or "@" not in email:
        raise DomainError("Enter the judge's email address.")
    existing = User.objects.filter(email=email).first()
    if existing and EventRole.objects.filter(event=event, user=existing, role=EventRole.Role.JUDGE).exists():
        raise Conflict(f"{email} is already a judge of this event.", code="already_judge")
    raw = secrets.token_urlsafe(24)
    with transaction.atomic():
        invitation = JudgeInvitation.objects.create(
            event=event, email=email, name=name, token_digest=_digest(raw), invited_by=actor,
            expires_at=clock.now() + INVITE_TTL,
        )
        invitation.tracks.set(tracks)
        audit.record("judge.invited", f"{actor.display_name} invited {email} to judge", actor=actor,
                     event=event, target=invitation)
    link = base_url + reverse("judging:accept-invite", args=[raw])
    track_names = ", ".join(t.name for t in tracks) or "any track"
    send_mail(
        subject=f"You're invited to judge {event.name}",
        message=(
            f"Hello{(' ' + name) if name else ''},\n\n"
            f"{actor.display_name} has invited you to judge {event.name} ({track_names}).\n\n"
            f"Accept the invitation here:\n{link}\n\n"
            f"The link works for 14 days and only for {email}.\n"
        ),
        from_email=None, recipient_list=[email], fail_silently=False,
    )
    invitation.raw_token = raw  # for the organizer UI in demo mode and for tests; never stored
    return invitation


def parse_roster(event, text: str):
    """Read a judge roster: one judge per line, `email,name,tracks`.

    Tracks are separated by `;` and may be given by name or id; a header row is
    skipped. Returns (rows, problems); rows are (email, name, [Track]).
    """
    import csv
    import io

    by_key = {}
    for track in event.tracks.all():
        by_key[track.public_id.lower()] = track
        by_key[track.name.lower()] = track
    rows, problems, seen = [], [], set()
    for number, cells in enumerate(csv.reader(io.StringIO(text.strip())), start=1):
        cells = [c.strip() for c in cells]
        if not cells or not any(cells):
            continue
        if number == 1 and cells[0].lower() in {"email", "e-mail"}:
            continue
        email = User.objects.normalize_email(cells[0])
        if "@" not in email:
            problems.append(f"line {number}: {cells[0]!r} is not an email address")
            continue
        if email in seen:
            problems.append(f"line {number}: {email} appears twice")
            continue
        seen.add(email)
        name = cells[1] if len(cells) > 1 else ""
        tracks = []
        for key in filter(None, (t.strip() for t in (cells[2] if len(cells) > 2 else "").split(";"))):
            track = by_key.get(key.lower())
            if track is None:
                problems.append(f"line {number}: no track called {key!r}")
            else:
                tracks.append(track)
        rows.append((email, name, tracks))
    return rows, problems


def invite_many(event, actor, text: str, *, base_url):
    """Invite a whole roster. Every line is checked first; if any is wrong, nobody is
    invited and every problem is reported, the same all-or-nothing rule as imports.
    People who already judge this event are skipped, not refused."""
    access.require_organizer(actor, event)
    rows, problems = parse_roster(event, text)
    if problems:
        raise DomainError("Nothing was sent. " + "; ".join(problems[:10]), code="invalid_roster")
    if not rows:
        raise DomainError("The roster is empty.", code="invalid_roster")
    judging = set(EventRole.objects.judges().filter(event=event).values_list("user__email", flat=True))
    pending = set(JudgeInvitation.objects.filter(event=event, accepted_at__isnull=True, revoked_at__isnull=True,
                                                 expires_at__gt=clock.now()).values_list("email", flat=True))
    sent, skipped = [], []
    with transaction.atomic():
        for email, name, tracks in rows:
            if email in judging or email in pending:
                skipped.append(email)
                continue
            sent.append(invite(event, actor, email=email, name=name, tracks=tracks, base_url=base_url))
    return sent, skipped


def find(raw) -> JudgeInvitation | None:
    return JudgeInvitation.objects.select_related("event").filter(token_digest=_digest(raw)).first()


def accept(raw, user) -> EventRole:
    invitation = find(raw)
    if invitation is None:
        raise DomainError("This invitation link is not valid.", status=404)
    if invitation.state != "pending":
        raise Conflict(f"This invitation is {invitation.state}.", code=f"invitation_{invitation.state}")
    if User.objects.normalize_email(user.email) != invitation.email:
        raise Forbidden(f"This invitation is for {invitation.email}. Sign in with that address to accept it.")
    event = invitation.event
    with transaction.atomic():
        role = event_services.grant_role(event, user, EventRole.Role.JUDGE, actor=user,
                                         tracks=list(invitation.tracks.all()))
        invitation.accepted_at = clock.now()
        invitation.accepted_by = user
        invitation.save(update_fields=["accepted_at", "accepted_by"])
        audit.record("judge.accepted", f"{user.display_name} accepted the invitation to judge",
                     actor=user, event=event, target=invitation)
        # The invitation was emailed to this address and opened, so the address is theirs.
        verification.mark_verified(user, "judge invitation")
    return role


def set_tracks(role: EventRole, tracks, *, actor):
    """Change which tracks a judge reviews. Existing assignments are left alone."""
    access.require_organizer(actor, role.event)
    tracks = list(tracks)
    with transaction.atomic():
        role.tracks.set(tracks)
        audit.record("judge.tracks_changed",
                     f"{actor.display_name} set {role.user.display_name}'s tracks to "
                     f"{', '.join(t.name for t in tracks) or 'any'}",
                     actor=actor, event=role.event, target=role,
                     data={"tracks": [t.public_id for t in tracks]})


def revoke(invitation, actor):
    access.require_organizer(actor, invitation.event)
    with transaction.atomic():
        invitation.revoked_at = clock.now()
        invitation.save(update_fields=["revoked_at"])
        audit.record("judge.invitation_revoked", f"{actor.display_name} revoked the invitation for {invitation.email}",
                     actor=actor, event=invitation.event, target=invitation)
