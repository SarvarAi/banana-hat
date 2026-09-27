"""REST API, version 1.

Every view names its permission classes explicitly; the project default is DenyAll and
tests/test_access_matrix.py fails if any view relies on it. Object-level decisions
(may this user read this judge's scores?) are made by `events.access`, the same module
the HTML pages use.
"""

import csv
import io
import json

from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import generics, serializers, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from api import serializers as s
from audit.models import AuditEntry
from audit.services import verify_chain
from core.errors import DomainError
from events import access
from events import services as event_services
from events.models import Event, EventRole, Project
from integrations import interchange, webhooks
from integrations.models import WebhookEndpoint
from judging import assignment as assignment_service
from judging import pairwise, scoring
from judging import results as results_service
from judging.models import Assignment, Score
from records import services as records
from records.models import SignedRecord
from voting import services as voting


def _event(event_id, user) -> Event:
    event = Event.objects.filter(public_id=event_id).first()
    if event is None or not access.can_view_event(user, event):
        raise NotFound("No such event.")
    return event


def _organized_event(event_id, user) -> Event:
    event = _event(event_id, user)
    if not access.is_organizer(user, event):
        raise PermissionDenied("Only this event's organizers can do that.")
    return event


def _scores_qs():
    return Score.objects.select_related(
        "assignment__event", "assignment__judge", "assignment__project"
    ).prefetch_related("values__criterion")


# --- events ------------------------------------------------------------------------

class EventListView(generics.ListCreateAPIView):
    """Events you can see. POST creates one (requires permission to create events)."""

    permission_classes = [AllowAny]
    serializer_class = s.EventSerializer

    def get_queryset(self):
        user = self.request.user
        if access.is_admin(user):
            return Event.objects.all()
        if user.is_authenticated:
            return Event.objects.filter(Q(is_listed=True) | Q(roles__user=user)).distinct()
        return Event.objects.listed()

    def perform_create(self, serializer):
        user = self.request.user
        if not (user.is_authenticated and user.may_create_events):
            raise PermissionDenied("Your account cannot create events.")
        from events.console import create_event

        serializer.instance = create_event(user, serializer.validated_data)


class EventDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = [AllowAny]
    serializer_class = s.EventDetailSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_object(self):
        event = _event(self.kwargs["event_id"], self.request.user)
        if self.request.method == "PATCH" and not access.is_organizer(self.request.user, event):
            raise PermissionDenied("Only this event's organizers can change it.")
        return event

    def perform_update(self, serializer):
        from events.console import update_event

        update_event(serializer.instance, self.request.user, serializer.validated_data)


class CriteriaView(APIView):
    """Read the rubric, or replace it (organizers, before any score exists)."""

    permission_classes = [AllowAny]

    @extend_schema(responses=s.CriterionSerializer(many=True))
    def get(self, request, event_id):
        event = _event(event_id, request.user)
        return Response(s.CriterionSerializer(event.criteria.all(), many=True).data)

    @extend_schema(request=s.CriterionSerializer(many=True), responses=s.CriterionSerializer(many=True))
    def put(self, request, event_id):
        event = _organized_event(event_id, request.user)
        serializer = s.CriterionSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)
        from judging.rubric import replace_rubric

        replace_rubric(event, request.user, serializer.validated_data)
        return Response(s.CriterionSerializer(event.criteria.all(), many=True).data)


# --- projects ----------------------------------------------------------------------

class ProjectListView(generics.ListAPIView):
    """The public gallery. Filter with ?event=evt_01&track=trk_01&q=text."""

    permission_classes = [AllowAny]
    serializer_class = s.ProjectSerializer

    @extend_schema(parameters=[
        OpenApiParameter("event", str), OpenApiParameter("track", str), OpenApiParameter("q", str),
    ])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        qs = Project.objects.public().select_related("event", "team", "track").order_by("title", "public_id")
        params = self.request.query_params
        if params.get("event"):
            qs = qs.filter(event__public_id=params["event"])
        if params.get("track"):
            qs = qs.filter(track__public_id=params["track"])
        if params.get("q"):
            q = params["q"]
            qs = qs.filter(Q(title__icontains=q) | Q(summary__icontains=q) | Q(team__name__icontains=q))
        return qs


