from django import forms

from events.models import Event, Prize, Project, Track


class DateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%dT%H:%M", **kwargs)


class GalleryFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search", widget=forms.TextInput(attrs={
        "type": "search", "placeholder": "Title, summary or team"}))
    event = forms.ModelChoiceField(queryset=Event.objects.none(), required=False, to_field_name="slug",
                                   empty_label="All events")
    track = forms.ModelChoiceField(queryset=Track.objects.none(), required=False, to_field_name="public_id",
                                   empty_label="All tracks")
    sort = forms.ChoiceField(required=False, choices=[
        ("title", "A to Z"), ("newest", "Newest first"), ("track", "By track")])

    def __init__(self, *args, events=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["event"].queryset = events if events is not None else Event.objects.listed()
        event = None
        if self.data.get("event"):
            event = self.fields["event"].queryset.filter(slug=self.data["event"]).first()
        tracks = Track.objects.filter(event=event) if event else Track.objects.filter(
            event__in=self.fields["event"].queryset)
        self.fields["track"].queryset = tracks.select_related("event")
        self.fields["track"].label_from_instance = (
            (lambda t: t.name) if event else (lambda t: f"{t.name} · {t.event.name}")
        )


class TeamForm(forms.Form):
    name = forms.CharField(max_length=120, label="Team name")


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["title", "track", "summary", "description", "repo_url", "demo_url", "video_url"]
        labels = {"repo_url": "Repository URL", "demo_url": "Demo URL", "video_url": "Video URL"}
        help_texts = {
            "summary": "One or two sentences. This is what shows in the gallery.",
            "description": "What it does, how it works, what you would do next.",
        }
        widgets = {"summary": forms.TextInput()}

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["track"].queryset = event.tracks.all()
        self.fields["track"].required = event.tracks.exists()
        self.fields["track"].empty_label = "Choose a track"


class EventForm(forms.ModelForm):
    class Meta:
        model = Event
        fields = [
            "name", "slug", "tagline", "description", "is_listed",
            "submissions_open_at", "submissions_close_at", "judging_close_at",
            "max_team_size", "reviews_per_project", "judge_max_load", "score_min", "score_max",
            "normalization",
            "voting_mode", "voting_open_at", "voting_close_at", "votes_per_voter",
        ]
        widgets = {
            "submissions_open_at": DateTimeInput(),
            "submissions_close_at": DateTimeInput(),
            "judging_close_at": DateTimeInput(),
            "voting_open_at": DateTimeInput(),
            "voting_close_at": DateTimeInput(),
            "description": forms.Textarea(attrs={"rows": 4}),
        }
        help_texts = {
            "slug": "Used in the event's address.",
            "submissions_close_at": "UTC. The deadline is enforced by the server at this exact minute.",
        }

    def clean(self):
        cleaned = super().clean()
        opens, closes = cleaned.get("submissions_open_at"), cleaned.get("submissions_close_at")
        if opens and closes and closes <= opens:
            self.add_error("submissions_close_at", "The deadline must be after submissions open.")
        vo, vc = cleaned.get("voting_open_at"), cleaned.get("voting_close_at")
        mode = cleaned.get("voting_mode")
        if mode and mode != Event.VotingMode.DISABLED and not (vo and vc):
            self.add_error("voting_open_at", "Community voting needs an opening and a closing time.")
        if vo and vc and vc <= vo:
            self.add_error("voting_close_at", "Voting must close after it opens.")
        lo, hi = cleaned.get("score_min"), cleaned.get("score_max")
        if lo is not None and hi is not None and hi <= lo:
            self.add_error("score_max", "The top of the scale must be above the bottom.")
        return cleaned


class EventCreateForm(EventForm):
    """The short form for a new event; everything else keeps its default until the
    organizer opens the settings."""

    class Meta(EventForm.Meta):
        fields = [
            "name", "slug", "tagline", "submissions_open_at", "submissions_close_at",
            "max_team_size", "reviews_per_project", "normalization",
            "voting_mode", "voting_open_at", "voting_close_at",
        ]


class TrackForm(forms.ModelForm):
    class Meta:
        model = Track
        fields = ["name", "description"]
        widgets = {"description": forms.TextInput()}


class PrizeForm(forms.ModelForm):
    class Meta:
        model = Prize
        fields = ["name", "value", "track", "source", "description"]
        widgets = {"description": forms.TextInput()}

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["track"].queryset = event.tracks.all()
        self.fields["track"].empty_label = "Overall (every track)"


class ExtensionForm(forms.Form):
    team = forms.ModelChoiceField(queryset=None)
    until = forms.DateTimeField(widget=DateTimeInput(), help_text="UTC")
    reason = forms.CharField(max_length=300)

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["team"].queryset = event.teams.all()
