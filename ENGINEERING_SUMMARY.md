# Engineering Summary

## 1. Objective and Engineering Approach

The Agentic SDLC orchestrator demonstrates how model-assisted engineering can operate inside explicit authority, execution, and evidence boundaries. Its lifecycle accepts requirements plus separate application-owned delivery policy, rejects unusable input at a deterministic entry gate, uses an LLM for structured requirement analysis, applies deterministic ambiguity policy, pauses for human decisions, derives an approved requirement specification, plans and validates an engineering dependency graph, pauses again for approval, executes bounded work in an isolated workspace, and evaluates a deterministic exit gate. When the application requests a runnable project, that gate now requires material evidence of a launch surface, automated tests, and root run instructions before durable export.

The engineering objective is controlled autonomy, not maximum autonomy. Models propose analysis, plans, and task outputs; they do not approve those outputs, assign themselves repository authority, or decide that incomplete evidence is sufficient. Deterministic controls establish identities, validate lineage and dependencies, constrain filesystem effects, and settle execution outcomes. Human decisions authorize the transitions where judgment changes downstream authority.

This makes the system materially different from a code generator or linear agent chain. Work is governed by an approved specification, decomposed into a per-run dependency DAG, synchronized across sequential and parallel paths, and accepted only through traceable validation and mutation evidence.

## 2. Architecture Rationale

The central design choice is to separate useful nondeterministic reasoning from authoritative decisions. An LLM can identify ambiguities and propose an engineering plan, but deterministic application code decides whether the analysis is `READY` or `BLOCKED`, whether a graph is a valid DAG with complete requirement coverage, whether its source specification is still authoritative, and whether execution evidence satisfies the exit gate. This permits model reasoning without making model confidence a control mechanism.

The implementation also separates two graph responsibilities. A relatively stable LangGraph control graph governs lifecycle transitions, human interrupts, retries, safe stops, and exit evaluation. A per-run engineering TaskGraph represents typed lifecycle work and its dependencies. The control graph interprets TaskGraph data; it does not rewrite itself into a new LangGraph topology for each requirement. This preserves a reviewable control plane while allowing each approved specification to produce different design, implementation, test, documentation, validation, and release work.

Authority flows downstream from the human-approved requirement analysis into a canonical approved requirement specification. A TaskGraph records the exact source specification identity and version from which it was produced. Human TaskGraph approval is necessary but not sufficient if that source later becomes stale: source-authority checks still guard execution initialization and every execution-loop advance. Deeper control, lineage, and transaction mechanics are documented in [ARCHITECTURE.md](ARCHITECTURE.md).

## 3. Demonstration Scenarios

The checked-in samples expose one coherent publication lineage plus an interactive ambiguity demonstration.

| Scenario | Engineering behavior demonstrated | Evidence and runnable product | Verified product tests |
| --- | --- | --- | ---: |
| V17 greenfield | Approved requirement revision, governed planning and validation, transactional creation, and verified publication of a four-file URL shortener | [`sample_output/url-shortener-v17/`](sample_output/url-shortener-v17/) with its manifest-bound [`sdlc-artifacts/`](sample_output/url-shortener-v17/sdlc-artifacts/) | 13 passed |
| V18 brownfield | Baseline selection and integrity, bounded codebase impact analysis, four preimage-checked `MODIFY` transactions, governed validation, and separate publication | [`sample_output/url-shortener-v18-expiration/`](sample_output/url-shortener-v18-expiration/) with V17 lineage in [`workspace_execution.json`](sample_output/url-shortener-v18-expiration/sdlc-artifacts/workspace_execution.json) | 20 passed |
| Ambiguous requirement | Requirement Analysis exposes ambiguities and blocks planning until human clarification establishes new downstream authority | Demonstrated interactively through the live CLI/Streamlit workflow; deterministic regression coverage remains in [`tests/test_ambiguity_demo.py`](tests/test_ambiguity_demo.py) | Live demonstration |

The live ambiguity demonstration begins with an underspecified requirement. Requirement Analysis exposes the unresolved product decisions, and deterministic policy marks planning readiness `BLOCKED`; the reviewer can only request changes or reject, and no approved specification or TaskGraph exists. Human clarification creates an immutable revised analysis. Only a `READY`, approved revision produces the authoritative specification and the first authorized TaskGraph.

