"""Exception types used by the plugin."""

HELP_URL = "https://support.kraken.com/articles/360000919966-how-to-create-an-api-key"


class NeedsAuth(Exception):
    """Credentials are missing or rejected. The user must act."""

    def __init__(self, message: str, *, code: str = "credentials_invalid") -> None:
        super().__init__(f"{message} Help: {HELP_URL}")
        self.code = code


class Transient(Exception):
    """A retryable failure."""


class BadRequest(Transient):
    """Kraken refused the request's arguments (`EQuery:`, `EGeneral:Invalid arguments`), e.g. an
    unknown pair. Still retryable where it escapes; price lookups skip the one pair instead."""


class RateLimited(Exception):
    """Internal signal: the API rate limit was hit. Never escapes sync."""


class OutOfTime(Exception):
    """Internal signal: the sync time budget ran out. Never escapes sync."""
