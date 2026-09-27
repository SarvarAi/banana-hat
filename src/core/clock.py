"""The one place the application asks what time it is.

Deadlines are enforced against this function, so tests can move the clock with
`monkeypatch.setattr("core.clock.now", ...)` instead of sleeping or faking dates in
fixtures.
"""

from django.utils import timezone


def now():
    return timezone.now()