That sequence demonstrates the governed requirements-to-planning boundary, not replacement of a Revision 0 plan—there was no such plan. Separate tests demonstrate stale-source protection: if an already-created TaskGraph no longer matches the current specification identity/version, execution stops before workspace initialization or before the next dispatch and mutation. A replacement must be regenerated, deterministically validated, and governed again; the running graph is not edited in place.

The curated V18 evidence identifies `url-shortener-v17` as its selected baseline and binds V17's originating run ID, publication bundle hash, source snapshot, verified seed, and governed baseline snapshot. V17 remains a distinct unchanged publication; V18 does not overwrite it. The copied evidence retains its original `runs/...` and `projects/...` paths because those values record the real execution and publication.

## 4. Important Engineering Decisions

| Decision | Rationale | Engineering consequence |
| --- | --- | --- |
| Stable lifecycle control graph plus per-run TaskGraph | Keep governance reviewable while allowing requirement-specific decomposition. | Dynamic work varies as data; the control plane remains explicit and testable. |
| Approved requirement specification as downstream authority | Prevent proposed or ambiguous text from silently authorizing work. | Planning receives a canonical specification with revision lineage rather than raw model output. |
| Additive requirement revisions and blocking before planning | Preserve what changed, why it changed, and who authorized continuation. | Clarification creates a new revision; Revision 0 remains inspectable and cannot feed the planner. |
| Source-spec validation at execution boundaries | Approval should not keep a plan authoritative after its premise changes. | A stale TaskGraph safe-stops before new workspace access, dispatch, or mutation. |
| Dependency-aware execution with a parallelism cap of two | Demonstrate real concurrency while bounding resource use and synchronization complexity. | Independent tasks may overlap; dependency joins and evidence settlement remain deterministic. |
| Isolated, transactional workspace mutation | Model-produced content should not translate directly into unrestricted filesystem writes. | The application derives and validates `CREATE`, `MODIFY`, or `NO_CHANGE`, applies eligible changes serially, and verifies resulting state. `DELETE` is not supported. |
| Deterministic evidence and reliability projection | Reliability claims should follow retained facts rather than inferred telemetry. | Metrics are recomputed read-only from execution and workspace evidence and do not influence the run. |
| Derived requirement-to-code reports | Readers need both an approachable summary and exact evidence without creating another authority system. | One read-only projection supplies Streamlit plus deterministic JSON/Markdown reports; missing links remain explicit, and manifest-verified publication retains and copies the reports without changing execution authority. |
| Structured governed validation authority | Artifact generation, provisioning, test execution, and passing validation are different facts; task prose must not grant process authority. | Human-reviewed `required_validations` select closed application profiles. `PYTHON_COMPILE` performs fixed compilation; `PYTHON_PYTEST` provisions accepted staged dependencies and runs generated tests in disposable Docker. Exact immutable PASS evidence and cleanup are required before live mutation and task success. |
| Application-required final validation | A planning omission must not make execution optional, and a task-level pass may become stale after later mutations. | For runnable projects, the exit gate derives compile/pytest requirements from the exact final snapshot and requires matching governed PASS evidence before readiness or publication, independently of planner-requested validations. |
| Application-owned runnable-project delivery policy | Product delivery completeness should not be an LLM-invented business requirement or a filename heuristic. | Structured deliverable roles participate in TaskGraph identity/review, executor and materialization validation, final authoritative-snapshot readiness evidence, and the exit gate. |
| Governed composite project publication; Git, CI/CD, and deployment outside autonomous authority | Workspace mutation, SDLC evidence ownership, and durable promotion are separate trust decisions. | Agent-controlled effects end in the isolated workspace; the application publishes only an exit-verified project projection plus a manifest-verified same-run evidence projection into a new project directory, while branch, commit, push, merge, release, and deployment remain external activities. |

Together, these decisions favor bounded blast radius, reproducibility, and reviewable authority over unconstrained exploration or self-modifying execution.

## 5. Validation Strategy

