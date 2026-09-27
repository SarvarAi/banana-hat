"""Community voting and comments.

The threat model (THREAT-MODEL.md) in one paragraph: we cannot stop a determined
person with many email addresses from voting many times, and we do not pretend to.
What we do is make every ballot cost something (a verified inbox, a signed-in account,
or a printed single-use link), make "one identity, one ballot" a database constraint,
fold the easy aliases of an email address into one identity, hold suspicious ballots
for an organizer instead of counting them, keep the tally invisible to everyone until
voting closes so there is no running score to chase, and write it all down.
"""

import hashlib
import hmac
import random
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.urls import reverse

from audit import services as audit
from core import clock, ratelimit
from core.errors import Conflict, DomainError, Forbidden
from events import access
from events.models import Event, Project, TeamMember
from voting.models import Ballot, BallotChoice, BallotToken, Comment

MAX_COMMENT = 2000
VERIFY_TTL = timedelta(hours=24)

# Heuristics. Each is a reason to hold a ballot for review, never a reason to discard
# it: an office full of people voting over one Wi-Fi network is normal.
BURST_WINDOW = timedelta(minutes=10)
BURST_SAME_NETWORK = 6
SAME_DEVICE = 3
NEW_ACCOUNT_AGE = timedelta(minutes=15)
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "trashmail.com",
    "yopmail.com", "sharklasers.com", "getnada.com", "dispostable.com", "maildrop.cc",
}


class VotingClosed(Forbidden):
    code = "voting_closed"


class ResultsHidden(Forbidden):
    code = "results_hidden"


# --- identities ------------------------------------------------------------------------

def canonical_email(email: str) -> str:
    """Fold the common aliases of one mailbox into one identity.

    Lower-case everything, drop a +tag, and for Gmail drop dots and treat googlemail.com
    as gmail.com, because Google delivers all of those to the same inbox.
    """
    email = (email or "").strip().lower()
    local, _, domain = email.partition("@")
    local = local.split("+", 1)[0]
    if domain in {"gmail.com", "googlemail.com"}:
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


def identity_digest(event: Event, kind: str, value: str) -> str:
    """Keyed, per event: the same person has unrelated digests in two events, and
    without SECRET_KEY the digest cannot be reversed by trying likely emails."""
    message = f"{event.public_id}:{kind}:{value}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def _digest(value: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:2]}***@{domain}"


def _fingerprint(meta):
    ip = meta.get("ip", "")
    return (_digest("net:" + ratelimit.network_of(ip)) if ip else "",
            _digest("ua:" + meta.get("agent", "") + "|" + ip) if ip else "")


def request_meta(request):
    return {"ip": ratelimit.client_ip(request), "agent": request.META.get("HTTP_USER_AGENT", "")[:300]}


# --- windows ---------------------------------------------------------------------------

def ensure_voting_open(event: Event, at=None):
    if not event.voting_enabled():
        raise VotingClosed("This event has no community vote.")
    at = at or clock.now()
    if not event.voting_open(at):
        if event.voting_open_at and at < event.voting_open_at:
            raise VotingClosed("Community voting has not opened yet.")
        raise VotingClosed("Community voting has closed.")


def ballot_projects(event: Event, ballot: Ballot):
    """The projects on a ballot, in an order that is random per ballot and stable for it.

    Position bias is real: the first entries on a list collect more votes. Each ballot
    gets its own shuffle, seeded by the ballot, so reloading the page does not reorder
    it but no two voters see the same order.
    """
    projects = list(Project.objects.canonical().filter(event=event).select_related("team", "track")
                    .order_by("public_id"))
    random.Random(ballot.shuffle_seed).shuffle(projects)
    return projects


# --- ballots ---------------------------------------------------------------------------

