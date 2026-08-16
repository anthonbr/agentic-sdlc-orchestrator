"""Deterministic inputs and outputs for the governed brownfield demo."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
from pathlib import Path
from threading import Lock

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
from agentic_sdlc.workspace_contracts import (
    WorkspaceSnapshot,
    normalize_repository_path,
)
from agentic_sdlc.workspace_integration_contracts import (
    WorkspaceBoundTaskExecutionRequest,
)
from agentic_sdlc.workspace_seeding import WorkspaceSeedResult
from tests.demo_url_shortener_project import (
    APP as GREENFIELD_APP,
    GENERATED_README as GREENFIELD_README,
    SERVICE as GREENFIELD_SERVICE,
    TESTS as GREENFIELD_TESTS,
)


BROWNFIELD_RUN_ID = "deterministic-v05-brownfield-analytics-demo"
BROWNFIELD_SOURCE_LABEL = "sample_output/demo-run/generated-project"
BROWNFIELD_SOURCE_PATHS = (
    "README.md",
    "pyproject.toml",
    "src/url_shortener/__init__.py",
    "src/url_shortener/app.py",
    "src/url_shortener/service.py",
    "tests/test_service.py",
)
BROWNFIELD_CONTEXT_PATHS = (
    "README.md",
    "pyproject.toml",
    "src/url_shortener/app.py",
    "src/url_shortener/service.py",
    "tests/test_service.py",
)
BROWNFIELD_CONTEXT_PATHS_BY_SOURCE_KEY = {
    "impact_analysis": BROWNFIELD_CONTEXT_PATHS,
    "service_analytics": (
        "src/url_shortener/service.py",
        "tests/test_service.py",
    ),
    "analytics_http_api": (
        "src/url_shortener/app.py",
        "src/url_shortener/service.py",
    ),
    "regression_tests": ("tests/test_service.py",),
    "documentation_update": ("README.md",),
}
BROWNFIELD_IMPACTED_PATHS = (
    "README.md",
    "src/url_shortener/app.py",
    "src/url_shortener/service.py",
    "tests/test_service.py",
)
BROWNFIELD_UNCHANGED_PATHS = (
    "pyproject.toml",
    "src/url_shortener/__init__.py",
)

BROWNFIELD_RAW_REQUIREMENT = (
    "Enhance the existing URL shortener to track how many times each short URL "
    "has been successfully resolved and expose analytics for a short code. "
    "Preserve all existing shortening and redirect behavior."
)

BROWNFIELD_IMPACT_ANALYSIS = """# Brownfield redirect-analytics impact analysis

## Existing architecture and flow

- `src/url_shortener/service.py` contains the process-local in-memory
  `URLShortener` domain service and its code-to-URL lookup.
- `src/url_shortener/app.py` is the thin WSGI adapter. Its current generic route
  resolves `GET /{code}` through `URLShortenerApplication._resolve`, then
  `URLShortener.resolve(code)`, the in-memory lookup, and HTTP 302 `Location`.
- `tests/test_service.py` is the current executable domain and HTTP regression
  suite, and `README.md` is the user/API documentation.
- `pyproject.toml` needs no dependency change for this in-memory enhancement.

## Shared service contract and enhanced flow

- `URLShortener.redirect_count(code: str) -> int` returns the current successful-
  resolution count for a known code and raises the existing
  `UnknownShortCodeError` for an unknown or empty code. Reading it never changes
  the count.
- Successful `GET /{code}` resolution validates that the code exists, increments
  that code's successful-resolution count, returns the URL, and preserves the
  existing HTTP 302 response.
- `GET /analytics/{code}` must be recognized before the generic `GET /{code}`
  branch, then return `code` plus `redirect_count` without incrementing it; an
  unknown analytics code returns HTTP 404.
- Lookup and successful count increment occur while holding the existing service
  lock. Analytics reads use the same lock, avoiding lost increments while
  preserving the current synchronization model.
- Unknown resolution attempts, shortening, and analytics lookup do not increment
  counts. Re-shortening a known URL must not reset its accumulated count.
- Existing shortening and redirect behavior remains intact. Storage stays
  process-local and in-memory; no database or external dependency is required.

## Expected mutation targets

- `src/url_shortener/service.py`
- `src/url_shortener/app.py`
- `tests/test_service.py`
- `README.md`

## Explicitly unchanged

