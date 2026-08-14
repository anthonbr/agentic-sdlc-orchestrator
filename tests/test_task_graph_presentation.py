"""Tests for deterministic, presentation-only TaskGraph Mermaid rendering."""

from __future__ import annotations

import re
from typing import Any

from agentic_sdlc.task_graph_presentation import task_graph_mermaid


def _graph(*, second_title: str = "Implement service") -> dict[str, Any]:
    return {
        "tasks": [
            {"task_id": "TASK-001", "title": "Define contract", "depends_on": []},
            {
                "task_id": "TASK-002",
                "title": second_title,
                "depends_on": ["TASK-001"],
            },
            {
                "task_id": "TASK-003",
                "title": "Build tests",
                "depends_on": ["TASK-001"],
            },
            {
                "task_id": "TASK-004",
                "title": "Publish guide",
                "depends_on": ["TASK-002", "TASK-003"],
            },
        ]
    }


def _semantics() -> dict[str, Any]:
    return {
        "topological_order": ["TASK-001", "TASK-002", "TASK-003", "TASK-004"],
        "execution_layers": [
            ["TASK-001"],
            ["TASK-002", "TASK-003"],
            ["TASK-004"],
        ],
        "entry_ready_tasks": ["TASK-001"],
        "exit_predecessor_tasks": ["TASK-004"],
        "synchronization_points": ["TASK-004"],
    }


def _node_ids(mermaid: str) -> dict[str, str]:
    matches = re.findall(
        r'  (task_[0-9a-f]{64})\["(TASK-[0-9]{3})<br/>',
        mermaid,
    )
    return {task_id: node_id for node_id, task_id in matches}


def test_mermaid_contains_canonical_nodes_and_authoritative_edges() -> None:
    mermaid = task_graph_mermaid(_graph(), _semantics())
    node_ids = _node_ids(mermaid)

    assert mermaid.startswith("flowchart LR\n")
    assert 'ENTRY(["ENTRY"])' in mermaid
    assert 'EXIT(["EXIT"])' in mermaid
    assert set(node_ids) == {"TASK-001", "TASK-002", "TASK-003", "TASK-004"}
    for task_id in node_ids:
        assert mermaid.count(task_id) == 1

    assert f"ENTRY --> {node_ids['TASK-001']}" in mermaid
    assert f"{node_ids['TASK-001']} --> {node_ids['TASK-002']}" in mermaid
    assert f"{node_ids['TASK-001']} --> {node_ids['TASK-003']}" in mermaid
    assert f"{node_ids['TASK-002']} --> {node_ids['TASK-004']}" in mermaid
    assert f"{node_ids['TASK-003']} --> {node_ids['TASK-004']}" in mermaid
    assert f"{node_ids['TASK-004']} --> EXIT" in mermaid


def test_mermaid_output_is_deterministic_for_the_same_authoritative_graph() -> None:
    first = task_graph_mermaid(_graph(), _semantics())
    second = task_graph_mermaid(_graph(), _semantics())

    assert first == second


def test_mermaid_title_cannot_inject_nodes_edges_directives_or_links() -> None:
    malicious_title = (
        'Review"]\nEVIL --> EXIT\nclick TASK-001 "https://example.test"\n'
        "%%{init: {'securityLevel': 'loose'}}%%"
    )

    mermaid = task_graph_mermaid(
        _graph(second_title=malicious_title),
        _semantics(),
    )

    assert len(mermaid.splitlines()) == 13
    assert "EVIL --> EXIT" not in mermaid
    assert "%%" not in mermaid
    assert not any(
        line.lstrip().startswith(("EVIL", "click", "%%"))
        for line in mermaid.splitlines()
    )
    assert mermaid.count("TASK-002") == 1
