from datetime import timedelta

from django.conf import settings
from rest_framework import authentication, exceptions

from accounts import demo
from accounts.models import ApiToken
from core import clock


class BearerTokenAuthentication(authentication.BaseAuthentication):
    """`Authorization: Bearer bh_...`, checked against the stored digest.

    Tokens bypass CSRF by design: they are never sent automatically by a browser, so a
    cross-site request cannot carry one.
    """

    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode("latin-1")
        if not header:
            return None
        scheme, _, raw = header.partition(" ")
        if scheme.lower() != self.keyword.lower():
            return None
        raw = raw.strip()
        if not raw:
            raise exceptions.AuthenticationFailed("Empty bearer token.")
        if raw.startswith(demo.TOKEN_PREFIX) and not settings.DEMO_MODE:
            # The demo tokens are printed in the README. Outside demo mode they are dead,
            # even if the database still holds them.
            raise exceptions.AuthenticationFailed("Demo tokens only work when DEMO_MODE is on.")
        token = (
            ApiToken.objects.usable()
            .select_related("user")
            .filter(digest=ApiToken.hash(raw))
            .first()
        )
        if token is None or not token.user.is_active:
            raise exceptions.AuthenticationFailed("Invalid, expired or revoked token.")
        # Touch last_used_at at most once a minute; it is for humans auditing their
        # tokens, not a per-request write.
        now = clock.now()
        if token.last_used_at is None or now - token.last_used_at > timedelta(minutes=1):
            ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
        return token.user, token

    def authenticate_header(self, request):
        return f'{self.keyword} realm="api"'
