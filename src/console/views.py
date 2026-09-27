"""The organizer console. Every view here starts with `_organized()`, which refuses
anyone who is not an organizer of the event (or a platform admin) with a 403."""

import csv
import io
import json

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from audit.models import AuditEntry
from audit.services import verify_chain
from core.access import authenticated
from core.errors import DomainError
from events import access, console
from events import services as event_services
from events.forms import EventCreateForm, EventForm, ExtensionForm, PrizeForm, TrackForm
from events.models import Event, EventRole, Project
from integrations import interchange, webhooks
from integrations.console import create_webhook, delete_webhook
from integrations.models import WEBHOOK_EVENT_TYPES, WebhookEndpoint
from judging import assignment as assignment_service
from judging import invitations, scoring
from judging import results as results_service
from judging.models import Assignment, JudgeInvitation, Score
from judging.rubric import replace_rubric
from records.models import SignedRecord
from voting import services as voting
from voting.models import Ballot

TABS = [
    ("overview", "Overview"), ("dashboard", "Live progress"), ("submissions", "Submissions"),
    ("judges", "Judges"), ("assignments", "Assignments"), ("results", "Results"),
    ("voting", "Community"), ("audit", "Audit trail"), ("data", "Data and webhooks"),
]


def _organized(request, slug) -> Event:
    event = get_object_or_404(Event, slug=slug)
    if not access.is_organizer(request.user, event):
        raise PermissionDenied("Only this event's organizers can open its console.")
    return event


def _render(request, template, event, tab, **context):
    return render(request, template, {"event": event, "tabs": TABS, "tab": tab, "nav": "mine", **context})


def _fail(request, exc):
    messages.error(request, exc.message if isinstance(exc, DomainError) else str(exc))


# --- event ------------------------------------------------------------------------

@authenticated
def create(request):
    if not request.user.may_create_events:
        raise PermissionDenied("Your account cannot create events. Ask a platform administrator.")
    form = EventCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = {k: v for k, v in form.cleaned_data.items()}
        try:
            event = console.create_event(request.user, data)
        except DomainError as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, "Event created with a starter rubric. Add tracks and prizes next.")
            return redirect("manage:overview", slug=event.slug)
    return render(request, "console/create.html", {"form": form, "nav": "mine"})


@authenticated
def overview(request, slug):
    event = _organized(request, slug)
    form = EventForm(request.POST or None, instance=event, prefix="event")
    if request.method == "POST" and "event-name" in request.POST and form.is_valid():
        try:
            changes = {k: form.cleaned_data[k] for k in form.changed_data}
            event.refresh_from_db()
            console.update_event(event, request.user, changes)
        except DomainError as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, "Settings saved.")
            return redirect("manage:overview", slug=event.slug)
    counts = {
        "projects": event.projects.canonical().count(),
        "drafts": event.projects.filter(status="draft").count(),
        "duplicates": event.projects.filter(duplicate_of__isnull=False).count(),
        "teams": event.teams.count(),
        "judges": event.roles.judges().count(),
        "scores": Score.objects.counted().filter(assignment__event=event).count(),
    }
    return _render(request, "console/overview.html", event, "overview", form=form, counts=counts,
                   tracks=event.tracks.annotate(n=Count("projects")), prizes=event.prizes.select_related("track"),
                   criteria=event.criteria.all(), track_form=TrackForm(prefix="track"),
                   prize_form=PrizeForm(prefix="prize", event=event),
                   organizers=event.roles.organizers().select_related("user"))


@authenticated
@require_POST
def tracks(request, slug):
    event = _organized(request, slug)
    try:
        if "delete" in request.POST:
            console.delete_track(get_object_or_404(event.tracks, public_id=request.POST["delete"]), request.user)
            messages.success(request, "Track removed.")
        else:
            form = TrackForm(request.POST, prefix="track")
            if form.is_valid():
                console.add_track(event, request.user, form.cleaned_data["name"], form.cleaned_data["description"])
                messages.success(request, "Track added.")
            else:
                messages.error(request, "A track needs a name.")
    except DomainError as exc:
        _fail(request, exc)
    return redirect("manage:overview", slug=slug)


@authenticated
@require_POST
def prizes(request, slug):
    event = _organized(request, slug)
    try:
        if "delete" in request.POST:
            console.delete_prize(get_object_or_404(event.prizes, public_id=request.POST["delete"]), request.user)
            messages.success(request, "Prize removed.")
        else:
            form = PrizeForm(request.POST, prefix="prize", event=event)
            if form.is_valid():
                console.add_prize(event, request.user, **form.cleaned_data)
                messages.success(request, "Prize added.")
            else:
                messages.error(request, "A prize needs a name.")
    except DomainError as exc:
        _fail(request, exc)
    return redirect("manage:overview", slug=slug)