def _project_for(user, project_id) -> Project:
    project = Project.objects.select_related("event", "team", "track").filter(public_id=project_id).first()
    if project is None or not access.can_view_event(user, project.event):
        raise NotFound("No such project.")
    visible = project.status == Project.Status.SUBMITTED or access.is_organizer(user, project.event) \
        or access.team_of(user, project.event) == project.team
    if not visible:
        raise NotFound("No such project.")
    return project


def _project_input(event, data):
    track = None
    if data.get("track"):
        track = event.tracks.filter(public_id=data["track"]).first()
        if track is None:
            raise DomainError("Unknown track for this event.", code="invalid_track")
    fields = {k: data[k] for k in ("title", "summary", "description", "repo_url", "demo_url", "video_url")
              if k in data}
    return event_services.ProjectInput(track=track, **fields)


class ProjectDetailView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses=s.ProjectSerializer)
    def get(self, request, project_id):
        project = _project_for(request.user, project_id)
        return Response(s.ProjectSerializer(project, context={"request": request}).data)

    @extend_schema(request=s.ProjectWriteSerializer, responses=s.ProjectSerializer)
    def patch(self, request, project_id):
        if not request.user.is_authenticated:
            raise PermissionDenied("Sign in to edit a project.")
        project = _project_for(request.user, project_id)
        event_services.ensure_team_can_edit(project.event, project.team)
        serializer = s.ProjectWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        event_services.update_project(project, request.user, _project_input(project.event, serializer.validated_data))
        if serializer.validated_data.get("submit") and project.status != Project.Status.SUBMITTED:
            event_services.submit_project(project, request.user)
        project.refresh_from_db()
        return Response(s.ProjectSerializer(project, context={"request": request}).data)


class EventProjectsView(APIView):
    """GET: the event's gallery. POST: create your team's project in this event.

    POST is refused with 403 `submissions_closed` once the deadline has passed. The
    deadline is checked before anything else about the request."""

    permission_classes = [AllowAny]

    @extend_schema(responses=s.ProjectSerializer(many=True))
    def get(self, request, event_id):
        event = _event(event_id, request.user)
        qs = Project.objects.canonical().filter(event=event).select_related("event", "team", "track")
        return Response(s.ProjectSerializer(qs, many=True, context={"request": request}).data)

    @extend_schema(request=s.ProjectWriteSerializer, responses={201: s.ProjectSerializer})
    def post(self, request, event_id):
        if not request.user.is_authenticated:
            raise PermissionDenied("Sign in to submit a project.")
        event = _event(event_id, request.user)
        # The deadline first: a late request is refused as late, whatever else is wrong with it.
        event_services.ensure_team_can_edit(event, access.team_of(request.user, event))
        serializer = s.ProjectWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        project = event_services.create_project(event, request.user, _project_input(event, data),
                                                submit=data.get("submit", False))
        return Response(s.ProjectSerializer(project, context={"request": request}).data,
                        status=status.HTTP_201_CREATED)


class ProjectSubmitView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses=s.ProjectSerializer)
    def post(self, request, project_id):
        project = _project_for(request.user, project_id)
        event_services.submit_project(project, request.user)
        return Response(s.ProjectSerializer(project, context={"request": request}).data)


# --- teams -----------------------------------------------------------------------

