"""Writing to and checking the audit chain."""

from dataclasses import dataclass

from django.db import transaction

from audit.models import AuditChainHead, AuditEntry
from core import clock

GENESIS = "0" * 64


def actor_label(user) -> str:
    if user is None or not getattr(user, "is_authenticated", False):
        return "system"
    return f"{user.display_name} <{user.email}>"


def record(action, summary, *, actor=None, event=None, target=None, data=None, source="web"):
    """Append an entry. Call inside the transaction that makes the change it describes.

    Then the change and its audit entry commit together or not at all: there is no
    window in which a score changed and the log does not say so.
    """
    target_type, target_id = "", ""
    if target is not None:
        target_type = target._meta.model_name
        target_id = str(getattr(target, "public_id", None) or target.pk)
    with transaction.atomic():
        head, _ = AuditChainHead.objects.select_for_update().get_or_create(id=1)
        entry = AuditEntry(
            at=clock.now(),
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            actor_label=actor_label(actor),
            event=event,
            action=action,
            target_type=target_type,
            target_id=target_id,
            summary=summary[:400],
            data=data or {},
            source=source,
            prev_digest=head.digest,
        )
        entry.digest = entry.compute_digest()
        entry.save()
        head.digest = entry.digest
        head.length += 1
        head.save(update_fields=["digest", "length"])
    return entry


@dataclass
class ChainReport:
    ok: bool
    checked: int
    head: str
    broken_at: int | None = None
    reason: str = ""


def verify_chain() -> ChainReport:
    """Walk the whole chain in order and recompute every link."""
    prev = GENESIS
    checked = 0
    for entry in AuditEntry.objects.order_by("id").iterator(chunk_size=2000):
        if entry.prev_digest != prev:
            return ChainReport(False, checked, prev, entry.pk,
                               "an entry before this one was removed or rewritten")
        if entry.compute_digest() != entry.digest:
            return ChainReport(False, checked, prev, entry.pk, "this entry was edited")
        prev = entry.digest
        checked += 1
    head = AuditChainHead.objects.filter(id=1).first()
    if head and head.digest != prev:
        return ChainReport(False, checked, prev, None, "the newest entries were deleted")
    return ChainReport(True, checked, prev)


def head_digest() -> str:
    head = AuditChainHead.objects.filter(id=1).first()
    return head.digest if head else GENESIS
