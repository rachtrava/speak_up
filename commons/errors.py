class CommonsError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, **detail):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, **self.detail}


class AuthError(CommonsError):
    status, code = 401, "unauthenticated"


class Forbidden(CommonsError):
    status, code = 403, "forbidden"


class NotFound(CommonsError):
    status, code = 404, "not_found"


class Conflict(CommonsError):
    status, code = 409, "conflict"


class InvalidTransition(Conflict):
    code = "invalid_transition"


class RateLimited(CommonsError):
    status, code = 429, "rate_limited"


class EmergencyStop(CommonsError):
    status, code = 503, "emergency_stop"


class LimitExceeded(CommonsError):
    status, code = 422, "limit_exceeded"
