"""Assignment, scoring, results and publishing."""

from collections import Counter

import pytest

from core.errors import Conflict, DomainError
from judging import assignment, results, scoring
from judging.models import Assignment, ResultSnapshot, Score
from records.models import SignedRecord


def test_every_project_reached_the_target_after_seeding(event):
    active = Assignment.objects.active().filter(event=event, project__duplicate_of__isnull=True)
    per_project = Counter(active.values_list("project_id", flat=True))
    assert min(per_project.values()) >= event.reviews_per_project


def test_auto_assignment_respects_tracks_and_conflicts(event):
    for a in Assignment.objects.filter(event=event, source="auto").select_related("judge", "project"):
        tracks = set(a.judge.tracks.values_list("pk", flat=True))
        assert not tracks or a.project.track_id in tracks


def test_plan_is_deterministic(event):
    event.reviews_per_project = 5
    first = assignment.plan_assignments(event, seed=42)
    second = assignment.plan_assignments(event, seed=42)
    assert [(j.pk, p.pk) for j, p in first.created] == [(j.pk, p.pk) for j, p in second.created]


def test_shortfall_is_reported_not_hidden(event):
    plan = assignment.plan_assignments(event, target=12)
    assert plan.shortfalls
    assert "judge" in plan.shortfalls[0].reason


def test_load_is_balanced(event):
    event.reviews_per_project = 5
    plan = assignment.plan_assignments(event, seed=1)
    loads = Counter(j.pk for j, _ in plan.created)
    assert max(loads.values()) - min(loads.values()) <= 4


def test_recusal_voids_score_and_blocks_reassignment(event, judge_a):
    a = Assignment.objects.filter(judge__user=judge_a, status="done").first()
    assignment.recuse(a, actor=judge_a, reason="I mentor this team")
    a.refresh_from_db()
    assert a.status == Assignment.Status.RECUSED
    assert Score.objects.get(assignment=a).voided_at is not None
    plan = assignment.plan_assignments(event, target=6)
    assert all(not (j.pk == a.judge_id and p.pk == a.project_id) for j, p in plan.created)


def test_scores_must_be_complete_and_in_range(judge_a):
    a = Assignment.objects.filter(judge__user=judge_a, status="pending").first() or \
        Assignment.objects.filter(judge__user=judge_a).first()
    with pytest.raises(DomainError, match="Missing"):
        scoring.submit_score(a, judge_a, {"functionality": 3})
    with pytest.raises(DomainError, match="from 1 to 5"):
        scoring.submit_score(a, judge_a, {"functionality": 6, "quality": 3, "innovation": 3})
    with pytest.raises(DomainError, match="Unknown"):
        scoring.submit_score(a, judge_a, {"functionality": 3, "quality": 3, "innovation": 3, "vibes": 5})


def test_judge_scores_over_the_api(api, judge_a):
    a = Assignment.objects.filter(judge__user=judge_a).first()
    response = api("judge_a").put(f"/api/v1/assignments/{a.public_id}/score",
                                  {"values": {"functionality": 5, "quality": 4, "innovation": 3}, "comment": "ok"})
    assert response.status_code == 200, response.content
    assert response.json()["values"] == {"functionality": 5, "quality": 4, "innovation": 3}


def test_weights_change_the_ranking_without_rescoring(event, organizer):
    from judging.rubric import replace_rubric

    before = results.compute(event, "raw").rows[0].project.public_id
    replace_rubric(event, organizer, [
        {"key": "functionality", "name": "Functionality", "weight": 1},
        {"key": "quality", "name": "Quality", "weight": 1},
        {"key": "innovation", "name": "Innovation", "weight": 20},
    ])
    after = results.compute(event, "raw").rows[0].project.public_id
    assert before != after


def test_rubric_structure_locked_after_scoring(event, organizer):
    from judging.rubric import replace_rubric

    with pytest.raises(Conflict):
        replace_rubric(event, organizer, [{"key": "functionality", "weight": 1}])


def test_every_method_ranks_all_competing_projects(event):
    for method in ("raw", "zscore", "judge_effect", "implied_bt"):
        r = results.compute(event, method)
        assert len(r.rows) == 40, method
        assert {x["project"] for x in r.excluded} == {"prj_41"}


