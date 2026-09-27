"""Fixed-window rate limiting backed by the database.

Used for the endpoints where abuse is the point of the attack: login, sign-up, voting,
comments, email verification. Limits are per (scope, subject) where the subject is a
user id or a hashed client address.
"""

import hashlib
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F

from core import clock
from core.errors import DomainError
from core.models import RateLimitCounter


class RateLimited(DomainError):
    status = 429
    code = "rate_limited"


@dataclass(frozen=True)
class Limit:
    scope: str
    count: int
    per_seconds: int


def _digest(scope: str, subject: str) -> str:
    material = f"{settings.SECRET_KEY}:{scope}:{subject}".encode()
    return hashlib.sha256(material).hexdigest()


def hit(limit: Limit, subject: str) -> bool:
    """Record one attempt. Returns False if the attempt is over the limit."""
    key = _digest(limit.scope, subject)
    window = int(clock.now().timestamp()) // limit.per_seconds * limit.per_seconds
    updated = RateLimitCounter.objects.filter(key=key, window_start=window).update(
        count=F("count") + 1
    )
    if not updated:
        try:
            with transaction.atomic():
                RateLimitCounter.objects.create(key=key, window_start=window, count=1)
        except IntegrityError:
            RateLimitCounter.objects.filter(key=key, window_start=window).update(
                count=F("count") + 1
            )
    count = RateLimitCounter.objects.values_list("count", flat=True).get(
        key=key, window_start=window
    )
    return count <= limit.count


def enforce(limit: Limit, subject: str) -> None:
    if not hit(limit, subject):
        raise RateLimited(
            f"Too many attempts. Try again in a few minutes ({limit.count} per "
            f"{limit.per_seconds // 60 or 1} min)."
        )


def purge_expired(older_than_seconds: int = 86400) -> int:
    cutoff = int(clock.now().timestamp()) - older_than_seconds
    deleted, _ = RateLimitCounter.objects.filter(window_start__lt=cutoff).delete()
    return deleted


def client_ip(request) -> str:
    """The caller's address. X-Forwarded-For is trusted only behind a declared proxy."""
    if getattr(settings, "TRUST_X_FORWARDED_FOR", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def network_of(ip: str) -> str:
    """The /24 (IPv4) or /48 (IPv6) an address belongs to, for spotting clusters."""
    if ":" in ip:
        return ":".join(ip.split(":")[:3]) + "::/48"
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3]) + ".0/24"
    return ip


LOGIN = Limit("login", 10, 300)
SIGNUP = Limit("signup", 5, 3600)
PASSWORD_RESET = Limit("password-reset", 5, 3600)
VOTE = Limit("vote", 20, 3600)
COMMENT = Limit("comment", 10, 600)
EMAIL_VERIFY = Limit("email-verify", 5, 3600)
