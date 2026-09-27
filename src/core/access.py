"""Access declarations for HTML views.

Every HTML view states who may reach it with one of the decorators below.
`core.middleware.AccessPolicyMiddleware` refuses any view that does not, so forgetting
to think about access fails closed rather than open. API views get the same guarantee
from DRF's DEFAULT_PERMISSION_CLASSES = DenyAll.

The decorators only answer "may this kind of visitor reach the view at all". Whether a
signed-in user may act on a particular event, project or score is decided inside the
view by the role helpers in `events.access`, because only the view knows which object
is being asked about.
"""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.views.decorators.clickjacking import xframe_options_exempt

PUBLIC = "public"
AUTHENTICATED = "authenticated"
STAFF = "staff"


def _mark(view, policy):
    view.access_policy = policy
    return view


def public(view):
    """Anyone, including visitors who are not signed in."""
    return _mark(view, PUBLIC)


def authenticated(view):
    """Any signed-in user. Object-level checks still happen inside the view."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        return view(request, *args, **kwargs)

    return _mark(wrapper, AUTHENTICATED)


def staff(view):
    """Platform administrators only."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return _mark(wrapper, STAFF)


def embeddable(view):
    """Allow this view to be framed by other sites (the gallery widget).

    Drops X-Frame-Options for the view and, via SecurityHeadersMiddleware, relaxes the
    CSP frame-ancestors directive. Every other page stays unframeable.
    """
    view = xframe_options_exempt(view)
    view.embeddable = True
    return view
