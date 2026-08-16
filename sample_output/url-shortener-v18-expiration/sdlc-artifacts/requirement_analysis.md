# Requirement Analysis

## Original submitted requirement

> Add optional expiration times to shortened URLs. URLs without an expiration should continue to behave exactly as they do today.

## Current validated analysis

- Requirement type: brownfield
- Needs clarification: false
- Planning readiness: READY
- Readiness reason: None
- Confidence: 0.99

### Normalized problem

Extend the existing process-local URL shortener to accept an optional `expires_at` value when creating a mapping. The value is an immutable, absolute, whole-second UTC timestamp in canonical RFC 3339 `YYYY-MM-DDTHH:MM:SSZ` form. Expiring mappings redirect normally before expiration and return a defined HTTP 410 error at or after expiration without incrementing analytics. Expired mappings remain available through analytics and in memory but are excluded from creation deduplication. Mappings created without `expires_at` must retain exactly the baseline request, response, deduplication, redirect, analytics, counting, and process-lifetime behavior.

### Functional requirements

- Allow POST /api/urls to accept an optional `expires_at` field in addition to the required `url` field.
- Accept `expires_at` only when it is a string containing a valid future whole-second UTC timestamp in exact `YYYY-MM-DDTHH:MM:SSZ` form and within Python's supported datetime range.
- Reject null, booleans, numbers, malformed timestamps, non-UTC offsets, fractional seconds, and timestamps equal to or earlier than the current UTC time.
- For invalid expiration input, return HTTP 400 with exactly `{"error":{"code":"invalid_expiration","message":"expires_at must be a future UTC timestamp in YYYY-MM-DDTHH:MM:SSZ format"}}` and do not mutate state.
- Store an accepted expiration as immutable mapping state in canonical `YYYY-MM-DDTHH:MM:SSZ` form.
- Treat a mapping as expired when the current UTC time is equal to or later than its `expires_at` value.
- Before expiration, redirect an expiring mapping with the existing HTTP 302 behavior and increment its `redirect_count` exactly once.
- At or after expiration, return HTTP 410 with exactly `{"error":{"code":"expired_url","message":"Short URL has expired"}}`, omit the `Location` header, and do not increment `redirect_count`.
- Keep analytics available for expired mappings through GET /api/urls/{code}, returning HTTP 200, the stored mapping data, `expires_at`, and the unchanged `redirect_count`.
- Retain expired mappings in memory until process restart; do not perform background or lazy deletion.
- Deduplicate creation by the combination of exact destination URL and expiration value, treating omitted expiration as distinct from every supplied expiration.
- Return an existing expiring mapping with HTTP 200 only when its exact URL and `expires_at` match the request and the mapping is not expired.
- Create a new mapping with HTTP 201 when the URL matches but expiration differs, when one request omits expiration and the other supplies it, or when the only matching mappings are expired, provided the submitted expiration itself is currently valid.
- Do not modify an existing mapping when creating another mapping for the same URL with a different expiration configuration.
- Include the existing four fields plus `expires_at` in creation and analytics representations for expiring mappings.
- Retain exactly the existing four-field JSON representation for non-expiring mappings, with no `expires_at` field.
- Continue accepting creation requests containing only `url` and preserve all existing behavior for the resulting non-expiring mappings.
- Do not provide any operation that adds, removes, extends, or shortens a mapping's expiration after creation.

### Nonfunctional requirements

- Expiration validation and redirect decisions must use the service's current UTC time.
- Expiration checking and redirect-count mutation must remain atomic under the MappingStore's existing lock-based concurrency model.
- Boundary behavior must be deterministic and testable immediately before, exactly at, and immediately after `expires_at`.
- The service must remain compatible with Python 3.12 and use only the Python standard library.
- The feature must preserve the existing process-local, in-memory lifecycle without persistence, external time services, background processing, or multi-process coordination.
- Non-expiring response representations must remain exactly backward compatible with the existing four-field schema.

### Constraints