Validation is intentionally split between the orchestrator and the products it creates or enhances.

The current orchestrator checkpoint completed with **797 tests passed** and five opt-in Docker integration tests skipped by the ordinary deterministic run. The suite uses scripted model adapters and deterministic executors, so control behavior can be exercised without API credentials or network variability. It covers structured-output parsing, deterministic requirement and project readiness, both human approval loops, canonical identities, specification and TaskGraph lineage, approved validation profiles, disposable candidate postimages, fixed-profile execution evidence and success gating, application-required final-snapshot validation, bounded validation diagnostics and retries, Docker lifecycle and dependency policy, DAG and deliverable-role coverage, sequential and fan-out/fan-in scheduling, true bounded overlap, live progress and heartbeat ordering, retry exhaustion, stale-source rejection, artifact filtering, conflict reconciliation, isolated workspace containment, preimage and postimage checks, rollback, rollback failure, hard safe stops, exit-gate completeness, live run-evidence ownership and manifest integrity, verified composite non-overwriting project publication, reliability derivation, and the read-only requirement-to-code traceability projection. Fault injection verifies failure paths rather than inferring them from positive demonstrations. The five repository-owned Docker smoke tests also passed separately against the fixed image.

Generated-product behavioral validation remains a separate governed layer. V0.13
introduced the fixed `PYTHON_COMPILE` profile. V0.14 adds the human-approved
`PYTHON_PYTEST` governed validation profile. The
application constructs a disposable candidate postimage, invokes only its own
fixed compile or Docker/pip/pytest argv, retains bounded immutable provisioning and
execution evidence, proves disposable-container cleanup, and blocks success without
an exact PASS. Compilation remains syntax-only. Pytest proves only that the
recorded generated test suite passed in its recorded provisioned container; it does
not prove benchmarks, deployment, production readiness, or performance. The curated
V17 greenfield and V18 brownfield products are dependency-free, independently
runnable URL-shortener projects whose suites completed with 13 and 20 passing tests
respectively. Those product checks were performed separately and confer no Git,
CI/CD, release, or deployment authority.

For `RUNNABLE_PROJECT`, these profiles are also application-required over the
exact final authoritative snapshot whenever Python source/tests are present. This
final gate is independent of the planner's task-level list, so an LLM omission
cannot publish an unexecuted Python project. Docker must be installed and running
(Docker Desktop on macOS/Windows or Docker Engine on Linux); unavailability fails
closed without a host-pytest fallback.

Each demonstration bundle retains the approved specification, requirement decisions, TaskGraph, execution attempts/results, engineering artifacts, workspace mutation records, and summary needed to inspect the demonstrated path. Within a run, frozen records and additive histories provide immutable-by-contract evidence. The default LangGraph checkpoint is process-local, and exported JSON and Markdown are ordinary files, not a tamper-evident durable event store. The evidence is therefore strong for deterministic reconstruction and review within the prototype's trust model, but it is not a claim of production proof or durable audit infrastructure.

Artifact ownership is explicit: `sample_output/` remains curated checked-in
reference material rather than live or authoritative execution history. Live CLI
and Streamlit evidence is isolated by the governed identity under
`runs/<run-id>/sdlc-artifacts/`, and successful durable delivery packages are
published under `projects/`. A deterministic terminal manifest indexes the live
bundle's actual relative files, sizes, and SHA-256 values without claiming signing
or tamper-proofing. Task Agents have no authority over the run-evidence directory
or the reserved project-local `sdlc-artifacts/` namespace. Publication retains the
live bundle and adds a verified copy while independently proving the application
projection still equals the authoritative workspace. Successful bundles also
contain `requirement_traceability.json` and `requirement_traceability.md`, both
deterministically derived before publication from the existing governed evidence.
They are marked non-authoritative, retain explicit gaps and exact relationship
bases, and cannot turn missing validation into a verified claim. Streamlit and the
Markdown report explain `UNVERIFIED` as implemented without a proven item-specific
validation link—not as failed implementation or failed tests—while technical IDs
remain available for audit.