class EventTeamsView(APIView):
    """POST creates a team in the event with you as captain."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=inline_serializer("TeamCreate", {"name": serializers.CharField()}),
                   responses={201: s.TeamSerializer})
    def post(self, request, event_id):
        event = _event(event_id, request.user)
        team = event_services.create_team(event, request.user, request.data.get("name", ""))
        return Response(s.TeamSerializer(team, context={"request": request}).data, status=201)


class MyTeamView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=s.TeamSerializer)
    def get(self, request, event_id):
        event = _event(event_id, request.user)
        team = access.team_of(request.user, event)
        if team is None:
            raise NotFound("You are not on a team in this event.")
        return Response(s.TeamSerializer(team, context={"request": request}).data)


class JoinTeamView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=inline_serializer("TeamJoin", {"code": serializers.CharField()}),
                   responses=s.TeamSerializer)
    def post(self, request):
        team = event_services.join_team(str(request.data.get("code", "")), request.user)
        return Response(s.TeamSerializer(team, context={"request": request}).data)


# --- judging ---------------------------------------------------------------------

class JudgeScoresView(APIView):
    """Scores written by one judge.

    `me` means you. Any other judge id is readable only by that judge themselves or by
    an organizer of the judge's event; everyone else gets 403. A `?judge=` query
    parameter naming someone else is refused the same way rather than ignored.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[OpenApiParameter("event", str, description="Only this event's scores.")],
        responses=s.ScoreSerializer(many=True),
    )
    def get(self, request, judge_id):
        user = request.user
        requested = request.query_params.get("judge") or judge_id
        if requested == "me":
            roles = EventRole.objects.judges().filter(user=user)
            if not roles.exists():
                raise PermissionDenied("You are not a judge in any event.")
        else:
            role = EventRole.objects.judges().select_related("event").filter(public_id=requested).first()
            allowed = role is not None and (role.user_id == user.pk or access.is_organizer(user, role.event))
            if not allowed:
                # Same answer whether or not the judge exists, so ids cannot be probed.
                raise PermissionDenied("Judges can only read their own scores.")
            roles = EventRole.objects.filter(pk=role.pk)
        scores = _scores_qs().visible_to(user).filter(assignment__judge__in=roles)
        if request.query_params.get("event"):
            scores = scores.filter(assignment__event__public_id=request.query_params["event"])
        return Response(s.ScoreSerializer(scores.order_by("assignment__project__public_id"), many=True).data)


class MyAssignmentsView(generics.ListAPIView):
    """Your review queue across events."""

    permission_classes = [IsAuthenticated]
    serializer_class = s.AssignmentSerializer

    def get_queryset(self):
        return (
            Assignment.objects.filter(judge__user=self.request.user)
            .select_related("event", "judge", "project__team", "project__track", "project__event", "score")
            .order_by("event__public_id", "status", "project__title")
        )


def _my_assignment(user, assignment_id) -> Assignment:
    assignment = Assignment.objects.select_related("event", "judge", "project").filter(
        public_id=assignment_id).first()
    if assignment is None or assignment.judge.user_id != user.pk:
        raise PermissionDenied("That is not one of your assignments.")
    return assignment


