from django.urls import path

from events import views

app_name = "events"

urlpatterns = [
    path("events/", views.event_list, name="list"),
    path("events/<slug:slug>/", views.event_detail, name="detail"),
    path("events/<slug:slug>/projects/", views.gallery, name="event-gallery"),
    path("events/<slug:slug>/team/", views.team, name="team"),
    path("events/<slug:slug>/team/leave/", views.team_leave, name="team-leave"),
    path("events/<slug:slug>/team/reset-invite/", views.team_rotate_invite, name="team-rotate"),
    path("events/<slug:slug>/submission/", views.submission, name="submission"),
    path("events/<slug:slug>/submission/<str:action>/", views.submission_action, name="submission-action"),
    path("projects/", views.gallery, name="gallery"),
    path("projects/new/", views.new_project, name="new-project"),
    path("projects/<str:public_id>/", views.project_detail, name="project"),
    path("join/<str:code>/", views.join, name="join"),
    path("me/events/", views.my_events, name="mine"),
    path("embed/<slug:slug>/", views.embed, name="embed"),
]
