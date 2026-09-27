import hashlib
import json

from django.conf import settings
from django.db import models

from core import clock


class AuditEntry(models.Model):
    """One thing that happened, in a hash chain.

    Each entry stores the digest of the entry before it, and its own digest covers that
    link plus everything it says. Editing or deleting any past row breaks every digest
    after it, which `manage.py verify_audit` and the organizer's audit page detect. It
    is tamper-evident, not tamper-proof: someone with database access can rewrite the
    whole chain, which is why the head digest is also printed in every published
    results record (see records.services), where it is signed and leaves the building.

    `summary` is a sentence written for an organizer at the moment of the event
    ("Wei Lindqvist changed Quality from 3 to 4 on Salt Ledger"). It is deliberately
    denormalised: it has to stay true after the project is renamed.
    """

    at = models.DateTimeField(default=clock.now, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    actor_label = models.CharField(max_length=160)
    event = models.ForeignKey(
        "events.Event", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="audit_entries",
    )
    action = models.CharField(max_length=64, db_index=True)
    target_type = models.CharField(max_length=32, blank=True)
    target_id = models.CharField(max_length=40, blank=True)
    summary = models.CharField(max_length=400)
    data = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=16, default="web")
    prev_digest = models.CharField(max_length=64)
    digest = models.CharField(max_length=64, unique=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["event", "-id"]),
            models.Index(fields=["target_type", "target_id"]),
        ]
        verbose_name_plural = "audit entries"

    def __str__(self):
        return f"#{self.pk} {self.action}: {self.summary}"

    def compute_digest(self) -> str:
        body = {
            "prev": self.prev_digest,
            "at": self.at.isoformat(),
            "actor": self.actor_id,
            "actor_label": self.actor_label,
            "event": self.event_id,
            "action": self.action,
            "target": [self.target_type, self.target_id],
            "summary": self.summary,
            "data": self.data,
            "source": self.source,
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    @property
    def category(self):
        return self.action.split(".", 1)[0]


class AuditChainHead(models.Model):
    """A single row holding the latest digest. Writers lock it to append in order."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    digest = models.CharField(max_length=64, default="0" * 64)
    length = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="audit_head_singleton")]

    def __str__(self):
        return f"audit head {self.digest[:12]}… ({self.length} entries)"
