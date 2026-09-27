from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import clock
from core.access import authenticated, embeddable, public
from core.errors import DomainError
from events import access, services
from events.forms import GalleryFilterForm, ProjectForm, TeamForm
from events.models import Event, EventRole, Project, Team

GALLERY_PAGE_SIZE = 48


def _visible_events(user):
    if access.is_admin(user):
        return Event.objects.all()
    if user.is_authenticated:
        return Event.objects.filter(Q(is_listed=True) | Q(roles__user=user)).distinct()
    return Event.objects.listed()


@public
def home(request):
    events = list(Event.objects.listed().annotate(
        project_count=Count("projects", filter=Q(projects__status="submitted",
                                                  projects__duplicate_of__isnull=True))
    )[:6])
    recent = Project.objects.public().select_related("team", "track", "event").order_by("-submitted_at")[:6]
    return render(request, "events/home.html", {"events": events, "recent": recent})


@public
def event_list(request):
    events = _visible_events(request.user).annotate(
        project_count=Count("projects", filter=Q(projects__status="submitted",
                                                  projects__duplicate_of__isnull=True), distinct=True),
        track_count=Count("tracks", distinct=True),
    )
    return render(request, "events/event_list.html", {"events": events, "nav": "events"})


def _event_or_404(request, slug) -> Event:
    event = get_object_or_404(Event, slug=slug)
    if not access.can_view_event(request.user, event):
        raise Http404
    return event


def timeline(event):
    now = clock.now()
    steps = [
        ("Submissions open", event.submissions_open_at),
        ("Submission deadline", event.submissions_close_at),
    ]
    if event.voting_enabled() and event.voting_open_at:
        steps.append(("Community voting opens", event.voting_open_at))
        steps.append(("Community voting closes", event.voting_close_at))
    if event.judging_close_at:
        steps.append(("Judging closes", event.judging_close_at))
    if event.results_published_at:
        steps.append(("Results published", event.results_published_at))
    steps.sort(key=lambda s: s[1])
    out, current_marked = [], False
    for label, at in steps:
        state = "past" if at <= now else "future"
        if state == "future" and not current_marked:
            state, current_marked = "now", True
        out.append({"label": label, "at": at, "state": state})
    return out


@public
def event_detail(request, slug):
    event = _event_or_404(request, slug)
    user = request.user
    team = access.team_of(user, event)
    project = None
    if team:
        project = team.projects.filter(duplicate_of__isnull=True).exclude(status="withdrawn").first()
    stats = {
        "projects": event.projects.canonical().count(),
        "teams": event.teams.count(),
        "judges": event.roles.judges().count(),
        "tracks": event.tracks.count(),
    }
    from judging.results import published_results

    return render(request, "events/event_detail.html", {
        "event": event,
        "timeline": timeline(event),
        "tracks": event.tracks.annotate(n=Count("projects", filter=Q(
            projects__status="submitted", projects__duplicate_of__isnull=True))),
        "prizes": event.prizes.select_related("track", "winner"),
        "stats": stats,
        "team": team,
        "project": project,
        "is_organizer": access.is_organizer(user, event),
        "is_judge": access.is_judge(user, event),
        "deadline": services.deadline_for(event, team),
        "results": published_results(event),
        "nav": "events",
    })


@public
def gallery(request, slug=None):
    events = _visible_events(request.user) if slug is None else None
    event = None
    data = request.GET.copy()
    if slug is not None:
        event = _event_or_404(request, slug)
        data["event"] = event.slug
        events = Event.objects.filter(pk=event.pk)
    form = GalleryFilterForm(data, events=events)
    projects = Project.objects.canonical().filter(event__in=form.fields["event"].queryset)
    projects = projects.select_related("team", "track", "event")
    if form.is_valid():
        q = form.cleaned_data["q"].strip()
        if q:
            projects = projects.filter(
                Q(title__icontains=q) | Q(summary__icontains=q) | Q(team__name__icontains=q)
            )
        if form.cleaned_data["event"]:
            projects = projects.filter(event=form.cleaned_data["event"])
        if form.cleaned_data["track"]:
            projects = projects.filter(track=form.cleaned_data["track"])
        sort = form.cleaned_data["sort"] or "title"
    else:
        sort = "title"
    ordering = {"title": ["title"], "newest": ["-submitted_at"], "track": ["track__position", "title"]}
    projects = projects.order_by(*ordering.get(sort, ["title"]), "public_id")
    page = Paginator(projects, GALLERY_PAGE_SIZE).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "events/gallery.html", {
        "form": form, "page": page, "event": event, "query": query.urlencode(),
        "total": page.paginator.count, "nav": "gallery",
    })


