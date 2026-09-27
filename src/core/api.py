from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from core.errors import DomainError


def exception_handler(exc, context):
    """Every API error has the same shape: {"error": {"code", "message", "fields"?}}."""
    if isinstance(exc, DomainError):
        return Response({"error": {"code": exc.code, "message": exc.message}}, status=exc.status)

    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    code = "error"
    if isinstance(exc, APIException):
        codes = exc.get_codes()
        code = codes if isinstance(codes, str) else "invalid"
    body: dict[str, object] = {"code": code}
    data = response.data
    if isinstance(data, dict) and set(data) == {"detail"}:
        body["message"] = str(data["detail"])
    else:
        body["message"] = "The request was not valid." if response.status_code == 400 else ""
        body["fields"] = data
    response.data = {"error": body}
    return response
