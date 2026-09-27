from audit.models import AuditEntry
from core.admin import register_read_only

register_read_only(AuditEntry, list_display=["id", "at", "actor_label", "action", "summary"],
                   list_filter=["action"], search_fields=["summary", "actor_label", "target_id"])