- The authoritative baseline inventory is complete and contains only README.md, server.py, state_engine.py, and tests/test_url_shortener.py.
- `expires_at` is absolute only; relative durations are not accepted.
- The only accepted expiration syntax is a whole-second UTC timestamp ending in `Z` and formatted as `YYYY-MM-DDTHH:MM:SSZ`.
- A supplied expiration must be strictly later than current UTC time when the creation request is validated.
- Expiration is immutable after mapping creation.
- Expired entries remain resident until process restart and remain accessible through analytics.
- Deduplication identity consists of the exact submitted destination URL and the optional expiration value; expired mappings are not eligible deduplication matches.
- The existing service has no database, persistence, external service, background processing, or third-party dependency.
- Existing short codes remain eight-character ASCII alphanumeric strings.
- Submitted destination URLs retain the existing local syntactic HTTP/HTTPS validation and exact-string preservation rules.
- Mappings without expiration must preserve their baseline API shape and behavior exactly.

### Ambiguities

- None identified.

### Assumptions

- Current UTC time is obtained from the local process environment without contacting an external time service.
- Expiration is evaluated against current UTC time on each relevant operation rather than being represented by a separately persisted expired flag.
- Expiration validation occurs before deduplication, so an `expires_at` value equal to or earlier than the current time receives the required HTTP 400 response even if an expired mapping stores the same value.
- No persistence or cross-process expiration coordination is introduced; restarting the process discards all mappings, expiration values, and redirect counts.

### Acceptance criteria

- POST /api/urls with a valid `url` and no `expires_at` creates a non-expiring mapping with HTTP 201 and exactly the baseline fields `code`, `short_url`, `url`, and `redirect_count`.
- Repeating a creation request without `expires_at` for the exact same URL returns the existing non-expiring mapping with HTTP 200, preserves the exact four-field representation, and does not increase store size.
- A valid future `expires_at` such as `2030-01-02T03:04:05Z` creates a mapping with HTTP 201 whose representation contains the existing four fields plus `expires_at` with the canonical submitted value.
- Explicit null, booleans, numbers, malformed strings, timestamps with offsets other than `Z`, fractional seconds, out-of-range dates, and timestamps equal to or earlier than current UTC time each return HTTP 400 with exactly `{"error":{"code":"invalid_expiration","message":"expires_at must be a future UTC timestamp in YYYY-MM-DDTHH:MM:SSZ format"}}`.
- A request rejected for invalid `expires_at` creates no mapping and does not modify any existing mapping or redirect count.
- Before an expiring mapping's boundary, GET /{code} returns HTTP 302 with the exact destination in `Location` and increments that mapping's `redirect_count` exactly once.
- When current UTC time equals `expires_at`, GET /{code} returns HTTP 410 with exactly `{"error":{"code":"expired_url","message":"Short URL has expired"}}`, contains no `Location` header, and does not increment `redirect_count`.
- After `expires_at`, GET /{code} continues to return the defined HTTP 410 response without incrementing `redirect_count`.
- GET /api/urls/{code} for an expired mapping returns HTTP 200 with `code`, `short_url`, `url`, unchanged `redirect_count`, and canonical `expires_at`.
- Expired mappings remain counted in the in-memory store and available to analytics until process restart.
- Repeating the exact URL and exact `expires_at` before expiration returns the existing mapping with HTTP 200 and does not increase store size.
- Submitting the same exact URL with a different valid `expires_at` creates an independent mapping with HTTP 201 and does not modify the original mapping.
- Submitting an expiration for a URL that already has a non-expiring mapping, or omitting expiration for a URL that has an expiring mapping, creates an independent mapping with HTTP 201.
- An expired mapping is not returned as a duplicate; a request with otherwise matching, currently valid URL and expiration identity can create a new mapping with HTTP 201.
- No supported request can add, remove, extend, or shorten the expiration of an existing mapping.
- A non-expiring mapping continues to redirect with HTTP 302 throughout the process lifetime and increments `redirect_count` exactly once per successful redirect.
- Unknown codes continue to return the existing JSON HTTP 404 response and do not mutate known mappings.
- Deterministic tests cover expiration validation and redirect behavior immediately before, exactly at, and immediately after the expiration boundary.
- Existing tests for destination validation, exact URL preservation, collision retry, mapping isolation, non-expiring deduplication, process reset, malformed JSON, content type, unsupported methods, unknown codes, and standard-library-only imports continue to pass.
- README.md documents the `expires_at` request contract, validation error, response shapes, boundary semantics, HTTP 410 behavior, analytics availability, deduplication identity, immutability, retention, and process-local lifecycle.

