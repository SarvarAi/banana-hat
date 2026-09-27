"""Domain errors.

Services raise these; they know nothing about HTTP. The API exception handler turns
them into a JSON error with the status below, and HTML views turn them into a message
on the page. One refusal, one reason, whichever door the request came through.
"""


class DomainError(Exception):
    status = 400
    code = "invalid"

    def __init__(self, message, *, code=None, status=None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status


class Forbidden(DomainError):
    status = 403
    code = "forbidden"


class Conflict(DomainError):
    status = 409
    code = "conflict"


class NotFound(DomainError):
    status = 404
    code = "not_found"
