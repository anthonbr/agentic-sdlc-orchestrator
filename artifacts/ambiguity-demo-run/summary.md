# Governed Ambiguous URL-Expiration Demo

## Scenario

> Enhance the URL shortener so shortened URLs automatically expire after a period of time.

The verified V0.5 brownfield URL shortener is the six-file starting application.

## Before clarification

- Analysis Revision 0
- `needs_clarification=true`
- Planning readiness: `BLOCKED`
- Reason: `UNRESOLVED_REQUIREMENT_AMBIGUITY`
- Planner invoked: false (0 calls)
- Approved requirement specification: absent
- TaskGraph: absent

Blocking ambiguities:

- Expiration duration/configurability: What is the expiration period, and is the TTL fixed or configurable?
- TTL start: When does the expiration interval begin?
- Expired redirect behavior: What should URL resolution return after expiration?
- Expired analytics behavior: What should analytics return for an expired code?
- Existing-code applicability: Does expiration apply to previously created or currently running codes?
- Persistence semantics: Must expiration survive application restart or require persistent storage?

## Human decision

Decision: `REQUEST_CHANGES`

> Short URLs expire 24 hours after creation. The TTL is fixed and not configurable. Expiration is process-local and in-memory and does not need to survive application restart. At or after expiration, both redirect resolution and analytics return HTTP 404. No migration or preservation of pre-existing runtime codes is required. Expiration is checked when a code is accessed; no background expiration job is required.

## After clarification

- Analysis Revision 1
- `needs_clarification=false`
- Planning readiness: `READY`
- Human decision: `APPROVE`
- Authoritative specification: `SPEC-0C11DA4A975F-V001` version 1
- Specification hash: `0c11da4a975f0ba4b8b68cab41c72902dba407f7b9a5359caeda01c0d1c1c7fd`

Clarified outcomes:

- FR: fixed 24-hour expiration from creation.
- FR: expired resolution and analytics each return HTTP 404.
- CON: process-local in-memory state; no restart persistence.
- CON: no configuration, scheduler, database, or migration.
- AC: active immediately before the boundary; 404 at and after it.
- AC: injected time makes validation immediate and repeatable.

## Downstream consequence

- Attempt 1: BLOCKED; planner invocation count 0.
- Attempt 2 trigger: `UPSTREAM_REQUIREMENTS_REVISED`.
- Attempt 2 reason: `CLARIFICATION_RESOLVED`.
- Attempt 2: PLANNED; planner invocation count 1.
- TaskGraph: `GRAPH-63884C17DACC-V001` version 1.
- Source-spec lineage: `SPEC-0C11DA4A975F-V001` version 1 (matches current authority).

- TASK-001 — Analyze expiration impact — FORBIDDEN — depends on ENTRY
- TASK-002 — Implement clarified expiration behavior — REQUIRED — depends on TASK-001
- TASK-003 — Add deterministic expiration tests — REQUIRED — depends on TASK-002
- TASK-004 — Update expiration documentation — REQUIRED — depends on TASK-002

## Governed execution

- Mutation summary: 0 CREATE, 3 MODIFY, 0 DELETE, 0 NO_CHANGE.
- TASK-001 impact analysis: non-mutating (`FORBIDDEN`).
- Modified: `src/url_shortener/service.py`, `tests/test_service.py`, `README.md`.
- Unchanged: `src/url_shortener/app.py`, `pyproject.toml`, `src/url_shortener/__init__.py`.
- Exported application validation: 20 tests passed.
- Final execution: `SUCCEEDED`.
- Exit gate: PASSED.
- Final workspace integrity: `VERIFIED`.
- Brownfield source and exported final snapshot hashes: VERIFIED.

## Scope boundary

This scenario demonstrates governed replanning at the requirements-to-planning boundary. It does not mutate a live TaskGraph, cancel active work, migrate execution state, or dynamically rewrite dependencies. A mid-execution authority change retains Checkpoint 1 safe-stop and governed-replanning semantics.
