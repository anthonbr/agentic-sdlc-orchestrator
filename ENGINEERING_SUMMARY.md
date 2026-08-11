# Engineering Summary

## 1. Objective and Engineering Approach

The Agentic SDLC orchestrator demonstrates how model-assisted engineering can operate inside explicit authority, execution, and evidence boundaries. Its lifecycle accepts requirements plus separate application-owned delivery policy, rejects unusable input at a deterministic entry gate, uses an LLM for structured requirement analysis, applies deterministic ambiguity policy, pauses for human decisions, derives an approved requirement specification, plans and validates an engineering dependency graph, pauses again for approval, executes bounded work in an isolated workspace, and evaluates a deterministic exit gate. When the application requests a runnable project, that gate now requires material evidence of a launch surface, automated tests, and root run instructions before durable export.

The engineering objective is controlled autonomy, not maximum autonomy. Models propose analysis, plans, and task outputs; they do not approve those outputs, assign themselves repository authority, or decide that incomplete evidence is sufficient. Deterministic controls establish identities, validate lineage and dependencies, constrain filesystem effects, and settle execution outcomes. Human decisions authorize the transitions where judgment changes downstream authority.

This makes the system materially different from a code generator or linear agent chain. Work is governed by an approved specification, decomposed into a per-run dependency DAG, synchronized across sequential and parallel paths, and accepted only through traceable validation and mutation evidence.

## 2. Architecture Rationale

The central design choice is to separate useful nondeterministic reasoning from authoritative decisions. An LLM can identify ambiguities and propose an engineering plan, but deterministic application code decides whether the analysis is `READY` or `BLOCKED`, whether a graph is a valid DAG with complete requirement coverage, whether its source specification is still authoritative, and whether execution evidence satisfies the exit gate. This permits model reasoning without making model confidence a control mechanism.

The implementation also separates two graph responsibilities. A relatively stable LangGraph control graph governs lifecycle transitions, human interrupts, retries, safe stops, and exit evaluation. A per-run engineering TaskGraph represents typed lifecycle work and its dependencies. The control graph interprets TaskGraph data; it does not rewrite itself into a new LangGraph topology for each requirement. This preserves a reviewable control plane while allowing each approved specification to produce different design, implementation, test, documentation, validation, and release work.

Authority flows downstream from the human-approved requirement analysis into a canonical approved requirement specification. A TaskGraph records the exact source specification identity and version from which it was produced. Human TaskGraph approval is necessary but not sufficient if that source later becomes stale: source-authority checks still guard execution initialization and every execution-loop advance. Deeper control, lineage, and transaction mechanics are documented in [ARCHITECTURE.md](ARCHITECTURE.md).

## 3. Reviewer Scenarios

The checked-in scenarios expose both the governed records and independently runnable product outcomes.

| Scenario | Engineering behavior demonstrated | Reviewer evidence and runnable product | Verified product tests |
| --- | --- | --- | ---: |
| Greenfield | Approved planning, bounded parallel execution, one governed retry, and transactional creation of a six-file URL shortener | [`artifacts/demo-run/`](artifacts/demo-run/) and [`generated-project/`](artifacts/demo-run/generated-project/) | 11 passed |
| Brownfield | Bounded reasoning over an existing six-file codebase, parallel task reasoning, and four serialized, preimage-checked `MODIFY` transactions | [`artifacts/brownfield-demo-run/`](artifacts/brownfield-demo-run/) and [`enhanced-project/`](artifacts/brownfield-demo-run/enhanced-project/) | 18 passed |
| Ambiguous requirement | Planning blocked until a human clarification establishes new requirement authority, followed by governed expiration work | [`artifacts/ambiguity-demo-run/`](artifacts/ambiguity-demo-run/) and [`expiration-project/`](artifacts/ambiguity-demo-run/expiration-project/) | 20 passed |

The ambiguity scenario begins with a request to make shortened URLs expire “after a period of time.” Revision 0 records unresolved TTL, timing, expiration-response, scope, and persistence decisions. Deterministic policy marks it `BLOCKED`; the reviewer can only request changes or reject, the planner receives zero calls, and no approved specification or TaskGraph exists. A human `REQUEST_CHANGES` decision supplies fixed 24-hour, in-memory semantics and 404 behavior. Immutable Revision 1 becomes `READY`, is approved, produces the authoritative specification, and then produces the first authorized TaskGraph.

That sequence demonstrates the governed requirements-to-planning boundary, not replacement of a Revision 0 plan—there was no such plan. Separate tests demonstrate stale-source protection: if an already-created TaskGraph no longer matches the current specification identity/version, execution stops before workspace initialization or before the next dispatch and mutation. A replacement must be regenerated, deterministically validated, and governed again; the running graph is not edited in place.

