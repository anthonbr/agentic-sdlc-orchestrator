"""In-memory URL-shortening domain service."""

from __future__ import annotations

import base64
import hashlib
from threading import Lock
from urllib.parse import urlsplit


class URLShortenerError(Exception):
    """Base class for domain-level URL-shortener failures."""


class InvalidURLError(URLShortenerError):
    """Raised when a submitted URL is not an absolute HTTP(S) URL."""


class UnknownShortCodeError(URLShortenerError):
    """Raised when no original URL exists for a short code."""


class URLShortener:
    """Deterministic, collision-safe, process-local URL repository."""

    def __init__(self) -> None:
        self._url_by_code: dict[str, str] = {}
        self._code_by_url: dict[str, str] = {}
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
                    return code
                nonce += 1

    def resolve(self, code: str) -> str:
        """Return the original URL for a known code."""

        if not isinstance(code, str) or not code:
            raise UnknownShortCodeError("Unknown short code.")
        with self._lock:
            try:
                return self._url_by_code[code]
            except KeyError as error:
                raise UnknownShortCodeError(
                    f"Unknown short code: {code}."
                ) from error


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