After final TaskGraph approval, the CLI now renders application-owned execution
progress and a bounded heartbeat while executor futures remain incomplete. Only
the orchestration thread reports wave membership, executor return, and final
settlement; worker threads still invoke only `TaskExecutor.execute()`. This output
requires no additional keyboard input and remains ephemeral: it does not change
governed state, canonical trace order, run evidence, manifest identity, or
reliability metrics, and it makes no percentage-complete claim.

## 6. Reliability and Failure Handling

Reliability behavior is fail-closed and bounded. Task execution permits at most three attempts. Retryability and recovery are decided by application policy; SDK-level hidden retries are disabled. The prototype does not silently switch providers or models. Dependency failures, exhausted attempts, stale authority, invalid evidence, or unverified workspace state route to explicit failure or safe-stop outcomes.

For eligible mutation failures, the runtime restores owned targets in reverse order, verifies rollback against the prior snapshot, and records either `ROLLED_BACK` or `ROLLBACK_FAILED`. Inability to prove restoration is not accepted as success: workspace integrity becomes `UNPROVABLE`, unsettled work is aborted, and the control graph takes a hard safe stop.

[`sample_output/reliability_metrics.json`](sample_output/reliability_metrics.json) is a deterministic, read-only projection over the curated V17 and V18 execution and workspace evidence. It reports supported structural measures such as task and attempt outcomes, retry frequency, mutation outcomes, rollback counts, and safe-stop status. MTTR and end-to-end latency are both `NOT_MEASURED` because the evidence model does not retain authoritative incident-to-recovery or elapsed-time boundaries. File timestamps are not substituted for missing telemetry.

## 7. Security and Change-Control Guardrails

The change-control path narrows authority at each boundary: bounded repository context → isolated workspace → restricted mutation vocabulary → deterministic validation → targeted preimage checks → serialized application → postimage verification → verified rollback or hard safe stop → exit-verified composite project publication → external Git and release control.

Executors receive explicit UTF-8 file content, hashes, and nonexistence facts for authorized paths rather than a repository root handle or autonomous discovery capability. Mutations reject absolute paths, traversal, protected `.git` and environment targets, symlinks, special files, and duplicate targets. `CREATE` requires nonexistence; `MODIFY` requires the expected preimage; `NO_CHANGE` verifies the desired state without writing. An authoritative workspace snapshot advances only after a verified `APPLIED` result.

This reduces filesystem blast radius, but the isolated temporary directory is not presented as a container, virtual machine, hostile-process sandbox, or cross-process transaction manager. The initial fixed compile profile is deliberately narrow: generated source is parsed and compiled but not imported or executed, interpreter startup uses isolation, environment inheritance is denied, and command side effects remain disposable. Stronger generated-code execution requires a replaceable container or OS-sandbox backend plus application-governed dependency provisioning. Likewise, permission to change workspace files is not permission to select commands, install packages, modify the authoritative Git repository, or promote a release.

## 8. Assumptions

The prototype assumes that identified human reviewers are the trusted source of requirement and TaskGraph approvals. It assumes the host filesystem can support the regular-file identity, atomic replacement, and snapshot checks used by the mutation layer. It also assumes repository context is deliberately selected by the application; autonomous repository discovery is outside scope.

Model providers may fail or return invalid structured data. The design does not assume model correctness: bounded parsing/retry behavior and safe stops contain those failures. Process-local checkpoints and retained temporary workspaces are sufficient for this demonstrator's review workflow, but not for distributed recovery.

## 9. Risks and Trade-offs

- **Authority checks versus workflow complexity.** Revision, identity, lineage, and approval checks add state and test burden. They also make it possible to explain why a task was authorized and to reject stale work before effects occur.

- **Immutable revisions versus in-place flexibility.** Additive revisions preserve history and make authority transitions auditable. The cost is explicit regeneration and renewed governance instead of quietly editing prior analyses or plans.

- **Hard safe stop versus continued progress.** Halting when integrity cannot be proven can reduce availability and require human intervention. Continuing would risk treating an unknown workspace as authoritative, which is a worse failure mode for this design.

