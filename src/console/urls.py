from django.urls import path

from console import views

app_name = "manage"

urlpatterns = [
    path("new/", views.create, name="create"),
    path("import/", views.import_event, name="import"),
    path("<slug:slug>/", views.overview, name="overview"),
    path("<slug:slug>/tracks/", views.tracks, name="tracks"),
    path("<slug:slug>/prizes/", views.prizes, name="prizes"),
    path("<slug:slug>/rubric/", views.rubric, name="rubric"),
    path("<slug:slug>/submissions/", views.submissions, name="submissions"),
    path("<slug:slug>/submissions/<str:public_id>/", views.project_action, name="project-action"),
    path("<slug:slug>/judges/", views.judges, name="judges"),
    path("<slug:slug>/assignments/", views.assignments, name="assignments"),
    path("<slug:slug>/dashboard/", views.dashboard, name="dashboard"),
    path("<slug:slug>/dashboard/live/", views.dashboard_live, name="dashboard-live"),
    path("<slug:slug>/results/", views.results, name="results"),
    path("<slug:slug>/community/", views.voting_view, name="voting"),
    path("<slug:slug>/audit/", views.audit_log, name="audit"),
    path("<slug:slug>/data/", views.data, name="data"),
]
