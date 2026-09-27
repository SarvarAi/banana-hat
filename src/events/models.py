import secrets

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.urls import reverse

from core import clock
from core.models import PublicIdMixin


class EventQuerySet(models.QuerySet):
    def listed(self):
        return self.filter(is_listed=True)

    def organized_by(self, user):
        return self.filter(roles__user=user, roles__role=EventRole.Role.ORGANIZER)


class Event(PublicIdMixin):
    """A hackathon.

    Its phase is never stored. "Are submissions open?" is answered from the clock and
    the dates below every time it is asked, so there is no status flag for someone to
    forget to flip on Sunday at 18:00 and no cron job whose failure reopens an event.
    """

    PUBLIC_ID_PREFIX = "evt"

    class VotingMode(models.TextChoices):
        DISABLED = "disabled", "No community vote"
        AUTHENTICATED = "authenticated", "Signed-in users"
        EMAIL = "email", "Verified email address"
        LINK = "link", "Single-use ballot links"

    class Normalization(models.TextChoices):
        JUDGE_EFFECT = "judge_effect", "Judge-effect model (recommended)"
        ZSCORE = "zscore", "Per-judge z-score, shrunk"
        RAW = "raw", "Raw weighted mean"
        IMPLIED_PAIRWISE = "implied_bt", "Bradley–Terry on each judge's ranking"
        PAIRWISE = "pairwise", "Bradley–Terry on pairwise comparisons"

    slug = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=160)
    tagline = models.CharField(max_length=240, blank=True)
    description = models.TextField(blank=True)
    timezone = models.CharField(
        max_length=64, default="UTC", help_text="Used to display dates. Deadlines are stored in UTC."
    )
    is_listed = models.BooleanField(
        default=False, help_text="Listed events are visible to the public. Drafts are not."
    )

    submissions_open_at = models.DateTimeField()
    submissions_close_at = models.DateTimeField()
    judging_close_at = models.DateTimeField(
        null=True, blank=True, help_text="Scores lock at this time. Empty: when results are published."
    )
    results_published_at = models.DateTimeField(null=True, blank=True)

    max_team_size = models.PositiveSmallIntegerField(default=4)
    score_min = models.PositiveSmallIntegerField(default=1)
    score_max = models.PositiveSmallIntegerField(default=5)
    reviews_per_project = models.PositiveSmallIntegerField(
        default=3, help_text="Target number of judges per project for automatic assignment."
    )
    judge_max_load = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Most projects one judge is assigned. Empty: balanced automatically."
    )
    normalization = models.CharField(
        max_length=16, choices=Normalization.choices, default=Normalization.JUDGE_EFFECT
    )

    voting_mode = models.CharField(
        max_length=16, choices=VotingMode.choices, default=VotingMode.DISABLED
    )
    voting_open_at = models.DateTimeField(null=True, blank=True)
    voting_close_at = models.DateTimeField(null=True, blank=True)
    votes_per_voter = models.PositiveSmallIntegerField(default=3)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=clock.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EventQuerySet.as_manager()

    class Meta:
        ordering = ["-submissions_close_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(submissions_close_at__gt=models.F("submissions_open_at")),
                name="event_submission_window_ordered",
            ),
            models.CheckConstraint(
                condition=Q(voting_open_at__isnull=True)
                | Q(voting_close_at__isnull=True)
                | Q(voting_close_at__gt=models.F("voting_open_at")),
                name="event_voting_window_ordered",
            ),
            models.CheckConstraint(
                condition=Q(score_max__gt=models.F("score_min")), name="event_score_range_ordered"
            ),
            models.CheckConstraint(
                condition=Q(max_team_size__gte=1), name="event_team_size_positive"
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("events:detail", args=[self.slug])

    # --- phase, derived from the clock --------------------------------------------

    def submissions_open(self, at=None):
        at = at or clock.now()
        return self.submissions_open_at <= at < self.submissions_close_at

    def judging_open(self, at=None):
        at = at or clock.now()
        if self.results_published_at:
            return False
        if self.judging_close_at and at >= self.judging_close_at:
            return False
        return at >= self.submissions_close_at

    def voting_enabled(self):
        return self.voting_mode != self.VotingMode.DISABLED

    def voting_open(self, at=None):
        at = at or clock.now()
        if not self.voting_enabled() or not (self.voting_open_at and self.voting_close_at):
            return False
        return self.voting_open_at <= at < self.voting_close_at

    def voting_closed(self, at=None):
        at = at or clock.now()
        return bool(self.voting_close_at and at >= self.voting_close_at)

    @property
    def results_public(self):
        return self.results_published_at is not None

    def phase(self, at=None):
        """A single word for badges. Informational only; nothing is enforced on it."""
        at = at or clock.now()
        if self.results_published_at:
            return "results"
        if at < self.submissions_open_at:
            return "upcoming"
        if self.submissions_open(at):
            return "open"
        if self.voting_open(at):
            return "voting"
        if self.judging_open(at):
            return "judging"
        return "closed"


class Track(PublicIdMixin):
    PUBLIC_ID_PREFIX = "trk"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="tracks")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["event", "position", "name"]
        constraints = [
            models.UniqueConstraint("event", Lower("name"), name="track_name_unique_per_event"),
        ]

    def __str__(self):
        return self.name