def _new_ballot(event, kind, identity, *, user=None, email_hint="", meta=None, status=Ballot.Status.COUNTED):
    network, agent = _fingerprint(meta or {})
    try:
        with transaction.atomic():
            return Ballot.objects.create(
                event=event, kind=kind, identity_digest=identity, user=user, email_hint=email_hint,
                network_digest=network, agent_digest=agent, status=status,
                shuffle_seed=secrets.randbits(62),
            ), True
    except IntegrityError:
        return Ballot.objects.get(event=event, identity_digest=identity), False


def ballot_for_user(event: Event, user, meta) -> Ballot:
    ensure_voting_open(event)
    if event.voting_mode != Event.VotingMode.AUTHENTICATED:
        raise Forbidden("This event does not use signed-in voting.")
    if not user.email_verified_at:
        # An unconfirmed account costs nothing to create, so it is not an identity.
        raise Forbidden("Confirm your email address to vote. The link is on your account page.",
                        code="email_unverified")
    if not event.voter_domain_allowed(user.email):
        raise Forbidden("Voting in this event is limited to addresses at: "
                        f"{event.voter_email_domains}.", code="domain_not_allowed")
    ballot, _ = _new_ballot(event, Ballot.Kind.USER, identity_digest(event, "user", str(user.pk)),
                            user=user, meta=meta)
    return ballot


def account_for_email(email: str):
    """The account that owns this mailbox, if any, matching on the canonical address.

    An email ballot is linked to it so the own-team rule applies: a participant cannot
    vote for their team just by using the email form in a private window.
    """
    from accounts.models import User

    canon = canonical_email(email)
    domain = canon.rsplit("@", 1)[-1]
    domains = ["gmail.com", "googlemail.com"] if domain == "gmail.com" else [domain]
    query = Q()
    for d in domains:
        query |= Q(email__iendswith="@" + d)
    return next((u for u in User.objects.filter(query).only("id", "email") if canonical_email(u.email) == canon),
                None)