def test_publish_freezes_signs_and_locks(event, organizer, judge_a, api):
    snapshot = results.publish(event, organizer)
    event.refresh_from_db()
    assert event.results_published_at
    assert snapshot.payload["overall"][0]["rank"] == 1
    assert SignedRecord.objects.filter(event=event, kind="results").exists()
    judge_records = SignedRecord.objects.filter(event=event, kind="judge_participation")
    assert judge_records.filter(subject=judge_a).exists()
    assert event.prizes.get(name="Grand prize").winner.public_id == snapshot.payload["overall"][0]["project"]
    a = Assignment.objects.filter(judge__user=judge_a).first()
    with pytest.raises(scoring.JudgingClosed):
        scoring.submit_score(a, judge_a, {"functionality": 1, "quality": 1, "innovation": 1})
    public = api().get("/api/v1/events/evt_01/results").json()
    assert public["overall"] == snapshot.payload["overall"]
    assert ResultSnapshot.objects.filter(event=event).count() == 1


def test_publish_refused_while_voting_is_open(open_event, organizer, clock):
    clock.set(open_event.submissions_close_at)
    with pytest.raises(Conflict, match="voting"):
        results.publish(open_event, organizer)


def test_community_prize_goes_to_the_vote_winner(open_event, organizer, stranger, clock):
    from datetime import timedelta

    from voting import services as voting

    favourite = open_event.projects.canonical().order_by("-title").first()
    ballot = voting.ballot_for_user(open_event, stranger, {"ip": "203.0.113.9", "agent": "t"})
    voting.cast(ballot, [favourite.public_id], voter=stranger)
    clock.set(open_event.voting_close_at + timedelta(minutes=1))
    snapshot = results.publish(open_event, organizer)
    prize = next(p for p in snapshot.payload["prizes"] if p["name"] == "People's choice")
    assert prize["source"] == "community" and prize["winner"] == favourite.public_id
    assert snapshot.payload["community"][0]["project"] == favourite.public_id


def test_participant_certificate_after_publish(event, organizer, participant, web):
    results.publish(event, organizer)
    response = web(participant).get("/certificates/sample-hack-2026/")
    assert response.status_code == 302
    page = web(participant).get(response["Location"])
    assert b"Certificate of participation" in page.content


def test_judge_scoring_page_flow(web, judge_a, event):
    a = Assignment.objects.filter(judge__user=judge_a, status="pending").first() or \
        Assignment.objects.filter(judge__user=judge_a).first()
    client = web(judge_a)
    url = f"/judge/{event.slug}/review/{a.public_id}/"
    assert client.get(url).status_code == 200
    response = client.post(url, {"c_functionality": "4", "c_quality": "4", "c_innovation": "5", "comment": "fine"})
    assert response.status_code == 302
    assert Score.objects.get(assignment=a).as_dict() == {"functionality": 4, "quality": 4, "innovation": 5}


def test_bulk_judge_invitations_from_a_roster(api, event, mailoutbox):
    roster = ("email,name,tracks\n"
              "new.judge@example.net,New Judge,Developer tools;trk_04\n"
              "diego.herrera@example.org,Diego,\n")  # already a judge: skipped
    response = api("organizer").post("/api/v1/events/evt_01/judges/invitations", {"roster": roster})
    assert response.status_code == 201, response.content
    assert response.json() == {"sent": ["new.judge@example.net"], "skipped": ["diego.herrera@example.org"]}
    invitation = event.judge_invitations.get(email="new.judge@example.net")
    assert sorted(t.public_id for t in invitation.tracks.all()) == ["trk_01", "trk_04"]
    assert len(mailoutbox) == 1


def test_bad_roster_invites_nobody_and_reports_every_line(api, event, mailoutbox):
    roster = "not-an-email,X,\nok@example.net,Ok,No Such Track\n"
    response = api("organizer").post("/api/v1/events/evt_01/judges/invitations", {"roster": roster})
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "line 1" in message and "line 2" in message
    assert not event.judge_invitations.exists() and not mailoutbox
    assert api("judge_a").post("/api/v1/events/evt_01/judges/invitations", {"roster": "a@b.co"}).status_code == 403


def test_pairwise_mode(event, judge_a):
    from judging import pairwise

    role = event.roles.get(user=judge_a, role="judge")
    a, b = pairwise.next_pair(event, role)
    pairwise.record(event, role, a.public_id, b.public_id)
    with pytest.raises(Conflict):
        pairwise.record(event, role, b.public_id, a.public_id)
    ranked = results.compute(event, "pairwise")
    assert ranked.rows[0].project.public_id == a.public_id
