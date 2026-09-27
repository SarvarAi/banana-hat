from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.access import authenticated, public
from core.errors import DomainError
from events import access
from events.models import Event, Project
from voting import services as voting
from voting.models import Ballot, Comment


def _session_key(event):
    return f"ballot:{event.public_id}"


@public
def ballot(request, slug):
    """The ballot page. How a voter gets a ballot depends on the event's voting mode."""
    event = get_object_or_404(Event, slug=slug)
    if not access.can_view_event(request.user, event) or not event.voting_enabled():
        raise Http404
    meta = voting.request_meta(request)
    mode = event.voting_mode
    ballot_obj = None
    context = {"event": event, "mode": mode, "nav": "events"}

    if not event.voting_open():
        context["closed"] = True
        context["tally"] = voting.tally_if_closed(event)
        return render(request, "voting/ballot.html", context)

    try:
        if mode == Event.VotingMode.AUTHENTICATED:
            if not request.user.is_authenticated:
                return render(request, "voting/ballot.html", {**context, "needs_login": True})
            ballot_obj = voting.ballot_for_user(event, request.user, meta)
        else:
            public_id = request.session.get(_session_key(event))
            ballot_obj = Ballot.objects.filter(event=event, public_id=public_id).first() if public_id else None
            if ballot_obj is None:
                if mode == Event.VotingMode.EMAIL and request.method == "POST" and "email" in request.POST:
                    voting.request_email_ballot(event, request.POST["email"], meta,
                                                base_url=request.build_absolute_uri("/")[:-1])
                    return render(request, "voting/ballot.html", {**context, "check_inbox": True})
                return render(request, "voting/ballot.html", {**context, "needs_identity": True})
    except DomainError as exc:
        messages.error(request, exc.message)
        return render(request, "voting/ballot.html", {**context, "needs_identity": mode != "authenticated"})

    if request.method == "POST" and "projects" in request.POST:
        try:
            voting.cast(ballot_obj, request.POST.getlist("projects"),
                        voter=request.user if request.user.is_authenticated else None)
        except DomainError as exc:
            messages.error(request, exc.message)
        else:
            ballot_obj.refresh_from_db()
            if ballot_obj.status == Ballot.Status.FLAGGED:
                messages.warning(request, "Your ballot is saved. It is held for a quick review before it counts.")
            else:
                messages.success(request, "Your vote is in. You can change it until voting closes.")
            return redirect("voting:ballot", slug=slug)

    chosen = set(ballot_obj.choices.values_list("project__public_id", flat=True))
    own_team = access.team_of(request.user, event)
    return render(request, "voting/ballot.html", {
        **context, "ballot": ballot_obj, "chosen": chosen,
        "projects": voting.ballot_projects(event, ballot_obj),
        "own_team_id": own_team.pk if own_team else None,
    })


@public
def verify(request, slug, token):
    event = get_object_or_404(Event, slug=slug)
    try:
        ballot_obj = voting.redeem_verify_token(event, token)
    except DomainError as exc:
        messages.error(request, exc.message)
        return redirect("voting:ballot", slug=slug)
    request.session[_session_key(event)] = ballot_obj.public_id
    messages.success(request, "Email confirmed. Here is your ballot.")
    return redirect("voting:ballot", slug=slug)


@public
def link(request, slug, token):
    event = get_object_or_404(Event, slug=slug)
    try:
        ballot_obj = voting.ballot_for_link(event, token, voting.request_meta(request))
    except DomainError as exc:
        messages.error(request, exc.message)
        return redirect("voting:ballot", slug=slug)
    request.session[_session_key(event)] = ballot_obj.public_id
    return redirect("voting:ballot", slug=slug)


@authenticated
@require_POST
def comment(request, public_id):
    project = get_object_or_404(Project, public_id=public_id)
    if not access.can_view_event(request.user, project.event):
        raise Http404
    try:
        voting.add_comment(project, request.user, request.POST.get("body", ""),
                           ip=voting.request_meta(request)["ip"])
    except DomainError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Comment posted.")
    return redirect(project.get_absolute_url() + "#comments")


@authenticated
@require_POST
def moderate_comment(request, public_id):
    comment_obj = get_object_or_404(Comment.objects.select_related("project__event"), public_id=public_id)
    try:
        if request.POST.get("action") == "restore":
            voting.unhide_comment(comment_obj, request.user)
        else:
            voting.hide_comment(comment_obj, request.user, request.POST.get("reason", ""))
    except DomainError as exc:
        messages.error(request, exc.message)
    return redirect(comment_obj.project.get_absolute_url() + "#comments")
