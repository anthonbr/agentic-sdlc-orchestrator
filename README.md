# Agentic SDLC Orchestrator

This repository incrementally demonstrates controlled, auditable execution across
the software-development lifecycle.

## Version progression

- **V0.1 — LangGraph orchestration foundation:** explicit sequential and parallel
  control flow, synchronization, gates, state, trace, and artifacts.
- **V0.2 — Stateful human governance:** checkpointed APPROVE,
  REQUEST_CHANGES, REJECT, bounded revisions, and safe stop.
- **V0.3 — Governed LLM requirement reasoning:** schema-backed requirement
  analysis, deterministic validation, human revision, and decision lineage.
- **V0.4 — Governed LLM task planning:** a human-approved requirement
  specification, LLM-proposed engineering task dependencies, deterministic graph
  normalization/validation, and human TaskGraph approval.
- **V0.5 runtime foundation (current slices):** separate execution state, a
  deterministic interpreter for readiness and transitions, and application-owned
  execution contracts, artifact canonicalization, and structural validation.

The current V0.5 slice interprets an approved plan but deliberately does **not
execute engineering work**, call an LLM task executor, write application code, or
implement the URL Shortener demonstration service.

## Two distinct graphs

V0.4 intentionally keeps two graph concepts separate.

### Orchestration / control graph

The static application workflow is implemented with LangGraph. It owns routing,
bounded retries and revisions, human interrupts, safe-stop behavior, checkpointed
state, the existing parallel artifact branches, synchronization, and the exit
gate. The LLM never creates or changes LangGraph nodes or routes.

```mermaid
flowchart TD
    START --> intake[requirements_intake]
    intake --> entry[entry_gate]
    entry -->|failed| END
    entry --> analyst[requirement_analysis_task]
    analyst --> validateAnalysis[validate_requirement_analysis]
    analyst -->|provider failure| analysisRetry[bounded analysis retry]
    validateAnalysis -->|invalid| analysisRetry
    analysisRetry --> analyst
    analyst -->|exhausted/non-retryable| safe[safe_stop]
    validateAnalysis -->|valid| requirementReview[requirement_analysis_review]
    requirementReview -->|request changes| analysisRevision[prepare analysis revision]
    analysisRevision --> analyst
    requirementReview -->|reject/revision limit| safe
    requirementReview -->|approve| spec[build_approved_requirement_spec]
    spec --> planner[task_decomposition_task]
    planner --> validateGraph[normalize_and_validate_task_graph]
    planner -->|provider failure| graphRetry[bounded task-planning retry]
    validateGraph -->|invalid| graphRetry
    graphRetry --> planner
    planner -->|exhausted/non-retryable| safe
    validateGraph -->|valid| graphReview[task_graph_review]
    graphReview -->|request changes| graphRevision[prepare graph revision]
    graphRevision --> planner
    graphReview -->|reject/revision limit| safe
    graphReview -->|approve| approved[approve_task_graph]
    approved --> architecture[architecture_task]
    approved --> tests[test_plan_task]
    architecture --> sync[synchronize]
    tests --> sync
    sync --> exit[exit_gate]
    safe --> END
    exit --> END
```

Both human checkpoints use LangGraph `interrupt()` and a process-local
`InMemorySaver`; the CLI resumes the same thread with `Command(resume=...)`.
Interrupted state survives within the current process, not across process restarts.

### Engineering task dependency graph

The `TaskGraph` is a dynamic per-run domain artifact. The LLM proposes semantic
tasks and dependencies using temporary keys. Application code assigns authoritative
identity, validates references and the DAG, derives graph semantics, and a human
approves the result. A future executor may interpret this graph; V0.4 does not.

Connectivity is represented once through `Task.depends_on`. Topological order,
execution layers, naturally parallel tasks, ENTRY-ready tasks, EXIT predecessors,
and synchronization points are derived rather than stored as competing
authoritative graph copies.

### Deterministic execution runtime foundation

The execution runtime keeps mutable progress separate from the immutable approved
plan:

```text
approved TaskGraph
    -> TaskGraphExecutionState
    -> deterministic readiness and synchronization transitions
```