- `pyproject.toml`: no dependency change is required.
- `src/url_shortener/__init__.py`: no public-export change is expected.
"""


def _replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise AssertionError("Brownfield fixture replacement must match exactly once.")
    return source.replace(old, new, 1)


ANALYTICS_SERVICE = _replace_once(
    GREENFIELD_SERVICE,
    """        self._url_by_code: dict[str, str] = {}
        self._code_by_url: dict[str, str] = {}
        self._lock = Lock()
""",
    """        self._url_by_code: dict[str, str] = {}
        self._code_by_url: dict[str, str] = {}
        self._redirect_count_by_code: dict[str, int] = {}
        self._lock = Lock()
""",
)
ANALYTICS_SERVICE = _replace_once(
    ANALYTICS_SERVICE,
    """                    self._url_by_code[code] = validated
                    self._code_by_url[validated] = code
                    return code
""",
    """                    self._url_by_code[code] = validated
                    self._code_by_url[validated] = code
                    self._redirect_count_by_code[code] = 0
                    return code
""",
)
ANALYTICS_SERVICE = _replace_once(
    ANALYTICS_SERVICE,
    """        with self._lock:
            try:
                return self._url_by_code[code]
            except KeyError as error:
                raise UnknownShortCodeError(
                    f"Unknown short code: {code}."
                ) from error


def _validate_url(url: str) -> str:
""",
    """        with self._lock:
            try:
                original_url = self._url_by_code[code]
            except KeyError as error:
                raise UnknownShortCodeError(
                    f"Unknown short code: {code}."
                ) from error
            self._redirect_count_by_code[code] += 1
            return original_url

    def redirect_count(self, code: str) -> int:
        \"\"\"Return the successful-resolution count without changing it.\"\"\"

        if not isinstance(code, str) or not code:
            raise UnknownShortCodeError("Unknown short code.")
        with self._lock:
            try:
                return self._redirect_count_by_code[code]
            except KeyError as error:
                raise UnknownShortCodeError(
                    f"Unknown short code: {code}."
                ) from error


def _validate_url(url: str) -> str:
""",
)

ANALYTICS_APP = _replace_once(
    GREENFIELD_APP,
    """        if method == "GET" and path.startswith("/") and path != "/":
            return self._resolve(unquote(path[1:]), start_response)
""",
    """        if method == "GET" and path.startswith("/analytics/"):
            return self._analytics(
                unquote(path[len("/analytics/") :]), start_response
            )
        if method == "GET" and path.startswith("/") and path != "/":
            return self._resolve(unquote(path[1:]), start_response)
""",
)
ANALYTICS_APP = _replace_once(
    ANALYTICS_APP,
    """    def _resolve(self, code: str, start_response: StartResponse) -> Iterable[bytes]:
""",
    """    def _analytics(
        self, code: str, start_response: StartResponse
    ) -> Iterable[bytes]:
        try:
            redirect_count = self.service.redirect_count(code)
        except UnknownShortCodeError:
            return _json_response(
                start_response,
                "404 Not Found",
                {"error": "unknown_code", "code": code},
            )
        return _json_response(
            start_response,
            "200 OK",
            {"code": code, "redirect_count": redirect_count},
        )

    def _resolve(self, code: str, start_response: StartResponse) -> Iterable[bytes]:
""",
)

ANALYTICS_TESTS = _replace_once(
    GREENFIELD_TESTS,
    """    def test_unknown_code_raises_domain_error(self) -> None:
        with self.assertRaises(UnknownShortCodeError):
            self.service.resolve("missing")

    def test_invalid_urls_are_rejected(self) -> None:
""",
    """    def test_redirect_count_starts_at_zero(self) -> None:
        code = self.service.shorten("https://example.com/count")
        self.assertEqual(self.service.redirect_count(code), 0)

    def test_successful_resolves_increment_exactly_once_each(self) -> None:
        url = "https://example.com/counted"
        code = self.service.shorten(url)
        self.assertEqual(self.service.resolve(code), url)
        self.assertEqual(self.service.resolve(code), url)
        self.assertEqual(self.service.redirect_count(code), 2)

    def test_redirect_count_lookup_does_not_increment(self) -> None:
        code = self.service.shorten("https://example.com/observe")
        self.service.resolve(code)
        self.assertEqual(self.service.redirect_count(code), 1)
        self.assertEqual(self.service.redirect_count(code), 1)

    def test_unknown_resolution_has_no_analytics_state(self) -> None:
        with self.assertRaises(UnknownShortCodeError):
            self.service.resolve("missing")
        with self.assertRaises(UnknownShortCodeError):
            self.service.redirect_count("missing")

    def test_reshortening_preserves_redirect_count(self) -> None:
        url = "https://example.com/reshorten"
        code = self.service.shorten(url)
        self.service.resolve(code)
        self.assertEqual(self.service.shorten(url), code)
        self.assertEqual(self.service.redirect_count(code), 1)

    def test_unknown_code_raises_domain_error(self) -> None:
        with self.assertRaises(UnknownShortCodeError):
            self.service.resolve("missing")

    def test_invalid_urls_are_rejected(self) -> None:
""",
)
ANALYTICS_TESTS = _replace_once(
    ANALYTICS_TESTS,
    """    def test_get_unknown_code_returns_not_found(self) -> None:
        status, _, body = invoke_wsgi(self.application, "GET", "/unknown")
        self.assertEqual(status, "404 Not Found")
        self.assertEqual(json.loads(body)["error"], "unknown_code")
""",
    """    def test_get_analytics_tracks_only_successful_redirects(self) -> None:
        code = self.application.service.shorten("https://example.com/analytics")

        status, _, body = invoke_wsgi(self.application, "GET", f"/analytics/{code}")
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body), {"code": code, "redirect_count": 0})
        status, _, body = invoke_wsgi(self.application, "GET", f"/analytics/{code}")
        self.assertEqual(json.loads(body)["redirect_count"], 0)

        self.assertEqual(invoke_wsgi(self.application, "GET", f"/{code}")[0], "302 Found")
        status, _, body = invoke_wsgi(self.application, "GET", f"/analytics/{code}")
        self.assertEqual(json.loads(body)["redirect_count"], 1)
        self.assertEqual(invoke_wsgi(self.application, "GET", f"/{code}")[0], "302 Found")
        status, _, body = invoke_wsgi(self.application, "GET", f"/analytics/{code}")
        self.assertEqual(json.loads(body)["redirect_count"], 2)

    def test_get_unknown_analytics_code_returns_not_found(self) -> None:
        status, _, body = invoke_wsgi(self.application, "GET", "/analytics/unknown")
        self.assertEqual(status, "404 Not Found")
        self.assertEqual(json.loads(body)["error"], "unknown_code")

    def test_get_unknown_code_returns_not_found(self) -> None:
        status, _, body = invoke_wsgi(self.application, "GET", "/unknown")
        self.assertEqual(status, "404 Not Found")
        self.assertEqual(json.loads(body)["error"], "unknown_code")
""",
)

ANALYTICS_README = _replace_once(
    GREENFIELD_README,
    """This dependency-free Python URL shortener is engineering output generated by the
governed Agentic SDLC demonstration. The approved workflow produced semantic
artifacts, validated file-materialization intents, derived change sets, and
transactionally created this project inside a disposable isolated workspace.
""",
    """This dependency-free Python URL shortener was originally produced by the
governed greenfield scenario and then enhanced by the governed brownfield analytics
scenario. The brownfield workflow seeded the verified existing six-file application
into a disposable isolated workspace, reasoned over bounded repository context, and
transactionally modified four existing files to add successful-redirect analytics
while preserving existing shortening and redirect behavior.
""",
)
ANALYTICS_README = _replace_once(
    ANALYTICS_README,
    """HTTP(S) URLs, generates stable collision-checked short codes, resolves known
codes, and reports typed errors for invalid URLs and unknown codes.
""",
    """HTTP(S) URLs, generates stable collision-checked short codes, resolves known
codes, counts successful resolutions, exposes per-code analytics, and reports typed
errors for invalid URLs and unknown codes.
""",
)
ANALYTICS_README = _replace_once(
    ANALYTICS_README,
    """A known code returns `302 Found`; an unknown code returns `404 Not Found`.

## Assumptions and trade-offs
""",
    """A known code returns `302 Found`; an unknown code returns `404 Not Found`.
Each successful redirect increments that code's process-local count.

Inspect analytics without incrementing the count:

```bash
curl -i http://127.0.0.1:8000/analytics/<code>
```

A known code returns `200 OK` with JSON such as
`{\"code\": \"abc12345\", \"redirect_count\": 7}`. An unknown analytics code
returns `404 Not Found`. Analytics lookup itself does not increment the count.

## Assumptions and trade-offs
""",
)
ANALYTICS_README = _replace_once(
    ANALYTICS_README,
    """- Persistence, expiration, analytics, authentication, and repository promotion
  remain outside this prototype.