The greenfield and brownfield bundles are retained V0.5 snapshots. Their historical analysis fields predate the V0.6 ambiguity gate, so the ambiguity bundle—not those frozen fields—is the reviewer evidence for current `BLOCKED` behavior.

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
| Application-owned runnable-project delivery policy | Product delivery completeness should not be an LLM-invented business requirement or a filename heuristic. | Structured deliverable roles participate in TaskGraph identity/review, executor and materialization validation, final authoritative-snapshot readiness evidence, and the exit gate. |
| Governed project export; Git, CI/CD, and deployment outside autonomous authority | Workspace mutation and durable promotion are separate trust decisions. | Agent-controlled effects end in the isolated workspace; the application exports only an exit-verified snapshot into a new project directory, while branch, commit, push, merge, release, and deployment remain external activities. |

Together, these decisions favor bounded blast radius, reproducibility, and reviewable authority over unconstrained exploration or self-modifying execution.

## 5. Validation Strategy

Validation is intentionally split between the orchestrator and the products it creates or enhances.

The final orchestrator checkpoint completed with **484 tests passed**. The suite uses scripted model adapters and deterministic executors, so control behavior can be exercised without API credentials or network variability. It covers structured-output parsing, deterministic requirement and project readiness, both human approval loops, canonical identities, specification and TaskGraph lineage, DAG and deliverable-role coverage, sequential and fan-out/fan-in scheduling, true bounded overlap, retry exhaustion, stale-source rejection, artifact filtering, conflict reconciliation, isolated workspace containment, preimage and postimage checks, rollback, rollback failure, hard safe stops, exit-gate completeness, live run-evidence ownership and manifest integrity, verified non-overwriting project export, and reliability derivation. Fault injection verifies failure paths rather than inferring them from positive demonstrations.

Generated-product validation is a separate layer. Runnable-project readiness proves required project surfaces, root instructions, materialization evidence, and final snapshot identities are present; it does not prove the generated application or tests executed. The exported greenfield, brownfield, and expiration products are dependency-free, independently runnable URL-shortener projects whose suites completed with 11, 18, and 20 passing tests respectively. Those product checks were performed separately and are not silently performed by the orchestrator; they confer no Git, CI/CD, release, or deployment authority.

Each reviewer bundle retains the approved specification, requirement decisions, TaskGraph, execution attempts/results, engineering artifacts, workspace mutation records, and summary needed to inspect the demonstrated path. Within a run, frozen records and additive histories provide immutable-by-contract evidence. The default LangGraph checkpoint is process-local, and exported JSON and Markdown are ordinary files, not a tamper-evident durable event store. The evidence is therefore strong for deterministic reconstruction and review within the prototype's trust model, but it is not a claim of production proof or durable audit infrastructure.

Artifact ownership is explicit: `artifacts/` remains curated checked-in evaluator
evidence, live CLI evidence is isolated by the governed identity under
`runs/<run-id>/sdlc-artifacts/`, and successful durable applications are promoted
separately under `projects/`. A deterministic terminal manifest indexes the live
bundle's actual relative files, sizes, and SHA-256 values without claiming signing
or tamper-proofing. Task Agents have no authority over the run-evidence directory,
and this slice does not yet package that evidence inside the durable project.

## 6. Reliability and Failure Handling

Reliability behavior is fail-closed and bounded. Task execution permits at most three attempts. Retryability and recovery are decided by application policy; SDK-level hidden retries are disabled. The prototype does not silently switch providers or models. Dependency failures, exhausted attempts, stale authority, invalid evidence, or unverified workspace state route to explicit failure or safe-stop outcomes.

For eligible mutation failures, the runtime restores owned targets in reverse order, verifies rollback against the prior snapshot, and records either `ROLLED_BACK` or `ROLLBACK_FAILED`. Inability to prove restoration is not accepted as success: workspace integrity becomes `UNPROVABLE`, unsettled work is aborted, and the control graph takes a hard safe stop.

[`artifacts/reliability_metrics.json`](artifacts/reliability_metrics.json) is a deterministic, read-only projection over the three scenarios' retained execution and workspace evidence. It reports supported structural measures such as task and attempt outcomes, retry frequency, mutation outcomes, rollback counts, and safe-stop status. MTTR and end-to-end latency are both `NOT_MEASURED` because the evidence model does not retain authoritative incident-to-recovery or elapsed-time boundaries. File timestamps are not substituted for missing telemetry.

## 7. Security and Change-Control Guardrails

The change-control path narrows authority at each boundary: bounded repository context → isolated workspace → restricted mutation vocabulary → deterministic validation → targeted preimage checks → serialized application → postimage verification → verified rollback or hard safe stop → exit-verified durable project export → external Git and release control.

Executors receive explicit UTF-8 file content, hashes, and nonexistence facts for authorized paths rather than a repository root handle or autonomous discovery capability. Mutations reject absolute paths, traversal, protected `.git` and environment targets, symlinks, special files, and duplicate targets. `CREATE` requires nonexistence; `MODIFY` requires the expected preimage; `NO_CHANGE` verifies the desired state without writing. An authoritative workspace snapshot advances only after a verified `APPLIED` result.

This reduces filesystem blast radius, but the isolated temporary directory is not presented as a container, virtual machine, hostile-process sandbox, or cross-process transaction manager. Likewise, permission to change workspace files is not permission to modify the authoritative Git repository or promote a release.

## 8. Assumptions

