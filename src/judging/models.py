from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q

from core import clock
from core.models import PublicIdMixin


class Criterion(PublicIdMixin):
    """One line of an event's rubric.

    Weights are relative: 2, 1, 1 means the first criterion counts for half. They are
    normalised to sum to one at computation time, so an organizer never has to make
    them add up to 100.
    """

    PUBLIC_ID_PREFIX = "crt"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="criteria")
    key = models.SlugField(max_length=40, help_text="Stable machine name, used in imports and exports.")
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True, help_text="Shown to judges next to the score.")
    weight = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("1.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["event", "position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "key"], name="criterion_key_unique_per_event"),
            models.CheckConstraint(condition=Q(weight__gt=0), name="criterion_weight_positive"),
        ]
        verbose_name_plural = "criteria"

    def __str__(self):
        return f"{self.name} (×{self.weight})"


class JudgeInvitation(models.Model):
    """An emailed, single-use link that makes the recipient a judge of one event.

    Only the digest of the token is stored.
    """

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="judge_invitations")
    email = models.EmailField()
    name = models.CharField(max_length=120, blank=True)
    tracks = models.ManyToManyField("events.Track", blank=True, related_name="+")
    token_digest = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=clock.now)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Invitation for {self.email} to {self.event}"

    @property
    def state(self):
        if self.accepted_at:
            return "accepted"
        if self.revoked_at:
            return "revoked"
        if self.expires_at <= clock.now():
            return "expired"
        return "pending"


class AssignmentQuerySet(models.QuerySet):
    def active(self):
        return self.exclude(status=Assignment.Status.RECUSED)


class Assignment(PublicIdMixin):
    """A judge is asked to review a project. The unit of judging work."""

    PUBLIC_ID_PREFIX = "asg"

    class Status(models.TextChoices):
        PENDING = "pending", "To review"
        DONE = "done", "Reviewed"
        RECUSED = "recused", "Recused (conflict of interest)"

    class Source(models.TextChoices):
        AUTO = "auto", "Automatic"
        MANUAL = "manual", "Manual"
        IMPORT = "import", "Imported"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="assignments")
    judge = models.ForeignKey("events.EventRole", on_delete=models.CASCADE, related_name="assignments")
    project = models.ForeignKey("events.Project", on_delete=models.CASCADE, related_name="assignments")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.MANUAL)
    recusal_reason = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=clock.now)

    objects = AssignmentQuerySet.as_manager()

    class Meta:
        ordering = ["event", "project__title"]
        constraints = [
            models.UniqueConstraint(fields=["judge", "project"], name="one_assignment_per_pair"),
        ]
        indexes = [models.Index(fields=["event", "status"])]

    def __str__(self):
        return f"{self.judge.user} → {self.project}"


class ScoreQuerySet(models.QuerySet):
    def visible_to(self, user):
        """The scores `user` may read, and no others.

        This is the backend half of "judges cannot see peer scores". Every view that
        returns scores starts from this queryset, so a view that forgets to filter
        still cannot leak: the rows were never fetched.

        - a judge sees exactly the scores they wrote
        - an organizer sees every score in events they organize
        - a platform admin sees everything
        - everybody else sees nothing; the public only ever gets aggregates, and only
          after results are published (see judging.results)
        """
        if user is None or not user.is_authenticated:
            return self.none()
        if user.is_staff:
            return self
        from events.models import EventRole

        organized = EventRole.objects.filter(
            user=user, role=EventRole.Role.ORGANIZER
        ).values("event_id")
        return self.filter(
            Q(assignment__judge__user=user) | Q(assignment__event_id__in=organized)
        )

    def counted(self):
        """Scores that enter results: not voided, on live, non-duplicate projects."""
        return self.filter(
            voided_at__isnull=True,
            assignment__project__status="submitted",
            assignment__project__duplicate_of__isnull=True,
        ).exclude(assignment__status=Assignment.Status.RECUSED)


class Score(PublicIdMixin):
    """A judge's review of one project: a value per criterion plus a comment."""

    PUBLIC_ID_PREFIX = "scr"

    assignment = models.OneToOneField(Assignment, on_delete=models.CASCADE, related_name="score")
    comment = models.TextField(blank=True)
    submitted_at = models.DateTimeField(default=clock.now)
    updated_at = models.DateTimeField(default=clock.now)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_reason = models.CharField(max_length=300, blank=True)

    objects = ScoreQuerySet.as_manager()

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Score {self.public_id}"

    def as_dict(self):
        return {v.criterion.key: v.value for v in self.values.all()}


class ScoreValue(models.Model):
    """One criterion's value within a score. Range-checked against the event's scale
    by the service that writes it; the database guarantees at least the widest scale."""

    score = models.ForeignKey(Score, on_delete=models.CASCADE, related_name="values")
    criterion = models.ForeignKey(Criterion, on_delete=models.PROTECT, related_name="values")
    value = models.PositiveSmallIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["score", "criterion"], name="one_value_per_criterion"),
            models.CheckConstraint(condition=Q(value__lte=100), name="score_value_sane"),
        ]

    def __str__(self):
        return f"{self.criterion.key}={self.value}"


class Conflict(models.Model):
    """A declared conflict of interest between a judge and a team.

    Declaring one recuses the judge from that team's project and stops automatic
    assignment from ever pairing them again.
    """

    judge = models.ForeignKey("events.EventRole", on_delete=models.CASCADE, related_name="conflicts")
    team = models.ForeignKey("events.Team", on_delete=models.CASCADE, related_name="conflicts")
    reason = models.CharField(max_length=300)
    declared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["judge", "team"], name="one_conflict_per_pair"),
        ]

    def __str__(self):
        return f"{self.judge.user} ✕ {self.team}"


class PairwiseComparison(models.Model):
    """Pairwise mode: a judge says which of two projects is better."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="comparisons")
    judge = models.ForeignKey("events.EventRole", on_delete=models.CASCADE, related_name="comparisons")
    winner = models.ForeignKey("events.Project", on_delete=models.CASCADE, related_name="+")
    loser = models.ForeignKey("events.Project", on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=~Q(winner=models.F("loser")), name="comparison_distinct"),
        ]
        indexes = [models.Index(fields=["event", "judge"])]

    def __str__(self):
        return f"{self.winner} > {self.loser}"


class ResultSnapshot(models.Model):
    """Results as they were published. Frozen: later edits cannot move a published
    ranking."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="result_snapshots")
    method = models.CharField(max_length=16)
    payload = models.JSONField()
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    published_at = models.DateTimeField(default=clock.now)

    class Meta:
        ordering = ["-published_at"]
        get_latest_by = "published_at"

    def __str__(self):
        return f"Results for {self.event} ({self.published_at:%Y-%m-%d})"
