"""The fixture format in and out: the migration path."""

import copy

import pytest

from events.models import Event, Project, Team
from integrations import interchange
from judging.models import Score


def test_fixture_loaded_with_every_awkward_case(event):
    assert event.projects.count() == 41
    assert Score.objects.filter(assignment__event=event).count() == 126
    dup = Project.objects.get(public_id="prj_41")
    assert dup.duplicate_of.public_id == "prj_07"
    assert Team.objects.filter(event=event, name="StillTrail").count() == 3
    assert event.submissions_close_at.isoformat() == "2026-03-01T18:00:00+00:00"
    assert not event.submissions_open()


def test_gallery_hides_the_duplicate(web):
    body = web().get("/projects/?event=sample-hack-2026").content.decode()
    assert body.count("Dry Harbour") == 1


def _fixture_view(doc):
    """Only the keys and meaning the fixture format defines."""
    return {
        "event": {k: doc["event"][k] for k in ("id", "name", "submissions_close")},
        "tracks": sorted((t["id"], t["name"]) for t in doc["tracks"]),
        "judges": sorted((j["id"], j["name"], j["email"], tuple(sorted(j["tracks"]))) for j in doc["judges"]),
        "teams": sorted((t["id"], t["name"], tuple(t["members"])) for t in doc["teams"]),
        "projects": sorted((p["id"], p["team"], p["track"], p["title"], p["summary"], p["repo_url"],
                            p["submitted_at"]) for p in doc["projects"]),
        "scores": sorted((s["judge"], s["project"], tuple(sorted(s["criteria"].items())), s["comment"])
                         for s in doc["scores"]),
    }


def test_export_round_trips_the_fixture_exactly(event, fixture_data):
    exported = interchange.export_event(event)
    assert _fixture_view(exported) == _fixture_view(fixture_data)


def test_export_reimports_losslessly(event, organizer):
    first = interchange.export_event(event)
    doc = copy.deepcopy(first)
    doc["event"]["id"] = "evt_copy"
    doc["event"]["slug"] = "sample-hack-copy"
    report = interchange.import_event(doc, actor=organizer)
    second = interchange.export_event(report.event)
    first["event"]["id"], first["event"]["slug"] = "evt_copy", "sample-hack-copy"
    for key in ("tracks", "judges", "teams", "projects", "scores", "criteria", "prizes", "assignments"):
        assert second[key] == first[key], key
    assert second["event"] == first["event"]


def test_invalid_document_reports_every_problem_and_writes_nothing(organizer):
    before = Event.objects.count()
    doc = {
        "event": {"id": "evt_bad", "name": "Bad", "submissions_close": "yesterday"},
        "tracks": [{"id": "t1", "name": "A"}],
        "judges": [{"id": "j1", "name": "J", "email": "j@example.org", "tracks": ["t9"]}],
        "teams": [{"id": "tm1", "name": "T", "members": ["m@example.org"]}],
        "projects": [{"id": "p1", "team": "tm9", "track": "t1", "title": "P", "submitted_at": "2026-01-01T00:00:00Z"}],
        "scores": [{"judge": "j1", "project": "p9", "criteria": {"x": 3}}],
    }
    with pytest.raises(interchange.InterchangeError) as info:
        interchange.import_event(doc, actor=organizer)
    problems = " ".join(info.value.problems)
    assert "unknown track 't9'" in problems
    assert "unknown team 'tm9'" in problems
    assert "unknown project 'p9'" in problems
    assert Event.objects.count() == before


def test_bad_timestamp_rolls_back(organizer):
    doc = {"event": {"id": "evt_ts", "name": "TS", "submissions_close": "not a date"}}
    with pytest.raises(interchange.InterchangeError):
        interchange.import_event(doc, actor=organizer)
    assert not Event.objects.filter(public_id="evt_ts").exists()


def test_import_via_api_makes_you_organizer(api, fixture_data):
    doc = copy.deepcopy(fixture_data)
    doc["event"]["id"] = "evt_api"
    doc["event"]["name"] = "Imported over the API"
    response = api("organizer").post("/api/v1/events/import", doc)
    assert response.status_code == 201, response.content
    assert response.json()["counts"]["projects"] == 41
    assert api("organizer").get("/api/v1/events/evt_api/progress").status_code == 200


def test_import_refused_without_permission(api, fixture_data):
    assert api("participant").post("/api/v1/events/import", fixture_data).status_code == 403


def test_csv_exports_are_complete(api):
    body = api("organizer").get("/api/v1/events/evt_01/exports/scores.csv").content.decode()
    lines = body.strip().splitlines()
    assert lines[0].startswith("project_id,project_title,judge_id")
    assert len(lines) == 1 + 126
    assert sum(1 for line in lines if line.endswith(",") is False and ",duplicate," in line) == 4
    results = api("organizer").get("/api/v1/events/evt_01/exports/results.csv").content.decode()
    assert results.splitlines()[0].startswith("rank,project_id")
    assert len(results.strip().splitlines()) == 1 + 40
