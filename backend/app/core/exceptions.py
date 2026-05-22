"""Custom application exception classes."""

from __future__ import annotations


class BallastError(Exception):
    """Base exception for all Ballast domain errors."""


class NotFoundError(BallastError):
    """Raised when a requested resource does not exist."""


class ValidationError(BallastError):
    """Raised when input fails domain validation rules."""


class UnauthorizedError(BallastError):
    """Raised when a request lacks valid credentials or permissions."""


class CalendarError(BallastError):
    """Raised when a Google Calendar API call fails."""
