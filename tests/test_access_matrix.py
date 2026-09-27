"""Role isolation, enforced in the backend.

Two kinds of test. The structural ones walk every URL the project serves and fail if
any view has not declared who may call it, so a new endpoint cannot ship open by
accident. The behavioural ones send real requests as every role and check the status
codes, the way a curl from an attacker would.
"""

import pytest
from django.urls import URLPattern, URLResolver, get_resolver

from core.permissions import DenyAll


def _walk(patterns, prefix=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p.callback


FRAMEWORK = ("django.contrib.admin", "django.contrib.staticfiles")


def test_every_html_view_declares_an_access_policy():
    undeclared = []
    for route, view in _walk(get_resolver().url_patterns):
        if route.startswith("admin/") or view.__module__.startswith(FRAMEWORK) or hasattr(view, "cls"):
            continue  # the admin site guards itself; DRF views are checked below
        if not getattr(view, "access_policy", None):
            undeclared.append(route)
    assert not undeclared, f"views without an access policy: {undeclared}"


def test_every_api_view_declares_permissions_explicitly():
    """No API view may lean on the project default (DenyAll) or DRF's (AllowAny)."""
    implicit = []
    for route, view in _walk(get_resolver().url_patterns):
        cls = getattr(view, "cls", None)
        if cls is None or not route.startswith("api/v1/"):
            continue
        if "permission_classes" not in vars(cls) and not any(
            "permission_classes" in vars(base) for base in cls.__mro__[1:]
            if base.__module__.startswith("api.")
        ):
            implicit.append(route)
        assert DenyAll not in cls.permission_classes, route
    assert not implicit, f"API views relying on the default permission: {implicit}"


def test_admin_pages_work_for_staff_and_refuse_everyone_else(web, admin_user, organizer):
    for path in ["/admin/", "/admin/accounts/user/add/", f"/admin/accounts/user/{organizer.pk}/password/"]:
        assert web(admin_user).get(path).status_code == 200, path
        assert web(organizer).get(path).status_code == 302, path  # not staff: to the admin login


def test_an_undeclared_view_is_refused(rf, db):
    from core.middleware import AccessPolicyMiddleware

    def naked_view(request):
        raise AssertionError("must not run")

    middleware = AccessPolicyMiddleware(lambda r: None)
    with pytest.raises(Exception, match="access policy"):
        middleware.process_view(rf.get("/"), naked_view, (), {})


# --- behaviour: who gets what ------------------------------------------------------

JUDGE_A_SCORES = "/api/v1/judges/jdg_24/scores"
JUDGE_B_SCORES = "/api/v1/judges/jdg_26/scores"
MY_SCORES = "/api/v1/judges/me/scores"
EVENT_SCORES = "/api/v1/events/evt_01/scores"
EXPORT = "/api/v1/events/evt_01/exports/scores.csv"
PROGRESS = "/api/v1/events/evt_01/progress"
AUDIT = "/api/v1/events/evt_01/audit"
PREVIEW = "/api/v1/events/evt_01/results?preview=1"
RESULTS = "/api/v1/events/evt_01/results"

MATRIX = [
    # path,           anon, participant, judge_a, judge_b, organizer
    (MY_SCORES,       401,  403,         200,     200,     403),
    (JUDGE_A_SCORES,  401,  403,         200,     403,     200),
    (JUDGE_B_SCORES,  401,  403,         403,     200,     200),
    (EVENT_SCORES,    401,  403,         403,     403,     200),
    (EXPORT,          401,  403,         403,     403,     200),
    (PROGRESS,        401,  403,         403,     403,     200),
    (AUDIT,           401,  403,         403,     403,     200),
    (PREVIEW,         403,  403,         403,     403,     200),
    (RESULTS,         403,  403,         403,     403,     403),  # unpublished: nobody
]
ROLES = [None, "participant", "judge_a", "judge_b", "organizer"]


@pytest.mark.parametrize(("path", "expected"), [(row[0], row[1:]) for row in MATRIX],
                         ids=[row[0] for row in MATRIX])
def test_access_matrix(api, path, expected):
    for role, status in zip(ROLES, expected, strict=True):
        got = api(role).get(path).status_code
        assert got == status, f"{role or 'anonymous'} GET {path}: got {got}, wanted {status}"


def test_query_parameter_cannot_widen_access(api):
    """?judge= naming someone else is refused, not silently ignored."""
    assert api("judge_b").get(MY_SCORES, judge="jdg_24").status_code == 403
    assert api("judge_b").get(MY_SCORES, judge="jdg_26").status_code == 200


def test_unknown_judge_id_looks_the_same_as_a_forbidden_one(api):
    assert api("judge_b").get("/api/v1/judges/jdg_99/scores").status_code == 403


def test_judge_sees_only_their_own_rows(api):
    rows = api("judge_a").get(MY_SCORES).json()
    assert rows and {r["judge"] for r in rows} == {"jdg_24"}


def test_organizer_of_another_event_sees_nothing(db, token_for, stranger):
    from events.models import Event, EventRole

    other = Event.objects.get(slug="open-jam")
    EventRole.objects.create(event=other, user=stranger, role=EventRole.Role.ORGANIZER)
    client = token_for(stranger)
    assert client.get(JUDGE_A_SCORES).status_code == 403
    assert client.get(EVENT_SCORES).status_code == 403
    assert client.get(EXPORT).status_code == 403


def test_score_queryset_scopes_rows(judge_a, organizer, participant):
    from judging.models import Score

    assert set(Score.objects.visible_to(judge_a).values_list("assignment__judge__public_id", flat=True)) == {"jdg_24"}
    assert Score.objects.visible_to(participant).count() == 0
    in_event = Score.objects.filter(assignment__event__public_id="evt_01").count()
    assert Score.objects.visible_to(organizer).count() == in_event


@pytest.mark.parametrize("page", ["", "dashboard/", "results/", "audit/", "judges/", "assignments/", "data/"])
def test_console_pages_refuse_non_organizers(web, judge_a, participant, page):
    url = f"/manage/sample-hack-2026/{page}"
    assert web(judge_a).get(url).status_code == 403
    assert web(participant).get(url).status_code == 403
    assert web().get(url).status_code == 302  # to the sign-in page


def test_judge_cannot_open_a_peers_scorecard(web, judge_b):
    from judging.models import Assignment

    theirs = Assignment.objects.filter(judge__public_id="jdg_24").first()
    assert web(judge_b).get(f"/judge/sample-hack-2026/review/{theirs.public_id}/").status_code == 404


def test_judge_cannot_write_a_peers_score(api):
    from judging.models import Assignment

    theirs = Assignment.objects.filter(judge__public_id="jdg_24").first()
    response = api("judge_b").put(f"/api/v1/assignments/{theirs.public_id}/score",
                                  {"values": {"functionality": 5, "quality": 5, "innovation": 5}})
    assert response.status_code == 403


def test_organizer_cannot_write_a_judges_score(organizer):
    from core.errors import Forbidden
    from judging import scoring
    from judging.models import Assignment

    assignment = Assignment.objects.filter(judge__public_id="jdg_24", status="pending").first() or \
        Assignment.objects.filter(judge__public_id="jdg_24").first()
    with pytest.raises(Forbidden):
        scoring.submit_score(assignment, organizer, {"functionality": 5, "quality": 5, "innovation": 5})


def test_revoked_token_stops_working(api, judge_a):
    from accounts.models import ApiToken
    from core import clock

    ApiToken.objects.filter(user=judge_a).update(revoked_at=clock.now())
    assert api("judge_a").get(MY_SCORES).status_code == 401


def test_roles_that_would_break_isolation_are_refused(event, organizer, participant, judge_a):
    from core.errors import Conflict
    from events import services
    from events.models import EventRole

    with pytest.raises(Conflict):
        services.grant_role(event, organizer, EventRole.Role.JUDGE)
    with pytest.raises(Conflict):
        services.grant_role(event, participant, EventRole.Role.JUDGE)
    with pytest.raises(Conflict):
        services.grant_role(event, judge_a, EventRole.Role.ORGANIZER)
