"""Custom exception hierarchy for omnifocus-cli.

All exceptions raised by this library inherit from :class:`OFError` so callers
can catch the full set with a single ``except OFError`` clause while still being
able to distinguish specific failure modes.
"""

from __future__ import annotations

__author__ = "Maciej Szymczak <maciej@szymczak.at>"


class OFError(Exception):
    """Base class for all omnifocus-cli errors."""


class OFBundleNotFound(OFError):
    """Raised when the .ofocus bundle directory cannot be located."""


class OFParseError(OFError):
    """Raised when the XML or ZIP structure is invalid or unexpected."""


class OFEncryptionError(OFError):
    """Raised when decryption fails (bad passphrase, corrupt data, etc.)."""


class OFWebDAVError(OFError):
    """Raised when a WebDAV operation fails after all retries.

    Attributes:
        status_code: HTTP status code of the final response, or None for
            connection-level errors.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OFTaskNotFound(OFError):
    """Raised when no task matches the given query or ID."""


class OFAmbiguousMatch(OFError):
    """Raised when a fuzzy query matches more than one task.

    Attributes:
        candidates: List of ``(score, task_id, task_name)`` tuples for the
            matched tasks, sorted by descending score.
    """

    def __init__(self, query: str, candidates: list[tuple[float, str, str]]) -> None:
        super().__init__(f"Query {query!r} matches {len(candidates)} tasks — be more specific.")
        self.query = query
        self.candidates = candidates


class OFProjectNotFound(OFError):
    """Raised when no project matches the given name."""


class OFNotRunning(OFError):
    """Raised when OmniFocus is required to be running but is not.

    Not used in the containerised path (WebDAV-based), but kept for completeness
    should a direct-AppleScript mode ever be added.
    """
