"""Load fixtures.json into a fresh portal, and in demo mode set up the demo accounts.

Idempotent: if the fixture event already exists it only prints the credentials again,
so it is safe to run on every container start.

    python manage.py seed --fixtures /app/fixtures.json
"""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts import demo
from accounts.models import ApiToken, User
from core import clock
from events import services as event_services
from events.models import Event, EventRole, Prize, Project, Team, TeamMember
from integrations.interchange import import_event
from judging import assignment as assignment_service

# Rubric weights for the fixture event. The fixture scores three criteria and says
# nothing about their relative importance; an organizer would, so the demo does too.
FIXTURE_WEIGHTS = {"functionality": Decimal("40"), "quality": Decimal("30"), "innovation": Decimal("30")}
FIXTURE_CRITERIA_TEXT = {
    "functionality": "Does it work? Can a stranger run it and get the promised result?",
    "quality": "Is the code, the documentation and the experience something you would maintain?",
    "innovation": "Is there an idea here you have not seen done this way before?",
}


class Command(BaseCommand):
    help = "Seed the portal from fixtures.json (and demo accounts when DEMO_MODE is on)."

    def add_arguments(self, parser):
        parser.add_argument("--fixtures", default=str(Path(settings.REPO_DIR) / "fixtures.json"))
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, fixtures, quiet, **options):
        path = Path(fixtures)
        if not path.exists():
            raise CommandError(f"fixtures file not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        event_id = data["event"]["id"]

        if Event.objects.filter(public_id=event_id).exists():
            self.stdout.write(f"fixture event {event_id} already present; not seeding again")
        else:
            with transaction.atomic():
                self._seed(data)

        if settings.DEMO_MODE and not quiet:
            self._print_credentials()

    # ------------------------------------------------------------------------------

    def _seed(self, data):
        report = import_event(data, listed=True)
        event = report.event
        self.stdout.write(
            "imported {name}: {projects} projects, {judges} judges, {scores} scores".format(
                name=event.name, **report.counts
            )
        )
        for warning in report.warnings:
            self.stdout.write(f"  note: {warning}")

        event.tagline = "The shared DOGFOOD fixture event: 41 projects, 30 judges, 8 tracks."
        event.description = (
            "Loaded from fixtures.json exactly as published. Submissions closed on "
            "1 March 2026, so the portal refuses new ones. The fixture's awkward cases are "
            "all here: a judge who gave every project the same score, review batches that "
            "were never finished, and one duplicate submission."
        )
        event.save()
        for criterion in event.criteria.all():
            criterion.weight = FIXTURE_WEIGHTS.get(criterion.key, criterion.weight)
            criterion.description = FIXTURE_CRITERIA_TEXT.get(criterion.key, "")
            criterion.save()
        Prize.objects.bulk_create([
            Prize(event=event, name="Grand prize", value="800 USD", position=0,
                  description="The highest normalized score overall."),
            Prize(event=event, name="Runner up", value="500 USD", position=1),
            Prize(event=event, name="Best accessibility project", value="100 USD", position=2,
                  track=event.tracks.get(public_id="trk_03")),
        ])

        if not settings.DEMO_MODE:
            return

        admin = self._user(demo.ADMIN_EMAIL, "Ada Admin", demo.PASSWORDS["admin"], staff=True)
        organizer = self._user(demo.ORGANIZER_EMAIL, "Olu Organizer", demo.PASSWORDS["organizer"])
        organizer.can_create_events = True
        organizer.save(update_fields=["can_create_events"])
        event_services.grant_role(event, organizer, EventRole.Role.ORGANIZER, actor=admin)

        judge_a = EventRole.objects.get(public_id=demo.JUDGE_A_ID).user
        judge_b = EventRole.objects.get(public_id=demo.JUDGE_B_ID).user
        participant = (
            Team.objects.get(public_id=demo.PARTICIPANT_TEAM_ID)
            .memberships.get(is_captain=True).user
        )
        for user, key in ((judge_a, "judge_a"), (judge_b, "judge_b"), (participant, "participant"),
                          (organizer, "organizer"), (admin, "admin")):
            user.set_password(demo.PASSWORDS[key])
            user.email_verified_at = clock.now()  # demo accounts can vote without a mailbox
            user.save(update_fields=["password", "email_verified_at"])

        for key, user in (("organizer", organizer), ("judge_a", judge_a), ("judge_b", judge_b),
                          ("participant", participant), ("admin", admin)):
            ApiToken.issue(user, "demo token (printed at boot)", raw=demo.TOKENS[key])

        # Top every project up to the event's review target. The fixture's unfinished
        # batches become pending assignments on the judges' queues and on the dashboard.
        plan = assignment_service.auto_assign(event, actor=organizer)
        self.stdout.write(f"assigned {len(plan.created)} pending reviews to reach "
                          f"{event.reviews_per_project} per project")

        # Automatic assignment gives the top-ups to the least-loaded judges, and judge A
        # (11 reviews) is the most loaded. Hand them two more, in their own tracks, so
        # the demo scorecard has something to score. Recorded like any manual assignment.
        judge_a_role = EventRole.objects.get(public_id=demo.JUDGE_A_ID)
        candidates = (
            Project.objects.canonical().filter(event=event, track__in=judge_a_role.tracks.all())
            .exclude(assignments__judge=judge_a_role).order_by("public_id")[:2]
        )
        for project in candidates:
            assignment_service.assign(event, judge_a_role, project, actor=organizer)

        self._seed_open_event(organizer, participant)

    def _seed_open_event(self, organizer, participant):
        """A second, live event, so every flow that the closed fixture event refuses
        (forming teams, submitting, voting) can be tried in the demo."""
        now = clock.now().replace(second=0, microsecond=0)
        event = Event.objects.create(
            slug="open-jam", name="Open Jam 2026", is_listed=True,
            tagline="A live event for trying everything the fixture event has already closed.",
            description="Submissions and community voting are open. Form a team, submit, "
                        "comment and vote. Created by the demo seed; not part of fixtures.json.",
            submissions_open_at=now - timedelta(days=1),
            submissions_close_at=now + timedelta(days=5),
            voting_mode=Event.VotingMode.AUTHENTICATED,
            voting_open_at=now - timedelta(hours=12),
            voting_close_at=now + timedelta(days=6),
            votes_per_voter=3,
            created_by=organizer,
        )
        event_services.grant_role(event, organizer, EventRole.Role.ORGANIZER, actor=organizer)
        tracks = [event.tracks.create(name=n, position=i) for i, n in
                  enumerate(["Tools for organizers", "Community", "Wildcard"])]
        from judging.models import Criterion

        for pos, (key, name, weight) in enumerate(
            [("impact", "Impact", 3), ("craft", "Craft", 2), ("delight", "Delight", 1)]
        ):
            Criterion.objects.create(event=event, key=key, name=name, weight=weight, position=pos)
        Prize.objects.create(event=event, name="People's choice", value="Banana hat", position=0,
                             source=Prize.Source.COMMUNITY, description="Most community votes.")

        seeded = [
            ("tm_02", "Quorum", "Ballots that count themselves, with receipts.", 0),
            ("tm_03", "Lanyard", "Badge printing that knows who is on which team.", 1),
            ("tm_04", "Night Shift", "A status page for the organizers' own infrastructure.", 0),
            ("tm_05", "Pitch Clock", "Keeps demo slots honest and on schedule.", 2),
        ]
        for fixture_team, title, summary, track in seeded:
            members = Team.objects.get(public_id=fixture_team).memberships.select_related("user")
            captain = members[0].user
            team = event_services.create_team(event, captain, f"{title} team")
            for m in members[1:]:
                TeamMember.objects.create(team=team, user=m.user)
                EventRole.objects.get_or_create(event=event, user=m.user, role=EventRole.Role.PARTICIPANT)
            event_services.create_project(
                event, captain,
                event_services.ProjectInput(title=title, summary=summary, track=tracks[track],
                                            repo_url="https://example.org/repo/" + title.lower().replace(" ", "-")),
                submit=True,
            )
        team = event_services.create_team(event, participant, "NorthKiln")
        event_services.create_project(
            event, participant,
            event_services.ProjectInput(title="Glass Signal, the sequel",
                                        summary="Work in progress: still a draft.", track=tracks[0]),
        )
        count = Project.objects.filter(event=event).count()
        self.stdout.write(f"created demo event {event.name} with {count} projects")
        return team

    def _user(self, email, name, password, staff=False):
        user = User.objects.filter(email=email).first()
        if user is None:
            if staff:
                user = User.objects.create_superuser(email=email, name=name, password=password)
            else:
                user = User.objects.create_user(email=email, name=name, password=password)
        return user

    def _print_credentials(self):
        lines = [
            "",
            "seeded. test logins:",
        ]
        width = max(len(k) for k in demo.TOKENS)
        for key in ("organizer", "judge_a", "judge_b", "participant", "admin"):
            lines.append(f"  {key.ljust(width)}  Authorization: Bearer {demo.TOKENS[key]}")
        lines.append("")
        lines.append("  web sign-in (or use the one-click demo panel on /accounts/login/):")
        for account in demo.accounts():
            lines.append(f"  {account['key'].ljust(width)}  {account['email']} / {demo.PASSWORDS[account['key']]}")
        lines.append("")
        lines.append(f"  portal   {settings.SITE_URL}")
        lines.append("  mail     http://localhost:8025 (Mailpit)")
        lines.append("")
        self.stdout.write("\n".join(lines))
