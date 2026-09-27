from django.urls import path

from voting import views

app_name = "voting"

urlpatterns = [
    path("vote/<slug:slug>/", views.ballot, name="ballot"),
    path("vote/<slug:slug>/verify/<str:token>/", views.verify, name="verify"),
    path("vote/<slug:slug>/link/<str:token>/", views.link, name="link"),
    path("projects/<str:public_id>/comments/", views.comment, name="comment"),
    path("comments/<str:public_id>/moderate/", views.moderate_comment, name="moderate-comment"),
]
