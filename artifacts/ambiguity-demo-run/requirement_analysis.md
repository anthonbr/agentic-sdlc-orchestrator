# Requirement Analysis

## Original requirement

> Enhance the URL shortener so shortened URLs automatically expire after a period of time.

## Current validated analysis

- Requirement type: brownfield
- Needs clarification: false
- Planning readiness: READY
- Readiness reason: None
- Confidence: 1.00

### Normalized problem

Add fixed process-local 24-hour expiration to the existing URL shortener, returning HTTP 404 for expired redirect and analytics access.

### Functional requirements

- Short URLs expire 24 hours after creation.
- Resolving an expired short code returns HTTP 404.
- Requesting analytics for an expired short code returns HTTP 404.

### Nonfunctional requirements

- Expiration boundary behavior must be deterministically testable without waiting for wall-clock time.

### Constraints

- The TTL is fixed at 24 hours and is not configurable.
- The TTL begins when the short URL is created.
- Expiration state is process-local and in-memory.
- Expiration does not need to survive application restart.
- Expiration is checked only when a code is accessed.
- No background expiration job or scheduler is required.
- No migration or preservation of pre-existing runtime codes is required.
- No database or persistent storage is required.

### Ambiguities

- None identified.

### Assumptions

- Existing shortening, analytics counting, and active-code behavior remain unchanged.

### Acceptance criteria

- A code resolves successfully before 24 hours have elapsed.
- Analytics remains available before 24 hours have elapsed.
- At exactly 24 hours after creation, resolution returns HTTP 404.
- At exactly 24 hours after creation, analytics returns HTTP 404.
- Resolution and analytics continue to return HTTP 404 after 24 hours.
- Expiration tests complete without waiting 24 real hours.

### Risks

- An incorrect boundary comparison could keep codes active at exactly 24 hours or hide analytics too early.

## Analysis lineage

1. Revision 0
   - Attempt: 1
   - Prompt: requirement-analysis-v1.2
   - Model: deterministic-ambiguity-analyst
   - Planning readiness: BLOCKED
   - Readiness reason: UNRESOLVED_REQUIREMENT_AMBIGUITY
   - Normalized problem: Enhance the URL shortener so shortened URLs automatically expire after a period of time.
   - Ambiguities: Expiration duration/configurability: What is the expiration period, and is the TTL fixed or configurable?; TTL start: When does the expiration interval begin?; Expired redirect behavior: What should URL resolution return after expiration?; Expired analytics behavior: What should analytics return for an expired code?; Existing-code applicability: Does expiration apply to previously created or currently running codes?; Persistence semantics: Must expiration survive application restart or require persistent storage?
   - Assumptions: None identified.
2. Revision 1
   - Attempt: 1
   - Prompt: requirement-analysis-v1.2
   - Model: deterministic-ambiguity-analyst
   - Planning readiness: READY
   - Readiness reason: None
   - Normalized problem: Add fixed process-local 24-hour expiration to the existing URL shortener, returning HTTP 404 for expired redirect and analytics access.
   - Ambiguities: None identified.
   - Assumptions: Existing shortening, analytics counting, and active-code behavior remain unchanged.
   - Reviewer feedback: Short URLs expire 24 hours after creation. The TTL is fixed and not configurable. Expiration is process-local and in-memory and does not need to survive application restart. At or after expiration, both redirect resolution and analytics return HTTP 404. No migration or preservation of pre-existing runtime codes is required. Expiration is checked when a code is accessed; no background expiration job is required.

## Human requirement-review history

1. REQUEST_CHANGES
   - Revision: 0
   - Feedback: Short URLs expire 24 hours after creation. The TTL is fixed and not configurable. Expiration is process-local and in-memory and does not need to survive application restart. At or after expiration, both redirect resolution and analytics return HTTP 404. No migration or preservation of pre-existing runtime codes is required. Expiration is checked when a code is accessed; no background expiration job is required.
2. APPROVE
   - Revision: 1
