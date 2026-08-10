"""Deterministic third reviewer scenario for governed ambiguity resolution."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

import agentic_sdlc.nodes as nodes
import agentic_sdlc.requirement_spec as requirement_spec
import agentic_sdlc.task_graph as task_graph
from agentic_sdlc.llm import FakeRequirementAnalysisClient, FakeTaskPlanningClient
from agentic_sdlc.requirement_analysis import RequirementAnalysis
from agentic_sdlc.state import WorkflowState
from agentic_sdlc.task_execution_contracts import (
    ArtifactMaterializationProposal,
    ArtifactOutput,
    EngineeringArtifactType,
    TaskExecutionResult,
)
from agentic_sdlc.task_graph import (
    ProposedTask,
    ProposedTaskGraph,
    Task,
    TaskMaterializationPolicy,
    TaskType,
)
from agentic_sdlc.workflow import build_workflow, resume_workflow, run_workflow
from agentic_sdlc.workspace_contracts import (
    WorkspaceChangeOperation,
    normalize_repository_path,
)
from agentic_sdlc.workspace_integration import GovernedWorkspaceRuntime
from agentic_sdlc.workspace_integration_contracts import (
    WorkspaceBoundTaskExecutionRequest,
)
from agentic_sdlc.workspace_seeding import (
    WorkspaceSeedResult,
    seed_isolated_workspace_from_approved_files,
)
from tests.demo_brownfield_scenario import (
    ANALYTICS_APP,
    ANALYTICS_README,
    ANALYTICS_SERVICE,
    ANALYTICS_TESTS,
    BROWNFIELD_SOURCE_PATHS,
    export_verified_brownfield_workspace,
)


AMBIGUITY_RUN_ID = "deterministic-v06-ambiguity-expiration-demo"
AMBIGUITY_SOURCE_LABEL = "artifacts/brownfield-demo-run/enhanced-project"
AMBIGUITY_RAW_REQUIREMENT = (
    "Enhance the URL shortener so shortened URLs automatically expire after a "
    "period of time."
)
HUMAN_EXPIRATION_CLARIFICATION = (
    "Short URLs expire 24 hours after creation. The TTL is fixed and not "
    "configurable. Expiration is process-local and in-memory and does not need to "
    "survive application restart. At or after expiration, both redirect resolution "
    "and analytics return HTTP 404. No migration or preservation of pre-existing "
    "runtime codes is required. Expiration is checked when a code is accessed; no "
    "background expiration job is required."
)
AMBIGUITY_SOURCE_PATHS = BROWNFIELD_SOURCE_PATHS
AMBIGUITY_IMPACTED_PATHS = (
    "README.md",
    "src/url_shortener/service.py",
    "tests/test_service.py",
)
AMBIGUITY_UNCHANGED_PATHS = (
    "pyproject.toml",
    "src/url_shortener/__init__.py",
    "src/url_shortener/app.py",
)
AMBIGUITY_CONTEXT_PATHS_BY_SOURCE_KEY = {
    "expiration_impact": (
        "README.md",
        "pyproject.toml",
        "src/url_shortener/app.py",
        "src/url_shortener/service.py",
        "tests/test_service.py",
    ),
    "expiration_implementation": (
        "src/url_shortener/app.py",
        "src/url_shortener/service.py",
        "tests/test_service.py",
    ),
    "expiration_tests": (
        "src/url_shortener/app.py",
        "src/url_shortener/service.py",
        "tests/test_service.py",
    ),
    "expiration_documentation": (
        "README.md",
        "src/url_shortener/app.py",
        "src/url_shortener/service.py",
    ),
}
FIXED_CANONICAL_TIME = datetime.fromisoformat("2026-08-10T12:00:00+00:00")

INITIAL_AMBIGUITIES = (
    "Expiration duration/configurability: What is the expiration period, and is "
    "the TTL fixed or configurable?",
    "TTL start: When does the expiration interval begin?",
    "Expired redirect behavior: What should URL resolution return after expiration?",
    "Expired analytics behavior: What should analytics return for an expired code?",
    "Existing-code applicability: Does expiration apply to previously created or "
    "currently running codes?",
    "Persistence semantics: Must expiration survive application restart or require "
    "persistent storage?",
)


def _replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise AssertionError("Ambiguity fixture replacement must match exactly once.")
    return source.replace(old, new, 1)


EXPIRATION_SERVICE = _replace_once(
    ANALYTICS_SERVICE,
    """import base64
import hashlib
from threading import Lock
""",
    """import base64
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Lock
""",
)
EXPIRATION_SERVICE = _replace_once(
    EXPIRATION_SERVICE,
    """class URLShortener:
    \"\"\"Deterministic, collision-safe, process-local URL repository.\"\"\"

    def __init__(self) -> None:
        self._url_by_code: dict[str, str] = {}
        self._code_by_url: dict[str, str] = {}
        self._redirect_count_by_code: dict[str, int] = {}
        self._lock = Lock()