Tasks without dependencies initialize as `READY`; dependent tasks initialize as
`BLOCKED`. Starting a ready task moves it to `RUNNING` and increments its attempt
count. Successful completion unlocks a blocked task only after every declared
dependency has succeeded. A task failure marks the graph execution `FAILED` and
freezes new dispatch. Already-running peers may settle without unlocking more
work; failure remains sticky, and `SAFE_STOPPED` requires no task to remain
`RUNNING`. Retry policy is deferred.

This module is not wired into the LangGraph workflow yet. Multiple tasks can be
ready at once, representing future parallel execution, but this slice adds no
concurrency, LLM task executor, repository mutation, or dynamic LangGraph nodes.

### Execution contract and artifact boundary

The deterministic contract layer stops before scheduler settlement:

```text
TaskExecutionRequest
    -> future executor
    -> TaskExecutionResult
    -> EngineeringArtifact(s)
    -> TaskExecutionValidationResult
    -> no automatic scheduler transition yet
```

`TaskExecutionRequest` is application-owned context for one already-running task
attempt. UUIDv5 request and attempt identities are derived from the approved spec,
graph, task, and attempt number. Every request contains the approved normalized
problem statement, requirement type, and assumptions, plus only the canonical
FR/NFR/CON/AC/RISK/AMB items referenced by that task. It does not contain the raw
conversation or requirement-analysis history. Dependency artifacts must be
canonical outputs from the current successful attempt of a declared direct
dependency; arbitrary or transitive artifacts are rejected.

`TaskExecutionResult` is a non-authoritative semantic proposal. It may contain a
summary, typed logical outputs, assumptions, and risks, but it cannot declare task
success or assign canonical IDs, lineage, hashes, or runtime state. Application
code converts each output into an immutable `EngineeringArtifact`, preserving
content while assigning provenance, an attempt-specific artifact ID, and a stable
artifact-slot lineage based on task lineage, output ordinal, type, and logical
name. Semantic cross-attempt reconciliation after output reordering or renaming is
deferred.

An artifact content hash covers canonical output content and authoritative
spec/graph/task/request/attempt/reference provenance, including output ordinal. It
excludes the application-supplied creation timestamp and the derived artifact and
lineage IDs. Artifact production does not equal acceptance: deterministic
`TaskExecutionValidationResult` records the separate judgment and the exact ordered
artifact IDs it evaluated, and no `accepted` flag is stored on the artifact. A
source task's `SUCCEEDED` runtime state is necessary but not sufficient for its
artifacts to become downstream context. The request builder also requires matching
successful validation evidence for each dependency's complete canonical artifact
set; missing, failed, partial, extra, stale, or mismatched evidence is rejected.
Every declared direct dependency requires evidence, including an explicitly
validated empty artifact-ID set when it produced no artifacts; omitting both its
artifacts and validation is not evidence of an empty accepted output. Fan-in
context is ordered by declared dependency and then canonical output order.

Initial validation checks correlation, required output presence, nonblank logical
names and contents, canonical artifact count, provenance, identity/hash integrity,
and output correspondence. V0.4 `expected_outputs` values are free-form descriptive
obligations, so this slice requires at least one output when obligations exist but
does not claim semantic one-to-one matching. A `SOURCE` artifact remains data only;
it is not written to the repository. No OpenAI task executor or filesystem/shell
execution exists in this slice.

## Governed planning and lineage

After requirement-analysis approval, deterministic code packages the exact
approved text into an immutable `ApprovedRequirementSpec`. There is no second LLM
rewrite between approval and specification creation.

The application assigns these human-readable namespaces:

- `FR-001`, `FR-002`, ... — functional requirements
- `NFR-001`, ... — nonfunctional requirements
- `CON-001`, ... — constraints
- `AC-001`, ... — acceptance criteria
- `RISK-001`, ... — risks
- `AMB-001`, ... — approved unresolved ambiguities

Each item also receives a deterministic application-generated lineage UUID. Its
initial identity includes the namespace, canonical item ID, and exact text, so two
same-namespace items with duplicate text still have distinct lineage IDs. Future
cross-version semantic reconciliation is deliberately deferred. Text is copied
exactly from the approved analysis.

The spec has a version, content hash, creation timestamp, optional predecessor ID,
and source analysis revision. A content hash covers the canonical hashed payload,
including its source provenance and application-assigned identities; it excludes
version-envelope fields such as timestamp, version, and predecessor. `SPEC-...-V001`
or `GRAPH-...-V002` identifies a specific immutable artifact version, while its
lineage UUID connects deliberately related versions.

