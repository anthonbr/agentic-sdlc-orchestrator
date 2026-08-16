"""Process-local domain state for the URL shortener prototype."""

from __future__ import annotations

from dataclasses import dataclass
import secrets
import string
from threading import Lock
from typing import Callable
import unicodedata
from urllib.parse import SplitResult, urlsplit

CODE_LENGTH = 8
CODE_ALPHABET = string.ascii_letters + string.digits
DEFAULT_SHORT_URL_BASE = "http://127.0.0.1:8000"


class InvalidURLError(ValueError):
    """Raised when a submitted URL violates the fixed validation rules."""


class UnknownCodeError(KeyError):
    """Raised when no mapping exists for a short code."""


@dataclass(frozen=True, slots=True)
class Mapping:
    """Immutable public snapshot of a stored mapping."""

    code: str
    short_url: str
    url: str
    redirect_count: int


@dataclass(slots=True)
class _Entry:
    code: str
    url: str
    redirect_count: int = 0


def validate_url(value: object) -> str:
    """Validate and return the exact submitted URL string.

    Validation is entirely local and syntactic. No normalization, DNS lookup,
    reachability check, or destination-safety request is performed.
    """

    if not isinstance(value, str) or not value:
        raise InvalidURLError("url must be a non-empty string")

    if any(character.isspace() or unicodedata.category(character) == "Cc" for character in value):
        raise InvalidURLError("url must not contain whitespace or control characters")

    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise InvalidURLError("url is malformed") from exc

    if parsed.scheme not in {"http", "https"}:
        raise InvalidURLError("url must use the http or https scheme")

    try:
        hostname = parsed.hostname
    except ValueError as exc:
        raise InvalidURLError("url host is malformed") from exc

    if not hostname:
        raise InvalidURLError("url must contain a host")

    _validate_explicit_port(parsed)
    return value


def _validate_explicit_port(parsed: SplitResult) -> None:
    """Reject malformed, empty, non-ASCII, or out-of-range explicit ports."""

    authority = parsed.netloc.rsplit("@", 1)[-1]
    port_text: str | None = None

    if authority.startswith("["):
        closing_bracket = authority.find("]")
        if closing_bracket < 0:
            raise InvalidURLError("url host is malformed")
        suffix = authority[closing_bracket + 1 :]
        if suffix:
            if not suffix.startswith(":") or ":" in suffix[1:]:
                raise InvalidURLError("url port is malformed")
            port_text = suffix[1:]
    elif ":" in authority:
        host_part, port_text = authority.rsplit(":", 1)
        if ":" in host_part:
            raise InvalidURLError("IPv6 hosts must use brackets")

    if port_text is not None and (
        not port_text or not port_text.isascii() or not port_text.isdigit()
    ):
        raise InvalidURLError("url port must be numeric")

    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidURLError("url port must be between 1 and 65535") from exc

    if port_text is not None and (port is None or not 1 <= port <= 65535):
        raise InvalidURLError("url port must be between 1 and 65535")


def generate_code() -> str:
    """Generate one eight-character ASCII alphanumeric candidate code."""

    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


class MappingStore:
    """Thread-safe, process-lifetime in-memory mappings and redirect counts."""

    def __init__(
        self,
        code_generator: Callable[[], str] = generate_code,
        short_url_base: str = DEFAULT_SHORT_URL_BASE,
    ) -> None:
        self._code_generator = code_generator
        self._short_url_base = short_url_base.rstrip("/")
        self._by_code: dict[str, _Entry] = {}
        self._code_by_url: dict[str, str] = {}
        self._lock = Lock()

    def create_or_get(self, submitted_url: object) -> tuple[Mapping, bool]:
        """Return a mapping snapshot and whether a new mapping was created."""

        url = validate_url(submitted_url)
        with self._lock:
            existing_code = self._code_by_url.get(url)
            if existing_code is not None:
                return self._snapshot(self._by_code[existing_code]), False

            while True:
                code = self._code_generator()
                self._require_valid_generated_code(code)
                if code not in self._by_code:
                    break

            entry = _Entry(code=code, url=url)
            self._by_code[code] = entry
            self._code_by_url[url] = code
            return self._snapshot(entry), True

    def get_mapping(self, code: str) -> Mapping:
        """Return analytics without changing the redirect count."""

        with self._lock:
            entry = self._by_code.get(code)
            if entry is None:
                raise UnknownCodeError(code)
            return self._snapshot(entry)

    def record_redirect(self, code: str) -> Mapping:
        """Atomically increment a known mapping once and return its snapshot."""

        with self._lock:
            entry = self._by_code.get(code)
            if entry is None:
                raise UnknownCodeError(code)
            entry.redirect_count += 1
            return self._snapshot(entry)

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_code)

    def _snapshot(self, entry: _Entry) -> Mapping:
        return Mapping(
            code=entry.code,
            short_url=f"{self._short_url_base}/{entry.code}",
            url=entry.url,
            redirect_count=entry.redirect_count,
        )

    @staticmethod
    def _require_valid_generated_code(code: str) -> None:
        if (
            not isinstance(code, str)
            or len(code) != CODE_LENGTH
            or any(character not in CODE_ALPHABET for character in code)
        ):
            raise ValueError(
                "code generator must return exactly eight ASCII alphanumeric characters"
            )
