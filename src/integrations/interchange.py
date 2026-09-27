"""Import and export in the DOGFOOD fixture format.

The fixture format is the one data shape every DOGFOOD portal already reads, which
makes it the natural migration path in and out: an event exported from here loads into
any portal that loads fixtures.json, and vice versa. We use it for seeding, for bulk
import, and for export.

Our exports are a strict superset: the fixture keys are always present with the
fixture's meaning, and anything extra (rubric weights, prizes, unscored assignments)
lives under keys a fixture reader will ignore. Import accepts the same superset, so a
round trip through this module loses nothing. tests/test_interchange.py proves that
against the real fixtures.json.

Import runs in one transaction. It either loads everything or nothing, and it reports
every problem it found rather than stopping at the first.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from accounts.models import User
from audit import services as audit
from core import clock
from core.errors import DomainError
from events.models import Event, EventRole, Prize, Project, Team, TeamMember, Track
from judging.models import Assignment, Criterion, Score, ScoreValue

FORMAT_VERSION = 1
DEFAULT_HACK_LENGTH = timedelta(hours=72)


class InterchangeError(DomainError):
    status = 400
    code = "invalid_import"

    def __init__(self, problems):
        self.problems = problems
        super().__init__(f"{len(problems)} problem(s) in the import: " + "; ".join(problems[:5]))


@dataclass
class ImportReport:
    event: Event | None = None
    counts: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def as_dict(self):
        return {
            "event": self.event.public_id if self.event else None,
            "counts": self.counts,
            "warnings": self.warnings,
        }


def _ts(value, path, problems):
    if value in (None, ""):
        return None
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None or parsed.tzinfo is None:
        problems.append(f"{path}: {value!r} is not an ISO 8601 timestamp with a timezone")
        return None
    return parsed


def _claim(model, wanted):
    """The public id for an imported row: the file's own id if nobody has it yet.

    Fixture ids are only unique within one file ("prj_01" exists in every DOGFOOD
    export), so a second import gets fresh ids. The original is always kept in
    external_id, which is what exports write, so the round trip stays lossless.
    """
    if wanted and not model.objects.filter(public_id=wanted).exists():
        return wanted
    return ""


def _name_from_email(email):
    local = email.split("@")[0]
    return " ".join(part.capitalize() for part in local.replace(".", " ").replace("_", " ").split())


def _unique_slug(name):
    base = slugify(name)[:70] or "event"
    slug, n = base, 2
    while Event.objects.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def validate(data) -> list[str]:
    """Structural checks that need no database. Returns a list of problems."""
    problems = []
    if not isinstance(data, dict):
        return ["the document must be a JSON object"]
    event = data.get("event")
    if not isinstance(event, dict) or not event.get("id") or not event.get("name"):
        problems.append("event: needs at least an id and a name")
    elif not event.get("submissions_close"):
        problems.append("event.submissions_close: required")
    for key in ("tracks", "judges", "teams", "projects", "scores"):
        if key in data and not isinstance(data[key], list):
            problems.append(f"{key}: must be a list")
    if problems:
        return problems

    def ids(key):
        seen, dupes = set(), set()
        for i, row in enumerate(data.get(key, [])):
            rid = row.get("id") if isinstance(row, dict) else None
            if not isinstance(rid, str) or not rid:
                problems.append(f"{key}[{i}].id: must be a non-empty string")
                continue
            (dupes if rid in seen else seen).add(rid)
        for rid in sorted(dupes):
            problems.append(f"{key}: id {rid!r} appears more than once")
        return seen

    track_ids, judge_ids, team_ids, project_ids = (
        ids("tracks"), ids("judges"), ids("teams"), ids("projects")
    )
    for i, j in enumerate(data.get("judges", [])):
        if not j.get("email"):
            problems.append(f"judges[{i}].email: required")
        for t in j.get("tracks", []):
            if t not in track_ids:
                problems.append(f"judges[{i}].tracks: unknown track {t!r}")
    for i, p in enumerate(data.get("projects", [])):
        if p.get("team") not in team_ids:
            problems.append(f"projects[{i}].team: unknown team {p.get('team')!r}")
        if p.get("track") and p["track"] not in track_ids:
            problems.append(f"projects[{i}].track: unknown track {p['track']!r}")
        if not p.get("title"):
            problems.append(f"projects[{i}].title: required")
    for i, s in enumerate(data.get("scores", [])):
        if s.get("judge") not in judge_ids:
            problems.append(f"scores[{i}].judge: unknown judge {s.get('judge')!r}")
        if s.get("project") not in project_ids:
            problems.append(f"scores[{i}].project: unknown project {s.get('project')!r}")
        if not isinstance(s.get("criteria"), dict) or not s["criteria"]:
            problems.append(f"scores[{i}].criteria: must be a non-empty object")
    return problems


@transaction.atomic
def import_event(data, *, actor=None, listed=False) -> ImportReport:
    problems = validate(data)
    if problems:
        raise InterchangeError(problems)

    report = ImportReport()
    warnings = report.warnings
    ev = data["event"]

    close = _ts(ev["submissions_close"], "event.submissions_close", problems)
    opens = _ts(ev.get("submissions_open"), "event.submissions_open", problems)
    submitted = [
        _ts(p.get("submitted_at"), f"projects[{i}].submitted_at", problems)
        for i, p in enumerate(data.get("projects", []))
    ]
    if problems:
        raise InterchangeError(problems)
    if opens is None:
        # The fixture has no opening time. A hackathon that closed at T opened at T - 72h
        # unless a submission proves it was open earlier.
        earliest = min((s for s in submitted if s), default=close)
        opens = min(close - DEFAULT_HACK_LENGTH, earliest - timedelta(minutes=1))

    scale = ev.get("score_scale") or {}
    event = Event.objects.create(
        public_id=_claim(Event, ev["id"]), external_id=ev["id"],
        slug=_unique_slug(ev.get("slug") or ev["name"]),
        name=ev["name"],
        tagline=ev.get("tagline", ""),
        description=ev.get("description", ""),
        submissions_open_at=opens,
        submissions_close_at=close,
        judging_close_at=_ts(ev.get("judging_close"), "event.judging_close", problems),
        score_min=scale.get("min", 1),
        score_max=scale.get("max", 5),
        reviews_per_project=ev.get("reviews_per_project", 3),
        is_listed=listed,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    report.event = event

    tracks = {}
    for pos, t in enumerate(data.get("tracks", [])):
        tracks[t["id"]] = Track.objects.create(
            event=event, public_id=_claim(Track, t["id"]), external_id=t["id"], name=t["name"],
            description=t.get("description", ""), position=pos,
        )

    # The rubric: explicit if the document has one, otherwise inferred from the
    # criteria the scores actually use, in the order they first appear.
    criteria = {}
    declared = data.get("criteria")
    if declared:
        for pos, c in enumerate(declared):
            criteria[c["key"]] = Criterion.objects.create(
                event=event, key=c["key"], name=c.get("name") or c["key"].replace("_", " ").title(),
                description=c.get("description", ""), weight=Decimal(str(c.get("weight", 1))),
                position=pos,
            )
    else:
        for s in data.get("scores", []):
            for key in s["criteria"]:
                if key not in criteria:
                    criteria[key] = Criterion.objects.create(
                        event=event, key=key, name=key.replace("_", " ").title(),
                        position=len(criteria),
                    )

    users = {}

    def user_for(email, name=""):
        email = User.objects.normalize_email(email)
        if email not in users:
            user = User.objects.filter(email=email).first()
            if user is None:
                user = User.objects.create_user(email=email, name=name or _name_from_email(email))
            users[email] = user
        return users[email]

    judges = {}
    for j in data.get("judges", []):
        role = EventRole.objects.create(
            event=event, user=user_for(j["email"], j.get("name", "")),
            role=EventRole.Role.JUDGE, public_id=_claim(EventRole, j["id"]), external_id=j["id"],
        )
        role.tracks.set([tracks[t] for t in j.get("tracks", [])])
        judges[j["id"]] = role
    judge_users = {r.user_id for r in judges.values()}

    teams, team_users = {}, {}
    names = {}
    for t in data.get("teams", []):
        names.setdefault(t["name"].strip().lower(), []).append(t["id"])
    for ids_with_name in names.values():
        if len(ids_with_name) > 1:
            warnings.append(f"teams {', '.join(ids_with_name)} share a name; "
                            "kept as different teams, told apart by id")
    for t in data.get("teams", []):
        team = Team.objects.create(event=event, public_id=_claim(Team, t["id"]), external_id=t["id"],
                                   name=t["name"])
        teams[t["id"]] = team
        team_users[t["id"]] = set()
        for pos, email in enumerate(t.get("members", [])):
            member = user_for(email)
            if member.pk in judge_users:
                warnings.append(f"team {t['id']}: {email} is a judge of this event; not added as a member")
                continue
            if TeamMember.objects.filter(event=event, user=member).exists():
                warnings.append(f"team {t['id']}: {email} is already on another team; skipped")
                continue
            TeamMember.objects.create(team=team, user=member, is_captain=pos == 0)
            EventRole.objects.get_or_create(event=event, user=member, role=EventRole.Role.PARTICIPANT)
            team_users[t["id"]].add(member.pk)

    projects = {}
    live_by_team = {}
    repos = {}
    for p, submitted_at in zip(data.get("projects", []), submitted, strict=True):
        team = teams[p["team"]]
        status = p.get("status") or (Project.Status.SUBMITTED if submitted_at else Project.Status.DRAFT)
        duplicate_of = None
        if p.get("duplicate_of") in projects:
            duplicate_of = projects[p["duplicate_of"]]
        elif status != Project.Status.WITHDRAWN and team.pk in live_by_team:
            duplicate_of = live_by_team[team.pk]
            warnings.append(
                f"project {p['id']} ({p['title']!r}) is a second live project from team "
                f"{p['team']}; kept and flagged as a duplicate of {duplicate_of.public_id}, "
                "excluded from results and the gallery"
            )
        project = Project.objects.create(
            event=event, public_id=_claim(Project, p["id"]), external_id=p["id"], team=team,
            track=tracks.get(p.get("track")), title=p["title"],
            summary=(p.get("summary") or "")[:300], description=p.get("description", ""),
            repo_url=p.get("repo_url", ""), demo_url=p.get("demo_url", ""),
            video_url=p.get("video_url", ""), status=status, submitted_at=submitted_at,
            duplicate_of=duplicate_of,
        )
        projects[p["id"]] = project
        if duplicate_of is None and status != Project.Status.WITHDRAWN:
            live_by_team[team.pk] = project
        repo = (project.repo_url or "").rstrip("/").lower()
        if repo and repo in repos and repos[repo].team_id != project.team_id:
            warnings.append(
                f"project {p['id']} has the same repository as {repos[repo].public_id} "
                "from a different team; worth a look"
            )
        repos.setdefault(repo, project)

    scored = 0
    for i, s in enumerate(data.get("scores", [])):
        judge, project = judges[s["judge"]], projects[s["project"]]
        if judge.user_id in team_users.get(project.team.public_id, set()):
            warnings.append(f"scores[{i}]: {s['judge']} is on the team of {s['project']}; skipped")
            continue
        unknown = set(s["criteria"]) - set(criteria)
        missing = set(criteria) - set(s["criteria"])
        if unknown or missing:
            warnings.append(
                f"scores[{i}] ({s['judge']} on {s['project']}): criteria do not match the rubric "
                f"(unknown {sorted(unknown)}, missing {sorted(missing)}); skipped"
            )
            continue
        values = s["criteria"]
        bad = {k: v for k, v in values.items()
               if not isinstance(v, int) or not event.score_min <= v <= event.score_max}
        if bad:
            warnings.append(
                f"scores[{i}]: values {bad} are outside {event.score_min}..{event.score_max}; skipped"
            )
            continue
        if judge.tracks.exists() and project.track_id and not judge.tracks.filter(pk=project.track_id).exists():
            warnings.append(f"scores[{i}]: {s['judge']} scored {s['project']} outside their tracks")
        assignment, created = Assignment.objects.get_or_create(
            judge=judge, project=project,
            defaults={"event": event, "source": Assignment.Source.IMPORT,
                      "status": Assignment.Status.DONE},
        )
        if not created and hasattr(assignment, "score"):
            warnings.append(f"scores[{i}]: second score by {s['judge']} for {s['project']}; skipped")
            continue
        assignment.status = Assignment.Status.DONE
        assignment.save(update_fields=["status"])
        at = _ts(s.get("submitted_at"), f"scores[{i}].submitted_at", problems) or clock.now()
        score = Score.objects.create(
            assignment=assignment, comment=s.get("comment") or "", submitted_at=at, updated_at=at,
        )
        ScoreValue.objects.bulk_create(
            ScoreValue(score=score, criterion=criteria[k], value=v) for k, v in values.items()
        )
        scored += 1

    for a in data.get("assignments", []):
        judge, project = judges.get(a.get("judge")), projects.get(a.get("project"))
        if judge and project:
            Assignment.objects.get_or_create(
                judge=judge, project=project,
                defaults={"event": event, "source": Assignment.Source.IMPORT},
            )

    for pos, pz in enumerate(data.get("prizes", [])):
        Prize.objects.create(
            event=event, name=pz["name"], description=pz.get("description", ""),
            value=pz.get("value", ""), track=tracks.get(pz.get("track")), position=pos,
            source=pz.get("source") if pz.get("source") in Prize.Source.values else Prize.Source.JUDGED,
        )

    if problems:
        raise InterchangeError(problems)

    report.counts = {
        "tracks": len(tracks), "criteria": len(criteria), "judges": len(judges),
        "teams": len(teams), "projects": len(projects), "scores": scored,
        "users_created": sum(1 for u in users.values() if not u.has_usable_password()),
        "duplicates_flagged": sum(1 for p in projects.values() if p.duplicate_of_id),
    }
    audit.record(
        "event.imported",
        f"Imported {event.name}: {len(projects)} projects, {len(judges)} judges, {scored} scores",
        actor=actor, event=event, target=event,
        data={"counts": report.counts, "warnings": warnings}, source="import",
    )
    return report


def _iso(dt: datetime | None):
    """UTC with a Z, the way the fixture writes timestamps."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def export_event(event: Event) -> dict:
    """The event in fixture format, plus the extras that make re-import lossless."""
    tracks = list(event.tracks.all())
    judges = (
        EventRole.objects.judges().filter(event=event).select_related("user")
        .prefetch_related("tracks").order_by("external_id", "public_id")
    )
    teams = event.teams.prefetch_related("memberships__user").order_by("external_id", "public_id")
    projects = event.projects.select_related("team", "track", "duplicate_of").order_by("external_id", "public_id")
    scores = (
        Score.objects.filter(assignment__event=event, voided_at__isnull=True)
        .select_related("assignment__judge", "assignment__project")
        .prefetch_related("values__criterion")
        .order_by("assignment__project__external_id", "assignment__project__public_id",
                  "assignment__judge__external_id", "assignment__judge__public_id")
    )
    unscored = (
        Assignment.objects.filter(event=event, score__isnull=True)
        .exclude(status=Assignment.Status.RECUSED)
        .select_related("judge", "project")
        .order_by("project__external_id", "project__public_id", "judge__external_id", "judge__public_id")
    )

    z = _iso
    return {
        "event": {
            "id": event.interchange_id,
            "name": event.name,
            "submissions_close": z(event.submissions_close_at),
            "submissions_open": z(event.submissions_open_at),
            "judging_close": z(event.judging_close_at),
            "slug": event.slug,
            "tagline": event.tagline,
            "description": event.description,
            "score_scale": {"min": event.score_min, "max": event.score_max},
            "reviews_per_project": event.reviews_per_project,
        },
        "tracks": [{"id": t.interchange_id, "name": t.name} for t in tracks],
        "judges": [
            {"id": j.interchange_id, "name": j.user.name, "email": j.user.email,
             "tracks": [t.interchange_id for t in j.tracks.all()]}
            for j in judges
        ],
        "teams": [
            {"id": t.interchange_id, "name": t.name,
             "members": [m.user.email for m in t.memberships.all()]}
            for t in teams
        ],
        "projects": [
            {
                "id": p.interchange_id, "team": p.team.interchange_id,
                "track": p.track.interchange_id if p.track else None,
                "title": p.title, "summary": p.summary, "repo_url": p.repo_url,
                "submitted_at": z(p.submitted_at),
                **({"status": p.status} if p.status != Project.Status.SUBMITTED else {}),
                **({"duplicate_of": p.duplicate_of.interchange_id} if p.duplicate_of else {}),
                **({"description": p.description} if p.description else {}),
                **({"demo_url": p.demo_url} if p.demo_url else {}),
                **({"video_url": p.video_url} if p.video_url else {}),
            }
            for p in projects
        ],
        "scores": [
            {
                "judge": s.assignment.judge.interchange_id,
                "project": s.assignment.project.interchange_id,
                "criteria": {v.criterion.key: v.value for v in s.values.all()},
                "comment": s.comment,
            }
            for s in scores
        ],
        "criteria": [
            {"key": c.key, "name": c.name, "weight": float(c.weight), "description": c.description}
            for c in event.criteria.all()
        ],
        "prizes": [
            {"name": pz.name, "value": pz.value, "description": pz.description, "source": pz.source,
             "track": pz.track.interchange_id if pz.track else None}
            for pz in event.prizes.select_related("track")
        ],
        "assignments": [
            {"judge": a.judge.interchange_id, "project": a.project.interchange_id} for a in unscored
        ],
        "format": {"name": "dogfood-fixtures", "version": FORMAT_VERSION, "producer": "bananahat"},
    }
