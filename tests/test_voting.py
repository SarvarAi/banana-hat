"""Community voting: identity, ordering, secrecy of the tally, and abuse handling."""

from datetime import timedelta

import pytest

from core.errors import Forbidden
from events.models import Event
from voting import services as voting
from voting.models import Ballot

META = {"ip": "203.0.113.7", "agent": "pytest"}


def _others(event, user, n=2):
    others = event.projects.canonical().exclude(team__memberships__user=user)
    return list(others.values_list("public_id", flat=True)[:n])


def test_signed_in_ballot_counts_once(open_event, stranger):
    ballot = voting.ballot_for_user(open_event, stranger, META)
    assert voting.ballot_for_user(open_event, stranger, META).pk == ballot.pk
    voting.cast(ballot, _others(open_event, stranger), voter=stranger)
    voting.cast(ballot, _others(open_event, stranger, 1), voter=stranger)  # changing is allowed
    assert ballot.choices.count() == 1
    assert Ballot.objects.filter(event=open_event).count() == 1


def test_cannot_vote_for_own_team(open_event, participant):
    own = open_event.projects.filter(team__memberships__user=participant).first()
    own.status = "submitted"
    own.submitted_at = own.created_at
    own.save()
    ballot = voting.ballot_for_user(open_event, participant, META)
    with pytest.raises(Forbidden):
        voting.cast(ballot, [own.public_id], voter=participant)


def test_too_many_picks_refused(open_event, stranger):
    open_event.votes_per_voter = 1
    open_event.save()
    ballot = voting.ballot_for_user(open_event, stranger, META)
    with pytest.raises(Exception, match="up to 1"):
        voting.cast(ballot, _others(open_event, stranger, 2), voter=stranger)


def _old_user(email):
    from accounts.models import User
    from core import clock

    return User.objects.create_user(email=email, name=email.split("@")[0],
                                    date_joined=clock.now() - timedelta(days=30))


def test_order_is_random_per_ballot_and_stable(open_event, db):
    orders = set()
    for i in range(8):
        user = _old_user(f"voter{i}@example.net")
        ballot = voting.ballot_for_user(open_event, user, META)
        first = [p.public_id for p in voting.ballot_projects(open_event, ballot)]
        assert first == [p.public_id for p in voting.ballot_projects(open_event, ballot)]
        orders.add(tuple(first))
    assert len(orders) > 1


def test_tally_hidden_from_everyone_while_open(api, open_event):
    for role in (None, "participant", "organizer", "admin"):
        response = api(role).get(f"/api/v1/events/{open_event.public_id}/votes")
        assert response.status_code == 403, role
        assert response.json()["error"]["code"] == "results_hidden"


def test_tally_published_after_close(api, open_event, stranger, clock):
    target = _others(open_event, stranger, 1)
    voting.cast(voting.ballot_for_user(open_event, stranger, META), target, voter=stranger)
    clock.set(open_event.voting_close_at + timedelta(minutes=1))
    data = api().get(f"/api/v1/events/{open_event.public_id}/votes").json()
    assert data["tally"][0]["project"] == target[0] and data["tally"][0]["votes"] == 1


def test_voting_closed_refuses_ballots(open_event, stranger, clock):
    ballot = voting.ballot_for_user(open_event, stranger, META)
    clock.set(open_event.voting_close_at)
    with pytest.raises(voting.VotingClosed):
        voting.cast(ballot, _others(open_event, stranger, 1), voter=stranger)


@pytest.mark.parametrize(("a", "b"), [
    ("Jane.Doe@gmail.com", "janedoe+votes@googlemail.com"),
    ("x@Example.ORG", "x+1@example.org"),
])
def test_email_aliases_fold_into_one_identity(a, b):
    assert voting.canonical_email(a) == voting.canonical_email(b)


def test_email_voting_one_ballot_per_mailbox(open_event, mailoutbox):
    open_event.voting_mode = Event.VotingMode.EMAIL
    open_event.save()
    base = "http://testserver"
    voting.request_email_ballot(open_event, "jane.doe@gmail.com", META, base_url=base)
    voting.request_email_ballot(open_event, "janedoe+again@gmail.com", {**META, "ip": "198.51.100.1"}, base_url=base)
    assert Ballot.objects.filter(event=open_event).count() == 1
    assert len(mailoutbox) == 2
    link = next(line for line in mailoutbox[0].body.splitlines() if "/verify/" in line)
    ballot = voting.redeem_verify_token(open_event, link.rstrip("/").split("/")[-1])
    assert ballot.status == Ballot.Status.COUNTED
    assert ballot.email_hint == "ja***@gmail.com"


