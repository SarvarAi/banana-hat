import logging

from django.conf import settings
from django.core.exceptions import PermissionDenied

log = logging.getLogger(__name__)

# Views owned by frameworks that enforce their own access rules.
_TRUSTED_VIEW_MODULES = ("django.contrib.admin.", "django.contrib.staticfiles.")


class AccessPolicyMiddleware:
    """Refuse any HTML view that has not declared an access policy.

    DRF views are recognised by the `cls` attribute `as_view()` leaves behind; they are
    guarded by DRF permission classes, whose default is DenyAll.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        request.view_embeddable = getattr(view_func, "embeddable", False)
        if getattr(view_func, "access_policy", None):
            return None
        if hasattr(view_func, "cls"):
            return None
        if view_func.__module__.startswith(_TRUSTED_VIEW_MODULES):
            return None
        # Everything the admin site routes is wrapped in its own staff check, including
        # the few views it borrows from other modules (user password change, redirects).
        match = getattr(request, "resolver_match", None)
        if match is not None and "admin" in match.namespaces:
            return None
        log.error("view %s.%s declares no access policy; refusing",
                  view_func.__module__, view_func.__name__)
        raise PermissionDenied("This view has no access policy.")


class SecurityHeadersMiddleware:
    """Content-Security-Policy and friends.

    Scripts are same-origin only: there is no inline JavaScript anywhere in the
    templates, so an injected <script> has nowhere to run. Inline style attributes are
    allowed because they cannot execute code.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        embeddable = getattr(request, "view_embeddable", False)
        frame_ancestors = "*" if embeddable else "'none'"
        response.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
            f"frame-ancestors {frame_ancestors}",
        )
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if settings.DEMO_MODE:
            response.setdefault("X-Demo-Mode", "true")
        return response
