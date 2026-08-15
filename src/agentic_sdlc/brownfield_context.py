"""Bounded, application-owned codebase context for brownfield reasoning."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentic_sdlc.brownfield_baseline import BrownfieldBaselineProvenance
from agentic_sdlc.workspace_contracts import (
    normalize_repository_path,
    workspace_file_content_hash,
)
from agentic_sdlc.workspace_integration_contracts import WorkspaceBinding
from agentic_sdlc.workspace_runtime import (
    IsolatedWorkspace,
    WorkspaceRuntimeError,
    read_isolated_workspace_file,
    snapshot_isolated_workspace,
)


BROWNFIELD_CODEBASE_CONTEXT_SCHEMA_VERSION = "brownfield-codebase-context-v1"
BROWNFIELD_TEXT_POLICY_VERSION = "brownfield-text-files-v1"
DEFAULT_BROWNFIELD_CONTEXT_MAX_FILES = 200
DEFAULT_BROWNFIELD_CONTEXT_MAX_BYTES_PER_FILE = 262_144
DEFAULT_BROWNFIELD_CONTEXT_MAX_TOTAL_TEXT_BYTES = 1_048_576

_TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".conf",
        ".css",
        ".csv",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".py",
        ".rst",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_TEXT_FILENAMES = frozenset({"dockerfile", "makefile"})


class BrownfieldCodebaseFileKind(StrEnum):
    """Whether authoritative file content is eligible for LLM reasoning."""

    TEXT = "TEXT"
    UNSUPPORTED = "UNSUPPORTED"


class BrownfieldCodebaseContextIssueCode(StrEnum):
    """Stable application failures while constructing bounded context."""

    BASELINE_MISMATCH = "BASELINE_MISMATCH"
    FILE_LIMIT = "FILE_LIMIT"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    TOTAL_TEXT_LIMIT = "TOTAL_TEXT_LIMIT"
    INVALID_TEXT = "INVALID_TEXT"
    WORKSPACE_INTEGRITY = "WORKSPACE_INTEGRITY"


class BrownfieldCodebaseContextError(RuntimeError):
    """Fail-closed bounded-context construction error."""

    def __init__(
        self,
        code: BrownfieldCodebaseContextIssueCode,
        message: str,
        *,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


class BrownfieldCodebaseContextLimits(BaseModel):
    """Immutable limits recorded with the resulting reasoning context."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_files: int = Field(ge=1)
    max_bytes_per_file: int = Field(ge=1)
    max_total_text_bytes: int = Field(ge=1)


DEFAULT_BROWNFIELD_CODEBASE_CONTEXT_LIMITS = BrownfieldCodebaseContextLimits(
    max_files=DEFAULT_BROWNFIELD_CONTEXT_MAX_FILES,
    max_bytes_per_file=DEFAULT_BROWNFIELD_CONTEXT_MAX_BYTES_PER_FILE,
    max_total_text_bytes=DEFAULT_BROWNFIELD_CONTEXT_MAX_TOTAL_TEXT_BYTES,
)