class AssignmentScoreView(APIView):
    """PUT your score for an assignment. Replaces any earlier score until judging closes."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=s.ScoreSerializer)
    def get(self, request, assignment_id):
        assignment = _my_assignment(request.user, assignment_id)
        score = _scores_qs().filter(assignment=assignment).first()
        if score is None:
            raise NotFound("Not scored yet.")
        return Response(s.ScoreSerializer(score).data)

    @extend_schema(request=s.ScoreWriteSerializer, responses=s.ScoreSerializer)
    def put(self, request, assignment_id):
        assignment = _my_assignment(request.user, assignment_id)
        serializer = s.ScoreWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        score = scoring.submit_score(assignment, request.user, serializer.validated_data["values"],
                                     serializer.validated_data.get("comment", ""))
        return Response(s.ScoreSerializer(_scores_qs().get(pk=score.pk)).data)


class RecuseView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=s.RecuseSerializer, responses=s.AssignmentSerializer)
    def post(self, request, assignment_id):
        assignment = _my_assignment(request.user, assignment_id)
        serializer = s.RecuseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        assignment_service.recuse(assignment, actor=request.user, reason=serializer.validated_data["reason"])
        assignment.refresh_from_db()
        return Response(s.AssignmentSerializer(assignment, context={"request": request}).data)


class PairwiseNextView(APIView):
    """Pairwise mode: the next pair of projects for you to compare."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=inline_serializer("PairwisePair", {
        "a": s.ProjectSerializer(), "b": s.ProjectSerializer()}))
    def get(self, request, event_id):
        event = _event(event_id, request.user)
        judge = access.judge_role(request.user, event)
        if judge is None:
            raise PermissionDenied("Only this event's judges can compare projects.")
        pair = pairwise.next_pair(event, judge)
        if pair is None:
            return Response(status=204)
        ctx = {"request": request}
        return Response({"a": s.ProjectSerializer(pair[0], context=ctx).data,
                         "b": s.ProjectSerializer(pair[1], context=ctx).data})

    @extend_schema(request=s.PairwiseWriteSerializer, responses={201: None})
    def post(self, request, event_id):
        event = _event(event_id, request.user)
        judge = access.judge_role(request.user, event)
        if judge is None:
            raise PermissionDenied("Only this event's judges can compare projects.")
        serializer = s.PairwiseWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pairwise.record(event, judge, serializer.validated_data["winner"], serializer.validated_data["loser"])
        return Response(status=201)


# --- organizer -------------------------------------------------------------------

class EventScoresView(APIView):
    """Every score in the event. Organizers only."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=s.ScoreSerializer(many=True))
    def get(self, request, event_id):
        event = _organized_event(event_id, request.user)
        scores = _scores_qs().visible_to(request.user).filter(assignment__event=event)
        return Response(s.ScoreSerializer(scores.order_by("assignment__project__public_id"), many=True).data)


class ProgressView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=inline_serializer("Progress", {
        "reviews_total": serializers.IntegerField(), "reviews_done": serializers.IntegerField(),
        "percent": serializers.IntegerField(), "tracks": serializers.ListField(),
        "judges": serializers.ListField(), "under_covered": serializers.ListField(),
    }))
    def get(self, request, event_id):
        event = _organized_event(event_id, request.user)
        p = results_service.progress(event)
        return Response({
            "reviews_total": p["total"], "reviews_done": p["done"], "recused": p["recused"],
            "percent": p["pct"], "components": p["components"],
            "tracks": [{"track": t["track"].public_id, "name": t["track"].name, "done": t["done"],
                        "total": t["total"]} for t in p["per_track"]],
            "judges": [{"judge": j["role"].public_id, "name": j["role"].user.name, "done": j["done"],
                        "total": j["total"], "bias": j["bias"], "flags": [f[0] for f in j["flags"]]}
                       for j in p["judges"]],
            "under_covered": [{"project": x.public_id, "title": x.title, "reviews": x.n}
                              for x in p["under_covered"]],
        })


class AutoAssignView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=s.AutoAssignSerializer, responses=inline_serializer("AssignmentPlan", {
        "created": serializers.IntegerField(), "shortfalls": serializers.ListField(),
        "seed": serializers.IntegerField()}))
    def post(self, request, event_id):
        event = _organized_event(event_id, request.user)
        serializer = s.AutoAssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        opts = serializer.validated_data
        fn = assignment_service.plan_assignments if opts.get("dry_run") else assignment_service.auto_assign
        kwargs = {"target": opts.get("target"), "max_load": opts.get("max_load")}
        if not opts.get("dry_run"):
            kwargs["actor"] = request.user
        plan = fn(event, **kwargs)
        return Response(plan.summary())


class ResultsView(APIView):
    """Published results. Organizers may preview live results with ?preview=1&method=."""

    permission_classes = [AllowAny]

    @extend_schema(parameters=[OpenApiParameter("preview", bool), OpenApiParameter("method", str)])
    def get(self, request, event_id):
        event = _event(event_id, request.user)
        if request.query_params.get("preview"):
            if not access.is_organizer(request.user, event):
                raise PermissionDenied("Only organizers can preview unpublished results.")
            method = request.query_params.get("method") or event.normalization
            if method not in results_service.METHOD_LABELS:
                raise DomainError(f"Unknown method {method!r}.")
            payload, _ = results_service.snapshot_payload(results_service.compute(event, method))
            return Response({**payload, "preview": True})
        payload = results_service.published_results(event)
        if payload is None:
            raise PermissionDenied("Results for this event have not been published.")
        return Response(payload)


class PublishView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=inline_serializer("Publish", {"method": serializers.CharField(required=False)}),
                   responses={201: None})
    def post(self, request, event_id):
        event = _organized_event(event_id, request.user)
        method = request.data.get("method") or None
        snapshot = results_service.publish(event, request.user, method)
        return Response({"published_at": snapshot.published_at, "record": snapshot.payload.get("record")},
                        status=201)


class AuditView(APIView):
    """The event's audit trail, newest first. ?before=<entry id> pages backwards."""

    permission_classes = [IsAuthenticated]

    def get(self, request, event_id):
        event = _organized_event(event_id, request.user)
        qs = AuditEntry.objects.filter(event=event).order_by("-id")
        if request.query_params.get("before"):
            qs = qs.filter(id__lt=int(request.query_params["before"]))
        if request.query_params.get("action"):
            qs = qs.filter(action__startswith=request.query_params["action"])
        rows = list(qs[:200])
        return Response({
            "entries": [{"id": e.pk, "at": e.at, "actor": e.actor_label, "action": e.action,
                         "summary": e.summary, "target": [e.target_type, e.target_id], "data": e.data,
                         "digest": e.digest} for e in rows],
            "chain": verify_chain().__dict__,
        })


