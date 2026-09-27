from core.admin import register_read_only
from voting.models import Ballot, Comment

register_read_only(Ballot, list_display=["public_id", "event", "kind", "status", "cast_at"],
                   list_filter=["kind", "status"])
register_read_only(Comment, list_display=["public_id", "project", "author", "created_at", "hidden_at"])