class Prize(PublicIdMixin):
    """A prize, overall (track empty) or for one track, decided by the judges' ranking
    or by the community vote."""

    PUBLIC_ID_PREFIX = "prz"

    class Source(models.TextChoices):
        JUDGED = "judged", "Judges' ranking"
        COMMUNITY = "community", "Community vote"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="prizes")
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.JUDGED)
    track = models.ForeignKey(
        Track, null=True, blank=True, on_delete=models.CASCADE, related_name="prizes"
    )
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    value = models.CharField(max_length=60, blank=True, help_text='Free text, e.g. "800 USD".')
    position = models.PositiveSmallIntegerField(default=0)
    winner = models.ForeignKey(
        "Project", null=True, blank=True, on_delete=models.SET_NULL, related_name="prizes_won"
    )

    class Meta:
        ordering = ["event", "position", "name"]

    def __str__(self):
        return self.name


class EventRoleQuerySet(models.QuerySet):
    def judges(self):
        return self.filter(role=EventRole.Role.JUDGE)

    def organizers(self):
        return self.filter(role=EventRole.Role.ORGANIZER)

    def participants(self):
        return self.filter(role=EventRole.Role.PARTICIPANT)


class EventRole(PublicIdMixin):
    """What one person may do in one event.

    A person can hold several roles in an event, with one exception enforced in
    `events.services.grant_role`: a judge may not also be a participant or an organizer
    of the same event. Participants would be judging their rivals; organizers can read
    every score, which would make "judges cannot see peer scores" a fiction.
    """

    class Role(models.TextChoices):
        ORGANIZER = "organizer", "Organizer"
        JUDGE = "judge", "Judge"
        PARTICIPANT = "participant", "Participant"

    PREFIXES = {Role.ORGANIZER: "org", Role.JUDGE: "jdg", Role.PARTICIPANT: "prt"}

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="roles")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="event_roles"
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    tracks = models.ManyToManyField(
        Track, blank=True, related_name="judges",
        help_text="Judges only: the tracks this judge may be assigned to. Empty means any.",
    )
    created_at = models.DateTimeField(default=clock.now)

    objects = EventRoleQuerySet.as_manager()

    class Meta:
        ordering = ["event", "role", "user__name"]
        constraints = [
            models.UniqueConstraint(fields=["event", "user", "role"], name="one_role_row_per_kind"),
        ]
        indexes = [models.Index(fields=["user", "role"])]

    def __str__(self):
        return f"{self.user} as {self.role} of {self.event}"

    def public_id_prefix(self) -> str:
        return self.PREFIXES.get(self.role, "rol")


def _invite_code():
    return secrets.token_urlsafe(12)


