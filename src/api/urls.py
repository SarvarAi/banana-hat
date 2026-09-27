from django.urls import path, re_path

from api import views

app_name = "api"

urlpatterns = [
    path("me", views.MeView.as_view(), name="me"),
    path("events", views.EventListView.as_view(), name="events"),
    path("events/import", views.ImportView.as_view(), name="event-import"),
    path("events/<str:event_id>", views.EventDetailView.as_view(), name="event"),
    path("events/<str:event_id>/criteria", views.CriteriaView.as_view(), name="event-criteria"),
    path("events/<str:event_id>/projects", views.EventProjectsView.as_view(), name="event-projects"),
    path("events/<str:event_id>/teams", views.EventTeamsView.as_view(), name="event-teams"),
    path("events/<str:event_id>/my-team", views.MyTeamView.as_view(), name="event-my-team"),
    path("events/<str:event_id>/roles", views.EventRolesView.as_view(), name="event-roles"),
    path("events/<str:event_id>/scores", views.EventScoresView.as_view(), name="event-scores"),
    path("events/<str:event_id>/progress", views.ProgressView.as_view(), name="event-progress"),
    path("events/<str:event_id>/assignments/auto", views.AutoAssignView.as_view(), name="event-auto-assign"),
    path("events/<str:event_id>/results", views.ResultsView.as_view(), name="event-results"),
    path("events/<str:event_id>/publish", views.PublishView.as_view(), name="event-publish"),
    path("events/<str:event_id>/audit", views.AuditView.as_view(), name="event-audit"),
    path("events/<str:event_id>/webhooks", views.WebhookListView.as_view(), name="event-webhooks"),
    path("events/<str:event_id>/ballot", views.BallotView.as_view(), name="event-ballot"),
    path("events/<str:event_id>/votes", views.VotesView.as_view(), name="event-votes"),
    path("events/<str:event_id>/pairwise", views.PairwiseNextView.as_view(), name="event-pairwise"),
    re_path(r"^events/(?P<event_id>[^/]+)/exports/(?P<kind>scores\.csv|results\.csv|projects\.csv|event\.json)$",
            views.ExportView.as_view(), name="event-export"),
    path("projects", views.ProjectListView.as_view(), name="projects"),
    path("projects/<str:project_id>", views.ProjectDetailView.as_view(), name="project"),
    path("projects/<str:project_id>/submit", views.ProjectSubmitView.as_view(), name="project-submit"),
    path("projects/<str:project_id>/comments", views.CommentsView.as_view(), name="project-comments"),
    path("teams/join", views.JoinTeamView.as_view(), name="team-join"),
    path("judges/<str:judge_id>/scores", views.JudgeScoresView.as_view(), name="judge-scores"),
    path("judges/me/assignments", views.MyAssignmentsView.as_view(), name="my-assignments"),
    path("judges/<str:judge_id>/record", views.JudgeRecordView.as_view(), name="judge-record"),
    path("assignments/<str:assignment_id>/score", views.AssignmentScoreView.as_view(), name="assignment-score"),
    path("assignments/<str:assignment_id>/recuse", views.RecuseView.as_view(), name="assignment-recuse"),
    path("webhooks/<str:webhook_id>", views.WebhookDetailView.as_view(), name="webhook"),
    path("webhooks/<str:webhook_id>/deliveries", views.WebhookDeliveriesView.as_view(), name="webhook-deliveries"),
    path("records/<str:record_id>", views.RecordView.as_view(), name="record"),
]