def _csv(rows, header, filename):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    response = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


class ExportView(APIView):
    """CSV and JSON exports. Organizers only.

    - scores.csv    one row per review: judge, project, every criterion, weighted total
    - results.csv   the ranking under the event's normalization method, with raw means
    - projects.csv  every project, including drafts and flagged duplicates
    - event.json    the whole event in DOGFOOD fixture format (re-importable)
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={(200, "text/csv"): str, (200, "application/json"): dict})
    def get(self, request, event_id, kind):
        event = _organized_event(event_id, request.user)
        if kind == "scores.csv":
            criteria = list(event.criteria.all())
            weights = {c.key: float(c.weight) for c in criteria}
            rows = []
            for score in _scores_qs().visible_to(request.user).filter(assignment__event=event) \
                    .order_by("assignment__project__public_id", "assignment__judge__public_id"):
                values = {v.criterion.key: v.value for v in score.values.all()}
                total = sum(weights[k] * v for k, v in values.items()) / sum(weights.values()) \
                    if set(values) == set(weights) else ""
                a = score.assignment
                rows.append([a.project.public_id, a.project.title, a.judge.public_id, a.judge.user.name,
                             *[values.get(c.key, "") for c in criteria],
                             round(total, 4) if total != "" else "", score.comment,
                             "voided" if score.voided_at else ("duplicate" if a.project.duplicate_of_id else "counted"),
                             score.updated_at.isoformat()])
            header = ["project_id", "project_title", "judge_id", "judge_name",
                      *[c.key for c in criteria], "weighted_total", "comment", "status", "updated_at"]
            return _csv(rows, header, f"{event.slug}-scores.csv")
        if kind == "results.csv":
            method = request.query_params.get("method") or event.normalization
            results = results_service.compute(event, method)
            rows = [[r.rank, r.project.public_id, r.project.title, r.project.team.name,
                     r.project.track.name if r.project.track else "", round(r.score, 4),
                     round(r.raw_mean, 4) if r.raw_mean is not None else "", r.n,
                     round(r.ci[0], 4) if r.ci else "", round(r.ci[1], 4) if r.ci else "", r.track_rank]
                    for r in results.rows]
            header = ["rank", "project_id", "title", "team", "track", f"score_{method}", "raw_mean",
                      "reviews", "ci95_low", "ci95_high", "rank_in_track"]
            return _csv(rows, header, f"{event.slug}-results-{method}.csv")
        if kind == "projects.csv":
            rows = [[p.public_id, p.title, p.team.public_id, p.team.name, p.track.name if p.track else "",
                     p.status, p.submitted_at.isoformat() if p.submitted_at else "", p.repo_url,
                     p.duplicate_of.public_id if p.duplicate_of else ""]
                    for p in event.projects.select_related("team", "track", "duplicate_of").order_by("public_id")]
            header = ["project_id", "title", "team_id", "team", "track", "status", "submitted_at",
                      "repo_url", "duplicate_of"]
            return _csv(rows, header, f"{event.slug}-projects.csv")
        if kind == "event.json":
            response = HttpResponse(json.dumps(interchange.export_event(event), indent=2),
                                    content_type="application/json")
            response["Content-Disposition"] = f'attachment; filename="{event.slug}.json"'
            return response
        raise NotFound("Unknown export.")


class ImportView(APIView):
    """Create an event from a DOGFOOD fixture-format document."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=dict, responses={201: dict})
    def post(self, request):
        if not request.user.may_create_events:
            raise PermissionDenied("Your account cannot create events.")
        report = interchange.import_event(request.data, actor=request.user)
        event_services.grant_role(report.event, request.user, EventRole.Role.ORGANIZER, actor=request.user)
        return Response(report.as_dict(), status=201)


class WebhookListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=s.WebhookSerializer(many=True))
    def get(self, request, event_id):
        event = _organized_event(event_id, request.user)
        return Response(s.WebhookSerializer(event.webhooks.all(), many=True).data)

    @extend_schema(request=s.WebhookSerializer, responses={201: s.WebhookSerializer})
    def post(self, request, event_id):
        event = _organized_event(event_id, request.user)
        serializer = s.WebhookSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from integrations.console import create_webhook

        endpoint = create_webhook(event, request.user, **serializer.validated_data)
        return Response(s.WebhookSerializer(endpoint).data, status=201)


def _organized_webhook(user, webhook_id) -> WebhookEndpoint:
    endpoint = WebhookEndpoint.objects.select_related("event").filter(public_id=webhook_id).first()
    if endpoint is None or not access.is_organizer(user, endpoint.event):
        raise NotFound("No such webhook.")
    return endpoint


class WebhookDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={204: None})
    def delete(self, request, webhook_id):
        from integrations.console import delete_webhook

        delete_webhook(_organized_webhook(request.user, webhook_id), request.user)
        return Response(status=204)


class WebhookDeliveriesView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=s.WebhookDeliverySerializer(many=True))
    def get(self, request, webhook_id):
        endpoint = _organized_webhook(request.user, webhook_id)
        return Response(s.WebhookDeliverySerializer(endpoint.deliveries.all()[:100], many=True).data)

    @extend_schema(request=None, responses=s.WebhookDeliverySerializer)
    def post(self, request, webhook_id):
        """Send a test ping now."""
        delivery = webhooks.ping(_organized_webhook(request.user, webhook_id))
        return Response(s.WebhookDeliverySerializer(delivery).data)


class EventRolesView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=s.RoleSerializer(many=True))
    def get(self, request, event_id):
        event = _organized_event(event_id, request.user)
        roles = event.roles.select_related("user").prefetch_related("tracks").order_by("role", "user__name")
        return Response(s.RoleSerializer(roles, many=True).data)


# --- community -------------------------------------------------------------------

