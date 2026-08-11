"""Resolve immutable user requirement submissions before workflow execution."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypedDict, cast


class RequirementSubmissionError(ValueError):
    """Raised when a requirement source cannot become valid workflow input."""


class RequirementSourceKind(StrEnum):
    """Application-owned origin of one exact textual requirement submission."""

    DEMO = "demo"
    INLINE = "inline"
    FILE = "file"


RequirementSourceKindValue = Literal["demo", "inline", "file"]


class RequirementSubmissionData(TypedDict):
    """JSON-safe immutable source evidence retained in checkpointed state."""

    source_kind: RequirementSourceKindValue
    original_text: str
    normalized_text: str
    original_sha256: str
    normalized_sha256: str
    source_filename: str | None


@dataclass(frozen=True, slots=True)
class RequirementSubmission:
    """Resolved exact and normalized identities for one requirement source."""

    source_kind: RequirementSourceKind
    original_text: str
    source_filename: str | None = None
    normalized_text: str = field(init=False)
    original_sha256: str = field(init=False)
    normalized_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        normalized_text = normalize_requirement_text(self.original_text)
        if not normalized_text:
            raise RequirementSubmissionError(
                "Requirement input must contain non-whitespace text."
            )
        object.__setattr__(self, "normalized_text", normalized_text)
        object.__setattr__(self, "original_sha256", _sha256_text(self.original_text))
        object.__setattr__(
            self,
            "normalized_sha256",
            _sha256_text(normalized_text),
        )

    @classmethod
    def from_text(
        cls,
        source_kind: RequirementSourceKind,
        original_text: str,
        *,
        source_filename: str | None = None,
    ) -> RequirementSubmission:
        """Resolve exact text once and derive its minimal deterministic form."""

        return cls(
            source_kind=source_kind,
            original_text=original_text,
            source_filename=source_filename,
        )

    def as_state_data(self) -> RequirementSubmissionData:
        """Return a JSON-safe copy for durable workflow state and evidence."""

        return {
            "source_kind": cast(RequirementSourceKindValue, self.source_kind.value),
            "original_text": self.original_text,
            "normalized_text": self.normalized_text,
            "original_sha256": self.original_sha256,
            "normalized_sha256": self.normalized_sha256,
            "source_filename": self.source_filename,
        }


def resolve_inline_requirement(text: str) -> RequirementSubmission:
    """Resolve one inline CLI submission without semantic transformation."""

    return RequirementSubmission.from_text(RequirementSourceKind.INLINE, text)


def resolve_requirement_file(path: Path) -> RequirementSubmission:
    """Read and decode one requirement file exactly once before workflow start."""

    source_path = Path(path)
    try:
        contents = source_path.read_bytes()
    except FileNotFoundError as error:
        raise RequirementSubmissionError(
            f"Requirement file does not exist: {source_path}"
        ) from error
    except OSError as error:
        detail = error.strerror or str(error)
        raise RequirementSubmissionError(
            f"Requirement file could not be read: {source_path} ({detail})"
        ) from error
    try:
        original_text = contents.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RequirementSubmissionError(
            f"Requirement file is not valid UTF-8: {source_path}"
        ) from error
    return RequirementSubmission.from_text(
        RequirementSourceKind.FILE,
        original_text,
        source_filename=_safe_source_filename(source_path),
    )


def normalize_requirement_text(text: str) -> str:
    """Apply only BOM, newline, and outer-whitespace normalization."""

    without_bom = text.removeprefix("\ufeff")
    normalized_newlines = without_bom.replace("\r\n", "\n").replace("\r", "\n")
    return normalized_newlines.strip()


def deterministic_project_name(submission: RequirementSubmission) -> str:
    """Derive a safe stable application identity when the user omits a name."""

    return f"project-{submission.normalized_sha256[:12]}"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_source_filename(path: Path) -> str:
    name = path.name
    safe_name = "".join(
        character
        if character not in {"/", "\\"}
        and not unicodedata.category(character).startswith("C")
        else "_"
        for character in name
    ).strip()
    return safe_name[:255] or "requirement"
