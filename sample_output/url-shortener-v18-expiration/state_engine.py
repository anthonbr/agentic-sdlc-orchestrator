"""Process-local domain state for the URL shortener prototype."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
import secrets
import string
from threading import Lock
from typing import Callable
import unicodedata
from urllib.parse import SplitResult, urlsplit

CODE_LENGTH = 8
CODE_ALPHABET = string.ascii_letters + string.digits
DEFAULT_SHORT_URL_BASE = "http://127.0.0.1:8000"
_EXPIRATION_PATTERN = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"T(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})Z$"
)
_OMITTED = object()


class InvalidURLError(ValueError):
    """Raised when a submitted URL violates the fixed validation rules."""


class InvalidExpirationError(ValueError):
    """Raised when a supplied expiration is malformed or is not in the future."""


class UnknownCodeError(KeyError):
    """Raised when no mapping exists for a short code."""


class ExpiredCodeError(LookupError):
    """Raised when a mapping exists but may no longer be redirected."""


@dataclass(frozen=True, slots=True)
class Mapping:
    """Immutable public snapshot of a non-expiring stored mapping."""

    code: str
    short_url: str
    url: str
    redirect_count: int


@dataclass(frozen=True, slots=True)
class ExpiringMapping(Mapping):
    """Immutable public snapshot of a stored mapping with an expiration."""

    expires_at: str


@dataclass(slots=True)
class _Entry:
    code: str
    url: str
    expires_at: str | None
    expires_at_utc: datetime | None
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


def validate_expires_at(value: object, now: datetime) -> tuple[str, datetime]:
    """Validate one canonical whole-second UTC expiration against one time sample."""

    if not isinstance(value, str):
        raise InvalidExpirationError("expires_at must be a string")

    match = _EXPIRATION_PATTERN.fullmatch(value)
    if match is None or not value.isascii():
        raise InvalidExpirationError(
            "expires_at must use exact YYYY-MM-DDTHH:MM:SSZ UTC form"
        )

    try:
        expiration = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
            tzinfo=UTC,
        )
    except ValueError as exc:
        raise InvalidExpirationError("expires_at is not a valid UTC timestamp") from exc

    current = _as_utc(now)
    if expiration <= current:
        raise InvalidExpirationError("expires_at must be later than current UTC time")

    return value, expiration


def generate_code() -> str:
    """Generate one eight-character ASCII alphanumeric candidate code."""

    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Return an injected clock value normalized to UTC."""

    if not isinstance(value, datetime):
        raise TypeError("UTC time provider must return a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC time provider must return an aware datetime")
    return value.astimezone(UTC)


class MappingStore:
    """Thread-safe, process-lifetime in-memory mappings and redirect counts."""

    def __init__(
        self,
        code_generator: Callable[[], str] = generate_code,
        short_url_base: str = DEFAULT_SHORT_URL_BASE,
        utc_now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._code_generator = code_generator
        self._short_url_base = short_url_base.rstrip("/")
        self._utc_now = utc_now
        self._by_code: dict[str, _Entry] = {}
        self._code_by_identity: dict[tuple[str, str | None], str] = {}
        self._lock = Lock()

    def create_or_get(
        self,
        submitted_url: object,
        expires_at: object = _OMITTED,
    ) -> tuple[Mapping, bool]:
        """Return a matching active mapping or create an independent mapping.

        Omitting ``expires_at`` creates or deduplicates a non-expiring mapping.
        Supplying any value, including ``None``, invokes strict expiration
        validation before duplicate lookup and before any state mutation.
        """

        url = validate_url(submitted_url)

        with self._lock:
            expiration_text: str | None = None
            expiration_utc: datetime | None = None
            current: datetime | None = None

            if expires_at is not _OMITTED:
                current = _as_utc(self._utc_now())
                expiration_text, expiration_utc = validate_expires_at(expires_at, current)

            identity = (url, expiration_text)
            existing_code = self._code_by_identity.get(identity)
            if existing_code is not None:
                existing = self._by_code[existing_code]
                if not self._is_expired(existing, current):
                    return self._snapshot(existing), False

            while True:
                code = self._code_generator()
                self._require_valid_generated_code(code)
                if code not in self._by_code:
                    break

            entry = _Entry(
                code=code,
                url=url,
                expires_at=expiration_text,
                expires_at_utc=expiration_utc,
            )
            self._by_code[code] = entry
            self._code_by_identity[identity] = code
            return self._snapshot(entry), True

    def get_mapping(self, code: str) -> Mapping:
        """Return analytics for an active or expired mapping without mutation."""

        with self._lock:
            entry = self._by_code.get(code)
            if entry is None:
                raise UnknownCodeError(code)
            return self._snapshot(entry)

    def record_redirect(self, code: str) -> Mapping:
        """Atomically decide expiration and count one successful redirect."""

        with self._lock:
            entry = self._by_code.get(code)
            if entry is None:
                raise UnknownCodeError(code)

            current = _as_utc(self._utc_now()) if entry.expires_at_utc is not None else None
            if self._is_expired(entry, current):
                raise ExpiredCodeError(code)

            entry.redirect_count += 1
            return self._snapshot(entry)

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_code)

    @staticmethod
    def _is_expired(entry: _Entry, current: datetime | None) -> bool:
        if entry.expires_at_utc is None:
            return False
        if current is None:
            raise ValueError("current UTC time is required for an expiring mapping")
        return current >= entry.expires_at_utc

    def _snapshot(self, entry: _Entry) -> Mapping:
        values = {
            "code": entry.code,
            "short_url": f"{self._short_url_base}/{entry.code}",
            "url": entry.url,
            "redirect_count": entry.redirect_count,
        }
        if entry.expires_at is None:
            return Mapping(**values)
        return ExpiringMapping(**values, expires_at=entry.expires_at)

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