@authenticated
def rubric(request, slug):
    event = _organized(request, slug)
    criteria = list(event.criteria.all())
    if request.method == "POST":
        rows = []
        keys = request.POST.getlist("key")
        for i, key in enumerate(keys):
            name = request.POST.getlist("name")[i].strip()
            if not (key or name):
                continue
            rows.append({"key": key or name, "name": name, "weight": request.POST.getlist("weight")[i] or "1",
                         "description": request.POST.getlist("description")[i]})
        try:
            replace_rubric(event, request.user, rows)
        except (DomainError, ArithmeticError, ValueError) as exc:
            _fail(request, exc)
        else:
            messages.success(request, "Rubric saved. Rankings recompute with the new weights.")
            return redirect("manage:rubric", slug=slug)
    total = sum(float(c.weight) for c in criteria) or 1
    rows = [{"c": c, "share": round(100 * float(c.weight) / total)} for c in criteria]
    locked = Score.objects.filter(assignment__event=event).exists()
    return _render(request, "console/rubric.html", event, "overview", rows=rows, locked=locked)


# --- submissions -------------------------------------------------------------------

@authenticated
def submissions(request, slug):
    event = _organized(request, slug)
    projects = (
        event.projects.select_related("team", "track", "duplicate_of")
        .annotate(reviews=Count("assignments", filter=Q(assignments__status="done")))
        .order_by("status", "title")
    )
    status = request.GET.get("status")
    if status == "duplicates":
        projects = projects.filter(duplicate_of__isnull=False)
    elif status:
        projects = projects.filter(status=status)
    extension_form = ExtensionForm(request.POST or None, event=event)
    if request.method == "POST" and extension_form.is_valid():
        try:
            event_services.grant_extension(event, extension_form.cleaned_data["team"],
                                           extension_form.cleaned_data["until"],
                                           extension_form.cleaned_data["reason"], actor=request.user)
        except DomainError as exc:
            extension_form.add_error(None, exc.message)
        else:
            messages.success(request, "Extension granted and recorded in the audit trail.")
            return redirect("manage:submissions", slug=slug)
    return _render(request, "console/submissions.html", event, "submissions", projects=projects,
                   status=status, extension_form=extension_form,
                   extensions=event.extensions.select_related("team", "granted_by"))


@authenticated
@require_POST
def project_action(request, slug, public_id):
    event = _organized(request, slug)
    project = get_object_or_404(Project, event=event, public_id=public_id)
    action = request.POST.get("action")
    try:
        if action == "withdraw":
            event_services.withdraw_project(project, request.user)
            messages.success(request, f"{project.title} withdrawn.")
        elif action == "make_canonical":
            event_services.resolve_duplicate(project, request.user, make_canonical=True)
            messages.success(request, f"{project.public_id} now competes; the other copy is the duplicate.")
        elif action == "confirm_duplicate":
            event_services.resolve_duplicate(project, request.user, make_canonical=False)
            messages.success(request, "Duplicate confirmed.")
    except DomainError as exc:
        _fail(request, exc)
    return redirect(request.POST.get("next") or "manage:submissions", slug=slug)


# --- judges ----------------------------------------------------------------------

@authenticated
def judges(request, slug):
    event = _organized(request, slug)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "invite":
                track_ids = request.POST.getlist("tracks")
                chosen = list(event.tracks.filter(public_id__in=track_ids))
                inv = invitations.invite(
                    event, request.user, email=request.POST.get("email", ""), name=request.POST.get("name", ""),
                    tracks=chosen, base_url=request.build_absolute_uri("/")[:-1],
                )
                messages.success(request, f"Invitation emailed to {inv.email}.")
            elif action == "revoke":
                invitations.revoke(get_object_or_404(JudgeInvitation, event=event, pk=request.POST["invitation"]),
                                   request.user)
                messages.success(request, "Invitation revoked.")
            elif action == "tracks":
                role = get_object_or_404(EventRole, event=event, public_id=request.POST["judge"],
                                         role=EventRole.Role.JUDGE)
                invitations.set_tracks(role, event.tracks.filter(public_id__in=request.POST.getlist("tracks")),
                                       actor=request.user)
                messages.success(request, "Tracks updated.")
            elif action == "remove":
                role = get_object_or_404(EventRole, event=event, public_id=request.POST["judge"],
                                         role=EventRole.Role.JUDGE)
                if Score.objects.filter(assignment__judge=role).exists():
                    raise DomainError("This judge has scored projects. Void their scores first if they must go.")
                event_services.revoke_role(role, actor=request.user)
                messages.success(request, "Judge removed.")
        except DomainError as exc:
            _fail(request, exc)
        return redirect("manage:judges", slug=slug)
    roles = (
        event.roles.judges().select_related("user").prefetch_related("tracks")
        .annotate(total=Count("assignments", filter=~Q(assignments__status="recused")),
                  done=Count("assignments", filter=Q(assignments__status="done")))
        .order_by("user__name")
    )
    return _render(request, "console/judges.html", event, "judges", roles=roles,
                   invitations=event.judge_invitations.prefetch_related("tracks").order_by("-created_at"),
                   tracks=event.tracks.all())


