from django.urls import path

from records import views

app_name = "records"

urlpatterns = [
    path(".well-known/bananahat-keys.json", views.keys, name="keys"),
    path("records/verify/", views.verify, name="verify"),
    path("records/<str:public_id>/", views.detail, name="detail"),
    path("records/<str:public_id>/certificate/", views.certificate, name="certificate"),
    path("certificates/<slug:slug>/", views.my_certificate, name="my-certificate"),
]
