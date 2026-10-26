"""Domain exceptions. main.py maps each one to an HTTP status with a ``{"detail": str}`` body.

Feature modules raise these instead of FastAPI's HTTPException so they stay framework-agnostic:

    NotFoundError   -> 404   (unknown session / order / design / context item)
    ForbiddenError  -> 403   (not yours to change, e.g. someone else's order request)
    ConflictError   -> 409   (wrong state: stale question_id, quiz not done, no profile yet, ...)
    LengthRequired  -> 411   (upload without a Content-Length)
    BadInputError   -> 422   (semantically invalid input that passed schema validation)
    PayloadTooLarge -> 413   (upload limits)
    RateLimited     -> 429   (too many costly requests from one client; sends Retry-After)
    LLMError        -> 502   (model call failed or returned unusable output; message is user-friendly)
    UpstreamError   -> 502   (other external dependency failed, e.g. web search)
"""

from __future__ import annotations


class AppError(Exception):
    """Base class. ``detail`` is shown to the user, so keep it friendly and secret-free."""

    status_code: int = 500
    default_detail: str = "Something went wrong."

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail or self.default_detail
        self.headers: dict[str, str] = {}
        super().__init__(self.detail)


class NotFoundError(AppError):
    status_code = 404
    default_detail = "Not found."


class ForbiddenError(AppError):
    status_code = 403
    default_detail = "You can't change that."


class ConflictError(AppError):
    status_code = 409
    default_detail = "That action doesn't fit the current state."


class LengthRequired(AppError):
    status_code = 411
    default_detail = "Send the request with a Content-Length header."


class RateLimited(AppError):
    status_code = 429
    default_detail = "You're going a little fast. Please wait a moment and try again."

    def __init__(self, detail: str | None = None, retry_after: int = 1) -> None:
        super().__init__(detail)
        self.headers = {"Retry-After": str(max(1, retry_after))}


class BadInputError(AppError):
    status_code = 422
    default_detail = "Invalid input."


class PayloadTooLarge(AppError):
    status_code = 413
    default_detail = "Upload too large."


class LLMError(AppError):
    status_code = 502
    default_detail = "Our AI had a hiccup. Please try again."


class UpstreamError(AppError):
    status_code = 502
    default_detail = "An external service didn't respond. Please try again."
