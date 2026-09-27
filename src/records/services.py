"""Signing and verifying records.

A record is canonical JSON signed with the installation's Ed25519 key. Anyone can check
one without trusting this server: fetch the public key once from
/.well-known/bananahat-keys.json, then run tools/verify_record.py, a standard-library
Python script with its own implementation of Ed25519 verification (RFC 8032).
"""

import base64
import hashlib
import json
import secrets

from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from django.conf import settings
from django.db import transaction

from audit import services as audit
from core import clock
from records.models import SignedRecord, SigningKey


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical_json(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _fernet():
    key = hashlib.sha256(f"bananahat-signing:{settings.SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def active_key() -> SigningKey:
    """The current signing key, created on first use."""
    key = SigningKey.objects.filter(retired_at__isnull=True).order_by("-created_at").first()
    if key is not None:
        return key
    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    raw_public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = "bh-" + hashlib.sha256(raw_public).hexdigest()[:16]
    return SigningKey.objects.create(
        key_id=key_id,
        public_key=b64u(raw_public),
        private_key_encrypted=_fernet().encrypt(raw_private).decode(),
    )


def _private(key: SigningKey) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_fernet().decrypt(key.private_key_encrypted.encode()))


def sign(kind, event, body: dict, *, subject=None, private_salt="") -> SignedRecord:
    key = active_key()
    record = SignedRecord(kind=kind, event=event, subject=subject, key=key, private_salt=private_salt)
    payload = {
        "type": f"bananahat/{kind}",
        "version": 1,
        "record": record.public_id,
        "issuer": settings.SITE_URL,
        "key_id": key.key_id,
        "issued_at": clock.now().replace(microsecond=0).isoformat(),
        "event": {"id": event.public_id, "name": event.name},
        **body,
    }
    canonical = canonical_json(payload)
    record.payload = payload
    record.canonical = canonical
    record.signature = b64u(_private(key).sign(canonical.encode()))
    record.save()
    return record


def verify(record: SignedRecord) -> bool:
    public = Ed25519PublicKey.from_public_bytes(unb64u(record.key.public_key))
    try:
        public.verify(unb64u(record.signature), record.canonical.encode())
    except InvalidSignature:
        return False
    return json.loads(record.canonical) == record.payload


def verify_document(document: dict) -> tuple[bool, str]:
    """Verify a record someone pasted or uploaded: {"payload": ..., "signature": ...}."""
    try:
        payload, signature = document["payload"], document["signature"]
        key = SigningKey.objects.get(key_id=payload["key_id"])
    except (KeyError, TypeError):
        return False, "not a Banana Hat record"
    except SigningKey.DoesNotExist:
        return False, "signed by a key this portal does not know"
    public = Ed25519PublicKey.from_public_bytes(unb64u(key.public_key))
    try:
        public.verify(unb64u(signature), canonical_json(payload).encode())
    except (InvalidSignature, ValueError):
        return False, "the signature does not match the contents"
    return True, "valid"


def export(record: SignedRecord) -> dict:
    """The portable form: everything needed to verify offline."""
    return {"payload": record.payload, "signature": record.signature, "algorithm": "Ed25519",
            "public_key": record.key.public_key}


def jwks():
    active_key()  # a fresh install publishes its key before it has signed anything
    return {"keys": [
        {"kid": k.key_id, "kty": "OKP", "crv": "Ed25519", "x": k.public_key, "use": "sig",
         "created": k.created_at.isoformat(), "retired": k.retired_at.isoformat() if k.retired_at else None}
        for k in SigningKey.objects.order_by("created_at")
    ]}


# --- what gets signed ----------------------------------------------------------------

def sign_results(snapshot):
    payload = snapshot.payload
    body = {
        "published_at": payload.get("published_at"),
        "method": payload["method"],
        "ranking": [{"rank": r["rank"], "project": r["project"], "title": r["title"], "score": r["score"]}
                    for r in payload["overall"]],
        "prizes": [{"name": p["name"], "winner": p["winner"]} for p in payload["prizes"]],
        "audit_head": payload["audit_head"],
        "results_sha256": hashlib.sha256(canonical_json(payload).encode()).hexdigest(),
    }
    record = sign(SignedRecord.Kind.RESULTS, snapshot.event, body)
    snapshot.payload = {**payload, "record": record.public_id}
    snapshot.save(update_fields=["payload"])
    issue_judge_records(snapshot.event)
    return record


def _score_commitment(judge_role, salt):
    """sha256 over the judge's scores and a secret salt. Publishes nothing about the
    scores, but binds the record to them: revealing salt + scores later proves exactly
    what this judge submitted, and that it has not changed."""
    from judging.models import Score

    rows = (
        Score.objects.filter(assignment__judge=judge_role)
        .order_by("assignment__project__public_id")
        .prefetch_related("values__criterion")
    )
    material = [
        {"project": s.assignment.project.public_id,
         "values": {v.criterion.key: v.value for v in s.values.all()},
         "voided": bool(s.voided_at)}
        for s in rows.select_related("assignment__project")
    ]
    return hashlib.sha256((salt + canonical_json(material)).encode()).hexdigest(), len(material)


def issue_judge_record(event, judge_role, *, actor=None) -> SignedRecord:
    from judging.models import Assignment

    salt = secrets.token_hex(16)
    commitment, count = _score_commitment(judge_role, salt)
    projects = Assignment.objects.filter(judge=judge_role, status=Assignment.Status.DONE).count()
    body = {
        "judge": {"id": judge_role.public_id, "name": judge_role.user.name},
        "reviews": projects,
        "tracks": sorted(t.name for t in judge_role.tracks.all()),
        "scores_commitment": {"algorithm": "sha256(salt || canonical_json(scores))", "digest": commitment,
                              "count": count},
        "statement": f"{judge_role.user.name} served as a judge at {event.name} and reviewed "
                     f"{projects} project{'s' if projects != 1 else ''}.",
    }
    with transaction.atomic():
        record = sign(SignedRecord.Kind.JUDGE, event, body, subject=judge_role.user, private_salt=salt)
        audit.record("record.issued", f"Signed a participation record for judge {judge_role.user.display_name}",
                     actor=actor, event=event, target=record)
    return record


def revoke_event_records(event, *, actor, reason):
    """Unpublishing makes every record built on the old results wrong: the results
    record itself, certificates that quote a rank, and judge records whose score
    commitment covers scores that may now change. They are revoked (kept, marked, and
    shown as revoked on their verification page) and reissued at the next publication."""
    live = SignedRecord.objects.filter(event=event, revoked_at__isnull=True)
    count = live.update(revoked_at=clock.now(), revoked_reason=reason[:200])
    if count:
        audit.record("record.revoked", f"Revoked {count} signed record(s) of {event.name}: {reason}",
                     actor=actor, event=event, target=event, data={"count": count})
    return count


def issue_judge_records(event):
    from judging.models import Assignment

    judges = event.roles.judges().filter(assignments__status=Assignment.Status.DONE).distinct()
    already = set(SignedRecord.objects.filter(event=event, kind=SignedRecord.Kind.JUDGE, revoked_at__isnull=True)
                  .values_list("subject_id", flat=True))
    for role in judges.select_related("user").prefetch_related("tracks"):
        if role.user_id not in already:
            issue_judge_record(event, role)


def certificate_for(event, user, *, actor=None) -> SignedRecord | None:
    """A participant's certificate, issued once results are published."""
    from events.models import TeamMember
    from judging.results import published_results

    results = published_results(event)
    membership = TeamMember.objects.select_related("team").filter(event=event, user=user).first()
    if results is None or membership is None:
        return None
    existing = SignedRecord.objects.filter(event=event, kind=SignedRecord.Kind.CERTIFICATE, subject=user,
                                           revoked_at__isnull=True).first()
    if existing:
        return existing
    project = membership.team.projects.filter(status="submitted", duplicate_of__isnull=True).first()
    row = next((r for r in results["overall"] if project and r["project"] == project.public_id), None)
    awards = [p["name"] for p in results["prizes"] if project and p["winner"] == project.public_id]
    body = {
        "recipient": {"name": user.name},
        "team": membership.team.name,
        "project": {"id": project.public_id, "title": project.title} if project else None,
        "rank": row["rank"] if row else None,
        "ranked_projects": len(results["overall"]),
        "awards": awards,
        "statement": (f"{user.name} took part in {event.name} with team {membership.team.name}"
                      + (f" and won {', '.join(awards)}" if awards else "") + "."),
    }
    record = sign(SignedRecord.Kind.CERTIFICATE, event, body, subject=user)
    audit.record("record.issued", f"Issued a certificate to {user.display_name}", actor=actor or user,
                 event=event, target=record)
    return record