class CommentsView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses=s.CommentSerializer(many=True))
    def get(self, request, project_id):
        project = _project_for(request.user, project_id)
        return Response(s.CommentSerializer(voting.visible_comments(project, request.user), many=True).data)

    @extend_schema(request=inline_serializer("CommentCreate", {"body": serializers.CharField()}),
                   responses={201: s.CommentSerializer})
    def post(self, request, project_id):
        if not request.user.is_authenticated:
            raise PermissionDenied("Sign in to comment.")
        project = _project_for(request.user, project_id)
        comment = voting.add_comment(project, request.user, request.data.get("body", ""),
                                     ip=voting.request_meta(request)["ip"])
        return Response(s.CommentSerializer(comment).data, status=201)


class BallotView(APIView):
    """Signed-in voting. GET shows your ballot in its own random order."""

    permission_classes = [IsAuthenticated]

    def get(self, request, event_id):
        event = _event(event_id, request.user)
        ballot = voting.ballot_for_user(event, request.user, voting.request_meta(request))
        chosen = set(ballot.choices.values_list("project__public_id", flat=True))
        return Response({
            "ballot": ballot.public_id,
            "votes_allowed": event.votes_per_voter,
            "status": ballot.status,
            "projects": [{"id": p.public_id, "title": p.title, "team": p.team.name, "chosen": p.public_id in chosen}
                         for p in voting.ballot_projects(event, ballot)],
        })

    @extend_schema(request=s.BallotWriteSerializer)
    def post(self, request, event_id):
        event = _event(event_id, request.user)
        serializer = s.BallotWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ballot = voting.ballot_for_user(event, request.user, voting.request_meta(request))
        voting.cast(ballot, serializer.validated_data["projects"], voter=request.user)
        return Response({"ballot": ballot.public_id, "status": ballot.status}, status=201)


class VotesView(APIView):
    """Community vote totals. 403 `results_hidden` for everyone until voting closes."""

    permission_classes = [AllowAny]

    def get(self, request, event_id):
        event = _event(event_id, request.user)
        return Response({"turnout": voting.turnout(event), "tally": voting.tally(event)})


# --- records ---------------------------------------------------------------------

class JudgeRecordView(APIView):
    """A judge's signed participation record.

    GET: the latest record, for the judge themselves or an organizer of their event.
    POST: issue a fresh one now (organizers). Records are also issued automatically to
    every judge when results are published.
    """

    permission_classes = [IsAuthenticated]

    def _role(self, request, judge_id, *, organizer_only):
        role = EventRole.objects.judges().select_related("event", "user").filter(public_id=judge_id).first()
        if role is None:
            raise PermissionDenied("No such judge, or not yours to see.")
        is_org = access.is_organizer(request.user, role.event)
        if not is_org and (organizer_only or role.user_id != request.user.pk):
            raise PermissionDenied("No such judge, or not yours to see.")
        return role

    def get(self, request, judge_id):
        role = self._role(request, judge_id, organizer_only=False)
        record = SignedRecord.objects.filter(event=role.event, kind=SignedRecord.Kind.JUDGE,
                                             subject=role.user).select_related("key").first()
        if record is None:
            raise NotFound("No record issued yet.")
        return Response(records.export(record))

    def post(self, request, judge_id):
        role = self._role(request, judge_id, organizer_only=True)
        record = records.issue_judge_record(role.event, role, actor=request.user)
        return Response({**records.export(record), "record": record.public_id}, status=201)


class RecordView(APIView):
    """A signed record, in the portable form tools/verify_record.py checks."""

    permission_classes = [AllowAny]

    def get(self, request, record_id):
        record = get_object_or_404(SignedRecord.objects.select_related("key"), public_id=record_id)
        return Response({**records.export(record), "valid": records.verify(record),
                         "revoked": record.revoked_at is not None})


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=s.UserSerializer)
    def get(self, request):
        data = s.UserSerializer(request.user).data
        data["roles"] = [{"event": r.event.public_id, "role": r.role, "id": r.public_id}
                         for r in request.user.event_roles.select_related("event")]
        return Response(data)