class Team(PublicIdMixin):
    """A team inside one event. Members join with the invite link.

    The name is a label, not an identity. fixtures.json has three different teams
    called StillTrail, so the database does not insist on unique names; the sign-up
    form does, for teams created here.
    """

    PUBLIC_ID_PREFIX = "tm"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="teams")
    name = models.CharField(max_length=120)
    invite_code = models.CharField(max_length=32, unique=True, default=_invite_code)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        ordering = ["event", "name"]
        indexes = [models.Index("event", Lower("name"), name="team_event_name_idx")]

    def __str__(self):
        return self.name

    def rotate_invite(self):
        self.invite_code = _invite_code()
        self.save(update_fields=["invite_code"])


class TeamMember(models.Model):
    """Membership. `event` repeats team.event so the database itself can refuse a
    second team for the same person in the same event (`one_team_per_event`)."""

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="memberships")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="team_memberships"
    )
    is_captain = models.BooleanField(default=False)
    joined_at = models.DateTimeField(default=clock.now)

    class Meta:
        ordering = ["-is_captain", "joined_at"]
        constraints = [
            models.UniqueConstraint(fields=["event", "user"], name="one_team_per_event"),
        ]

    def __str__(self):
        return f"{self.user} in {self.team}"

    def save(self, *args, **kwargs):
        self.event_id = self.team.event_id
        super().save(*args, **kwargs)


class ProjectQuerySet(models.QuerySet):
    def submitted(self):
        return self.filter(status=Project.Status.SUBMITTED)

    def canonical(self):
        """Submitted projects that are not flagged as duplicates: the ones that compete."""
        return self.submitted().filter(duplicate_of__isnull=True)

    def public(self):
        return self.canonical().filter(event__is_listed=True)


class Project(PublicIdMixin):
    """A team's submission.

    A team has at most one live project per event, enforced by a partial unique index
    rather than by hope. Duplicates that already exist (the fixture has one) are kept
    for the record, pointed at the project they duplicate, and excluded from judging
    results and the public gallery.
    """

    PUBLIC_ID_PREFIX = "prj"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        WITHDRAWN = "withdrawn", "Withdrawn"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="projects")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="projects")
    track = models.ForeignKey(
        Track, null=True, blank=True, on_delete=models.PROTECT, related_name="projects"
    )
    title = models.CharField(max_length=160)
    summary = models.CharField(max_length=300, blank=True)
    description = models.TextField(blank=True)
    repo_url = models.URLField(max_length=400, blank=True)
    demo_url = models.URLField(max_length=400, blank=True)
    video_url = models.URLField(max_length=400, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    submitted_at = models.DateTimeField(
        null=True, blank=True, help_text="When the project was last submitted or edited while submitted."
    )
    duplicate_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicates"
    )
    created_at = models.DateTimeField(default=clock.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ProjectQuerySet.as_manager()

    class Meta:
        ordering = ["title"]
        constraints = [
            models.UniqueConstraint(
                fields=["team"],
                condition=Q(duplicate_of__isnull=True) & ~Q(status="withdrawn"),
                name="one_live_project_per_team",
            ),
            models.CheckConstraint(
                condition=~Q(status="submitted") | Q(submitted_at__isnull=False),
                name="submitted_project_has_timestamp",
            ),
            models.CheckConstraint(
                condition=Q(duplicate_of__isnull=True) | ~Q(duplicate_of=models.F("id")),
                name="project_not_duplicate_of_itself",
            ),
        ]
        indexes = [
            models.Index(fields=["event", "status"]),
            models.Index(fields=["event", "track"]),
        ]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("events:project", args=[self.public_id])

    @property
    def is_duplicate(self):
        return self.duplicate_of_id is not None


class SubmissionExtension(models.Model):
    """An organizer lets one team keep editing past the deadline, on the record."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="extensions")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="extensions")
    until = models.DateTimeField()
    reason = models.CharField(max_length=300)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=clock.now)

    class Meta:
        ordering = ["-until"]
        constraints = [
            models.UniqueConstraint(fields=["team"], name="one_extension_per_team"),
        ]

    def __str__(self):
        return f"{self.team} until {self.until:%Y-%m-%d %H:%M}"