class BrownfieldCodebaseFile(BaseModel):
    """One authoritative baseline file and any safely included UTF-8 content."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: BrownfieldCodebaseFileKind
    content: str | None
    byte_count: int | None = Field(default=None, ge=0)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_repository_path(value)

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        if self.kind is BrownfieldCodebaseFileKind.UNSUPPORTED:
            if self.content is not None or self.byte_count is not None:
                raise ValueError("Unsupported files must not expose content.")
            return self
        if self.content is None or self.byte_count is None:
            raise ValueError("Text files require complete content and byte count.")
        if self.byte_count != len(self.content.encode("utf-8")):
            raise ValueError("Text byte count must match complete UTF-8 content.")
        if self.content_hash != workspace_file_content_hash(self.content):
            raise ValueError("Text content hash must match complete content.")
        return self


class BrownfieldCodebaseContextData(TypedDict):
    """Checkpoint-safe JSON representation of a brownfield codebase context."""

    schema_version: Literal["brownfield-codebase-context-v1"]
    text_policy_version: Literal["brownfield-text-files-v1"]
    context_id: str
    baseline_id: str
    selected_project_name: str
    binding: dict[str, str]
    limits: dict[str, int]
    complete_authoritative_inventory: Literal[True]
    total_text_bytes: int
    files: list[dict[str, object]]


class BrownfieldCodebaseContext(BaseModel):
    """Complete bounded projection of one exact seeded engineering baseline."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["brownfield-codebase-context-v1"]
    text_policy_version: Literal["brownfield-text-files-v1"]
    context_id: str = Field(min_length=1)
    baseline_id: str = Field(min_length=1)
    selected_project_name: str = Field(min_length=1)
    binding: WorkspaceBinding
    limits: BrownfieldCodebaseContextLimits
    complete_authoritative_inventory: Literal[True]
    total_text_bytes: int = Field(ge=0)
    files: tuple[BrownfieldCodebaseFile, ...]

    @model_validator(mode="after")
    def validate_canonical_context(self) -> Self:
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("Brownfield context files must be uniquely canonical.")
        if len(self.files) > self.limits.max_files:
            raise ValueError("Brownfield context exceeds its recorded file limit.")
        observed_text_bytes = sum(
            item.byte_count or 0
            for item in self.files
            if item.kind is BrownfieldCodebaseFileKind.TEXT
        )
        if observed_text_bytes != self.total_text_bytes:
            raise ValueError("Brownfield context total text bytes are inconsistent.")
        if any(
            (item.byte_count or 0) > self.limits.max_bytes_per_file
            for item in self.files
            if item.kind is BrownfieldCodebaseFileKind.TEXT
        ):
            raise ValueError("Brownfield context exceeds its per-file limit.")
        if self.total_text_bytes > self.limits.max_total_text_bytes:
            raise ValueError("Brownfield context exceeds its total text limit.")
        if self.context_id != _brownfield_codebase_context_id(self):
            raise ValueError("Brownfield codebase context identity is not canonical.")
        return self

    @property
    def text_paths(self) -> tuple[str, ...]:
        """Return only the application-approved paths safe for text reasoning."""

        return tuple(
            item.path
            for item in self.files
            if item.kind is BrownfieldCodebaseFileKind.TEXT
        )


