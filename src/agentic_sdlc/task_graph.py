"""Canonical engineering task graph and deterministic graph interpretation."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentic_sdlc.requirement_spec import (
    LINEAGE_NAMESPACE,
    ApprovedRequirementSpec,
)


class TaskType(StrEnum):
    """Broad SDLC work classifications; tasks are definitions, not executions."""

    DESIGN = "DESIGN"
    IMPLEMENTATION = "IMPLEMENTATION"
    TEST = "TEST"
    DOCUMENTATION = "DOCUMENTATION"
    VALIDATION = "VALIDATION"
    RELEASE = "RELEASE"


class ProposedTask(BaseModel):
    """Semantic task proposed by the LLM using temporary keys."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    key: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    task_type: TaskType
    depends_on: list[str]
    requirement_refs: list[str]
    acceptance_criteria_refs: list[str]
    risk_refs: list[str]
    ambiguity_refs: list[str]
    expected_outputs: list[str]

    @field_validator(
        "depends_on",
        "requirement_refs",
        "acceptance_criteria_refs",
        "risk_refs",
        "ambiguity_refs",
        "expected_outputs",
    )
    @classmethod
    def reject_blank_list_items(cls, values: list[str]) -> list[str]:
        stripped = [value.strip() for value in values]
        if any(not value for value in stripped):
            raise ValueError("collection items must be non-empty text")
        return stripped


class ProposedTaskGraph(BaseModel):
    """Schema-backed LLM proposal awaiting deterministic normalization."""

    model_config = ConfigDict(extra="forbid", strict=True)

    tasks: list[ProposedTask] = Field(min_length=1)


class Task(BaseModel):
    """Canonical task definition with application-owned identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str
    lineage_id: str
    source_key: str
    title: str
    description: str
    task_type: TaskType
    depends_on: tuple[str, ...]
    requirement_refs: tuple[str, ...]
    acceptance_criteria_refs: tuple[str, ...]
    risk_refs: tuple[str, ...]
    ambiguity_refs: tuple[str, ...]
    expected_outputs: tuple[str, ...]


class TaskGraph(BaseModel):
    """Immutable canonical task graph; connectivity lives only in depends_on."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    graph_id: str
    lineage_id: str
    version: int = Field(ge=1)
    requirement_spec_id: str
    requirement_spec_version: int
    supersedes_graph_id: str | None
    created_at: str
    content_hash: str
    tasks: tuple[Task, ...]