### Risks

- Direct use of wall-clock time can cause surprising behavior if the system clock moves backward or forward and can make tests flaky unless time is controllable.
- Validation and duplicate lookup performed using different time samples could produce inconsistent results near the expiration boundary.
- The current URL-only deduplication index cannot support the required URL-plus-expiration identity or exclusion of expired matches without modification.
- Retaining all expired mappings until restart can cause unbounded memory growth in a long-running process.
- Adding `expires_at` unconditionally to serialized mappings would break the explicitly required four-field compatibility for non-expiring mappings.
- A non-atomic expiration check and counter update could allow a redirect count increment at or after expiration.
- Permissive datetime parsing could accidentally accept offsets, fractional seconds, alternate forms, or values outside the required canonical syntax.
- If invalid-expiration validation does not precede duplicate handling, a request containing a now-expired timestamp could incorrectly return an old mapping instead of the required HTTP 400 error.
- Using a single exception path for unknown and expired codes could return the wrong HTTP status or error payload and could make expired analytics unavailable.

## Analysis lineage

1. Revision 0
   - Attempt: 1
   - Prompt: requirement-analysis-v1.4
   - Model: gpt-5.6-sol
   - Planning readiness: BLOCKED
   - Readiness reason: UNRESOLVED_REQUIREMENT_AMBIGUITY
   - Normalized problem: Extend the existing in-memory URL shortener so a mapping may optionally be created with an expiration time. An unexpired mapping must continue to redirect normally; after expiration it must no longer provide a successful redirect. Mappings created without expiration must retain the baseline behavior for creation, deduplication, redirecting, analytics, redirect counting, and process-lifetime availability.
   - Ambiguities: What request field specifies expiration, and what representation is accepted—for example, an absolute timestamp, relative duration, or both?; Which timezone and precision rules apply, and is a mapping expired when current time is equal to the expiration time or only when it is later?; Which expiration values are invalid, including past timestamps, zero or negative durations, null, booleans, malformed strings, or excessively distant values, and what HTTP error contract applies?; What HTTP status, error code, and message should redirect requests receive after expiration?; Should analytics for an expired mapping remain available, return not found, or return a distinct expired response?; Should expired entries remain in memory, be removed lazily, or be cleaned up by another mechanism?; How does exact-URL deduplication interact with expiration: does the same URL with a different or omitted expiration return the existing mapping, create a new mapping, or update its expiration?; Should responses for expiring mappings include expiration metadata, and must responses for non-expiring mappings retain exactly the current four-field JSON shape?; If an expired mapping remains stored, may a later creation request for the same destination URL create a new code?; Can an existing mapping's expiration be extended, shortened, or removed, or is expiration immutable after creation?
   - Assumptions: Expiration is evaluated using the service's local notion of current time without contacting an external time service.; No persistence or cross-process expiration coordination is introduced because the baseline is explicitly process-local and the raw requirement does not request a lifecycle change.; Expiration applies to mappings rather than to individual redirect requests.
2. Revision 1
   - Attempt: 1
   - Prompt: requirement-analysis-v1.4
   - Model: gpt-5.6-sol
   - Planning readiness: READY
   - Readiness reason: None
   - Normalized problem: Extend the existing process-local URL shortener to accept an optional `expires_at` value when creating a mapping. The value is an immutable, absolute, whole-second UTC timestamp in canonical RFC 3339 `YYYY-MM-DDTHH:MM:SSZ` form. Expiring mappings redirect normally before expiration and return a defined HTTP 410 error at or after expiration without incrementing analytics. Expired mappings remain available through analytics and in memory but are excluded from creation deduplication. Mappings created without `expires_at` must retain exactly the baseline request, response, deduplication, redirect, analytics, counting, and process-lifetime behavior.
   - Ambiguities: None identified.
   - Assumptions: Current UTC time is obtained from the local process environment without contacting an external time service.; Expiration is evaluated against current UTC time on each relevant operation rather than being represented by a separately persisted expired flag.; Expiration validation occurs before deduplication, so an `expires_at` value equal to or earlier than the current time receives the required HTTP 400 response even if an expired mapping stores the same value.; No persistence or cross-process expiration coordination is introduced; restarting the process discards all mappings, expiration values, and redirect counts.
   - Reviewer feedback: 1. Use an optional `expires_at` field containing an absolute RFC 3339 timestamp. Relative durations are not accepted.

