from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from accounts.models import ApiToken, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ["email"]
    list_display = ["email", "name", "is_staff", "can_create_events", "is_active", "date_joined"]
    list_filter = ["is_staff", "can_create_events", "is_active"]
    search_fields = ["email", "name"]
    fieldsets = [
        (None, {"fields": ["email", "name", "password"]}),
        ("Platform", {"fields": ["is_active", "is_staff", "is_superuser", "can_create_events"]}),
        ("Dates", {"fields": ["last_login", "date_joined"]}),
    ]
    add_fieldsets = [
        (None, {"classes": ["wide"], "fields": ["email", "name", "password1", "password2"]}),
    ]
    filter_horizontal = []


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    list_display = ["name", "user", "prefix", "created_at", "last_used_at", "expires_at", "revoked_at"]
    search_fields = ["name", "user__email", "prefix"]
    readonly_fields = ["prefix", "digest", "created_at", "last_used_at"]
