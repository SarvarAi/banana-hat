from core.admin import register_read_only
from events.models import Event, EventRole, Project, Team

register_read_only(Event, list_display=["name", "public_id", "slug", "is_listed", "submissions_close_at",
                                        "results_published_at"], search_fields=["name", "slug", "public_id"])
register_read_only(EventRole, list_display=["user", "role", "event", "public_id"], list_filter=["role"],
                   search_fields=["user__email", "public_id"])
register_read_only(Team, list_display=["name", "public_id", "event"], search_fields=["name", "public_id"])
register_read_only(Project, list_display=["title", "public_id", "event", "status", "duplicate_of"],
                   list_filter=["status"], search_fields=["title", "public_id"])
