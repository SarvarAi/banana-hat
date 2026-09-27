from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("signup/", views.signup, name="signup"),
    path("demo-login/", views.demo_login, name="demo-login"),
    path("password-reset/", views.password_reset, name="password-reset"),
    path("password-reset/sent/", views.password_reset_sent, name="password-reset-sent"),
    path("password-reset/<uidb64>/<token>/", views.password_reset_confirm, name="password-reset-confirm"),
    path("password-reset/complete/", views.password_reset_complete, name="password-reset-complete"),
    path("", views.account, name="account"),
    path("tokens/", views.create_token, name="create-token"),
    path("tokens/<int:token_id>/revoke/", views.revoke_token, name="revoke-token"),
]
