"""One full event lifecycle through the HTML pages, the way DEMO.md walks it:
create, form a team, submit, invite a judge, close, assign, score, publish."""

import re
from datetime import timedelta

from core import clock
from events.models import Event, Project
from judging.models import Assignment
from records.models import SignedRecord


def _dt(value):
    return value.strftime("%Y-%m-%dT%H:%M")


def test_create_submit_judge_publish(web, organizer, participant, judge_a, mailoutbox):
    now = clock.now()
    org, team_user, judge = web(organizer), web(participant), web(judge_a)

    # Create
    response = org.post("/manage/new/", {
        "name": "Demo Day", "slug": "demo-day", "tagline": "",
        "submissions_open_at": _dt(now - timedelta(hours=1)),
        "submissions_close_at": _dt(now + timedelta(hours=1)),
        "max_team_size": 4, "reviews_per_project": 1, "normalization": "judge_effect",
        "voting_mode": "disabled", "voting_open_at": "", "voting_close_at": "",
    })
    assert response.status_code == 302, response.content
    event = Event.objects.get(slug="demo-day")
    assert event.criteria.count() == 3
    org.post("/manage/demo-day/tracks/", {"track-name": "Tools", "track-description": ""})
    org.post("/manage/demo-day/prizes/", {"prize-name": "Best in show", "prize-value": "a hat",
                                          "prize-track": "", "prize-source": "judged", "prize-description": ""})
    settings_form = {f"event-{k}": v for k, v in {
        "name": "Demo Day", "slug": "demo-day", "tagline": "", "description": "", "is_listed": "on",
        "submissions_open_at": _dt(now - timedelta(hours=1)), "submissions_close_at": _dt(now + timedelta(hours=1)),
        "judging_close_at": "", "max_team_size": 4, "reviews_per_project": 1, "judge_max_load": "",
        "score_min": 1, "score_max": 5, "normalization": "judge_effect", "voting_mode": "disabled",
        "voting_open_at": "", "voting_close_at": "", "votes_per_voter": 3}.items()}
    assert org.post("/manage/demo-day/", settings_form).status_code == 302
    event.refresh_from_db()
    assert event.is_listed

    # Team and submission
    team_user.post("/events/demo-day/team/", {"name": "Night Owls"})
    track = event.tracks.get(name="Tools")
    response = team_user.post("/events/demo-day/submission/", {
        "title": "Owl Lamp", "track": track.pk, "summary": "A lamp for night shifts.", "submit": "1"})
    assert response.status_code == 302
    project = Project.objects.get(event=event)
    assert project.status == Project.Status.SUBMITTED
    assert b"Owl Lamp" in web().get("/projects/?event=demo-day").content

    # Invite a judge; the judge accepts from the emailed link
    org.post("/manage/demo-day/judges/", {"action": "invite", "email": judge_a.email, "name": "Diego"})
    link = re.search(r"/invites/[^\s/]+/", mailoutbox[-1].body).group(0)
    assert judge.post(link).status_code == 302
    assert event.roles.filter(user=judge_a, role="judge").exists()

    # Close the deadline; the team is locked out
    settings_form["event-submissions_close_at"] = _dt(now - timedelta(minutes=1))
    settings_form["event-submissions_open_at"] = _dt(now - timedelta(hours=2))
    assert org.post("/manage/demo-day/", settings_form).status_code == 302
    team_user.post("/events/demo-day/submission/", {"title": "Changed", "track": track.pk, "summary": "x"})
    assert Project.objects.get(pk=project.pk).title == "Owl Lamp"

    # Assign and score
    org.post("/manage/demo-day/assignments/", {"action": "run", "target": "1"})
    assignment = Assignment.objects.get(event=event)
    judge.post(f"/judge/demo-day/review/{assignment.public_id}/",
               {"c_functionality": "5", "c_quality": "4", "c_innovation": "4", "comment": "bright"})
    assignment.refresh_from_db()
    assert assignment.status == Assignment.Status.DONE

    # Publish
    org.post("/manage/demo-day/results/", {"action": "publish", "method": "judge_effect"})
    event.refresh_from_db()
    assert event.results_published_at
    assert event.prizes.get().winner == project
    page = web().get("/events/demo-day/").content.decode()
    assert "Owl Lamp" in page and "signed record" in page
    assert SignedRecord.objects.filter(event=event, kind="results").exists()