def request_email_ballot(event: Event, email: str, meta, *, base_url) -> None:
    """Send a verification link. Always looks the same from outside, whether or not the
    address has voted before, so the form cannot be used to find out who voted."""
    ensure_voting_open(event)
    if event.voting_mode != Event.VotingMode.EMAIL:
        raise Forbidden("This event does not use email voting.")
    email = (email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise DomainError("Enter a valid email address.")
    if not event.voter_domain_allowed(email):
        raise Forbidden(f"Voting in this event is limited to addresses at: {event.voter_email_domains}.",
                        code="domain_not_allowed")
    ratelimit.enforce(ratelimit.EMAIL_VERIFY, "ip:" + meta.get("ip", ""))
    ratelimit.enforce(ratelimit.EMAIL_VERIFY, "email:" + canonical_email(email))
    identity = identity_digest(event, "email", canonical_email(email))
    raw = secrets.token_urlsafe(24)
    with transaction.atomic():
        ballot, _ = _new_ballot(event, Ballot.Kind.EMAIL, identity, email_hint=mask_email(email), meta=meta,
                                user=account_for_email(email), status=Ballot.Status.PENDING)
        if email.split("@")[-1] in DISPOSABLE_DOMAINS and "disposable email domain" not in ballot.flags:
            ballot.flags = [*ballot.flags, "disposable email domain"]
            ballot.save(update_fields=["flags"])
        BallotToken.objects.create(event=event, purpose=BallotToken.Purpose.VERIFY, ballot=ballot,
                                   token_digest=_digest("tok:" + raw), expires_at=clock.now() + VERIFY_TTL)
        audit.record("ballot.requested", f"Ballot link emailed to {mask_email(email)}", event=event,
                     target=ballot, source="vote")
    link = base_url + reverse("voting:verify", args=[event.slug, raw])
    send_mail(
        subject=f"Your ballot for {event.name}",
        message=(f"Open this link to vote in {event.name}:\n\n{link}\n\n"
                 f"It works for 24 hours. If you did not ask for it, ignore this email."
                 + ("\n\nYou have already voted; the link lets you review or change your ballot."
                    if ballot.cast_at else "")),
        from_email=None, recipient_list=[email], fail_silently=False,
    )


def redeem_verify_token(event: Event, raw: str) -> Ballot:
    token = BallotToken.objects.select_related("ballot").filter(
        event=event, purpose=BallotToken.Purpose.VERIFY, token_digest=_digest("tok:" + raw)).first()
    if token is None or (token.expires_at and token.expires_at <= clock.now()):
        raise DomainError("This link has expired or is not valid. Ask for a new one.", status=404)
    ballot = token.ballot
    with transaction.atomic():
        if token.used_at is None:
            token.used_at = clock.now()
            token.save(update_fields=["used_at"])
        if ballot.status == Ballot.Status.PENDING:
            ballot.status = Ballot.Status.COUNTED
            ballot.save(update_fields=["status"])
            audit.record("ballot.verified", f"Ballot {ballot.public_id} ({ballot.email_hint}) confirmed its email",
                         event=event, target=ballot, source="vote")
    return ballot


def create_ballot_links(event: Event, count: int, label: str, *, actor) -> list[tuple[str, str]]:
    """Mint `count` single-use ballot links (e.g. to print on table cards). Returned
    once; only digests are stored."""
    access.require_organizer(actor, event)
    if not 1 <= count <= 1000:
        raise DomainError("Create between 1 and 1000 links at a time.")
    out = []
    with transaction.atomic():
        for i in range(1, count + 1):
            raw = secrets.token_urlsafe(18)
            name = f"{label} {i}".strip()
            BallotToken.objects.create(event=event, purpose=BallotToken.Purpose.LINK, label=name,
                                       token_digest=_digest("tok:" + raw), created_by=actor)
            out.append((name, raw))
        audit.record("ballot.links_created", f"{actor.display_name} created {count} ballot links ({label})",
                     actor=actor, event=event, target=event)
    return out


def ballot_for_link(event: Event, raw: str, meta) -> Ballot:
    ensure_voting_open(event)
    token = BallotToken.objects.select_related("ballot").filter(
        event=event, purpose=BallotToken.Purpose.LINK, token_digest=_digest("tok:" + raw)).first()
    if token is None:
        raise DomainError("This ballot link is not valid.", status=404)
    if token.ballot_id:
        return token.ballot
    ballot, _ = _new_ballot(event, Ballot.Kind.LINK, identity_digest(event, "link", token.token_digest),
                            email_hint=token.label, meta=meta)
    token.ballot = ballot
    token.used_at = clock.now()
    token.save(update_fields=["ballot", "used_at"])
    return ballot


def assess(ballot: Ballot) -> list[str]:
    """Reasons to hold this ballot for review. Cheap queries, run at cast time."""
    flags = [f for f in ballot.flags if f == "disposable email domain"]
    now = clock.now()
    recent = Ballot.objects.filter(event=ballot.event, cast_at__gte=now - BURST_WINDOW).exclude(pk=ballot.pk)
    same_network = recent.filter(network_digest=ballot.network_digest).count() if ballot.network_digest else 0
    if same_network + 1 >= BURST_SAME_NETWORK:
        flags.append(f"{BURST_SAME_NETWORK}+ ballots from one network within 10 minutes")
    if ballot.agent_digest:
        same_device = Ballot.objects.filter(event=ballot.event, agent_digest=ballot.agent_digest,
                                            cast_at__isnull=False).exclude(pk=ballot.pk).count()
        if same_device + 1 >= SAME_DEVICE:
            flags.append(f"{same_device + 1} ballots from the same browser and address")
    if ballot.user_id and ballot.user.date_joined >= now - NEW_ACCOUNT_AGE and ballot.event.voting_open_at \
            and ballot.user.date_joined >= ballot.event.voting_open_at:
        flags.append("account created minutes before voting")
    return flags


def cast(ballot: Ballot, project_ids, *, voter=None) -> Ballot:
    event = ballot.event
    ensure_voting_open(event)
    if ballot.status == Ballot.Status.PENDING:
        raise Forbidden("Verify your email address before voting.")
    if ballot.status == Ballot.Status.VOID:
        raise Forbidden("This ballot was voided by the organizers.")
    wanted = list(dict.fromkeys(project_ids or []))
    if not wanted:
        raise DomainError("Pick at least one project.")
    if len(wanted) > event.votes_per_voter:
        raise DomainError(f"You can pick up to {event.votes_per_voter} projects.")
    allowed = {p.public_id: p for p in Project.objects.canonical().filter(event=event)}
    unknown = [pid for pid in wanted if pid not in allowed]
    if unknown:
        raise DomainError("One of those projects is not on this ballot.")
    user = voter or ballot.user
    if user is not None and user.is_authenticated:
        own = set(TeamMember.objects.filter(event=event, user=user).values_list("team_id", flat=True))
        if any(allowed[pid].team_id in own for pid in wanted):
            raise Forbidden("You cannot vote for your own team.", code="own_team")
    ratelimit.enforce(ratelimit.VOTE, "ballot:" + ballot.public_id)
    with transaction.atomic():
        # Lock the ballot: two simultaneous casts must not both replace its choices and
        # leave twice the allowed number behind.
        Ballot.objects.select_for_update().filter(pk=ballot.pk).first()
        first_time = ballot.cast_at is None
        ballot.choices.all().delete()
        BallotChoice.objects.bulk_create(BallotChoice(ballot=ballot, project=allowed[pid]) for pid in wanted)
        ballot.cast_at = clock.now()
        flags = assess(ballot)
        if flags and ballot.status == Ballot.Status.COUNTED and ballot.reviewed_at is None:
            ballot.status = Ballot.Status.FLAGGED
        ballot.flags = flags
        ballot.save(update_fields=["cast_at", "flags", "status"])
        # The audit log records that a ballot was cast, never what is on it: the log
        # is readable by organizers during voting, and the tally must not be.
        audit.record(
            "ballot.cast" if first_time else "ballot.changed",
            f"Ballot {ballot.public_id} ({ballot.get_kind_display().lower()}"
            f"{', ' + ballot.email_hint if ballot.email_hint else ''}) was "
            f"{'cast' if first_time else 'changed'}"
            + (f"; held for review: {'; '.join(flags)}" if ballot.status == Ballot.Status.FLAGGED else ""),
            actor=user if user and user.is_authenticated else None, event=event, target=ballot,
            data={"flags": flags}, source="vote",
        )
    return ballot


def review_ballot(ballot: Ballot, actor, decision: str):
    access.require_organizer(actor, ballot.event)
    if decision not in {"count", "void"}:
        raise DomainError("Decide to count or void the ballot.")
    if ballot.event.voting_closed():
        # Once voting closes the totals are public. Deciding held ballots after seeing
        # them would let an organizer choose the outcome.
        raise VotingClosed("Voting has closed; held ballots can only be reviewed while it is open.")
    with transaction.atomic():
        ballot.status = Ballot.Status.COUNTED if decision == "count" else Ballot.Status.VOID
        ballot.reviewed_by = actor
        ballot.reviewed_at = clock.now()
        ballot.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        audit.record("ballot.reviewed",
                     f"{actor.display_name} decided to {decision} ballot {ballot.public_id} "
                     f"(flags: {'; '.join(ballot.flags) or 'none'})",
                     actor=actor, event=ballot.event, target=ballot, data={"decision": decision})


def turnout(event: Event):
    """Counts by status. Safe to show during voting: it says nothing about projects."""
    rows = dict(Ballot.objects.filter(event=event, cast_at__isnull=False)
                .values_list("status").annotate(n=Count("id")))
    return {"counted": rows.get("counted", 0), "flagged": rows.get("flagged", 0),
            "void": rows.get("void", 0), "total": sum(rows.values())}


def tally(event: Event):
    """Votes per project from counted ballots. Refused to everyone, organizers and
    admins included, until voting closes."""
    if not event.voting_enabled():
        raise ResultsHidden("This event has no community vote.")
    if not event.voting_closed():
        raise ResultsHidden("Community vote totals are hidden until voting closes.")
    counts = dict(
        BallotChoice.objects.filter(ballot__event=event, ballot__status=Ballot.Status.COUNTED)
        .values_list("project_id").annotate(n=Count("id"))
    )
    projects = Project.objects.canonical().filter(event=event).select_related("team")
    rows = sorted(({"project": p.public_id, "title": p.title, "team": p.team.name,
                    "votes": counts.get(p.pk, 0)} for p in projects),
                  key=lambda r: (-r["votes"], r["title"]))
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
    return rows


def tally_if_closed(event: Event):
    try:
        return tally(event)
    except ResultsHidden:
        return None


# --- comments ------------------------------------------------------------------------

def visible_comments(project, user):
    comments = Comment.objects.filter(project=project).select_related("author")
    if not access.is_organizer(user, project.event):
        comments = comments.filter(hidden_at__isnull=True)
    return comments


def can_comment(project, user):
    return bool(
        user.is_authenticated
        and project.status == Project.Status.SUBMITTED
        and project.event.is_listed
    )


def add_comment(project, user, body, *, ip=""):
    if not can_comment(project, user):
        raise Forbidden("Sign in to comment on submitted projects.")
    body = (body or "").strip()
    if not body:
        raise DomainError("Write something first.")
    if len(body) > MAX_COMMENT:
        raise DomainError(f"Comments are limited to {MAX_COMMENT} characters.")
    ratelimit.enforce(ratelimit.COMMENT, f"user:{user.pk}")
    if ip:
        ratelimit.enforce(ratelimit.COMMENT, f"ip:{ip}")
    # The same text twice in a row from the same person is a double click or a bot.
    last = Comment.objects.filter(project=project, author=user).order_by("-created_at").first()
    if last and last.body == body:
        raise Conflict("You already posted that.", code="duplicate_comment")
    with transaction.atomic():
        comment = Comment.objects.create(project=project, author=user, body=body)
        audit.record("comment.created", f"{user.display_name} commented on {project.title}",
                     actor=user, event=project.event, target=comment)
        from integrations import webhooks

        webhooks.emit(project.event, "comment.created", {
            "project": project.public_id, "comment": comment.public_id, "author": user.display_name,
            "body": body,
        })
    return comment


def hide_comment(comment, actor, reason):
    access.require_organizer(actor, comment.project.event)
    reason = (reason or "").strip() or "hidden by an organizer"
    with transaction.atomic():
        comment.hidden_at = clock.now()
        comment.hidden_by = actor
        comment.hidden_reason = reason[:200]
        comment.save(update_fields=["hidden_at", "hidden_by", "hidden_reason"])
        audit.record(
            "comment.hidden",
            f"{actor.display_name} hid a comment by {comment.author.display_name} on "
            f"{comment.project.title}: {reason}",
            actor=actor, event=comment.project.event, target=comment,
        )


def unhide_comment(comment, actor):
    access.require_organizer(actor, comment.project.event)
    with transaction.atomic():
        comment.hidden_at = None
        comment.hidden_by = None
        comment.hidden_reason = ""
        comment.save(update_fields=["hidden_at", "hidden_by", "hidden_reason"])
        audit.record("comment.restored", f"{actor.display_name} restored a comment on {comment.project.title}",
                     actor=actor, event=comment.project.event, target=comment)


def flagged_ballots(event):
    return Ballot.objects.filter(event=event, status=Ballot.Status.FLAGGED).order_by("-cast_at")


def suspicious_clusters(event):
    """Networks that cast an unusual share of ballots; for the organizer's review page."""
    return list(
        Ballot.objects.filter(event=event, cast_at__isnull=False).exclude(network_digest="")
        .values("network_digest").annotate(n=Count("id"), flagged=Count("id", filter=Q(status="flagged")))
        .filter(n__gte=3).order_by("-n")[:10]
    )
