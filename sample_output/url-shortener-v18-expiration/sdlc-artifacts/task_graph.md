# Engineering Task Dependency Graph

- Graph: GRAPH-5753BE08E77D-V001
- Version: 1
- Requirement specification: SPEC-BE94ADD07B4C-V001
- Project delivery policy: RUNNABLE_PROJECT
- Content hash: `5753be08e77db19ba946d45707fe0a96c4b0841b96e35aa26e46d144dd4cf274`
- Execution status: SUCCEEDED

## Derived execution layers

### Layer 1

#### TASK-001 — Implement expiration-aware mapping state

- Type: IMPLEMENTATION
- Materialization policy: REQUIRED
- Delivery roles: None
- Required validations: PYTHON_COMPILE
- Depends on: ENTRY
- Runtime status: SUCCEEDED
- Attempts: 1
- Execution waves: 1 (attempt 1)
- Requirements: FR-002, FR-003, FR-005, FR-006, FR-007, FR-009, FR-010, FR-011, FR-012, FR-013, FR-014, FR-018, NFR-001, NFR-002, NFR-003, NFR-004, NFR-005, CON-001, CON-002, CON-003, CON-004, CON-005, CON-006, CON-007, CON-008, CON-009, CON-010, CON-011
- Acceptance criteria: AC-005, AC-006, AC-007, AC-008, AC-009, AC-010, AC-011, AC-012, AC-013, AC-014, AC-015, AC-016, AC-017
- Risks: RISK-001, RISK-002, RISK-003, RISK-004, RISK-006, RISK-007, RISK-008, RISK-009
- Ambiguities: None
- Description: Incrementally update the existing in-memory state engine with strict canonical expiration validation, immutable optional expiration state, a test-controllable UTC time dependency, composite URL-and-expiration deduplication, retained expired entries, and atomic expiration-aware redirect outcomes. Preserve URL validation, code generation, collision handling, non-expiring behavior, and the existing process-local lock-based lifecycle.
- Expected outputs: Updated state_engine.py with strict whole-second UTC expiration parsing and future-time validation, Optional immutable expiration data in mapping entries and public snapshots, Composite deduplication that distinguishes omitted expiration and excludes expired matches, Locked redirect handling that distinguishes unknown, active, and expired mappings without deleting state, Preserved eight-character code generation, URL validation, collision retry, and non-expiring lifecycle behavior

### Layer 2

#### TASK-002 — Integrate expiration into the HTTP API

- Type: IMPLEMENTATION
- Materialization policy: REQUIRED
- Delivery roles: RUNNABLE_ENTRYPOINT
- Required validations: PYTHON_COMPILE
- Depends on: TASK-001
- Runtime status: SUCCEEDED
- Attempts: 1
- Execution waves: 2 (attempt 1)
- Requirements: FR-001, FR-004, FR-007, FR-008, FR-009, FR-015, FR-016, FR-017, NFR-001, NFR-004, NFR-006, CON-001, CON-002, CON-003, CON-004, CON-005, CON-006, CON-008, CON-009, CON-010, CON-011
- Acceptance criteria: AC-001, AC-002, AC-003, AC-004, AC-005, AC-006, AC-007, AC-008, AC-009, AC-011, AC-012, AC-013, AC-014, AC-015, AC-016, AC-017
- Risks: RISK-005, RISK-007, RISK-008, RISK-009
- Ambiguities: None
- Description: Incrementally update the existing HTTP service to distinguish omitted expiration from supplied values, pass expiration through creation, return the exact invalid-expiration and expired-URL contracts, conditionally serialize expiration only for expiring mappings, and preserve all unaffected routing and API behavior. Retain server.py as the genuine runnable service entry point.
- Expected outputs: Updated server.py POST handling for optional expires_at with omission distinguished from explicit null, Exact HTTP 400 invalid_expiration response without state mutation, Exact HTTP 410 expired_url response without a Location header, Conditional mapping serialization that adds expires_at only to expiring mappings, Preserved runnable Python 3.12 standard-library HTTP service entry point and unaffected HTTP contracts

### Layer 3