""",
    """Clock = Callable[[], datetime]
SHORT_URL_TTL = timedelta(hours=24)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class URLShortener:
    \"\"\"Deterministic, collision-safe, process-local URL repository.\"\"\"

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or _utc_now
        self._url_by_code: dict[str, str] = {}
        self._code_by_url: dict[str, str] = {}
        self._redirect_count_by_code: dict[str, int] = {}
        self._created_at_by_code: dict[str, datetime] = {}
        self._lock = Lock()
""",
)
EXPIRATION_SERVICE = _replace_once(
    EXPIRATION_SERVICE,
    """                    self._url_by_code[code] = validated
                    self._code_by_url[validated] = code
                    self._redirect_count_by_code[code] = 0
                    return code
""",
    """                    self._url_by_code[code] = validated
                    self._code_by_url[validated] = code
                    self._redirect_count_by_code[code] = 0
                    self._created_at_by_code[code] = _validated_now(self._clock())
                    return code
""",
)
EXPIRATION_SERVICE = _replace_once(
    EXPIRATION_SERVICE,
    """        with self._lock:
            try:
                original_url = self._url_by_code[code]
            except KeyError as error:
                raise UnknownShortCodeError(
                    f\"Unknown short code: {code}.\"
                ) from error
            self._redirect_count_by_code[code] += 1
            return original_url

    def redirect_count(self, code: str) -> int:
        \"\"\"Return the successful-resolution count without changing it.\"\"\"

        if not isinstance(code, str) or not code:
            raise UnknownShortCodeError(\"Unknown short code.\")
        with self._lock:
            try:
                return self._redirect_count_by_code[code]
            except KeyError as error:
                raise UnknownShortCodeError(
                    f\"Unknown short code: {code}.\"
                ) from error


def _validate_url(url: str) -> str:
""",
    """        with self._lock:
            try:
                original_url = self._url_by_code[code]
                created_at = self._created_at_by_code[code]
            except KeyError as error:
                raise UnknownShortCodeError(
                    f\"Unknown short code: {code}.\"
                ) from error
            if self._is_expired(created_at):
                raise UnknownShortCodeError(f\"Unknown or expired short code: {code}.\")
            self._redirect_count_by_code[code] += 1
            return original_url

    def redirect_count(self, code: str) -> int:
        \"\"\"Return the successful-resolution count without changing it.\"\"\"

        if not isinstance(code, str) or not code:
            raise UnknownShortCodeError(\"Unknown short code.\")
        with self._lock:
            try:
                redirect_count = self._redirect_count_by_code[code]
                created_at = self._created_at_by_code[code]
            except KeyError as error:
                raise UnknownShortCodeError(
                    f\"Unknown short code: {code}.\"
                ) from error
            if self._is_expired(created_at):
                raise UnknownShortCodeError(f\"Unknown or expired short code: {code}.\")
            return redirect_count

    def _is_expired(self, created_at: datetime) -> bool:
        return _validated_now(self._clock()) >= created_at + SHORT_URL_TTL


def _validated_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(\"Clock must return a timezone-aware datetime.\")
    return value


def _validate_url(url: str) -> str:
""",
)

EXPIRATION_TESTS = _replace_once(
    ANALYTICS_TESTS,
    """import json
import unittest
from io import BytesIO
""",
    """import json
import unittest
from datetime import UTC, datetime, timedelta
from io import BytesIO
""",
)
EXPIRATION_TESTS = _replace_once(
    EXPIRATION_TESTS,
    """class URLShortenerServiceTests(unittest.TestCase):
""",
    """class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class URLShortenerServiceTests(unittest.TestCase):
""",
)
EXPIRATION_TESTS = _replace_once(
    EXPIRATION_TESTS,
    """    def test_unknown_code_raises_domain_error(self) -> None:
        with self.assertRaises(UnknownShortCodeError):
            self.service.resolve(\"missing\")

    def test_invalid_urls_are_rejected(self) -> None:
""",
    """    def test_unknown_code_raises_domain_error(self) -> None:
        with self.assertRaises(UnknownShortCodeError):
            self.service.resolve(\"missing\")

    def test_expiration_boundary_for_resolution_and_analytics(self) -> None:
        created_at = datetime(2030, 1, 1, tzinfo=UTC)
        clock = MutableClock(created_at)
        service = URLShortener(clock=clock)
        url = \"https://example.com/expiring\"
        code = service.shorten(url)

        clock.current = created_at + timedelta(hours=24) - timedelta(seconds=1)
        self.assertEqual(service.resolve(code), url)
        self.assertEqual(service.redirect_count(code), 1)

        clock.current = created_at + timedelta(hours=24)
        with self.assertRaises(UnknownShortCodeError):
            service.resolve(code)
        with self.assertRaises(UnknownShortCodeError):
            service.redirect_count(code)

        clock.current = created_at + timedelta(hours=24, seconds=1)
        with self.assertRaises(UnknownShortCodeError):
            service.resolve(code)
        with self.assertRaises(UnknownShortCodeError):
            service.redirect_count(code)

    def test_invalid_urls_are_rejected(self) -> None:
""",
)
EXPIRATION_TESTS = _replace_once(
    EXPIRATION_TESTS,
    """    def test_get_unknown_analytics_code_returns_not_found(self) -> None:
""",
    """    def test_http_expiration_boundary_for_resolution_and_analytics(self) -> None:
        created_at = datetime(2030, 1, 1, tzinfo=UTC)
        clock = MutableClock(created_at)
        application = URLShortenerApplication(
            URLShortener(clock=clock), base_url=\"http://short.test\"
        )
        code = application.service.shorten(\"https://example.com/http-expiring\")

        clock.current = created_at + timedelta(hours=24) - timedelta(seconds=1)
        self.assertEqual(invoke_wsgi(application, \"GET\", f\"/{code}\")[0], \"302 Found\")
        self.assertEqual(
            invoke_wsgi(application, \"GET\", f\"/analytics/{code}\")[0], \"200 OK\"
        )

        clock.current = created_at + timedelta(hours=24)
        self.assertEqual(
            invoke_wsgi(application, \"GET\", f\"/{code}\")[0], \"404 Not Found\"
        )
        self.assertEqual(
            invoke_wsgi(application, \"GET\", f\"/analytics/{code}\")[0],
            \"404 Not Found\",
        )

        clock.current = created_at + timedelta(hours=24, seconds=1)
        self.assertEqual(
            invoke_wsgi(application, \"GET\", f\"/{code}\")[0], \"404 Not Found\"
        )
        self.assertEqual(
            invoke_wsgi(application, \"GET\", f\"/analytics/{code}\")[0],
            \"404 Not Found\",
        )

    def test_get_unknown_analytics_code_returns_not_found(self) -> None:
""",
)

EXPIRATION_README = """# Governed URL Shortener Demo

This dependency-free Python URL shortener was originally produced by the governed
greenfield scenario, enhanced by the governed brownfield analytics scenario, and
then enhanced by the governed ambiguity-resolution scenario. The third workflow
blocked planning for an underspecified expiration request, preserved the human
clarification and analysis revision lineage, and transactionally modified three
existing files only after the revised requirement became authoritative.

## Architecture

`URLShortener` contains the domain rules and process-local in-memory mappings.
`URLShortenerApplication` is a thin WSGI adapter. The service accepts absolute
HTTP(S) URLs, generates stable collision-checked short codes, resolves active codes,
counts successful resolutions, exposes per-code analytics, and reports typed errors
for invalid URLs and unknown or expired codes.

Each code records a timezone-aware creation time and expires exactly 24 hours later.
The fixed TTL starts at creation and is checked on redirect or analytics access. At
or after the boundary, both operations return HTTP 404 through the existing adapter.
Expiration and analytics state are process-local and in-memory: neither survives a
restart. There is no configuration, database, migration, cleanup scheduler, or
background expiration job.

## Run

Python 3.11 or newer is required. No third-party runtime dependency is needed.

```bash
PYTHONPATH=src python -m url_shortener.app
```

The server binds to `127.0.0.1:8000` by default. Override it with
`URL_SHORTENER_HOST` and `URL_SHORTENER_PORT`.

## Test

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Tests inject a mutable timezone-aware clock, run entirely in-process, wait no real
time, and require no network access.

## HTTP API

Shorten a URL:

```bash
curl -i -X POST http://127.0.0.1:8000/shorten \\
  -H 'Content-Type: application/json' \\
  -d '{"url":"https://example.com/path"}'
```

The `201 Created` JSON response contains `code` and `short_url`. Resolve the code:

```bash
curl -i http://127.0.0.1:8000/<code>
```

An active code returns `302 Found`; an unknown or expired code returns
`404 Not Found`. Each successful pre-expiration redirect increments that code's
process-local count.

Inspect analytics without incrementing the count:

```bash
curl -i http://127.0.0.1:8000/analytics/<code>
```

An active code returns `200 OK` with JSON such as
`{"code": "abc12345", "redirect_count": 7}`. An unknown or expired analytics
code returns `404 Not Found`. Analytics lookup itself does not increment the count.

## Assumptions and trade-offs

- Repeated shortening of the same URL returns the existing code and does not reset
  its creation time or redirect count.
- SHA-256-derived candidates plus collision checking provide deterministic codes.
- Redirect counts and expiration timestamps are process-local, in-memory, and reset
  when the process restarts.
- Persistence, configurable TTLs, background cleanup, migration, authentication,
  and repository promotion remain outside this prototype.
- The orchestrator does not execute generated source or tests. Reviewer tooling
  validates this exported copy only after governed execution and integrity checks.
"""

EXPIRATION_IMPACT_ANALYSIS = """# Clarified URL-expiration impact analysis

## Existing brownfield flow

- `src/url_shortener/service.py` owns process-local code lookup and analytics state.
- `src/url_shortener/app.py` already converts `UnknownShortCodeError` from both
  redirect resolution and analytics lookup into HTTP 404.
- `tests/test_service.py` contains domain and WSGI regression coverage.
- `README.md` currently documents expiration as unresolved and out of scope.

## Clarified design

- Record one timezone-aware `created_at` value when a new code is created.
- Use a fixed `timedelta(hours=24)` and an injected callable clock whose default is
  current UTC time. Active means `now < created_at + TTL`; expired means
  `now >= created_at + TTL`.
- Check expiration under the existing service lock before successful resolution or
  analytics access. An expired lookup raises the existing domain lookup error, so
  the unchanged WSGI adapter deterministically returns HTTP 404 for both routes.
- Add deterministic service and HTTP boundary tests with a mutable clock. No sleep,
  scheduler, persistence, configuration, migration, or dependency is required.

## Mutation boundary

- Modify `src/url_shortener/service.py`, `tests/test_service.py`, and `README.md`.
- Preserve `src/url_shortener/app.py`, `src/url_shortener/__init__.py`, and
  `pyproject.toml` byte-for-byte.
"""


class AmbiguityRepositoryContextPathProvider:
    """Provide deterministic task-scoped brownfield context paths."""

    def paths_for_attempt(
        self,
        task: Task,
        *,
        dependency_paths: tuple[str, ...],
        retry_paths: tuple[str, ...],
    ) -> tuple[str, ...]:
        configured = AMBIGUITY_CONTEXT_PATHS_BY_SOURCE_KEY[task.source_key]
        return tuple(
            sorted(
                {
                    *(normalize_repository_path(path) for path in configured),
                    *(normalize_repository_path(path) for path in dependency_paths),
                    *(normalize_repository_path(path) for path in retry_paths),
                }
            )
        )


def ambiguity_input() -> WorkflowState:
    """Return the exact intentionally ambiguous reviewer intake."""

    return {
        "project_name": "URL Shortener Expiration",
        "requirements": [AMBIGUITY_RAW_REQUIREMENT],
        "raw_requirement": AMBIGUITY_RAW_REQUIREMENT,
    }


def initial_ambiguity_analysis() -> RequirementAnalysis:
    """Return Revision 0 without inventing product answers."""

    return RequirementAnalysis(
        normalized_problem_statement=AMBIGUITY_RAW_REQUIREMENT,
        requirement_type="ambiguous",
        functional_requirements=[
            "Shortened URLs must expire according to a product policy that requires "
            "human clarification before engineering planning."
        ],
        nonfunctional_requirements=[],
        constraints=[
            "Preserve the existing URL-shortener and redirect-analytics baseline."
        ],
        ambiguities=list(INITIAL_AMBIGUITIES),
        assumptions=[],
        acceptance_criteria=[
            "Engineering behavior is not implementation-ready until the expiration "
            "policy ambiguities are resolved."
        ],
        risks=[
            "Inventing expiration policy could silently violate expected product "
            "behavior."
        ],
        needs_clarification=True,
        confidence=0.99,
    )


def revised_expiration_analysis() -> RequirementAnalysis:
    """Return Revision 1 containing the authoritative human clarification."""

    return RequirementAnalysis(
        normalized_problem_statement=(
            "Add fixed process-local 24-hour expiration to the existing URL "
            "shortener, returning HTTP 404 for expired redirect and analytics access."
        ),
        requirement_type="brownfield",
        functional_requirements=[
            "Short URLs expire 24 hours after creation.",
            "Resolving an expired short code returns HTTP 404.",
            "Requesting analytics for an expired short code returns HTTP 404.",
        ],
        nonfunctional_requirements=[
            "Expiration boundary behavior must be deterministically testable without "
            "waiting for wall-clock time."
        ],
        constraints=[
            "The TTL is fixed at 24 hours and is not configurable.",
            "The TTL begins when the short URL is created.",
            "Expiration state is process-local and in-memory.",
            "Expiration does not need to survive application restart.",
            "Expiration is checked only when a code is accessed.",
            "No background expiration job or scheduler is required.",
            "No migration or preservation of pre-existing runtime codes is required.",
            "No database or persistent storage is required.",
        ],
        ambiguities=[],
        assumptions=[
            "Existing shortening, analytics counting, and active-code behavior remain "
            "unchanged."
        ],
        acceptance_criteria=[
            "A code resolves successfully before 24 hours have elapsed.",
            "Analytics remains available before 24 hours have elapsed.",
            "At exactly 24 hours after creation, resolution returns HTTP 404.",
            "At exactly 24 hours after creation, analytics returns HTTP 404.",
            "Resolution and analytics continue to return HTTP 404 after 24 hours.",
            "Expiration tests complete without waiting 24 real hours.",
        ],
        risks=[
            "An incorrect boundary comparison could keep codes active at exactly 24 "
            "hours or hide analytics too early."
        ],
        needs_clarification=False,
        confidence=1.0,
    )


def _ids(prefix: str, count: int) -> list[str]:
    return [f"{prefix}-{index:03d}" for index in range(1, count + 1)]


def ambiguity_task_graph_proposal() -> ProposedTaskGraph:
    """Return a small graph derived from the clarified canonical namespaces."""

    analysis = revised_expiration_analysis()
    all_requirements = [
        *_ids("FR", len(analysis.functional_requirements)),
        *_ids("NFR", len(analysis.nonfunctional_requirements)),
        *_ids("CON", len(analysis.constraints)),
    ]
    all_acceptance = _ids("AC", len(analysis.acceptance_criteria))
    all_risks = _ids("RISK", len(analysis.risks))

    def proposed_task(
        key: str,
        title: str,
        task_type: TaskType,
        *,
        depends_on: list[str],
        materialization_policy: TaskMaterializationPolicy,
        expected_output: str,
        requirement_refs: list[str],
        acceptance_refs: list[str],
    ) -> ProposedTask:
        return ProposedTask(
            key=key,
            title=title,
            description=f"Produce the governed {title.lower()} output.",
            task_type=task_type,
            materialization_policy=materialization_policy,
            depends_on=depends_on,
            requirement_refs=requirement_refs,
            acceptance_criteria_refs=acceptance_refs,
            risk_refs=all_risks,
            ambiguity_refs=[],
            expected_outputs=[expected_output],
        )

    return ProposedTaskGraph(
        tasks=[
            proposed_task(
                "expiration_impact",
                "Analyze expiration impact",
                TaskType.DESIGN,
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.FORBIDDEN,
                expected_output="expiration-impact-analysis",
                requirement_refs=all_requirements,
                acceptance_refs=all_acceptance,
            ),
            proposed_task(
                "expiration_implementation",
                "Implement clarified expiration behavior",
                TaskType.IMPLEMENTATION,
                depends_on=["expiration_impact"],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
                expected_output="url-shortener-expiration-service",
                requirement_refs=[
                    *_ids("FR", len(analysis.functional_requirements)),
                    *_ids("CON", len(analysis.constraints)),
                ],
                acceptance_refs=_ids("AC", 5),
            ),
            proposed_task(
                "expiration_tests",
                "Add deterministic expiration tests",
                TaskType.TEST,
                depends_on=["expiration_implementation"],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
                expected_output="url-shortener-expiration-tests",
                requirement_refs=[
                    *_ids("FR", len(analysis.functional_requirements)),
                    *_ids("NFR", len(analysis.nonfunctional_requirements)),
                ],
                acceptance_refs=all_acceptance,
            ),
            proposed_task(
                "expiration_documentation",
                "Update expiration documentation",
                TaskType.DOCUMENTATION,
                depends_on=["expiration_implementation"],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
                expected_output="url-shortener-expiration-documentation",
                requirement_refs=[
                    *_ids("FR", len(analysis.functional_requirements)),
                    *_ids("CON", len(analysis.constraints)),
                ],
                acceptance_refs=_ids("AC", 5),
            ),
        ]
    )


class AmbiguityExpirationExecutor:
    """Concurrency-safe deterministic executor with no direct filesystem authority."""

    model_name = "deterministic-ambiguity-expiration-executor"

    def __init__(self) -> None:
        self.calls_by_task_id: dict[str, WorkspaceBoundTaskExecutionRequest] = {}
        self._lock = Lock()

    def execute(
        self, request: WorkspaceBoundTaskExecutionRequest
    ) -> TaskExecutionResult:
        with self._lock:
            self.calls_by_task_id[request.task_id] = request
        outputs = {
            "expiration_impact": (
                EngineeringArtifactType.DESIGN,
                "expiration-impact-analysis",
                EXPIRATION_IMPACT_ANALYSIS,
                None,
            ),
            "expiration_implementation": (
                EngineeringArtifactType.SOURCE,
                "url-shortener-expiration-service",
                EXPIRATION_SERVICE,
                "src/url_shortener/service.py",
            ),
            "expiration_tests": (
                EngineeringArtifactType.TEST,
                "url-shortener-expiration-tests",
                EXPIRATION_TESTS,
                "tests/test_service.py",
            ),
            "expiration_documentation": (
                EngineeringArtifactType.DOCUMENTATION,
                "url-shortener-expiration-documentation",
                EXPIRATION_README,
                "README.md",
            ),
        }
        artifact_type, logical_name, content, target_path = outputs[
            request.task.source_key
        ]
        proposals = (
            ()
            if target_path is None
            else (
                ArtifactMaterializationProposal(
                    output_index=1, target_path=target_path
                ),
            )
        )
        return TaskExecutionResult(
            request_id=request.request_id,
            attempt_id=request.attempt_id,
            task_id=request.task_id,
            summary=f"Produced governed expiration output for {request.task_id}.",
            outputs=(
                ArtifactOutput(
                    artifact_type=artifact_type,
                    logical_name=logical_name,
                    content=content,
                ),
            ),
            materialization_proposals=proposals,
            assumptions=("Expiration state remains process-local and in-memory.",),
            risks=("Incorrect time-boundary handling could expose expired codes.",),
        )


class FixedDateTime:
    """Fixed clock for deterministic application-owned identities."""

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        del tz
        return FIXED_CANONICAL_TIME


@contextmanager
def fixed_canonical_time() -> Iterator[None]:
    """Temporarily fix only canonical artifact/lineage timestamps."""

    targets = (requirement_spec, task_graph, nodes)
    originals = tuple(target.datetime for target in targets)
    try:
        for target in targets:
            target.datetime = FixedDateTime  # type: ignore[misc]
        yield
    finally:
        for target, original in zip(targets, originals, strict=True):
            target.datetime = original  # type: ignore[misc]


@dataclass(frozen=True)
class AmbiguityDemoRun:
    """Captured checkpoints and evidence from one deterministic reviewer run."""

    initial_state: WorkflowState
    revised_state: WorkflowState
    graph_review_state: WorkflowState
    final_state: WorkflowState
    seed_result: WorkspaceSeedResult
    executor: AmbiguityExpirationExecutor
    runtime: GovernedWorkspaceRuntime
    analyst: FakeRequirementAnalysisClient
    planner: FakeTaskPlanningClient
    planner_calls_at_initial_block: int
    planner_calls_after_revision: int
    exported_application_test_count: int | None


def run_ambiguity_demo(
    workspace_parent: Path,
    *,
    source_root: Path,
    artifact_dir: Path | None = None,
    run_id: str = AMBIGUITY_RUN_ID,
) -> AmbiguityDemoRun:
    """Execute all three governed reviewer checkpoints without network access."""

    workspace_parent.mkdir(parents=True, exist_ok=True)
    source_before = _project_bytes(source_root)
    runtime = GovernedWorkspaceRuntime(parent_directory=workspace_parent)
    workspace = runtime.establish_workspace_for_run(run_id)
    seed_result, seed_snapshot = seed_isolated_workspace_from_approved_files(
        workspace,
        source_root=source_root,
        source_root_label=AMBIGUITY_SOURCE_LABEL,
        relative_paths=AMBIGUITY_SOURCE_PATHS,
    )
    analyst = FakeRequirementAnalysisClient(
        [initial_ambiguity_analysis(), revised_expiration_analysis()],
        model_name="deterministic-ambiguity-analyst",
    )
    planner = FakeTaskPlanningClient(
        [ambiguity_task_graph_proposal()],
        model_name="deterministic-ambiguity-planner",
    )
    executor = AmbiguityExpirationExecutor()
    workflow = build_workflow(
        analyst,
        planner,
        executor,
        workspace_runtime=runtime,
        repository_context_path_provider=AmbiguityRepositoryContextPathProvider(),
    )

    with fixed_canonical_time():
        initial_state = run_workflow(
            ambiguity_input(),
            thread_id=run_id,
            artifact_dir=artifact_dir,
            workflow=workflow,
        )
        planner_calls_at_initial_block = len(planner.calls)
        if planner_calls_at_initial_block:
            raise AssertionError("Blocked Revision 0 must not invoke task planning.")
        revised_state = resume_workflow(
            run_id,
            {
                "decision": "REQUEST_CHANGES",
                "feedback": HUMAN_EXPIRATION_CLARIFICATION,
            },
            artifact_dir=artifact_dir,
            workflow=workflow,
        )
        planner_calls_after_revision = len(planner.calls)
        if planner_calls_after_revision:
            raise AssertionError("Planning must wait for revised human approval.")
        graph_review_state = resume_workflow(
            run_id,
            {"decision": "APPROVE", "feedback": ""},
            artifact_dir=artifact_dir,
            workflow=workflow,
        )
        final_state = resume_workflow(
            run_id,
            {"decision": "APPROVE", "feedback": ""},
            artifact_dir=artifact_dir,
            workflow=workflow,
        )

    application_test_count = None
    if artifact_dir is not None:
        final_snapshot = _snapshot_by_id(
            final_state,
            final_state["governed_workspace_session"].authoritative_snapshot_id,
        )
        export_root = artifact_dir / "expiration-project"
        export_verified_brownfield_workspace(
            workspace.root,
            export_root,
            final_snapshot,
        )
        application_test_count = validate_exported_application(export_root)
        write_ambiguity_review_artifacts(
            artifact_dir,
            initial_state=initial_state,
            revised_state=revised_state,
            final_state=final_state,
            seed_result=seed_result,
            planner_invocation_count=len(planner.calls),
            application_test_count=application_test_count,
        )

    if _project_bytes(source_root) != source_before:
        raise AssertionError("Approved brownfield source was modified by the scenario.")
    if seed_snapshot.snapshot_id != seed_result.baseline_snapshot_id:
        raise AssertionError("Seed evidence does not match the baseline snapshot.")
    return AmbiguityDemoRun(
        initial_state=initial_state,
        revised_state=revised_state,
        graph_review_state=graph_review_state,
        final_state=final_state,
        seed_result=seed_result,
        executor=executor,
        runtime=runtime,
        analyst=analyst,
        planner=planner,
        planner_calls_at_initial_block=planner_calls_at_initial_block,
        planner_calls_after_revision=planner_calls_after_revision,
        exported_application_test_count=application_test_count,
    )


def validate_exported_application(export_root: Path) -> int:
    """Run the exported standard-library tests as reviewer tooling, not task work."""

    environment = {
        "PYTHONPATH": str(export_root / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-v",
        ],
        cwd=export_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    combined_output = result.stdout + result.stderr
    if result.returncode != 0:
        raise AssertionError("Exported application tests failed:\n" + combined_output)
    match = re.search(r"Ran (\d+) tests?", combined_output)
    if match is None:
        raise AssertionError("Exported application test count was not reported.")
    return int(match.group(1))


def write_ambiguity_review_artifacts(
    output_dir: Path,
    *,
    initial_state: WorkflowState,
    revised_state: WorkflowState,
    final_state: WorkflowState,
    seed_result: WorkspaceSeedResult,
    planner_invocation_count: int,
    application_test_count: int,
) -> None:
    """Add concise machine and human evidence to normal workflow artifacts."""

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "workspace_seed.json").write_text(
        json.dumps(seed_result.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    initial_record = initial_state["requirement_analysis_history"][0]
    revised_record = revised_state["requirement_analysis_history"][1]
    specification = final_state["approved_requirement_spec"]
    graph = final_state["approved_task_graph"]
    session = final_state["governed_workspace_session"]
    review_events = final_state["requirement_review_history"]
    resolution = {
        "raw_requirement": final_state["raw_requirement"],
        "initial_analysis": {
            "revision_number": initial_record["revision_number"],
            "needs_clarification": initial_record["analysis"]["needs_clarification"],
            "planning_readiness": initial_record["planning_readiness"],
            "blocking_ambiguities": initial_record["planning_readiness"][
                "blocking_ambiguities"
            ],
            "approved_requirement_spec_present": bool(
                initial_state.get("approved_requirement_spec")
            ),
            "task_graph_present": bool(initial_state.get("candidate_task_graph")),
        },
        "human_review": review_events[0],
        "human_review_history": review_events,
        "revised_analysis": {
            "revision_number": revised_record["revision_number"],
            "derived_from_revision": initial_record["revision_number"],
            "reviewer_feedback": revised_record["reviewer_feedback"],
            "needs_clarification": revised_record["analysis"]["needs_clarification"],
            "planning_readiness": revised_record["planning_readiness"],
        },
        "approved_requirement_spec": {
            "spec_id": specification["spec_id"],
            "version": specification["version"],
            "content_hash": specification["content_hash"],
            "source_analysis_revision": specification["source_analysis_revision"],
        },
        "planning_attempts": [
            {
                "attempt": 1,
                "source": "requirement_analysis_revision_0",
                "status": "BLOCKED",
                "reason": "UNRESOLVED_REQUIREMENT_AMBIGUITY",
                "planner_invoked": False,
            },
            {
                "attempt": 2,
                "source": specification["spec_id"],
                "trigger": "UPSTREAM_REQUIREMENTS_REVISED",
                "reason": "CLARIFICATION_RESOLVED",
                "status": "PLANNED",
                "planner_invoked": True,
            },
        ],
        "planner_invocation_count_before_clarification": 0,
        "planner_invocation_count_total": planner_invocation_count,
        "task_graph": {
            "graph_id": graph["graph_id"],
            "version": graph["version"],
            "requirement_spec_id": graph["requirement_spec_id"],
            "requirement_spec_version": graph["requirement_spec_version"],
        },
        "execution": {
            "status": final_state["task_graph_execution"].status.value,
            "workflow_status": final_state["workflow_status"],
            "exit_gate_passed": final_state["exit_gate_passed"],
            "workspace_integrity": session.integrity_status.value,
            "exported_application_validation": {
                "status": "PASSED",
                "test_count": application_test_count,
                "network_required": False,
                "api_credentials_required": False,
            },
        },
    }
    (output_dir / "ambiguity_resolution.json").write_text(
        json.dumps(resolution, indent=2) + "\n", encoding="utf-8"
    )

    changes = [
        change
        for change_set in final_state["workspace_change_sets"]
        for change in change_set.file_changes
    ]
    operation_counts = {
        operation.value: sum(change.operation is operation for change in changes)
        for operation in WorkspaceChangeOperation
    }
    task_lines = []
    for task in graph["tasks"]:
        dependencies = ", ".join(task["depends_on"]) or "ENTRY"
        task_lines.append(
            f"- {task['task_id']} — {task['title']} — "
            f"{task['materialization_policy']} — depends on {dependencies}"
        )
    lines = [
        "# Governed Ambiguous URL-Expiration Demo",
        "",
        "## Scenario",
        "",
        f"> {final_state['raw_requirement']}",
        "",
        "The verified V0.5 brownfield URL shortener is the six-file starting "
        "application.",
        "",
        "## Before clarification",
        "",
        "- Analysis Revision 0",
        "- `needs_clarification=true`",
        "- Planning readiness: `BLOCKED`",
        "- Reason: `UNRESOLVED_REQUIREMENT_AMBIGUITY`",
        "- Planner invoked: false (0 calls)",
        "- Approved requirement specification: absent",
        "- TaskGraph: absent",
        "",
        "Blocking ambiguities:",
        "",
        *(f"- {item}" for item in initial_record["analysis"]["ambiguities"]),
        "",
        "## Human decision",
        "",
        "Decision: `REQUEST_CHANGES`",
        "",
        f"> {review_events[0]['feedback']}",
        "",
        "## After clarification",
        "",
        "- Analysis Revision 1",
        "- `needs_clarification=false`",
        "- Planning readiness: `READY`",
        "- Human decision: `APPROVE`",
        f"- Authoritative specification: `{specification['spec_id']}` version "
        f"{specification['version']}",
        f"- Specification hash: `{specification['content_hash']}`",
        "",
        "Clarified outcomes:",
        "",
        "- FR: fixed 24-hour expiration from creation.",
        "- FR: expired resolution and analytics each return HTTP 404.",
        "- CON: process-local in-memory state; no restart persistence.",
        "- CON: no configuration, scheduler, database, or migration.",
        "- AC: active immediately before the boundary; 404 at and after it.",
        "- AC: injected time makes validation immediate and repeatable.",
        "",
        "## Downstream consequence",
        "",
        "- Attempt 1: BLOCKED; planner invocation count 0.",
        "- Attempt 2 trigger: `UPSTREAM_REQUIREMENTS_REVISED`.",
        "- Attempt 2 reason: `CLARIFICATION_RESOLVED`.",
        "- Attempt 2: PLANNED; planner invocation count 1.",
        f"- TaskGraph: `{graph['graph_id']}` version {graph['version']}.",
        f"- Source-spec lineage: `{graph['requirement_spec_id']}` version "
        f"{graph['requirement_spec_version']} (matches current authority).",
        "",
        *task_lines,
        "",
        "## Governed execution",
        "",
        f"- Mutation summary: {operation_counts.get('CREATE', 0)} CREATE, "
        f"{operation_counts.get('MODIFY', 0)} MODIFY, "
        f"{operation_counts.get('DELETE', 0)} DELETE, "
        f"{operation_counts.get('NO_CHANGE', 0)} NO_CHANGE.",
        "- TASK-001 impact analysis: non-mutating (`FORBIDDEN`).",
        "- Modified: `src/url_shortener/service.py`, `tests/test_service.py`, "
        "`README.md`.",
        "- Unchanged: `src/url_shortener/app.py`, `pyproject.toml`, "
        "`src/url_shortener/__init__.py`.",
        f"- Exported application validation: {application_test_count} tests passed.",
        f"- Final execution: `{final_state['task_graph_execution'].status.value}`.",
        f"- Exit gate: {'PASSED' if final_state['exit_gate_passed'] else 'FAILED'}.",
        f"- Final workspace integrity: `{session.integrity_status.value}`.",
        "- Brownfield source and exported final snapshot hashes: VERIFIED.",
        "",
        "## Scope boundary",
        "",
        "This scenario demonstrates governed replanning at the requirements-to-planning "
        "boundary. It does not mutate a live TaskGraph, cancel active work, migrate "
        "execution state, or dynamically rewrite dependencies. A mid-execution "
        "authority change retains Checkpoint 1 safe-stop and governed-replanning "
        "semantics.",
        "",
    ]
    if operation_counts != {"CREATE": 0, "MODIFY": 3, "NO_CHANGE": 0}:
        raise AssertionError("Ambiguity scenario requires exactly three MODIFY changes.")
    (output_dir / "summary.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def _snapshot_by_id(state: WorkflowState, snapshot_id: str):
    return next(
        snapshot
        for snapshot in state["workspace_snapshots"]
        if snapshot.snapshot_id == snapshot_id
    )


def _project_bytes(root: Path) -> dict[str, bytes]:
    return {path: (root / path).read_bytes() for path in AMBIGUITY_SOURCE_PATHS}


def main() -> None:
    """Regenerate checked-in reviewer evidence from deterministic adapters."""

    repository_root = Path(__file__).parents[1]
    output_dir = (
        Path(sys.argv[1]).resolve()
        if len(sys.argv) > 1
        else repository_root / "artifacts/ambiguity-demo-run"
    )
    source_root = repository_root / "artifacts/brownfield-demo-run/enhanced-project"
    with tempfile.TemporaryDirectory(prefix="agentic-sdlc-ambiguity-") as temporary:
        run_ambiguity_demo(
            Path(temporary),
            source_root=source_root,
            artifact_dir=output_dir,
        )


if __name__ == "__main__":
    main()
