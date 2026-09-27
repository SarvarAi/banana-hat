from core.admin import register_read_only
from judging.models import Assignment, Score

register_read_only(Assignment, list_display=["public_id", "judge", "project", "status", "source"],
                   list_filter=["status", "source"], search_fields=["public_id"])
register_read_only(Score, list_display=["public_id", "assignment", "updated_at", "voided_at"],
                   search_fields=["public_id"])
