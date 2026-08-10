# Requirement Analysis

## Original requirement

> Build a URL Shortener that:
> 1. Accept a long URL.
> 2. Generate a unique short URL.
> 3. Redirect the short URL to the original URL.
> 4. Return an error for unknown short URLs.

## Current validated analysis

- Requirement type: greenfield
- Needs clarification: true
- Confidence: 0.85

### Normalized problem

v1: Provide short URLs that resolve to submitted long URLs.

### Functional requirements

- Accept a long URL.
- Generate a unique short URL.
- Redirect the short URL to the original URL.
- Return an error for unknown short URLs.

### Nonfunctional requirements

- Short-code lookup should be reliable.

### Constraints

- The persistence technology is not yet selected.

### Ambiguities

- URL expiration behavior is unspecified.

### Assumptions

- The workflow produces semantic artifacts without writing the service.

### Acceptance criteria

- A submitted valid URL receives a unique short URL.
- An unknown short code returns a defined error.

### Risks

- Short-code collisions could produce incorrect redirects.

## Analysis lineage

1. Revision 0
   - Attempt: 1
   - Prompt: requirement-analysis-v1.1
   - Model: fake-requirement-analyst
   - Normalized problem: v1: Provide short URLs that resolve to submitted long URLs.
   - Ambiguities: URL expiration behavior is unspecified.
   - Assumptions: The workflow produces semantic artifacts without writing the service.

## Human requirement-review history

1. APPROVE
   - Revision: 0
