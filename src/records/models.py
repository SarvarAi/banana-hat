from django.conf import settings
from django.db import models

from core import clock
from core.models import PublicIdMixin


class SigningKey(models.Model):
    """An Ed25519 key pair belonging to this installation.

    The public half is published at /.well-known/bananahat-keys.json and never
    changes for a given key id. The private half is stored encrypted with a key derived
    from SECRET_KEY, so a copy of the database alone cannot forge records. Retiring a
    key keeps it listed, so everything it signed stays verifiable.
    """

    key_id = models.CharField(max_length=40, unique=True)
    algorithm = models.CharField(max_length=16, default="Ed25519")
    public_key = models.CharField(max_length=64, help_text="Raw public key, base64url.")
    private_key_encrypted = models.TextField()
    created_at = models.DateTimeField(default=clock.now)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.key_id


class SignedRecord(PublicIdMixin):
    """A statement this portal makes and signs: published results, a judge's
    participation, a certificate.

    `canonical` holds the exact bytes that were signed (sorted-key, compact JSON), so
    verification never depends on re-serialising `payload` the same way twice.
    """

    PUBLIC_ID_PREFIX = "rec"

    class Kind(models.TextChoices):
        RESULTS = "results", "Published results"
        JUDGE = "judge_participation", "Judge participation"
        CERTIFICATE = "certificate", "Certificate"

    kind = models.CharField(max_length=24, choices=Kind.choices)
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="records")
    subject = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="records"
    )
    payload = models.JSONField()
    canonical = models.TextField()
    signature = models.CharField(max_length=128)
    key = models.ForeignKey(SigningKey, on_delete=models.PROTECT, related_name="records")
    private_salt = models.CharField(
        max_length=64, blank=True,
        help_text="Never published. Opens the score commitment in a judge record if an audit needs it.",
    )
    issued_at = models.DateTimeField(default=clock.now)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-issued_at"]
        indexes = [models.Index(fields=["event", "kind"])]

    def __str__(self):
        return f"{self.get_kind_display()} {self.public_id}"