# --- assignments -----------------------------------------------------------------

@authenticated
def assignments(request, slug):
    event = _organized(request, slug)
    preview = None
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            target = int(request.POST.get("target") or event.reviews_per_project)
            max_load = int(request.POST["max_load"]) if request.POST.get("max_load") else None
            if action == "preview":
                preview = assignment_service.plan_assignments(event, target=target, max_load=max_load)
            elif action == "run":
                plan = assignment_service.auto_assign(event, actor=request.user, target=target, max_load=max_load)
                messages.success(request, f"Created {len(plan.created)} assignments."
                                 + (f" {len(plan.shortfalls)} project(s) could not reach {target}."
                                    if plan.shortfalls else ""))
                return redirect("manage:assignments", slug=slug)
            elif action == "assign":
                judge = get_object_or_404(EventRole, event=event, public_id=request.POST["judge"],
                                          role=EventRole.Role.JUDGE)
                project = get_object_or_404(Project, event=event, public_id=request.POST["project"])
                assignment_service.assign(event, judge, project, actor=request.user)
                messages.success(request, f"Assigned {judge.user.display_name} to {project.title}.")
                return redirect("manage:assignments", slug=slug)
            elif action == "unassign":
                assignment_service.unassign(get_object_or_404(Assignment, event=event,
                                                              public_id=request.POST["assignment"]),
                                            actor=request.user)
                messages.success(request, "Assignment removed.")
                return redirect("manage:assignments", slug=slug)
            elif action == "void":
                score = get_object_or_404(Score, assignment__event=event, public_id=request.POST["score"])
                scoring.void_score(score, actor=request.user, reason=request.POST.get("reason", ""))
                messages.success(request, "Score voided. It no longer counts.")
                return redirect("manage:assignments", slug=slug)
        except (DomainError, ValueError) as exc:
            _fail(request, exc)
    projects = (
        Project.objects.canonical().filter(event=event).select_related("track", "team")
        .prefetch_related(Prefetch("assignments", queryset=Assignment.objects.select_related(
            "judge__user", "score").order_by("judge__user__name")))
        .order_by("track__position", "title")
    )
    return _render(request, "console/assignments.html", event, "assignments", projects=projects,
                   preview=preview, judges=event.roles.judges().select_related("user").order_by("user__name"))


# --- dashboard -------------------------------------------------------------------

@authenticated
def dashboard(request, slug):
    event = _organized(request, slug)
    return _render(request, "console/dashboard.html", event, "dashboard", p=results_service.progress(event))


@authenticated
def dashboard_live(request, slug):
    """The dashboard body alone; the page re-fetches it every few seconds."""
    event = _organized(request, slug)
    return render(request, "console/_dashboard_body.html", {"event": event, "p": results_service.progress(event)})


# --- results ---------------------------------------------------------------------

@authenticated
def results(request, slug):
    event = _organized(request, slug)
    method = request.GET.get("method") or event.normalization
    if method not in results_service.METHOD_LABELS:
        method = event.normalization
    if request.method == "POST":
        try:
            if request.POST.get("action") == "publish":
                results_service.publish(event, request.user, request.POST.get("method") or None)
                messages.success(request, "Results published and signed.")
            elif request.POST.get("action") == "unpublish":
                reason = request.POST.get("reason", "").strip() or "no reason given"
                results_service.unpublish(event, request.user, reason)
                messages.success(request, "Results taken down. Judging is open again.")
            elif request.POST.get("action") == "default_method":
                chosen = request.POST.get("method")
                if chosen in results_service.METHOD_LABELS:
                    console.update_event(event, request.user, {"normalization": chosen})
                    messages.success(request, "Default method changed.")
        except DomainError as exc:
            _fail(request, exc)
        return redirect("manage:results", slug=slug)
    computed = results_service.compute(event, method)
    return _render(request, "console/results.html", event, "results", results=computed, method=method,
                   methods=results_service.METHOD_LABELS, comparison=results_service.compare_methods(event),
                   published=results_service.published_results(event),
                   records=SignedRecord.objects.filter(event=event).order_by("kind", "-issued_at")[:60])


# --- community -------------------------------------------------------------------

