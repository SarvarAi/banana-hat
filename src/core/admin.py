"""Read-only admin for judging data.

The admin is a platform operator's window into the database, not a side door around
the services: editing a score or a deadline here would skip the checks and the audit
trail. So judging data is viewable and searchable, never editable. Use the organizer
console or the API to change things.
"""

from django.contrib import admin


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def register_read_only(model, **options):
    admin.site.register(model, type(f"{model.__name__}Admin", (ReadOnlyAdmin,), options))