2. `expires_at` must be a whole-second UTC timestamp ending in `Z`, for example `2030-01-02T03:04:05Z`. A mapping is expired when the current UTC time is equal to or later than `expires_at`.

3. If supplied, `expires_at` must be a string representing a valid future UTC timestamp within Python’s supported datetime range. Null, booleans, numbers, malformed timestamps, non-UTC timestamps, fractional seconds, and timestamps equal to or earlier than the current time are invalid. Invalid input returns HTTP 400 with `{"error":{"code":"invalid_expiration","message":"expires_at must be a future UTC timestamp in YYYY-MM-DDTHH:MM:SSZ format"}}` and does not mutate state.

4. Redirecting through an expired code returns HTTP 410 with `{"error":{"code":"expired_url","message":"Short URL has expired"}}`, without a `Location` header or redirect-count increment.

5. Analytics remains available for expired mappings and returns HTTP 200 with the stored mapping data, including `expires_at` and the unchanged `redirect_count`.

6. Expired entries remain in memory until process restart. No background or lazy deletion is required.

7. Deduplication uses the exact destination URL and expiration value. Repeating the same URL with the same `expires_at` returns the existing unexpired mapping with HTTP 200. A different expiration, or switching between an omitted and supplied expiration, creates a new mapping with HTTP 201 and does not modify existing mappings.

8. Creation and analytics responses for expiring mappings contain the existing four fields plus `expires_at` in canonical `YYYY-MM-DDTHH:MM:SSZ` form. Responses for non-expiring mappings retain exactly the existing four-field JSON shape.

9. Expired mappings are excluded from creation deduplication. A later request with the same destination URL may create a new code, even if its URL and expiration value match an expired mapping.

10. Expiration is immutable after creation. Existing mappings cannot have expiration added, extended, shortened, or removed.

## Human requirement-review history

1. REQUEST_CHANGES
   - Revision: 0
   - Feedback: 1. Use an optional `expires_at` field containing an absolute RFC 3339 timestamp. Relative durations are not accepted.

2. `expires_at` must be a whole-second UTC timestamp ending in `Z`, for example `2030-01-02T03:04:05Z`. A mapping is expired when the current UTC time is equal to or later than `expires_at`.

3. If supplied, `expires_at` must be a string representing a valid future UTC timestamp within Python’s supported datetime range. Null, booleans, numbers, malformed timestamps, non-UTC timestamps, fractional seconds, and timestamps equal to or earlier than the current time are invalid. Invalid input returns HTTP 400 with `{"error":{"code":"invalid_expiration","message":"expires_at must be a future UTC timestamp in YYYY-MM-DDTHH:MM:SSZ format"}}` and does not mutate state.

4. Redirecting through an expired code returns HTTP 410 with `{"error":{"code":"expired_url","message":"Short URL has expired"}}`, without a `Location` header or redirect-count increment.

5. Analytics remains available for expired mappings and returns HTTP 200 with the stored mapping data, including `expires_at` and the unchanged `redirect_count`.

6. Expired entries remain in memory until process restart. No background or lazy deletion is required.

7. Deduplication uses the exact destination URL and expiration value. Repeating the same URL with the same `expires_at` returns the existing unexpired mapping with HTTP 200. A different expiration, or switching between an omitted and supplied expiration, creates a new mapping with HTTP 201 and does not modify existing mappings.

8. Creation and analytics responses for expiring mappings contain the existing four fields plus `expires_at` in canonical `YYYY-MM-DDTHH:MM:SSZ` form. Responses for non-expiring mappings retain exactly the existing four-field JSON shape.

9. Expired mappings are excluded from creation deduplication. A later request with the same destination URL may create a new code, even if its URL and expiration value match an expired mapping.

10. Expiration is immutable after creation. Existing mappings cannot have expiration added, extended, shortened, or removed.
2. APPROVE
   - Revision: 1