class TaskGraphSemantics(BaseModel):
    """Deterministically derived, non-authoritative graph interpretation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    topological_order: tuple[str, ...]
    execution_layers: tuple[tuple[str, ...], ...]
    entry_ready_tasks: tuple[str, ...]
    exit_predecessor_tasks: tuple[str, ...]
    synchronization_points: tuple[str, ...]


class TaskGraphValidationError(ValueError):
    """Deterministic semantic or dependency-graph validation failure."""


def normalize_and_validate_task_graph(
    proposal: ProposedTaskGraph,
    spec: ApprovedRequirementSpec,
    *,
    version: int,
    supersedes_graph_id: str | None = None,
    graph_lineage_id: str | None = None,
    created_at: str | None = None,
) -> tuple[TaskGraph, TaskGraphSemantics]:
    """Assign authoritative identities, validate lineage, and derive semantics."""

    keys = [task.key for task in proposal.tasks]
    if len(keys) != len(set(keys)):
        raise TaskGraphValidationError("Task proposal keys must be unique.")
    if any(key.upper() in {"ENTRY", "EXIT"} for key in keys):
        raise TaskGraphValidationError("ENTRY and EXIT are deterministic semantics.")

    key_to_id = {
        key: f"TASK-{index:03d}" for index, key in enumerate(keys, start=1)
    }
    graph_lineage = graph_lineage_id or str(
        uuid5(LINEAGE_NAMESPACE, f"task-graph:{spec.lineage_id}")
    )
    _validate_proposal_references(proposal, spec, key_to_id)

    tasks = tuple(
        Task(
            task_id=key_to_id[proposed.key],
            lineage_id=str(
                uuid5(
                    LINEAGE_NAMESPACE,
                    f"task:{graph_lineage}:{proposed.key}",
                )
            ),
            source_key=proposed.key,
            title=proposed.title,
            description=proposed.description,
            task_type=proposed.task_type,
            depends_on=tuple(key_to_id[key] for key in proposed.depends_on),
            requirement_refs=tuple(proposed.requirement_refs),
            acceptance_criteria_refs=tuple(proposed.acceptance_criteria_refs),
            risk_refs=tuple(proposed.risk_refs),
            ambiguity_refs=tuple(proposed.ambiguity_refs),
            expected_outputs=tuple(proposed.expected_outputs),
        )
        for proposed in proposal.tasks
    )
    semantics = derive_task_graph_semantics(tasks)
    content = {
        "requirement_spec_id": spec.spec_id,
        "requirement_spec_version": spec.version,
        "tasks": [task.model_dump(mode="json") for task in tasks],
    }
    content_hash = _content_hash(content)
    graph = TaskGraph(
        graph_id=f"GRAPH-{content_hash[:12].upper()}-V{version:03d}",
        lineage_id=graph_lineage,
        version=version,
        requirement_spec_id=spec.spec_id,
        requirement_spec_version=spec.version,
        supersedes_graph_id=supersedes_graph_id,
        created_at=created_at or datetime.now(UTC).isoformat(),
        content_hash=content_hash,
        tasks=tasks,
    )
    return graph, semantics


def derive_task_graph_semantics(tasks: tuple[Task, ...]) -> TaskGraphSemantics:
    """Validate a canonical dependency DAG and derive stable execution layers."""

    if not tasks:
        raise TaskGraphValidationError("A task graph must contain at least one task.")
    task_ids = [task.task_id for task in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise TaskGraphValidationError("Canonical task IDs must be unique.")
    known_ids = set(task_ids)
    dependents: dict[str, set[str]] = defaultdict(set)
    indegree: dict[str, int] = {}
    synchronization_points: list[str] = []
    for task in tasks:
        if len(task.depends_on) != len(set(task.depends_on)):
            raise TaskGraphValidationError(
                f"{task.task_id} contains duplicate dependencies."
            )
        if task.task_id in task.depends_on:
            raise TaskGraphValidationError(
                f"{task.task_id} cannot depend on itself."
            )
        missing = set(task.depends_on) - known_ids
        if missing:
            raise TaskGraphValidationError(
                f"{task.task_id} references missing dependencies: "
                + ", ".join(sorted(missing))
                + "."
            )
        indegree[task.task_id] = len(task.depends_on)
        if len(task.depends_on) > 1:
            synchronization_points.append(task.task_id)
        for dependency in task.depends_on:
            dependents[dependency].add(task.task_id)

    current_layer = sorted(
        task_id for task_id, dependency_count in indegree.items() if not dependency_count
    )
    if not current_layer:
        raise TaskGraphValidationError("Task graph has no ENTRY-ready task.")
    layers: list[tuple[str, ...]] = []
    topological_order: list[str] = []
    while current_layer:
        layer = tuple(current_layer)
        layers.append(layer)
        topological_order.extend(layer)
        next_layer: set[str] = set()
        for task_id in layer:
            for dependent in sorted(dependents[task_id]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    next_layer.add(dependent)
        current_layer = sorted(next_layer)

    if len(topological_order) != len(tasks):
        cyclic = sorted(known_ids - set(topological_order))
        raise TaskGraphValidationError(
            "Task graph contains a dependency cycle involving: "
            + ", ".join(cyclic)
            + "."
        )

    exit_predecessors = sorted(task_id for task_id in task_ids if not dependents[task_id])
    if not exit_predecessors:
        raise TaskGraphValidationError("Task graph has no EXIT-predecessor task.")
    return TaskGraphSemantics(
        topological_order=tuple(topological_order),
        execution_layers=tuple(layers),
        entry_ready_tasks=layers[0],
        exit_predecessor_tasks=tuple(exit_predecessors),
        synchronization_points=tuple(sorted(synchronization_points)),
    )


def _validate_proposal_references(
    proposal: ProposedTaskGraph,
    spec: ApprovedRequirementSpec,
    key_to_id: dict[str, str],
) -> None:
    valid_refs = {
        "requirement": spec.item_ids("FR")
        | spec.item_ids("NFR")
        | spec.item_ids("CON"),
        "acceptance-criteria": spec.item_ids("AC"),
        "risk": spec.item_ids("RISK"),
        "ambiguity": spec.item_ids("AMB"),
    }
    known_keys = set(key_to_id)
    for task in proposal.tasks:
        if task.key in task.depends_on:
            raise TaskGraphValidationError(
                f"Task proposal {task.key} cannot depend on itself."
            )
        missing_dependencies = set(task.depends_on) - known_keys
        if missing_dependencies:
            raise TaskGraphValidationError(
                f"Task proposal {task.key} references missing dependencies: "
                + ", ".join(sorted(missing_dependencies))
                + "."
            )
        reference_groups = (
            ("requirement", task.requirement_refs),
            ("acceptance-criteria", task.acceptance_criteria_refs),
            ("risk", task.risk_refs),
            ("ambiguity", task.ambiguity_refs),
        )
        for label, references in reference_groups:
            missing = set(references) - valid_refs[label]
            if missing:
                raise TaskGraphValidationError(
                    f"Task proposal {task.key} has invalid {label} references: "
                    + ", ".join(sorted(missing))
                    + "."
                )


def _content_hash(value: object) -> str:
    canonical_json = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
