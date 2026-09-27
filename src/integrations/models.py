import secrets

from django.conf import settings
from django.db import models

from core import clock
from core.models import PublicIdMixin

WEBHOOK_EVENT_TYPES = [
    ("project.submitted", "A project is submitted"),
    ("project.withdrawn", "A project is withdrawn"),
    ("judging.progress", "A review is completed (counts only, never scores)"),
    ("results.published", "Results are published"),
    ("results.unpublished", "Results are taken down"),
    ("comment.created", "Someone comments on a project"),
    ("ping", "Test delivery"),
]


def _secret():
    return "whsec_" + secrets.token_urlsafe(24)


class WebhookEndpoint(PublicIdMixin):
    """Where an organizer wants events sent. Deliveries are signed with `secret`."""

    PUBLIC_ID_PREFIX = "whk"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="webhooks")
    url = models.URLField(max_length=500)
    description = models.CharField(max_length=200, blank=True)
    event_types = models.JSONField(default=list, help_text="Empty means every type.")
    secret = models.CharField(max_length=64, default=_secret)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return self.url

    def wants(self, event_type):
        return self.is_active and (not self.event_types or event_type in self.event_types or event_type == "ping")


class WebhookDelivery(models.Model):
    """One attempt-tracked message in the outbox.

    Rows are written in the same transaction as the change they announce (the
    transactional outbox pattern): if the change rolls back, no webhook is ever sent,
    and if it commits, the worker will keep trying until the receiver answers or the
    attempts run out. A crash between commit and delivery loses nothing.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Delivered"
        FAILED = "failed", "Failed, will retry"
        DEAD = "dead", "Gave up"

    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name="deliveries")
    delivery_id = models.CharField(max_length=40, unique=True)
    event_type = models.CharField(max_length=40)
    payload = models.JSONField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=clock.now)
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=clock.now)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]
        verbose_name_plural = "webhook deliveries"

    def __str__(self):
        return f"{self.event_type} → {self.endpoint.url} ({self.status})"
