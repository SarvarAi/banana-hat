from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth import views as auth_views
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts import demo
from accounts.forms import ClaimingPasswordResetForm, LoginForm, ProfileForm, SignupForm, TokenForm
from accounts.models import ApiToken, User
from audit import services as audit
from core import clock, ratelimit
from core.access import authenticated, public
from core.errors import DomainError


class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        # Two buckets: one per address, one per account, so neither a single machine
        # nor a botnet aimed at one account gets unlimited guesses.
        email = User.objects.normalize_email(request.POST.get("username", ""))
        try:
            ratelimit.enforce(ratelimit.LOGIN, "ip:" + ratelimit.client_ip(request))
            ratelimit.enforce(ratelimit.LOGIN, "email:" + email)
        except DomainError as exc:
            form = self.get_form()
            form.add_error(None, exc.message)
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if settings.DEMO_MODE:
            context["demo_accounts"] = demo.accounts()
        return context


login_view = public(LoginView.as_view())
logout_view = public(auth_views.LogoutView.as_view())


class PasswordResetView(auth_views.PasswordResetView):
    """Django's reset flow, rate limited. It answers the same way whether or not the
    address has an account, so it cannot be used to discover who is registered."""

    template_name = "accounts/password_reset.html"
    form_class = ClaimingPasswordResetForm
    email_template_name = "accounts/password_reset_email.txt"
    subject_template_name = "accounts/password_reset_subject.txt"
    success_url = "/accounts/password-reset/sent/"

    def post(self, request, *args, **kwargs):
        try:
            ratelimit.enforce(ratelimit.PASSWORD_RESET, "ip:" + ratelimit.client_ip(request))
        except DomainError as exc:
            form = self.get_form()
            form.add_error(None, exc.message)
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *args, **kwargs)


password_reset = public(PasswordResetView.as_view())
password_reset_sent = public(auth_views.PasswordResetDoneView.as_view(
    template_name="accounts/password_reset_sent.html"))
password_reset_confirm = public(auth_views.PasswordResetConfirmView.as_view(
    template_name="accounts/password_reset_confirm.html", success_url="/accounts/password-reset/complete/"))
password_reset_complete = public(auth_views.PasswordResetCompleteView.as_view(
    template_name="accounts/password_reset_complete.html"))


@public
def signup(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = SignupForm(request.POST or None)
    if request.method == "POST":
        try:
            ratelimit.enforce(ratelimit.SIGNUP, "ip:" + ratelimit.client_ip(request))
        except DomainError as exc:
            form.add_error(None, exc.message)
        else:
            if form.is_valid():
                user = form.save()
                audit.record("account.created", f"{user.display_name} created an account",
                             actor=user, target=user)
                login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                messages.success(request, "Welcome aboard. Your account is ready.")
                return redirect(_safe_next(request) or "home")
    return render(request, "accounts/signup.html", {"form": form})


@public
@require_POST
def demo_login(request):
    """One-click sign-in as a seeded account. Exists only in demo mode."""
    if not settings.DEMO_MODE:
        raise Http404
    email = request.POST.get("email", "")
    if email not in {a["email"] for a in demo.accounts()}:
        raise Http404
    user = get_object_or_404(User, email=email, is_active=True)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    messages.info(request, f"Signed in as {user.display_name} (demo account).")
    return redirect(_safe_next(request) or demo.landing_url(email))


@authenticated
def account(request):
    profile_form = ProfileForm(request.POST or None, instance=request.user, prefix="profile")
    token_form = TokenForm(prefix="token")
    if request.method == "POST" and "profile-name" in request.POST and profile_form.is_valid():
        profile_form.save()
        messages.success(request, "Profile saved.")
        return redirect("accounts:account")
    roles = (
        request.user.event_roles.select_related("event").order_by("-event__submissions_close_at")
    )
    return render(request, "accounts/account.html", {
        "profile_form": profile_form,
        "token_form": token_form,
        "tokens": request.user.api_tokens.all(),
        "roles": roles,
        "new_token": request.session.pop("new_token", None),
    })


@authenticated
@require_POST
def create_token(request):
    form = TokenForm(request.POST, prefix="token")
    if not form.is_valid():
        messages.error(request, "Give the token a name.")
        return redirect("accounts:account")
    days = form.cleaned_data["expires_in_days"]
    expires_at = clock.now() + timedelta(days=days) if days else None
    token, raw = ApiToken.issue(request.user, form.cleaned_data["name"], expires_at=expires_at)
    audit.record("token.created", f"{request.user.display_name} created API token “{token.name}”",
                 actor=request.user, target=token)
    # Shown exactly once, on the next page load, then gone.
    request.session["new_token"] = raw
    return redirect("accounts:account")


@authenticated
@require_POST
def revoke_token(request, token_id):
    token = get_object_or_404(ApiToken, pk=token_id, user=request.user)
    if token.revoked_at is None:
        token.revoked_at = clock.now()
        token.save(update_fields=["revoked_at"])
        audit.record("token.revoked", f"{request.user.display_name} revoked API token “{token.name}”",
                     actor=request.user, target=token)
        messages.success(request, f"Token “{token.name}” revoked. It stops working immediately.")
    return redirect("accounts:account")


def _safe_next(request):
    target = request.POST.get("next") or request.GET.get("next")
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return None
