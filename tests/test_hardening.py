"""Regression tests for the findings of an independent review, one per finding."""

import copy
import re
from datetime import timedelta

import pytest
from django.core.management import call_command

from accounts.models import ApiToken, User
from audit import services as audit
from core import clock as app_clock
from core.errors import Conflict, Forbidden
from events.models import Event
from judging import assignment, results, scoring
from judging.models import Assignment
from records.models import SignedRecord
from voting import services as voting
from voting.models import Ballot

META = {"ip": "203.0.113.50", "agent": "pytest"}


def test_recusal_cannot_void_a_score_after_judging_closes(event, judge_a):
    done = Assignment.objects.filter(judge__user=judge_a, status="done").first()
    event.judging_close_at = event.submissions_close_at + timedelta(days=1)
    event.save()
    with pytest.raises(scoring.JudgingClosed):
        assignment.recuse(done, actor=judge_a, reason="too late")
    done.refresh_from_db()
    assert done.status == Assignment.Status.DONE and done.score.voided_at is None


def test_recusal_cannot_void_a_score_after_publication(event, organizer, judge_a):
    results.publish(event, organizer)
    done = Assignment.objects.filter(judge__user=judge_a, status="done").first()
    with pytest.raises(scoring.JudgingClosed):
        assignment.recuse(done, actor=judge_a, reason="after the fact")


def test_email_ballot_is_linked_to_the_account_so_self_voting_fails(open_event, participant):
    open_event.voting_mode = Event.VotingMode.EMAIL
    open_event.save()
    voting.request_email_ballot(open_event, participant.email.upper(), META, base_url="http://testserver")
    ballot = Ballot.objects.get(event=open_event)
    assert ballot.user == participant
    ballot.status = Ballot.Status.COUNTED
    ballot.save()
    own = open_event.projects.filter(team__memberships__user=participant).first()
    own.status, own.submitted_at = "submitted", app_clock.now()
    own.save()
    with pytest.raises(Forbidden):
        voting.cast(ballot, [own.public_id])


def test_demo_tokens_die_when_demo_mode_is_off(api, settings):
    settings.DEMO_MODE = False
    assert api("organizer").get("/api/v1/events/evt_01/progress").status_code == 401
    settings.DEMO_MODE = True
    assert api("organizer").get("/api/v1/events/evt_01/progress").status_code == 200


def test_disarm_demo_revokes_tokens_and_scrubs_passwords(admin_user):
    from accounts import demo

    assert admin_user.check_password(demo.PASSWORDS["admin"])
    call_command("disarm_demo", verbosity=0)
    admin_user.refresh_from_db()
    assert not admin_user.has_usable_password()
    assert not ApiToken.objects.usable().filter(digest=ApiToken.hash(demo.TOKENS["organizer"])).exists()


def test_signed_in_voting_needs_a_confirmed_email(open_event, web, mailoutbox):
    client = web()
    client.post("/accounts/signup/", {"name": "Voter", "email": "voter@example.net",
                                      "password": "a-long-enough-passphrase"})
    user = User.objects.get(email="voter@example.net")
    with pytest.raises(Forbidden, match="Confirm your email"):
        voting.ballot_for_user(open_event, user, META)
    assert b"Email me a confirmation link" in client.get("/vote/open-jam/").content
    link = re.search(r"http://testserver(/accounts/verify/[^\s]+/)", mailoutbox[-1].body).group(1)
    client.get(link)
    user.refresh_from_db()
    assert user.email_verified_at
    assert voting.ballot_for_user(open_event, user, META).user == user


def test_voter_domains_restrict_who_may_vote(open_event, stranger):
    open_event.voter_email_domains = "uni.example, lab.example.org"
    open_event.save()
    with pytest.raises(Forbidden, match="limited to"):
        voting.ballot_for_user(open_event, stranger, META)
    assert open_event.voter_domain_allowed("ada@cs.uni.example")
    assert not open_event.voter_domain_allowed("ada@uni.example.evil.com")


def test_unpublish_revokes_records_and_republish_reissues(event, organizer, judge_a):
    results.publish(event, organizer)
    first = SignedRecord.objects.get(event=event, kind="judge_participation", subject=judge_a)
    results.unpublish(event, organizer, "wrong method")
    first.refresh_from_db()
    assert first.revoked_at and "unpublished" in first.revoked_reason
    assert not SignedRecord.objects.filter(event=event, revoked_at__isnull=True).exists()
    results.publish(event, organizer)
    current = SignedRecord.objects.filter(event=event, kind="judge_participation", subject=judge_a,
                                          revoked_at__isnull=True)
    assert current.count() == 1 and current.get().pk != first.pk


def test_deleting_a_user_does_not_break_the_audit_chain(stranger):
    audit.record("test.event", "a stranger did something", actor=stranger)
    stranger.delete()
    assert audit.verify_chain().ok


def test_import_that_would_make_the_organizer_a_participant_rolls_back(organizer, fixture_data):
    doc = copy.deepcopy(fixture_data)
    doc["event"]["id"] = "evt_mixed"
    doc["teams"][0]["members"].append(organizer.email)
    from integrations import interchange

    before = Event.objects.count()
    with pytest.raises(Conflict):
        interchange.import_as_organizer(doc, organizer)
    assert Event.objects.count() == before


def test_voting_settings_freeze_once_voting_closes(open_event, organizer, clock):
    from events import console

    clock.set(open_event.voting_close_at + timedelta(minutes=5))
    with pytest.raises(Conflict, match="frozen"):
        console.update_event(open_event, organizer, {"voting_close_at": open_event.voting_close_at + timedelta(days=1)})


def test_forwarded_for_is_ignored_unless_behind_a_proxy(rf, settings):
    from core.ratelimit import client_ip

    request = rf.get("/", HTTP_X_FORWARDED_FOR="198.51.100.77", REMOTE_ADDR="10.1.2.3")
    settings.TRUST_X_FORWARDED_FOR = False
    assert client_ip(request) == "10.1.2.3"
    settings.TRUST_X_FORWARDED_FOR = True
    assert client_ip(request) == "198.51.100.77"


def test_anonymous_throttle_ignores_forged_forwarded_for(db):
    from rest_framework.settings import api_settings
    from rest_framework.test import APIRequestFactory
    from rest_framework.throttling import AnonRateThrottle

    assert api_settings.NUM_PROXIES == 0
    request = APIRequestFactory().get("/", HTTP_X_FORWARDED_FOR="198.51.100.9", REMOTE_ADDR="10.9.9.9")
    assert AnonRateThrottle().get_ident(request) == "10.9.9.9"


@pytest.mark.parametrize("address", ["100.64.0.1", "127.0.0.1", "10.0.0.5", "169.254.169.254"])
def test_webhook_targets_outside_the_public_internet_are_refused(settings, monkeypatch, address):
    import socket

    from integrations import webhooks

    settings.WEBHOOK_ALLOW_PRIVATE_TARGETS = False
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, (address, 443))])
    with pytest.raises(webhooks.UnsafeTarget):
        webhooks.check_target("https://hooks.example.org/x")