""",
    """- Redirect counts are process-local, in-memory, and reset when the process
  restarts. Persistence, expiration, authentication, and repository promotion
  remain outside this prototype.
""",
)


class BrownfieldRepositoryContextPathProvider:
    """Task-scoped explicit paths plus normal dependency/retry evidence paths."""

    def paths_for_attempt(
        self,
        task: Task,
        *,
        dependency_paths: tuple[str, ...],
        retry_paths: tuple[str, ...],
    ) -> tuple[str, ...]:
        configured = BROWNFIELD_CONTEXT_PATHS_BY_SOURCE_KEY[task.source_key]
        return tuple(
            sorted(
                {
                    *(normalize_repository_path(path) for path in configured),
                    *(normalize_repository_path(path) for path in dependency_paths),
                    *(normalize_repository_path(path) for path in retry_paths),
                }
            )
        )


def brownfield_input() -> WorkflowState:
    """Return the deterministic brownfield enhancement intake."""

    return {
        "project_name": "URL Shortener Redirect Analytics",
        "requirements": [BROWNFIELD_RAW_REQUIREMENT],
        "raw_requirement": BROWNFIELD_RAW_REQUIREMENT,
    }


def brownfield_analysis() -> RequirementAnalysis:
    """Return the network-free structured brownfield requirement analysis."""

    return RequirementAnalysis(
        normalized_problem_statement=BROWNFIELD_RAW_REQUIREMENT,
        requirement_type="brownfield",
        functional_requirements=[
            "Count each successful short-code resolution.",
            "Expose redirect analytics for a known short code.",
            "Return a defined error for analytics on an unknown code.",
            "Preserve current shortening and redirect behavior.",
        ],
        nonfunctional_requirements=[
            "The enhancement must remain deterministic and dependency-free."
        ],
        constraints=[
            "Use the existing in-memory architecture and bounded repository context."
        ],
        ambiguities=[],
        assumptions=[
            "Counts are process-local and are not persisted across restarts."
        ],
        acceptance_criteria=[
            "Only successful resolution increments redirect_count.",
            "Analytics lookup does not increment redirect_count.",
            "Existing shorten and redirect behavior remains compatible.",
        ],
        risks=[
            "Incorrect placement of counting could count failed or analytics lookups."
        ],
        needs_clarification=False,
        confidence=0.95,
    )


def _task(
    key: str,
    title: str,
    task_type: TaskType,
    *,
    depends_on: list[str],
    materialization_policy: TaskMaterializationPolicy,
    expected_output: str,
) -> ProposedTask:
    return ProposedTask(
        key=key,
        title=title,
        description=f"Produce the governed {title.lower()} output.",
        task_type=task_type,
        materialization_policy=materialization_policy,
        depends_on=depends_on,
        requirement_refs=["FR-001", "FR-002", "FR-003", "FR-004"],
        acceptance_criteria_refs=["AC-001", "AC-002", "AC-003"],
        risk_refs=["RISK-001"],
        ambiguity_refs=[],
        expected_outputs=[expected_output],
    )


def brownfield_task_graph_proposal() -> ProposedTaskGraph:
    """Return the approved five-task brownfield enhancement graph."""

    analysis_task = _task(
        "impact_analysis",
        "Analyze brownfield redirect analytics impact",
        TaskType.DESIGN,
        depends_on=[],
        materialization_policy=TaskMaterializationPolicy.FORBIDDEN,
        expected_output="brownfield-impact-analysis",
    )
    analysis_task = analysis_task.model_copy(
        update={
            "requirement_refs": [
                "FR-001",
                "FR-002",
                "FR-003",
                "FR-004",
                "NFR-001",
                "CON-001",
            ]
        }
    )
    return ProposedTaskGraph(
        tasks=[
            analysis_task,
            _task(
                "service_analytics",
                "Implement service redirect analytics",
                TaskType.IMPLEMENTATION,
                depends_on=["impact_analysis"],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
                expected_output="url-shortener-service-analytics",
            ),
            _task(
                "analytics_http_api",
                "Implement analytics HTTP API",
                TaskType.IMPLEMENTATION,
                depends_on=["impact_analysis"],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
                expected_output="url-shortener-analytics-http-api",
            ),
            _task(
                "regression_tests",
                "Add analytics regression tests",
                TaskType.TEST,
                depends_on=["service_analytics", "analytics_http_api"],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
                expected_output="url-shortener-analytics-tests",
            ),
            _task(
                "documentation_update",
                "Document redirect analytics",
                TaskType.DOCUMENTATION,
                depends_on=["service_analytics", "analytics_http_api"],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
                expected_output="url-shortener-analytics-documentation",
            ),
        ]
    )


class BrownfieldAnalyticsExecutor:
    """Concurrency-safe deterministic executor with no filesystem authority."""

    model_name = "deterministic-brownfield-analytics-executor"

    def __init__(self) -> None:
        self.calls_by_task_id: dict[str, WorkspaceBoundTaskExecutionRequest] = {}
        self._lock = Lock()

    def execute(
        self, request: WorkspaceBoundTaskExecutionRequest
    ) -> TaskExecutionResult:
        with self._lock:
            self.calls_by_task_id[request.task_id] = request
        outputs = {
            "impact_analysis": (
                EngineeringArtifactType.DESIGN,
                "brownfield-impact-analysis",
                BROWNFIELD_IMPACT_ANALYSIS,
                None,
            ),
            "service_analytics": (
                EngineeringArtifactType.SOURCE,
                "url-shortener-service-analytics",
                ANALYTICS_SERVICE,
                "src/url_shortener/service.py",
            ),
            "analytics_http_api": (
                EngineeringArtifactType.SOURCE,
                "url-shortener-analytics-http-api",
                ANALYTICS_APP,
                "src/url_shortener/app.py",
            ),
            "regression_tests": (
                EngineeringArtifactType.TEST,
                "url-shortener-analytics-tests",
                ANALYTICS_TESTS,
                "tests/test_service.py",
            ),
            "documentation_update": (
                EngineeringArtifactType.DOCUMENTATION,
                "url-shortener-analytics-documentation",
                ANALYTICS_README,
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
            summary=f"Produced governed brownfield output for {request.task_id}.",
            outputs=(
                ArtifactOutput(
                    artifact_type=artifact_type,
                    logical_name=logical_name,
                    content=content,
                ),
            ),
            materialization_proposals=proposals,
            assumptions=("Redirect analytics remain process-local and in-memory.",),
            risks=("Counts reset when the process exits.",),
        )


def export_verified_brownfield_workspace(
    workspace_root: Path,
    export_root: Path,
    final_snapshot: WorkspaceSnapshot,
) -> None:
    """Export only final verified regular files for external reviewer use."""

    if export_root.is_symlink():
        raise AssertionError("Brownfield export root must not be a symlink.")
    if export_root.exists():
        shutil.rmtree(export_root)
    export_root.mkdir(parents=True)

    expected_hashes = {
        item.path: item.content_hash for item in final_snapshot.files
    }
    observed_hashes: dict[str, str] = {}
    for source in sorted(
        workspace_root.rglob("*"),
        key=lambda item: item.relative_to(workspace_root).as_posix(),
    ):
        relative = source.relative_to(workspace_root)
        relative_path = relative.as_posix()
        source_status = source.lstat()
        if stat.S_ISLNK(source_status.st_mode):
            raise AssertionError("Verified brownfield workspace contains a symlink.")
        if any(part.startswith(".") for part in relative.parts):
            raise AssertionError("Verified brownfield workspace contains hidden data.")
        if stat.S_ISDIR(source_status.st_mode):
            continue
        if not stat.S_ISREG(source_status.st_mode):
            raise AssertionError("Verified brownfield workspace has a special file.")
        contents = source.read_bytes()
        observed_hashes[relative_path] = hashlib.sha256(contents).hexdigest()
        destination = export_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents)
    if observed_hashes != expected_hashes:
        raise AssertionError("Brownfield export differs from authoritative snapshot.")


def write_brownfield_review_artifacts(
    output_dir: Path,
    state: WorkflowState,
    seed_result: WorkspaceSeedResult,
) -> None:
    """Add scenario bootstrap evidence and a concise evaluator summary."""

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "workspace_seed.json").write_text(
        json.dumps(seed_result.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    session = state["governed_workspace_session"]
    changes = [
        change
        for change_set in state["workspace_change_sets"]
        for change in change_set.file_changes
    ]
    mutation_by_change_set = {
        result.change_set_id: result for result in state["workspace_mutation_results"]
    }
    waves = [
        ", ".join(attempt.task_id for attempt in wave.task_attempts)
        for wave in state["task_execution_waves"]
    ]
    lines = [
        "# Brownfield Governed Redirect-Analytics Demo",
        "",
        "- Scenario type: BROWNFIELD",
        "- Starting codebase: Existing six-file URL-shortener project produced by "
        "the greenfield governed scenario.",
        "- Enhancement: Successful redirect analytics with `GET /analytics/{code}`.",
        f"- Baseline file count: {len(seed_result.files)}",
        f"- Baseline authoritative snapshot: {session.baseline_snapshot_id}",
        f"- Final authoritative snapshot: {session.authoritative_snapshot_id}",
        f"- Final workspace integrity: {session.integrity_status.value}",
        "- Mutation result: 4 MODIFY, 0 CREATE, 0 DELETE",
        "- Greenfield source project preserved: VERIFIED",
        "- Enhanced-project hashes match final snapshot: VERIFIED",
        "",
        "## Approved TaskGraph",
        "",
        "- TASK-001 analysis — FORBIDDEN",
        "- TASK-002 service analytics — REQUIRED",
        "- TASK-003 analytics HTTP API — REQUIRED",
        "- TASK-004 regression tests — REQUIRED",
        "- TASK-005 documentation — REQUIRED",
        "",
        "## Brownfield codebase reasoning",
        "",
        "TASK-001 reasoned against bounded existing content from:",
        "",
        "- `README.md`",
        "- `pyproject.toml`",
        "- `src/url_shortener/app.py`",
        "- `src/url_shortener/service.py`",
        "- `tests/test_service.py`",
        "",
        "Impact conclusion:",
        "",
        "- Modified: `src/url_shortener/service.py`, "
        "`src/url_shortener/app.py`, `tests/test_service.py`, and `README.md`.",
        "- Not impacted: `pyproject.toml` (no dependency change) and "
        "`src/url_shortener/__init__.py` (no package-export change).",
        "- Existing resolution flows from generic `GET /{code}` through the WSGI "
        "adapter into `URLShortener.resolve`.",
        "- Successful resolution is the counting boundary; "
        "`GET /analytics/{code}` must be routed before generic `GET /{code}`.",
        "- Analytics remains in-memory; no database or external dependency was "
        "introduced.",
        "",
        "## Execution waves",
        "",
        *(f"- Wave {index}: {members}" for index, members in enumerate(waves, 1)),
        "",
        "TASK-002 and TASK-003 reason in one controlled parallel wave against the "
        "same authoritative baseline binding. Their disjoint service/app writes "
        "are conflict-free; governed transactions then apply in deterministic "
        "serialized order and advance the authoritative snapshot after each "
        "verified mutation. TASK-004 and TASK-005 follow the same pattern against "
        "the shared post-implementation binding.",
        "",
        "## Governed MODIFY evidence",
        "",
    ]
    for change_set in state["workspace_change_sets"]:
        result = mutation_by_change_set[change_set.change_set_id]
        for change, evidence in zip(
            change_set.file_changes, result.file_evidence, strict=True
        ):
            lines.extend(
                [
                    f"### `{change.path}`",
                    "",
                    f"- Operation: {change.operation.value}",
                    f"- Mutation status: {result.status.value}",
                    f"- Pre-mutation snapshot: {result.pre_mutation_snapshot_id}",
                    f"- Post-mutation snapshot: {result.post_mutation_snapshot_id}",
                    f"- Expected preimage: `{evidence.expected_preimage_hash}`",
                    f"- Observed preimage: `{evidence.observed_preimage_hash}`",
                    f"- Desired postimage: `{evidence.desired_postimage_hash}`",
                    f"- Observed postimage: `{evidence.observed_postimage_hash}`",
                    f"- Write performed: {str(evidence.write_performed).lower()}",
                    "- Preimage verified: "
                    + str(
                        evidence.expected_preimage_hash
                        == evidence.observed_preimage_hash
                    ).lower(),
                    "- Postimage verified: "
                    + str(
                        evidence.desired_postimage_hash
                        == evidence.observed_postimage_hash
                    ).lower(),
                    "",
                ]
            )
    lines.extend(
        [
            "## Modified",
            "",
            *(f"- `{path}`" for path in BROWNFIELD_IMPACTED_PATHS),
            "",
            "## Unchanged",
            "",
            *(f"- `{path}`" for path in BROWNFIELD_UNCHANGED_PATHS),
            "",
        ]
    )
    if len(changes) != 4:
        raise AssertionError("Completed brownfield scenario requires four changes.")
    (output_dir / "summary.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
