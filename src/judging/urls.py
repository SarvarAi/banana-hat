from django.urls import path

from judging import views

app_name = "judging"

urlpatterns = [
    path("judge/", views.home, name="home"),
    path("judge/<slug:slug>/", views.event_queue, name="event"),
    path("judge/<slug:slug>/review/<str:assignment_id>/", views.review, name="review"),
    path("judge/<slug:slug>/review/<str:assignment_id>/recuse/", views.recuse, name="recuse"),
    path("judge/<slug:slug>/pairwise/", views.pairwise_view, name="pairwise"),
    path("invites/<str:token>/", views.accept_invite, name="accept-invite"),
]
