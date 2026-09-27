from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache

from core.access import public


@public
@never_cache
def healthz(request):
    """Liveness and readiness in one: the process answers and the database does too."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok"})


def permission_denied(request, exception=None):
    message = str(exception) if exception and str(exception) else ""
    return render(request, "errors/403.html", {"message": message}, status=403)


def page_not_found(request, exception=None):
    return render(request, "errors/404.html", status=404)


def server_error(request):
    return render(request, "errors/500.html", status=500)
