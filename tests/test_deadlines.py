"""The deadline holds, from every door."""

from datetime import timedelta

import pytest

from core.errors import Forbidden
from events import services
from events.models import Project


def test_closed_fixture_event_refuses_api_submission(api):
    response = api("participant").post("/api/v1/events/evt_01/projects",
                                       {"title": "late", "summary": "too late"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "submissions_closed"


def test_closed_event_refuses_edit_of_existing_project(api):
    response = api("participant").patch("/api/v1/projects/prj_01", {"title": "Renamed after close"})
    assert response.status_code == 403
    assert Project.objects.get(public_id="prj_01").title == "Glass Signal"


def test_closed_event_refuses_html_edit(web, participant):
    client = web(participant)
    response = client.post("/events/sample-hack-2026/submission/", {"title": "Changed", "summary": "x"})
    assert response.status_code == 302
    assert Project.objects.get(public_id="prj_01").title == "Glass Signal"


def test_deadline_is_checked_before_anything_else(api, event):
    """Even a request that is wrong in other ways is refused as late."""
    response = api("participant").post("/api/v1/events/evt_01/projects", {"track": "trk_99"})
    assert response.json()["error"]["code"] == "submissions_closed"


def test_open_event_accepts_then_refuses_at_the_minute(open_event, participant, clock):
    project = open_event.projects.get(team__memberships__user=participant)
    services.update_project(project, participant, services.ProjectInput(summary="Still editing."))
    clock.set(open_event.submissions_close_at - timedelta(seconds=1))
    services.update_project(project, participant, services.ProjectInput(summary="One second left."))
    clock.set(open_event.submissions_close_at)
    with pytest.raises(services.SubmissionsClosed):
        services.update_project(project, participant, services.ProjectInput(summary="Too late."))
    project.refresh_from_db()
    assert project.summary == "One second left."


def test_submission_before_opening_is_refused(open_event, participant, clock):
    clock.set(open_event.submissions_open_at - timedelta(minutes=1))
    project = open_event.projects.get(team__memberships__user=participant)
    with pytest.raises(services.SubmissionsNotOpen):
        services.update_project(project, participant, services.ProjectInput(summary="early"))


def test_extension_applies_to_one_team_only(event, organizer, participant, clock):
    from events.models import Team

    team = Team.objects.get(public_id="tm_01")
    other = Team.objects.get(public_id="tm_02").memberships.first().user
    clock.set(event.submissions_close_at + timedelta(hours=1))
    services.grant_extension(event, team, event.submissions_close_at + timedelta(hours=2), "power cut",
                             actor=organizer)
    project = Project.objects.get(public_id="prj_01")
    services.update_project(project, participant, services.ProjectInput(summary="Saved by the extension."))
    with pytest.raises(services.SubmissionsClosed):
        services.update_project(Project.objects.get(public_id="prj_02"), other,
                                services.ProjectInput(summary="nope"))
    clock.shift(hours=2)
    with pytest.raises(services.SubmissionsClosed):
        services.update_project(project, participant, services.ProjectInput(summary="after extension"))


def test_team_cannot_form_after_deadline(event, stranger):
    with pytest.raises(services.SubmissionsClosed):
        services.create_team(event, stranger, "Latecomers")


def test_team_flow_on_open_event(open_event, stranger, db):
    from accounts.models import User

    team = services.create_team(open_event, stranger, "Fresh Team")
    friend = User.objects.create_user(email="friend@example.net", name="Friend")
    services.join_team(team.invite_code, friend)
    assert team.memberships.count() == 2
    old_code = team.invite_code
    services.rotate_invite(team, stranger)
    other = User.objects.create_user(email="other@example.net", name="Other")
    with pytest.raises(Exception, match="not valid"):
        services.join_team(old_code, other)
    project = services.create_project(open_event, stranger, services.ProjectInput(
        title="Brand new", summary="Built today", track=open_event.tracks.first()), submit=True)
    assert project.status == Project.Status.SUBMITTED
    with pytest.raises(Exception, match="already has a project"):
        services.create_project(open_event, friend, services.ProjectInput(title="Second"))


def test_team_size_limit(open_event, stranger):
    from accounts.models import User

    open_event.max_team_size = 2
    open_event.save()
    team = services.create_team(open_event, stranger, "Pair")
    services.join_team(team.invite_code, User.objects.create_user(email="a@example.net", name="A"))
    with pytest.raises(Exception, match="full"):
        services.join_team(team.invite_code, User.objects.create_user(email="b@example.net", name="B"))


def test_judges_cannot_join_teams_in_their_event(event, judge_a, clock):
    from events.models import Team

    clock.set(event.submissions_open_at + timedelta(hours=1))
    with pytest.raises(Forbidden):
        services.join_team(Team.objects.get(public_id="tm_02").invite_code, judge_a)


def test_draft_projects_are_private(web, open_event, stranger):
    draft = open_event.projects.get(status="draft")
    assert web(stranger).get(f"/projects/{draft.public_id}/").status_code == 404
    assert web().get("/projects/").content.decode().count(draft.title) == 0


def test_organizer_can_withdraw_after_deadline(event, organizer):
    project = Project.objects.get(public_id="prj_05")
    services.withdraw_project(project, organizer)
    assert Project.objects.get(public_id="prj_05").status == Project.Status.WITHDRAWN
