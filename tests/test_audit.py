"""The audit trail records what matters and notices when it is tampered with."""

from audit import services as audit
from audit.models import AuditEntry
from judging import scoring
from judging.models import Assignment


def _pending_for(user):
    return Assignment.objects.filter(judge__user=user, status="pending").select_related("event", "project").first()


def test_chain_verifies_after_seeding(db):
    report = audit.verify_chain()
    assert report.ok and report.checked > 0


def test_score_changes_are_recorded_with_before_and_after(judge_a):
    assignment = _pending_for(judge_a) or Assignment.objects.filter(judge__user=judge_a).first()
    scoring.submit_score(assignment, judge_a, {"functionality": 3, "quality": 3, "innovation": 3})
    scoring.submit_score(assignment, judge_a, {"functionality": 4, "quality": 3, "innovation": 3}, "better")
    entry = AuditEntry.objects.filter(action="score.updated").first()
    assert "Functionality 3→4" in entry.summary
    assert entry.data["before"]["functionality"] == 3 and entry.data["after"]["functionality"] == 4
    assert audit.verify_chain().ok


def test_editing_an_old_entry_breaks_the_chain(db):
    victim = AuditEntry.objects.order_by("id")[5]
    AuditEntry.objects.filter(pk=victim.pk).update(summary="nothing to see here")
    report = audit.verify_chain()
    assert not report.ok
    assert report.broken_at == victim.pk
    assert "edited" in report.reason


def test_deleting_an_entry_breaks_the_chain(db):
    AuditEntry.objects.order_by("id")[3].delete()
    assert not audit.verify_chain().ok


def test_deleting_the_newest_entry_is_noticed(db):
    AuditEntry.objects.order_by("-id").first().delete()
    report = audit.verify_chain()
    assert not report.ok and "newest" in report.reason


def test_organizer_can_read_the_trail_as_a_page_and_csv(web, organizer):
    page = web(organizer).get("/manage/sample-hack-2026/audit/")
    assert page.status_code == 200
    assert "Chain intact" in page.content.decode()
    csv = web(organizer).get("/manage/sample-hack-2026/audit/?format=csv").content.decode()
    assert csv.splitlines()[0] == "id,at,actor,action,target_type,target_id,summary,digest"


def test_ballot_entries_never_reveal_choices(open_event, participant):
    from voting import services as voting

    other = open_event.projects.exclude(team__memberships__user=participant).filter(status="submitted").first()
    ballot = voting.ballot_for_user(open_event, participant, {"ip": "10.0.0.1", "agent": "test"})
    voting.cast(ballot, [other.public_id], voter=participant)
    entry = AuditEntry.objects.filter(action="ballot.cast").first()
    assert other.public_id not in str(entry.data) and other.title not in entry.summary
