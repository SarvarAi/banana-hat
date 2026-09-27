from rest_framework.permissions import BasePermission


class DenyAll(BasePermission):
    """The default for every API view. Endpoints must opt in to being reachable."""

    message = "This endpoint has not declared who may call it."

    def has_permission(self, request, view):
        return False


class IsStaff(BasePermission):
    """Platform administrators."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)
