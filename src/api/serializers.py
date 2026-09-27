from decimal import Decimal

from rest_framework import serializers

from accounts.models import User
from events.models import Event, EventRole, Prize, Project, Team, Track
from integrations.models import WEBHOOK_EVENT_TYPES, WebhookDelivery, WebhookEndpoint
from judging.models import Assignment, Criterion, Score
from voting.models import Comment


class TrackSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="public_id", read_only=True)

    class Meta:
        model = Track
        fields = ["id", "name", "description"]


class CriterionSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="public_id", read_only=True)
    weight = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal("0.01"))

    class Meta:
        model = Criterion
        fields = ["id", "key", "name", "description", "weight"]


class PrizeSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="public_id", read_only=True)
    track = serializers.CharField(source="track.public_id", read_only=True, default=None)
    winner = serializers.SerializerMethodField()

    class Meta:
        model = Prize
        fields = ["id", "name", "value", "description", "track", "winner"]

    def get_winner(self, prize) -> str | None:
        if prize.winner_id and prize.event.results_published_at:
            return prize.winner.public_id
        return None


class EventSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="public_id", read_only=True)
    phase = serializers.SerializerMethodField()
    submissions_open = serializers.SerializerMethodField()
    url = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = [
            "id", "slug", "name", "tagline", "description", "phase", "submissions_open",
            "submissions_open_at", "submissions_close_at", "judging_close_at", "results_published_at",
            "max_team_size", "score_min", "score_max", "reviews_per_project", "normalization",
            "voting_mode", "voting_open_at", "voting_close_at", "votes_per_voter", "voter_email_domains",
            "is_listed", "url",
        ]
        read_only_fields = ["results_published_at"]

    def get_phase(self, event) -> str:
        return event.phase()

    def get_submissions_open(self, event) -> bool:
        return event.submissions_open()

    def get_url(self, event) -> str:
        request = self.context.get("request")
        path = event.get_absolute_url()
        return request.build_absolute_uri(path) if request else path

    def validate(self, attrs):
        instance = self.instance
        opens = attrs.get("submissions_open_at", getattr(instance, "submissions_open_at", None))
        closes = attrs.get("submissions_close_at", getattr(instance, "submissions_close_at", None))
        if opens and closes and closes <= opens:
            raise serializers.ValidationError({"submissions_close_at": "Must be after submissions_open_at."})
        return attrs


class EventDetailSerializer(EventSerializer):
    tracks = TrackSerializer(many=True, read_only=True)
    criteria = CriterionSerializer(many=True, read_only=True)
    prizes = PrizeSerializer(many=True, read_only=True)

    class Meta(EventSerializer.Meta):
        fields = [*EventSerializer.Meta.fields, "tracks", "criteria", "prizes"]


class ProjectSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="public_id", read_only=True)
    event = serializers.CharField(source="event.public_id", read_only=True)
    team = serializers.SerializerMethodField()
    track = serializers.SerializerMethodField()
    duplicate_of = serializers.CharField(source="duplicate_of.public_id", read_only=True, default=None)
    url = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = ["id", "event", "title", "summary", "description", "repo_url", "demo_url", "video_url",
                  "track", "team", "status", "submitted_at", "duplicate_of", "url"]

    def get_team(self, project) -> dict:
        return {"id": project.team.public_id, "name": project.team.name}

    def get_track(self, project) -> dict | None:
        return {"id": project.track.public_id, "name": project.track.name} if project.track else None

    def get_url(self, project) -> str:
        request = self.context.get("request")
        path = project.get_absolute_url()
        return request.build_absolute_uri(path) if request else path


class ProjectWriteSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=160, required=False)
    summary = serializers.CharField(max_length=300, required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    repo_url = serializers.URLField(max_length=400, required=False, allow_blank=True)
    demo_url = serializers.URLField(max_length=400, required=False, allow_blank=True)
    video_url = serializers.URLField(max_length=400, required=False, allow_blank=True)
    track = serializers.CharField(required=False, help_text="Track id, e.g. trk_01.")
    submit = serializers.BooleanField(required=False, default=False,
                                      help_text="Submit immediately instead of saving a draft.")


class TeamSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="public_id", read_only=True)
    event = serializers.CharField(source="event.public_id", read_only=True)
    members = serializers.SerializerMethodField()
    invite_code = serializers.SerializerMethodField()

    class Meta:
        model = Team
        fields = ["id", "event", "name", "members", "invite_code"]

    def get_members(self, team) -> list[dict]:
        return [{"name": m.user.display_name, "captain": m.is_captain}
                for m in team.memberships.select_related("user")]

    def get_invite_code(self, team) -> str | None:
        """Only members see the invite code."""
        request = self.context.get("request")
        if request and team.memberships.filter(user_id=request.user.pk).exists():
            return team.invite_code
        return None


class ScoreSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="public_id", read_only=True)
    event = serializers.CharField(source="assignment.event.public_id", read_only=True)
    judge = serializers.CharField(source="assignment.judge.public_id", read_only=True)
    project = serializers.CharField(source="assignment.project.public_id", read_only=True)
    project_title = serializers.CharField(source="assignment.project.title", read_only=True)
    values = serializers.SerializerMethodField()
    voided = serializers.SerializerMethodField()

    class Meta:
        model = Score
        fields = ["id", "event", "judge", "project", "project_title", "values", "comment",
                  "submitted_at", "updated_at", "voided"]

    def get_values(self, score) -> dict:
        return {v.criterion.key: v.value for v in score.values.all()}

    def get_voided(self, score) -> bool:
        return score.voided_at is not None


class ScoreWriteSerializer(serializers.Serializer):
    values = serializers.DictField(child=serializers.IntegerField(),
                                   help_text='Criterion key to value, e.g. {"functionality": 4}.')
    comment = serializers.CharField(required=False, allow_blank=True, max_length=4000)


class AssignmentSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="public_id", read_only=True)
    event = serializers.CharField(source="event.public_id", read_only=True)
    judge = serializers.CharField(source="judge.public_id", read_only=True)
    project = ProjectSerializer(read_only=True)
    scored = serializers.SerializerMethodField()

    class Meta:
        model = Assignment
        fields = ["id", "event", "judge", "project", "status", "source", "scored"]

    def get_scored(self, assignment) -> bool:
        return hasattr(assignment, "score")


class RecuseSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=300)


class AutoAssignSerializer(serializers.Serializer):
    target = serializers.IntegerField(min_value=1, max_value=20, required=False)
    max_load = serializers.IntegerField(min_value=1, required=False)
    dry_run = serializers.BooleanField(required=False, default=False)


class CommentSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="public_id", read_only=True)
    author = serializers.CharField(source="author.display_name", read_only=True)

    class Meta:
        model = Comment
        fields = ["id", "author", "body", "created_at"]
        read_only_fields = ["created_at"]


class BallotWriteSerializer(serializers.Serializer):
    projects = serializers.ListField(child=serializers.CharField(), min_length=1, max_length=50)


class PairwiseWriteSerializer(serializers.Serializer):
    winner = serializers.CharField()
    loser = serializers.CharField()


class WebhookSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="public_id", read_only=True)
    event_types = serializers.ListField(
        child=serializers.ChoiceField(choices=[t for t, _ in WEBHOOK_EVENT_TYPES]), required=False
    )

    class Meta:
        model = WebhookEndpoint
        fields = ["id", "url", "description", "event_types", "secret", "is_active", "created_at"]
        read_only_fields = ["secret", "created_at"]


class WebhookDeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookDelivery
        fields = ["delivery_id", "event_type", "status", "attempts", "last_status_code", "last_error",
                  "created_at", "delivered_at", "next_attempt_at"]


class RoleSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="public_id", read_only=True)
    user = serializers.SerializerMethodField()
    tracks = serializers.SlugRelatedField(many=True, read_only=True, slug_field="public_id")

    class Meta:
        model = EventRole
        fields = ["id", "role", "user", "tracks"]

    def get_user(self, role) -> dict:
        return {"name": role.user.name, "email": role.user.email}


# --- response shapes, for the OpenAPI document ----------------------------------------

class SignedRecordSerializer(serializers.Serializer):
    payload = serializers.DictField(help_text="The signed statement. Its canonical JSON is what was signed.")
    signature = serializers.CharField(help_text="Ed25519 signature, base64url.")
    algorithm = serializers.CharField()
    public_key = serializers.CharField(help_text="Convenience copy; verify against /.well-known/bananahat-keys.json.")
    record = serializers.CharField(required=False)
    valid = serializers.BooleanField(required=False)
    revoked = serializers.BooleanField(required=False)


class AuditEntrySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    at = serializers.DateTimeField()
    actor = serializers.CharField()
    action = serializers.CharField()
    summary = serializers.CharField()
    target = serializers.ListField(child=serializers.CharField())
    data = serializers.DictField()
    digest = serializers.CharField()


class ChainReportSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    checked = serializers.IntegerField()
    head = serializers.CharField()
    broken_at = serializers.IntegerField(allow_null=True)
    reason = serializers.CharField()


class AuditPageSerializer(serializers.Serializer):
    entries = AuditEntrySerializer(many=True)
    chain = ChainReportSerializer()


class BallotProjectSerializer(serializers.Serializer):
    id = serializers.CharField()
    title = serializers.CharField()
    team = serializers.CharField()
    chosen = serializers.BooleanField()


class BallotSerializer(serializers.Serializer):
    ballot = serializers.CharField()
    votes_allowed = serializers.IntegerField()
    status = serializers.CharField()
    projects = BallotProjectSerializer(many=True, help_text="In this ballot's own random order.")


class TallyRowSerializer(serializers.Serializer):
    rank = serializers.IntegerField()
    project = serializers.CharField()
    title = serializers.CharField()
    team = serializers.CharField()
    votes = serializers.IntegerField()


class VotesSerializer(serializers.Serializer):
    turnout = serializers.DictField(child=serializers.IntegerField())
    tally = TallyRowSerializer(many=True)


class ResultRowSerializer(serializers.Serializer):
    rank = serializers.IntegerField()
    track_rank = serializers.IntegerField(allow_null=True)
    project = serializers.CharField()
    title = serializers.CharField()
    team = serializers.CharField()
    track = serializers.CharField(allow_null=True)
    track_name = serializers.CharField(allow_null=True)
    score = serializers.FloatField()
    raw_mean = serializers.FloatField(allow_null=True)
    reviews = serializers.IntegerField()
    ci = serializers.ListField(child=serializers.FloatField(), allow_null=True)
    shift = serializers.IntegerField(allow_null=True)


class ResultsSerializer(serializers.Serializer):
    event = serializers.DictField()
    method = serializers.CharField()
    method_label = serializers.CharField()
    on_scale = serializers.BooleanField()
    scale = serializers.ListField(child=serializers.IntegerField())
    criteria = serializers.ListField(child=serializers.DictField())
    diagnostics = serializers.DictField()
    reviews = serializers.IntegerField()
    overall = ResultRowSerializer(many=True)
    tracks = serializers.ListField(child=serializers.DictField())
    prizes = serializers.ListField(child=serializers.DictField())
    ties = serializers.ListField(child=serializers.DictField())
    excluded = serializers.ListField(child=serializers.DictField())
    community = TallyRowSerializer(many=True, allow_null=True)
    audit_head = serializers.CharField()
    published_at = serializers.CharField(required=False)
    record = serializers.CharField(required=False)


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["email", "name", "is_staff", "can_create_events"]
