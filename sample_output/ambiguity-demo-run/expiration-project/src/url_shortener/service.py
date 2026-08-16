"""In-memory URL-shortening domain service."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Lock
from urllib.parse import urlsplit


class URLShortenerError(Exception):
    """Base class for domain-level URL-shortener failures."""


class InvalidURLError(URLShortenerError):
    """Raised when a submitted URL is not an absolute HTTP(S) URL."""


class UnknownShortCodeError(URLShortenerError):
    """Raised when no original URL exists for a short code."""


Clock = Callable[[], datetime]
SHORT_URL_TTL = timedelta(hours=24)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class URLShortener:
    """Deterministic, collision-safe, process-local URL repository."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or _utc_now
        self._url_by_code: dict[str, str] = {}
        self._code_by_url: dict[str, str] = {}
        self._redirect_count_by_code: dict[str, int] = {}
        self._created_at_by_code: dict[str, datetime] = {}
        self._lock = Lock()

    def shorten(self, url: str) -> str:
        """Return a stable unique code for one valid absolute HTTP(S) URL."""

        validated = _validate_url(url)
        with self._lock:
            existing = self._code_by_url.get(validated)
            if existing is not None:
                return existing

            nonce = 0
            while True:
                code = _candidate_code(validated, nonce)
                owner = self._url_by_code.get(code)
                if owner is None or owner == validated:
                    self._url_by_code[code] = validated
                    self._code_by_url[validated] = code
                    self._redirect_count_by_code[code] = 0
                    self._created_at_by_code[code] = _validated_now(self._clock())
                    return code
                nonce += 1

    def resolve(self, code: str) -> str:
        """Return the original URL for a known code."""

        if not isinstance(code, str) or not code:
            raise UnknownShortCodeError("Unknown short code.")
        with self._lock:
            try:
                original_url = self._url_by_code[code]
                created_at = self._created_at_by_code[code]
            except KeyError as error:
                raise UnknownShortCodeError(
                    f"Unknown short code: {code}."
                ) from error
            if self._is_expired(created_at):
                raise UnknownShortCodeError(f"Unknown or expired short code: {code}.")
            self._redirect_count_by_code[code] += 1
            return original_url

    def redirect_count(self, code: str) -> int:
        """Return the successful-resolution count without changing it."""

        if not isinstance(code, str) or not code:
            raise UnknownShortCodeError("Unknown short code.")
        with self._lock:
            try:
                redirect_count = self._redirect_count_by_code[code]
                created_at = self._created_at_by_code[code]
            except KeyError as error:
                raise UnknownShortCodeError(
                    f"Unknown short code: {code}."
                ) from error
            if self._is_expired(created_at):
                raise UnknownShortCodeError(f"Unknown or expired short code: {code}.")
            return redirect_count

    def _is_expired(self, created_at: datetime) -> bool:
        return _validated_now(self._clock()) >= created_at + SHORT_URL_TTL


def _validated_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Clock must return a timezone-aware datetime.")
    return value


def _validate_url(url: str) -> str:
    if not isinstance(url, str) or not url or url != url.strip():
        raise InvalidURLError("URL must be a non-empty absolute HTTP(S) URL.")
    if any(character.isspace() for character in url):
        raise InvalidURLError("URL must not contain whitespace.")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise InvalidURLError("URL authority is malformed.") from error
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise InvalidURLError("URL must be an absolute HTTP(S) URL.")
    return url


def _candidate_code(url: str, nonce: int) -> str:
    digest = hashlib.sha256(f"{url}\0{nonce}".encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest[:6]).decode("ascii").rstrip("=")