@public
def project_detail(request, public_id):
    project = get_object_or_404(
        Project.objects.select_related("event", "team", "track", "duplicate_of"), public_id=public_id
    )
    event = project.event
    user = request.user
    on_team = access.team_of(user, event) == project.team
    if not access.can_view_event(user, event):
        raise Http404
    if project.status != Project.Status.SUBMITTED and not (on_team or access.is_organizer(user, event)):
        raise Http404
    from voting import services as voting

    return render(request, "events/project_detail.html", {
        "project": project,
        "event": event,
        "members": project.team.memberships.select_related("user"),
        "on_team": on_team,
        "is_organizer": access.is_organizer(user, event),
        "comments": voting.visible_comments(project, user),
        "can_comment": voting.can_comment(project, user),
        "nav": "gallery",
    })


@authenticated
def my_events(request):
    roles = (
        EventRole.objects.filter(user=request.user)
        .select_related("event").order_by("-event__submissions_close_at")
    )
    grouped = {"organizer": [], "judge": [], "participant": []}
    for role in roles:
        grouped[role.role].append(role.event)
    creatable = request.user.may_create_events
    return render(request, "events/my_events.html", {
        "grouped": grouped, "creatable": creatable, "nav": "mine",
    })


@authenticated
def team(request, slug):
    event = _event_or_404(request, slug)
    user = request.user
    my_team = access.team_of(user, event)
    form = TeamForm(request.POST or None)
    if request.method == "POST" and my_team is None and form.is_valid():
        try:
            services.create_team(event, user, form.cleaned_data["name"])
        except DomainError as exc:
            form.add_error("name", exc.message)
        else:
            messages.success(request, "Team created. Share the invite link with your teammates.")
            return redirect("events:team", slug=event.slug)
    invite_url = None
    if my_team:
        invite_url = request.build_absolute_uri(
            f"/join/{my_team.invite_code}/"
        )
    return render(request, "events/team.html", {
        "event": event,
        "team": my_team,
        "members": my_team.memberships.select_related("user") if my_team else [],
        "is_captain": bool(my_team and my_team.memberships.filter(user=user, is_captain=True).exists()),
        "invite_url": invite_url,
        "form": form,
        "can_edit": event.submissions_open() or (my_team and clock.now() < services.deadline_for(event, my_team)),
        "deadline": services.deadline_for(event, my_team),
    })


@authenticated
@require_POST
def team_leave(request, slug):
    event = _event_or_404(request, slug)
    my_team = access.team_of(request.user, event)
    if my_team is None:
        raise Http404
    try:
        services.leave_team(my_team, request.user)
    except DomainError as exc:
        messages.error(request, exc.message)
        return redirect("events:team", slug=slug)
    messages.success(request, f"You left {my_team.name}.")
    return redirect("events:detail", slug=slug)


@authenticated
@require_POST
def team_rotate_invite(request, slug):
    event = _event_or_404(request, slug)
    my_team = access.team_of(request.user, event)
    if my_team is None:
        raise Http404
    try:
        services.rotate_invite(my_team, request.user)
    except DomainError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "New invite link created. The old one no longer works.")
    return redirect("events:team", slug=slug)


@public
def join(request, code):
    team_obj = get_object_or_404(Team.objects.select_related("event"), invite_code=code)
    event = team_obj.event
    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect(f"/accounts/login/?next=/join/{code}/")
        try:
            services.join_team(code, request.user)
        except DomainError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, f"You joined {team_obj.name}.")
            return redirect("events:team", slug=event.slug)
    return render(request, "events/join.html", {
        "team": team_obj, "event": event,
        "members": team_obj.memberships.select_related("user"),
        "full": team_obj.memberships.count() >= event.max_team_size,
    })