def build_brownfield_codebase_context(
    workspace: IsolatedWorkspace,
    provenance: BrownfieldBaselineProvenance,
    *,
    limits: BrownfieldCodebaseContextLimits = (
        DEFAULT_BROWNFIELD_CODEBASE_CONTEXT_LIMITS
    ),
) -> BrownfieldCodebaseContext:
    """Read only the verified seeded baseline through its workspace capability."""

    if len(provenance.engineering_files) > limits.max_files:
        raise BrownfieldCodebaseContextError(
            BrownfieldCodebaseContextIssueCode.FILE_LIMIT,
            "Brownfield engineering inventory exceeds the reasoning file limit.",
        )
    try:
        before = snapshot_isolated_workspace(workspace)
    except WorkspaceRuntimeError as error:
        raise BrownfieldCodebaseContextError(
            BrownfieldCodebaseContextIssueCode.WORKSPACE_INTEGRITY,
            "Brownfield workspace could not be verified for reasoning context.",
        ) from error
    if (
        before.workspace_id != provenance.seed_result.workspace_id
        or before.snapshot_id != provenance.governed_baseline_snapshot_id
        or before.files != provenance.engineering_files
    ):
        raise BrownfieldCodebaseContextError(
            BrownfieldCodebaseContextIssueCode.BASELINE_MISMATCH,
            "Brownfield workspace does not match its verified seeded baseline.",
        )

    files: list[BrownfieldCodebaseFile] = []
    total_text_bytes = 0
    for file_state in provenance.engineering_files:
        if not _is_eligible_text_path(file_state.path):
            files.append(
                BrownfieldCodebaseFile(
                    path=file_state.path,
                    content_hash=file_state.content_hash,
                    kind=BrownfieldCodebaseFileKind.UNSUPPORTED,
                    content=None,
                    byte_count=None,
                )
            )
            continue
        try:
            contents = read_isolated_workspace_file(workspace, file_state.path)
        except WorkspaceRuntimeError as error:
            raise BrownfieldCodebaseContextError(
                BrownfieldCodebaseContextIssueCode.WORKSPACE_INTEGRITY,
                "Brownfield text file could not be read from the governed workspace.",
                path=file_state.path,
            ) from error
        if contents is None or hashlib.sha256(contents).hexdigest() != (
            file_state.content_hash
        ):
            raise BrownfieldCodebaseContextError(
                BrownfieldCodebaseContextIssueCode.BASELINE_MISMATCH,
                "Brownfield text file differs from baseline evidence.",
                path=file_state.path,
            )
        if len(contents) > limits.max_bytes_per_file:
            raise BrownfieldCodebaseContextError(
                BrownfieldCodebaseContextIssueCode.FILE_TOO_LARGE,
                "Brownfield text file exceeds the per-file reasoning limit.",
                path=file_state.path,
            )
        try:
            text = contents.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise BrownfieldCodebaseContextError(
                BrownfieldCodebaseContextIssueCode.INVALID_TEXT,
                "Eligible brownfield text file is not valid UTF-8.",
                path=file_state.path,
            ) from error
        total_text_bytes += len(contents)
        if total_text_bytes > limits.max_total_text_bytes:
            raise BrownfieldCodebaseContextError(
                BrownfieldCodebaseContextIssueCode.TOTAL_TEXT_LIMIT,
                "Brownfield text content exceeds the total reasoning limit.",
            )
        files.append(
            BrownfieldCodebaseFile(
                path=file_state.path,
                content_hash=file_state.content_hash,
                kind=BrownfieldCodebaseFileKind.TEXT,
                content=text,
                byte_count=len(contents),
            )
        )

    try:
        after = snapshot_isolated_workspace(workspace)
    except WorkspaceRuntimeError as error:
        raise BrownfieldCodebaseContextError(
            BrownfieldCodebaseContextIssueCode.WORKSPACE_INTEGRITY,
            "Brownfield workspace could not be reverified after context reads.",
        ) from error
    if after != before:
        raise BrownfieldCodebaseContextError(
            BrownfieldCodebaseContextIssueCode.WORKSPACE_INTEGRITY,
            "Brownfield workspace changed while reasoning context was constructed.",
        )

    values = {
        "schema_version": BROWNFIELD_CODEBASE_CONTEXT_SCHEMA_VERSION,
        "text_policy_version": BROWNFIELD_TEXT_POLICY_VERSION,
        "context_id": "pending",
        "baseline_id": provenance.baseline_id,
        "selected_project_name": provenance.selected_project_name,
        "binding": WorkspaceBinding(
            workspace_id=before.workspace_id,
            snapshot_id=before.snapshot_id,
        ),
        "limits": limits,
        "complete_authoritative_inventory": True,
        "total_text_bytes": total_text_bytes,
        "files": tuple(files),
    }
    provisional = BrownfieldCodebaseContext.model_construct(**values)
    values["context_id"] = _brownfield_codebase_context_id(provisional)
    return BrownfieldCodebaseContext.model_validate(values)


def brownfield_codebase_context_from_value(
    value: BrownfieldCodebaseContext | dict[str, object],
) -> BrownfieldCodebaseContext:
    """Restore and revalidate canonical context from checkpoint-safe state."""

    if isinstance(value, BrownfieldCodebaseContext):
        return BrownfieldCodebaseContext.model_validate_json(value.model_dump_json())
    return BrownfieldCodebaseContext.model_validate_json(json.dumps(value))


def _is_eligible_text_path(path: str) -> bool:
    normalized = normalize_repository_path(path)
    name = PurePosixPath(normalized).name.casefold()
    suffix = PurePosixPath(normalized).suffix.casefold()
    return name in _TEXT_FILENAMES or suffix in _TEXT_SUFFIXES


def _brownfield_codebase_context_id(context: BrownfieldCodebaseContext) -> str:
    payload = context.model_dump(mode="json", exclude={"context_id"})
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return f"BROWNFIELD-CONTEXT-{digest[:20].upper()}"
