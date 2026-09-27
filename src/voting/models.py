from django.conf import settings
from django.db import models
from django.db.models import Q

from core import clock
from core.models import PublicIdMixin


class Comment(PublicIdMixin):
    """A public comment on a project. Organizers can hide one; nothing is deleted."""

    PUBLIC_ID_PREFIX = "cmt"

    project = models.ForeignKey("events.Project", on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="comments")
    body = models.TextField(max_length=2000)
    created_at = models.DateTimeField(default=clock.now)
    hidden_at = models.DateTimeField(null=True, blank=True)
    hidden_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    hidden_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["project", "created_at"])]

    def __str__(self):
        return f"{self.author} on {self.project}: {self.body[:40]}"


class Ballot(PublicIdMixin):
    """One voter's ballot in one event's community vote.

    A voter is identified by a keyed digest of who they are: their user id, their
    canonical email address, or the ballot link they used. `(event, identity_digest)`
    is unique, so "one person, one ballot" is a database constraint, and the table
    never holds a voter's email address in clear.

    `status` is where anti-abuse lands. A flagged ballot is kept but not counted until an
    organizer looks at it; `flags` records why it was flagged.
    """

    PUBLIC_ID_PREFIX = "blt"

    class Kind(models.TextChoices):
        USER = "user", "Signed-in user"
        EMAIL = "email", "Verified email"
        LINK = "link", "Ballot link"

    class Status(models.TextChoices):
        PENDING = "pending", "Awaiting email verification"
        COUNTED = "counted", "Counted"
        FLAGGED = "flagged", "Held for review"
        VOID = "void", "Void"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="ballots")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="ballots"
    )
    identity_digest = models.CharField(max_length=64)
    email_hint = models.CharField(max_length=80, blank=True, help_text="Masked, e.g. pr***@example.org")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    flags = models.JSONField(default=list, blank=True)
    network_digest = models.CharField(max_length=64, blank=True)
    agent_digest = models.CharField(max_length=64, blank=True)
    shuffle_seed = models.BigIntegerField()
    created_at = models.DateTimeField(default=clock.now)
    cast_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["event", "identity_digest"], name="one_ballot_per_voter"),
        ]
        indexes = [
            models.Index(fields=["event", "status"]),
            models.Index(fields=["event", "network_digest"]),
        ]

    def __str__(self):
        return f"Ballot {self.public_id} ({self.status})"


class BallotChoice(models.Model):
    ballot = models.ForeignKey(Ballot, on_delete=models.CASCADE, related_name="choices")
    project = models.ForeignKey("events.Project", on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["ballot", "project"], name="one_choice_per_project"),
        ]

    def __str__(self):
        return f"{self.ballot_id} → {self.project_id}"


class BallotToken(models.Model):
    """A single-use secret: either a printed ballot link, or an email verification link.

    Only the digest is stored.
    """

    class Purpose(models.TextChoices):
        LINK = "link", "Ballot link"
        VERIFY = "verify", "Email verification"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="ballot_tokens")
    purpose = models.CharField(max_length=8, choices=Purpose.choices)
    token_digest = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=80, blank=True, help_text='For links: e.g. "Table 12".')
    ballot = models.ForeignKey(Ballot, null=True, blank=True, on_delete=models.CASCADE, related_name="tokens")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=clock.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(purpose="verify") | Q(ballot__isnull=False),
                name="verify_token_has_ballot",
            ),
        ]

    def __str__(self):
        return f"{self.get_purpose_display()} {self.label or self.pk}"