@authenticated
def voting_view(request, slug):
    event = _organized(request, slug)
    links = None
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action in {"count", "void"}:
                voting.review_ballot(get_object_or_404(Ballot, event=event, public_id=request.POST["ballot"]),
                                     request.user, action)
                messages.success(request, f"Ballot marked {'counted' if action == 'count' else 'void'}.")
                return redirect("manage:voting", slug=slug)
            if action == "links":
                links = voting.create_ballot_links(event, int(request.POST.get("count") or 10),
                                                   request.POST.get("label", "Ballot").strip() or "Ballot",
                                                   actor=request.user)
                if request.POST.get("format") == "csv":
                    buffer = io.StringIO()
                    writer = csv.writer(buffer)
                    writer.writerow(["label", "url"])
                    for label, raw in links:
                        writer.writerow([label, request.build_absolute_uri(f"/vote/{event.slug}/link/{raw}/")])
                    response = HttpResponse(buffer.getvalue(), content_type="text/csv")
                    response["Content-Disposition"] = f'attachment; filename="{event.slug}-ballot-links.csv"'
                    return response
                links = [(label, request.build_absolute_uri(f"/vote/{event.slug}/link/{raw}/")) for label, raw in links]
        except (DomainError, ValueError) as exc:
            _fail(request, exc)
    tally = voting.tally_if_closed(event)
    return _render(request, "console/voting.html", event, "voting", turnout=voting.turnout(event),
                   flagged=voting.flagged_ballots(event), clusters=voting.suspicious_clusters(event),
                   tally=tally, links=links,
                   hidden_comments=event.projects.filter(comments__hidden_at__isnull=False).distinct().count())


# --- audit -----------------------------------------------------------------------

@authenticated
def audit_log(request, slug):
    event = _organized(request, slug)
    entries = AuditEntry.objects.filter(event=event).order_by("-id")
    category = request.GET.get("category", "")
    if category:
        entries = entries.filter(action__startswith=category + ".")
    q = request.GET.get("q", "").strip()
    if q:
        entries = entries.filter(Q(summary__icontains=q) | Q(actor_label__icontains=q) | Q(target_id=q))
    categories = sorted({a.split(".")[0] for a in AuditEntry.objects.filter(event=event)
                        .values_list("action", flat=True).distinct()})
    if request.GET.get("format") == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["id", "at", "actor", "action", "target_type", "target_id", "summary", "digest"])
        for e in entries.iterator():
            writer.writerow([e.pk, e.at.isoformat(), e.actor_label, e.action, e.target_type, e.target_id,
                             e.summary, e.digest])
        response = HttpResponse(buffer.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{event.slug}-audit.csv"'
        return response
    return _render(request, "console/audit.html", event, "audit", entries=entries[:300],
                   categories=categories, category=category, q=q, chain=verify_chain())


# --- data ------------------------------------------------------------------------

@authenticated
def data(request, slug):
    event = _organized(request, slug)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "webhook":
                url = request.POST.get("url", "").strip()
                if not url.startswith(("http://", "https://")):
                    raise DomainError("Enter a full http(s) URL.")
                create_webhook(event, request.user, url=url, description=request.POST.get("description", ""),
                               event_types=request.POST.getlist("event_types"))
                messages.success(request, "Webhook added. Its signing secret is shown below.")
            elif action == "delete_webhook":
                delete_webhook(get_object_or_404(WebhookEndpoint, event=event, public_id=request.POST["webhook"]),
                               request.user)
                messages.success(request, "Webhook removed.")
            elif action == "ping":
                delivery = webhooks.ping(get_object_or_404(WebhookEndpoint, event=event,
                                                           public_id=request.POST["webhook"]))
                if delivery.status == delivery.Status.SUCCEEDED:
                    messages.success(request, f"Ping delivered (HTTP {delivery.last_status_code}).")
                else:
                    messages.error(request, f"Ping failed: {delivery.last_error}")
        except DomainError as exc:
            _fail(request, exc)
        return redirect("manage:data", slug=slug)
    return _render(request, "console/data.html", event, "data",
                   hooks=event.webhooks.prefetch_related("deliveries"), event_types=WEBHOOK_EVENT_TYPES)


@authenticated
def import_event(request):
    """Create a new event from a fixture-format JSON file."""
    if not request.user.may_create_events:
        raise PermissionDenied("Your account cannot create events.")
    report = None
    if request.method == "POST" and request.FILES.get("file"):
        try:
            document = json.load(request.FILES["file"])
            report = interchange.import_as_organizer(document, request.user)
        except (ValueError, UnicodeDecodeError):
            messages.error(request, "That file is not valid JSON.")
        except DomainError as exc:
            problems = getattr(exc, "problems", [exc.message])
            return render(request, "console/import.html", {"problems": problems, "nav": "mine"})
        else:
            messages.success(request, f"Imported {report.event.name}.")
    return render(request, "console/import.html", {"report": report, "nav": "mine"})