#### TASK-003 — Add expiration and compatibility regression tests

- Type: TEST
- Materialization policy: REQUIRED
- Delivery roles: AUTOMATED_TESTS
- Required validations: PYTHON_PYTEST
- Depends on: TASK-001, TASK-002
- Runtime status: SUCCEEDED
- Attempts: 1
- Execution waves: 3 (attempt 1)
- Requirements: FR-001, FR-002, FR-003, FR-004, FR-005, FR-006, FR-007, FR-008, FR-009, FR-010, FR-011, FR-012, FR-013, FR-014, FR-015, FR-016, FR-017, FR-018, NFR-001, NFR-002, NFR-003, NFR-004, NFR-005, NFR-006, CON-001, CON-002, CON-003, CON-004, CON-005, CON-006, CON-007, CON-008, CON-009, CON-010, CON-011
- Acceptance criteria: AC-001, AC-002, AC-003, AC-004, AC-005, AC-006, AC-007, AC-008, AC-009, AC-010, AC-011, AC-012, AC-013, AC-014, AC-015, AC-016, AC-017, AC-018, AC-019
- Risks: RISK-001, RISK-002, RISK-003, RISK-005, RISK-006, RISK-007, RISK-008, RISK-009
- Ambiguities: None
- Description: Extend the existing test suite with deterministic store and HTTP tests for strict expiration validation, boundary behavior, atomic redirect counting, analytics retention, composite deduplication, immutability, and conditional representations. Retain and exercise baseline regression coverage, including standard-library-only imports and unaffected non-expiring behavior.
- Expected outputs: Updated tests/test_url_shortener.py with deterministic current-time control, Store tests immediately before, exactly at, and immediately after expiration, API tests for accepted and rejected timestamp forms and exact error payloads, Tests for HTTP 302 versus HTTP 410 headers, bodies, analytics, and redirect counts, Tests for expiration-aware deduplication, independent mappings, retention, and immutability, Passing baseline regressions for URL handling, collisions, isolation, reset, malformed requests, routing, unknown codes, and standard-library-only imports

### Layer 4

#### TASK-004 — Update run and API documentation

- Type: DOCUMENTATION
- Materialization policy: REQUIRED
- Delivery roles: RUN_INSTRUCTIONS
- Required validations: None
- Depends on: TASK-002, TASK-003
- Runtime status: SUCCEEDED
- Attempts: 1
- Execution waves: 4 (attempt 1)
- Requirements: FR-001, FR-002, FR-003, FR-004, FR-005, FR-006, FR-007, FR-008, FR-009, FR-010, FR-011, FR-012, FR-013, FR-014, FR-015, FR-016, FR-017, FR-018, NFR-001, NFR-004, NFR-005, NFR-006, CON-002, CON-003, CON-004, CON-005, CON-006, CON-007, CON-008, CON-011
- Acceptance criteria: AC-020
- Risks: RISK-004, RISK-005
- Ambiguities: None
- Description: Update the root README.md while preserving its exact setup, run, test, and minimal usage guidance. Document the optional expiration request contract, conditional response schemas, exact validation and HTTP 410 errors, boundary and counting semantics, analytics availability, composite deduplication, immutability, retention, and unchanged process-local lifecycle.
- Expected outputs: Updated root README.md with exact Python 3.12 setup, service run, automated test, and minimal API usage instructions, Creation examples for both non-expiring and expiring mappings, Documented canonical expires_at syntax and exact invalid_expiration response, Documented expiration boundary, HTTP 410 response, absent Location header, and unchanged redirect count, Documented expired analytics availability, deduplication identity, immutability, retention, and process-reset lifecycle, Corrected prototype limitations without introducing persistence, cleanup, external services, or multi-process coordination

## Deterministic graph semantics

- ENTRY-ready: TASK-001
- EXIT predecessors: TASK-004
- Synchronization points: TASK-003, TASK-004
- Topological order: TASK-001, TASK-002, TASK-003, TASK-004
- Required specification coverage: complete (FR/NFR/CON/AC)

## Human task-graph review history

1. APPROVE
   - Revision: 0
