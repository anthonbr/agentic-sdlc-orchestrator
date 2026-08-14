"""Deterministic, non-authoritative TaskGraph presentation helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any


class TaskGraphPresentationError(ValueError):
    """Raised when an authoritative graph projection cannot be rendered safely."""


def task_graph_mermaid(
    candidate_task_graph: Mapping[str, Any],
    graph_semantics: Mapping[str, Any],
) -> str:
    """Render one canonical TaskGraph as deterministic presentation-only Mermaid."""

    tasks = _mapping_sequence(candidate_task_graph.get("tasks"))
    task_by_id: dict[str, Mapping[str, Any]] = {}
    for task in tasks:
        task_id = _required_text(task, "task_id")
        if task_id in task_by_id:
            raise TaskGraphPresentationError(
                f"TaskGraph presentation received duplicate task ID: {task_id}"
            )
        task_by_id[task_id] = task

    if not task_by_id:
        raise TaskGraphPresentationError(
            "TaskGraph presentation requires at least one canonical task."
        )

    lines = [
        "flowchart LR",
        '  ENTRY(["ENTRY"])',
        '  EXIT(["EXIT"])',
    ]
    for task_id, task in task_by_id.items():
        title = _required_text(task, "title")
        lines.append(
            f'  {_mermaid_node_id(task_id)}["{_safe_label(task_id)}'
            f'<br/>{_safe_label(title)}"]'
        )

    for task_id in _text_sequence(graph_semantics.get("entry_ready_tasks")):
        lines.append(f"  ENTRY --> {_known_node_id(task_id, task_by_id)}")

    for task_id, task in task_by_id.items():
        target_node = _mermaid_node_id(task_id)
        for dependency_id in _text_sequence(task.get("depends_on")):
            dependency_node = _known_node_id(dependency_id, task_by_id)
            lines.append(f"  {dependency_node} --> {target_node}")

    for task_id in _text_sequence(
        graph_semantics.get("exit_predecessor_tasks")
    ):
        lines.append(f"  {_known_node_id(task_id, task_by_id)} --> EXIT")

    return "\n".join(lines)


def _known_node_id(
    task_id: str,
    task_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    if task_id not in task_by_id:
        raise TaskGraphPresentationError(
            "TaskGraph presentation references an unknown canonical task ID: "
            f"{task_id}"
        )
    return _mermaid_node_id(task_id)


def _mermaid_node_id(task_id: str) -> str:
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return f"task_{digest}"


def _safe_label(value: str) -> str:
    """Keep readable text while removing every Mermaid control character."""

    collapsed = " ".join(value.split())
    allowed_punctuation = frozenset(" .,:;!?()/_+-'")
    safe = "".join(
        character
        if character.isalnum() or character in allowed_punctuation
        else " "
        for character in collapsed
    )
    normalized = " ".join(safe.split())
    return normalized or "Untitled task"


def _required_text(value: Mapping[str, Any], field_name: str) -> str:
    field_value = value.get(field_name)
    if not isinstance(field_value, str) or not field_value:
        raise TaskGraphPresentationError(
            f"TaskGraph presentation requires non-empty {field_name}."
        )
    return field_value


def _text_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TaskGraphPresentationError(
            "TaskGraph presentation expected a sequence of canonical task IDs."
        )
    if any(not isinstance(item, str) or not item for item in value):
        raise TaskGraphPresentationError(
            "TaskGraph presentation received an invalid canonical task ID."
        )
    return tuple(value)


def _mapping_sequence(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TaskGraphPresentationError(
            "TaskGraph presentation expected a sequence of canonical tasks."
        )
    if any(not isinstance(item, Mapping) for item in value):
        raise TaskGraphPresentationError(
            "TaskGraph presentation received an invalid canonical task."
        )
    return tuple(value)