The task planner receives only this approved specification. It may propose task
titles, descriptions, types, temporary keys, dependencies, traceability references,
and expected outputs. It cannot assign `TASK-###`, graph IDs, lineage IDs,
timestamps, hashes, versions, layers, ENTRY/EXIT tasks, approval state, or execution
state.

Deterministic normalization maps proposal order to `TASK-001`, `TASK-002`, and so
on, remaps temporary dependency keys, and assigns stable task lineage from the graph
lineage plus semantic key. Validation rejects duplicate keys/IDs, missing or self
dependencies, cycles, invalid specification references, and graphs without valid
synthetic ENTRY/EXIT semantics. Invalid graphs never reach human review.

Core traceability is bidirectional: every task reference must resolve to the
approved specification, and every approved FR, NFR, CON, and AC item must be
covered by at least one task. Missing coverage enters the bounded task-planning
retry path and cannot be human-approved as structurally complete. RISK and AMB
references are validated when present, but complete risk/ambiguity disposition is
intentionally deferred until a later milestone has an explicit disposition model.

An approved ambiguity remains explicit. For example, `AMB-001: URL expiration is
unspecified` can support a task to resolve that policy; the planner is instructed
not to silently replace it with a made-up 30-day expiration rule.

TaskGraph review supports APPROVE, REQUEST_CHANGES, and REJECT. Requested changes
preserve feedback and the prior validated graph, receive a fresh three-attempt
machine retry budget, create a new immutable graph version, and return to review.
Human graph revisions are separately limited to three. Rejection or exhaustion
safe-stops without executing tasks.

The models include versions, content hashes, lineage IDs, and optional predecessor
IDs so a future milestone can represent `SPEC-v1 -> GRAPH-v1` followed by
`SPEC-v2 -> GRAPH-v2` without mutating history. V0.4 does not implement upstream
change reconciliation or execution-state migration.

## Setup and run

Python 3.13 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env
```

Set a real `OPENAI_API_KEY` in the ignored local `.env`. `OPENAI_MODEL` defaults
to `gpt-5.6-luna`. The project does not load `.env` itself, so export it before the
interactive demo:

```bash
set -a
source .env
set +a
.venv/bin/python -m agentic_sdlc demo
```

The CLI presents the full requirement analysis first. After requirement approval,
it displays the canonical specification namespaces, TaskGraph tasks and links,
derived execution layers, parallelism, joins, and ENTRY/EXIT semantics. It then
pauses for separate TaskGraph approval. REQUEST_CHANGES feedback at either stage
may span multiple lines and ends with a blank line.

Missing credentials never trigger a fake fallback. A missing key at either LLM
stage records a clear non-retryable failure and safely stops.

Successful artifacts are written under `artifacts/demo-run/`:

```text
requirements.json
requirement_analysis.md
approved_requirement_spec.json
task_graph.json
task_graph.md
architecture.md
test_plan.md
summary.md
```

`task_graph.json` is the canonical graph. `task_graph.md` is a human-readable view
that includes derived layers and governance history. The generated
`artifacts/workflow_diagram.png` documents the static LangGraph control plane, not
the per-run engineering TaskGraph.

## Tests

```bash
.venv/bin/python -m pytest
```

Tests inject scripted `FakeRequirementAnalysisClient` and
`FakeTaskPlanningClient` instances. They require no API key or network access and
cover structured parsing, retries, safe stops, identity assignment, lineage,
reference integrity, DAG validation, derived parallel/join semantics, both human
approval loops, artifacts, the preserved static parallel branches, and isolated
deterministic TaskGraph runtime transitions. Execution-contract tests additionally
cover approved-context filtering, direct dependency artifact flow, canonical
identity/provenance, executor trust boundaries, and separate validation.

## Deliberately deferred

The current V0.5 slice does not include actual engineering-task execution, an LLM
task executor, concurrent execution, retry/recovery policy, code-generation or
repository-writing agents, dynamically generated LangGraph nodes, full dynamic
replanning, completed-task reconciliation, brownfield impact analysis, rollback,
a database/audit store, distributed scheduling, deployment, or a web UI.