The prototype assumes that identified human reviewers are the trusted source of requirement and TaskGraph approvals. It assumes the host filesystem can support the regular-file identity, atomic replacement, and snapshot checks used by the mutation layer. It also assumes repository context is deliberately selected by the application; autonomous repository discovery is outside scope.

Model providers may fail or return invalid structured data. The design does not assume model correctness: bounded parsing/retry behavior and safe stops contain those failures. Process-local checkpoints and retained temporary workspaces are sufficient for this demonstrator's review workflow, but not for distributed recovery.

## 9. Risks and Trade-offs

- **Authority checks versus workflow complexity.** Revision, identity, lineage, and approval checks add state and test burden. They also make it possible to explain why a task was authorized and to reject stale work before effects occur.

- **Immutable revisions versus in-place flexibility.** Additive revisions preserve history and make authority transitions auditable. The cost is explicit regeneration and renewed governance instead of quietly editing prior analyses or plans.

- **Hard safe stop versus continued progress.** Halting when integrity cannot be proven can reduce availability and require human intervention. Continuing would risk treating an unknown workspace as authoritative, which is a worse failure mode for this design.

- **Filesystem isolation versus full process isolation.** A dedicated workspace and strict path/mutation policy materially reduce blast radius with modest prototype complexity. They do not supply container-level resource, network, or process containment.

- **Static lifecycle governance versus self-modifying control flow.** A stable LangGraph topology is reproducible and amenable to exhaustive transition testing. It deliberately gives up live dependency surgery and active-task migration.

- **Parallel reasoning versus serialized effects.** Bounded task overlap demonstrates concurrency and shortens independent reasoning paths, while canonical reconciliation and serialized mutations protect deterministic state. Mutation throughput is traded for simpler conflict and rollback semantics.

- **Evidence-only metrics versus broader observability.** Reporting only derivable measures avoids fabricated precision. It leaves latency and recovery-time questions unanswered until authoritative timing boundaries exist.

## 10. Deliberate Limitations

The prototype does not mutate an executing TaskGraph, rewrite dependencies, cancel or migrate running tasks, or automatically install a replacement graph. Governed replanning means changed upstream authority invalidates stale downstream authority; a newly generated graph must pass validation and human governance before execution.

It has no autonomous Git branch, commit, push, pull-request, merge, or authoritative-repository promotion capability. It does not promote CI/CD stages or deploy software. Runnable readiness is structural and evidence-based: the system does not execute generated code, start generated servers, install generated dependencies, or run generated product tests as part of its exit gate. It also does not provide distributed workers or persistent crash recovery, support unrestricted filesystem operations or `DELETE`, or implement a durable tamper-evident audit service.

MTTR and end-to-end latency remain unmeasured. These are deliberate evidence boundaries, not zero-valued results. The implementation is a governed orchestration prototype, not a production distributed scheduler, repository-management service, CI/CD platform, or deployment system.

## 11. Reviewer Artifact Map

| Purpose | Location |
| --- | --- |
| Fast repository entry point | [README.md](README.md) |
| Detailed architecture and control mechanics | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Engineering rationale and readiness | [ENGINEERING_SUMMARY.md](ENGINEERING_SUMMARY.md) |
| Greenfield reviewer bundle | [`artifacts/demo-run/`](artifacts/demo-run/) |
| Greenfield runnable product | [`artifacts/demo-run/generated-project/`](artifacts/demo-run/generated-project/) |
| Brownfield reviewer bundle | [`artifacts/brownfield-demo-run/`](artifacts/brownfield-demo-run/) |
| Brownfield runnable product | [`artifacts/brownfield-demo-run/enhanced-project/`](artifacts/brownfield-demo-run/enhanced-project/) |
| Ambiguity reviewer bundle | [`artifacts/ambiguity-demo-run/`](artifacts/ambiguity-demo-run/) |
| Ambiguity authority transition | [`artifacts/ambiguity-demo-run/ambiguity_resolution.json`](artifacts/ambiguity-demo-run/ambiguity_resolution.json) |
| Ambiguity runnable product | [`artifacts/ambiguity-demo-run/expiration-project/`](artifacts/ambiguity-demo-run/expiration-project/) |
| Cross-scenario reliability projection | [`artifacts/reliability_metrics.json`](artifacts/reliability_metrics.json) |

The most efficient review path is each bundle's `summary.md`, followed by its requirement analysis, approved specification, TaskGraph, task-execution evidence, and workspace-execution evidence where a claim needs deeper verification.

## 12. Final Readiness

The repository demonstrates a controlled-autonomy lifecycle from deterministic intake and governed requirement authority through validated planning, bounded dependency-aware execution, transactional isolated-workspace mutation, runnable-project readiness, exit evaluation, verified durable project export, and retained evidence. The final checkpoints are 484 passing orchestrator tests and independently runnable generated products with 11, 18, and 20 passing tests.

This describes the implemented prototype, not autonomous Git, release, or production deployment. Its authority boundaries, failure behavior, trade-offs, and unmeasured reliability dimensions are explicit, and the checked-in evidence provides concrete paths for independent verification.