def _project_input(form):
    data = form.cleaned_data
    return services.ProjectInput(
        title=data.get("title"), summary=data.get("summary", ""), description=data.get("description", ""),
        repo_url=data.get("repo_url", ""), demo_url=data.get("demo_url", ""),
        video_url=data.get("video_url", ""), track=data.get("track"),
    )


@authenticated
def submission(request, slug):
    """Create or edit the team's project. Saving keeps it a draft; "Submit" publishes it."""
    event = _event_or_404(request, slug)
    user = request.user
    my_team = access.team_of(user, event)
    if my_team is None:
        messages.info(request, "Create or join a team first.")
        return redirect("events:team", slug=slug)
    project = my_team.projects.filter(duplicate_of__isnull=True).exclude(status="withdrawn").first()
    deadline = services.deadline_for(event, my_team)
    editable = event.submissions_open_at <= clock.now() < deadline
    form = ProjectForm(request.POST or None, instance=project, event=event)
    if request.method == "POST":
        if not editable:
            messages.error(request, "The deadline has passed. This project can no longer be changed.")
            return redirect("events:submission", slug=slug)
        if form.is_valid():
            submit = "submit" in request.POST
            try:
                data = _project_input(form)
                if project is None:
                    project = services.create_project(event, user, data, submit=submit)
                else:
                    services.update_project(project, user, data)
                    if submit and project.status != Project.Status.SUBMITTED:
                        services.submit_project(project, user)
            except DomainError as exc:
                form.add_error(None, exc.message)
            else:
                if submit:
                    messages.success(request, "Submitted. You can keep editing until the deadline.")
                else:
                    messages.success(request, "Saved.")
                return redirect("events:submission", slug=slug)
    return render(request, "events/submission.html", {
        "event": event, "team": my_team, "project": project, "form": form,
        "editable": editable, "deadline": deadline,
        "extended": deadline != event.submissions_close_at,
    })


@authenticated
@require_POST
def submission_action(request, slug, action):
    event = _event_or_404(request, slug)
    my_team = access.team_of(request.user, event)
    project = my_team and my_team.projects.filter(duplicate_of__isnull=True).exclude(status="withdrawn").first()
    if not project:
        raise Http404
    handlers = {
        "submit": (services.submit_project, "Submitted."),
        "unsubmit": (services.unsubmit_project, "Moved back to draft. It is hidden from the gallery."),
        "withdraw": (services.withdraw_project, "Project withdrawn."),
    }
    if action not in handlers:
        raise Http404
    handler, done = handlers[action]
    try:
        handler(project, request.user)
    except DomainError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, done)
    return redirect("events:submission", slug=slug)


@public
@embeddable
def embed(request, slug):
    """The gallery widget: a compact, frameable page. Embed it with static/js/embed.js,
    or with a plain <iframe src="/embed/<slug>/">. Listed events only."""
    event = get_object_or_404(Event, slug=slug, is_listed=True)
    projects = Project.objects.canonical().filter(event=event).select_related("team", "track")
    track = request.GET.get("track")
    if track:
        projects = projects.filter(track__public_id=track)
    try:
        limit = min(max(int(request.GET.get("limit", 12)), 1), 48)
    except ValueError:
        limit = 12
    from judging.results import published_results

    results = published_results(event)
    ranked = {}
    if results and request.GET.get("results") != "0":
        ranked = {r["project"]: r["rank"] for r in results["overall"]}
        projects = sorted(projects, key=lambda p: ranked.get(p.public_id, 10_000))
    else:
        projects = projects.order_by("title")
    projects = list(projects)[:limit]
    for p in projects:
        p.rank = ranked.get(p.public_id)
    return render(request, "events/embed.html", {
        "event": event, "projects": projects,
        "theme": "dark" if request.GET.get("theme") == "dark" else "light",
    })


@authenticated
def new_project(request):
    """The generic "submit a project" entry point: pick one of your open events."""
    now = clock.now()
    events = _visible_events(request.user).filter(
        submissions_open_at__lte=now, submissions_close_at__gt=now
    )
    if events.count() == 1:
        return redirect("events:submission", slug=events.first().slug)
    return render(request, "events/new_project.html", {"events": events})