- **Demo container isolation versus production sandboxing.** Generated pytest no longer executes on the host: one disposable Docker container receives a copied staged postimage, no host workspace/secrets/socket mounts, dropped capabilities, no-new-privileges, and modest memory/PID limits. The fixed tag, public unlocked dependencies, default Docker runtime, and best-effort test-network disconnection remain prototype trade-offs rather than production hostile-code or supply-chain guarantees.

- **Static lifecycle governance versus self-modifying control flow.** A stable LangGraph topology is reproducible and amenable to exhaustive transition testing. It deliberately gives up live dependency surgery and active-task migration.

- **Parallel reasoning versus serialized effects.** Bounded task overlap demonstrates concurrency and shortens independent reasoning paths, while canonical reconciliation and serialized mutations protect deterministic state. Mutation throughput is traded for simpler conflict and rollback semantics.

- **Evidence-only metrics versus broader observability.** Reporting only derivable measures avoids fabricated precision. It leaves latency and recovery-time questions unanswered until authoritative timing boundaries exist.

## 10. Deliberate Limitations

The prototype does not mutate an executing TaskGraph, rewrite dependencies, cancel or migrate running tasks, or automatically install a replacement graph. Governed replanning means changed upstream authority invalidates stale downstream authority; a newly generated graph must pass validation and human governance before execution.

It has no autonomous Git branch, commit, push, pull-request, merge, or authoritative-repository promotion capability. It does not promote CI/CD stages or deploy software. Governed validation is limited to fixed `PYTHON_COMPILE` and Docker-backed `PYTHON_PYTEST`; the system does not start generated servers, install the generated project, run benchmarks, accept arbitrary commands, use private indexes, require dependency locks/hashes, or provide production package-supply-chain guarantees. The Task Agent cannot choose Docker, pip, pytest, image, index, environment, or shell authority. The system also does not provide distributed workers or persistent crash recovery, support unrestricted filesystem operations or `DELETE`, or implement a durable tamper-evident audit service. Separately governed benchmark profiles and stronger image/dependency provenance remain future work.

MTTR and end-to-end latency remain unmeasured. These are deliberate evidence boundaries, not zero-valued results. The implementation is a governed orchestration prototype, not a production distributed scheduler, repository-management service, CI/CD platform, or deployment system.

## 11. Evidence and Artifact Map

| Purpose | Location |
| --- | --- |
| Fast repository entry point | [README.md](README.md) |
| Detailed architecture and control mechanics | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Engineering rationale and readiness | [ENGINEERING_SUMMARY.md](ENGINEERING_SUMMARY.md) |
| V17 greenfield publication and evidence | [`sample_output/url-shortener-v17/`](sample_output/url-shortener-v17/) |
| V18 brownfield publication and evidence | [`sample_output/url-shortener-v18-expiration/`](sample_output/url-shortener-v18-expiration/) |
| V18 baseline identity and integrity | [`workspace_execution.json`](sample_output/url-shortener-v18-expiration/sdlc-artifacts/workspace_execution.json) |
| V18 impact analysis and revised authority | [`approved_requirement_spec.json`](sample_output/url-shortener-v18-expiration/sdlc-artifacts/approved_requirement_spec.json) |
| Interactive ambiguity regression coverage | [`tests/test_ambiguity_demo.py`](tests/test_ambiguity_demo.py) |
| Curated reliability projection | [`sample_output/reliability_metrics.json`](sample_output/reliability_metrics.json) |

The most efficient review path is each bundle's `summary.md`, followed by its requirement analysis, approved specification, TaskGraph, task-execution evidence, and workspace-execution evidence where a claim needs deeper verification.

## 12. Final Readiness

The repository demonstrates a controlled-autonomy lifecycle from deterministic intake and governed requirement authority through validated planning, bounded dependency-aware execution, fixed-profile governed validation, transactional isolated-workspace mutation, runnable-project readiness, exit evaluation, verified composite project publication, and independently retained evidence. The current checkpoints are 797 passing orchestrator tests (plus five separately passed opt-in Docker integration tests) and independently runnable V17 and V18 products with 13 and 20 passing tests.

This describes the implemented prototype, not autonomous Git, release, or production deployment. Its authority boundaries, failure behavior, trade-offs, and unmeasured reliability dimensions are explicit, and the checked-in evidence provides concrete paths for independent verification.