def test_pending_email_ballot_cannot_vote(open_event):
    open_event.voting_mode = Event.VotingMode.EMAIL
    open_event.save()
    voting.request_email_ballot(open_event, "someone@example.org", META, base_url="http://testserver")
    ballot = Ballot.objects.get(event=open_event)
    with pytest.raises(Forbidden):
        voting.cast(ballot, list(open_event.projects.canonical().values_list("public_id", flat=True)[:1]))


def test_ballot_links_are_single_identity(open_event, organizer):
    open_event.voting_mode = Event.VotingMode.LINK
    open_event.save()
    (label, raw), = voting.create_ballot_links(open_event, 1, "Table", actor=organizer)
    first = voting.ballot_for_link(open_event, raw, META)
    again = voting.ballot_for_link(open_event, raw, {**META, "ip": "198.51.100.9"})
    assert first.pk == again.pk and first.email_hint == "Table 1"


def test_burst_from_one_network_is_held_not_counted(open_event, db):
    target = list(open_event.projects.canonical().values_list("public_id", flat=True)[:1])
    statuses = []
    for i in range(voting.BURST_SAME_NETWORK + 1):
        user = _old_user(f"bot{i}@example.net")
        ballot = voting.ballot_for_user(open_event, user, {"ip": f"198.51.100.{i + 10}", "agent": f"ua{i}"})
        statuses.append(voting.cast(ballot, target, voter=user).status)
    assert statuses[0] == Ballot.Status.COUNTED
    assert statuses[-1] == Ballot.Status.FLAGGED


def test_brand_new_account_is_held_for_review(open_event, db):
    from accounts.models import User

    fresh = User.objects.create_user(email="fresh@example.net", name="Fresh")
    ballot = voting.cast(voting.ballot_for_user(open_event, fresh, META), _others(open_event, fresh, 1), voter=fresh)
    assert ballot.status == Ballot.Status.FLAGGED
    assert "account created minutes before voting" in ballot.flags


def test_flagged_ballots_do_not_count_until_reviewed(open_event, organizer, stranger, clock):
    target = _others(open_event, stranger, 1)
    ballot = voting.cast(voting.ballot_for_user(open_event, stranger, META), target, voter=stranger)
    Ballot.objects.filter(pk=ballot.pk).update(status=Ballot.Status.FLAGGED)
    clock.set(open_event.voting_close_at + timedelta(minutes=1))
    assert all(r["votes"] == 0 for r in voting.tally(open_event))
    voting.review_ballot(Ballot.objects.get(pk=ballot.pk), organizer, "count")
    assert voting.tally(open_event)[0]["votes"] == 1


def test_comments_rate_limited_and_deduplicated(open_event, stranger):
    project = open_event.projects.canonical().first()
    voting.add_comment(project, stranger, "Lovely work")
    with pytest.raises(Exception, match="already posted"):
        voting.add_comment(project, stranger, "Lovely work")
    for i in range(8):  # the refused duplicate above still counted against the limit
        voting.add_comment(project, stranger, f"comment {i}")
    with pytest.raises(Exception, match="Too many"):
        voting.add_comment(project, stranger, "one more")


def test_hidden_comment_visible_only_to_organizers(open_event, organizer, stranger, web):
    project = open_event.projects.canonical().first()
    comment = voting.add_comment(project, stranger, "spam spam spam")
    voting.hide_comment(comment, organizer, "spam")
    assert "spam spam spam" not in web().get(project.get_absolute_url()).content.decode()
    assert "spam spam spam" in web(organizer).get(project.get_absolute_url()).content.decode()


def test_ballot_page_flow(web, stranger, open_event):
    client = web(stranger)
    page = client.get("/vote/open-jam/")
    assert page.status_code == 200 and b"Cast my vote" in page.content
    pick = _others(open_event, stranger, 1)
    client.post("/vote/open-jam/", {"projects": pick})
    assert Ballot.objects.get(event=open_event, user=stranger).choices.count() == 1
