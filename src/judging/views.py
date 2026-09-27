"""The judge's side: review queue, scorecard, pairwise mode, invitations."""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.access import authenticated, public
from core.errors import DomainError
from events import access
from events.models import Event, EventRole
from judging import assignment as assignment_service
from judging import invitations, pairwise, scoring
from judging.models import Assignment, PairwiseComparison
from records.models import SignedRecord


@authenticated
def home(request):
    roles = (
        EventRole.objects.judges().filter(user=request.user).select_related("event")
        .order_by("-event__submissions_close_at")
    )
    rows = []
    for role in roles:
        a = Assignment.objects.filter(judge=role).exclude(status=Assignment.Status.RECUSED)
        rows.append({"event": role.event, "total": a.count(), "done": a.filter(status=Assignment.Status.DONE).count()})
    if len(rows) == 1:
        return redirect("judging:event", slug=rows[0]["event"].slug)
    return render(request, "judging/home.html", {"rows": rows, "nav": "mine"})


def _judge(request, slug):
    event = get_object_or_404(Event, slug=slug)
    role = access.judge_role(request.user, event)
    if role is None:
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied("You are not a judge of this event.")
    return event, role


@authenticated
def event_queue(request, slug):
    event, role = _judge(request, slug)
    assignments = (
        Assignment.objects.filter(judge=role)
        .select_related("project__team", "project__track", "score")
        .order_by("status", "project__title")
    )
    pending = [a for a in assignments if a.status == Assignment.Status.PENDING]
    done = [a for a in assignments if a.status == Assignment.Status.DONE]
    recused = [a for a in assignments if a.status == Assignment.Status.RECUSED]
    record = SignedRecord.objects.filter(event=event, kind=SignedRecord.Kind.JUDGE, subject=request.user).first()
    return render(request, "judging/queue.html", {
        "event": event, "role": role, "pending": pending, "done": done, "recused": recused,
        "judging_open": event.judging_open(), "record": record,
        "comparisons": PairwiseComparison.objects.filter(judge=role).count(),
        "pairwise_enabled": event.normalization == Event.Normalization.PAIRWISE, "nav": "mine",
    })


@authenticated
def review(request, slug, assignment_id):
    event, role = _judge(request, slug)
    assignment = get_object_or_404(
        Assignment.objects.select_related("project__team", "project__track"), public_id=assignment_id, judge=role
    )
    criteria = list(event.criteria.all())
    score = getattr(assignment, "score", None)
    current = score.as_dict() if score else {}
    comment = score.comment if score else ""
    error = None
    if request.method == "POST":
        values = {c.key: request.POST.get(f"c_{c.key}") for c in criteria}
        values = {k: v for k, v in values.items() if v not in (None, "")}
        comment = request.POST.get("comment", "")
        try:
            scoring.submit_score(assignment, request.user, values, comment)
        except DomainError as exc:
            error = exc.message
            current = values
        else:
            messages.success(request, f"Score saved for {assignment.project.title}.")
            nxt = Assignment.objects.filter(judge=role, status=Assignment.Status.PENDING).exclude(
                pk=assignment.pk).order_by("project__title").first()
            if nxt and "next" in request.POST:
                return redirect("judging:review", slug=slug, assignment_id=nxt.public_id)
            return redirect("judging:event", slug=slug)
    scale = list(range(event.score_min, event.score_max + 1))
    rows = [{"c": c, "value": str(current.get(c.key, "")), "name": f"c_{c.key}"} for c in criteria]
    return render(request, "judging/review.html", {
        "event": event, "assignment": assignment, "project": assignment.project, "rows": rows,
        "scale": scale, "comment": comment, "error": error, "score": score,
        "judging_open": event.judging_open(), "nav": "mine",
    })


@authenticated
@require_POST
def recuse(request, slug, assignment_id):
    event, role = _judge(request, slug)
    assignment = get_object_or_404(Assignment, public_id=assignment_id, judge=role)
    try:
        assignment_service.recuse(assignment, actor=request.user, reason=request.POST.get("reason", ""))
    except DomainError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "You are recused. The organizers will see the conflict and reassign the project.")
    return redirect("judging:event", slug=slug)


@authenticated
def pairwise_view(request, slug):
    event, role = _judge(request, slug)
    if request.method == "POST":
        try:
            pairwise.record(event, role, request.POST.get("winner", ""), request.POST.get("loser", ""))
        except DomainError as exc:
            messages.error(request, exc.message)
        return redirect("judging:pairwise", slug=slug)
    pair = pairwise.next_pair(event, role) if event.judging_open() else None
    return render(request, "judging/pairwise.html", {
        "event": event, "pair": pair, "count": PairwiseComparison.objects.filter(judge=role).count(), "nav": "mine",
    })


@public
def accept_invite(request, token):
    invitation = invitations.find(token)
    if invitation is None:
        raise Http404
    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect(f"/accounts/login/?next=/invites/{token}/")
        try:
            invitations.accept(token, request.user)
        except DomainError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, f"You are now a judge of {invitation.event.name}.")
            return redirect("judging:event", slug=invitation.event.slug)
    return render(request, "judging/accept.html", {"invitation": invitation, "token": token})
