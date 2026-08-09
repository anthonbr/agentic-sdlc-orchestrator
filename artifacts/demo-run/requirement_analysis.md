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
- Confidence: 0.97

### Normalized problem

Build a service that accepts a long URL, creates a unique short URL associated with it, redirects requests for the short URL to the original URL, and reports an error when the short URL does not exist.

### Functional requirements

- The system shall accept a long URL as input for shortening.
- The system shall generate a short URL identifier that is unique among stored mappings.
- The system shall persist an association between each generated short identifier and its original long URL.
- When a request uses a known short URL, the system shall redirect the requester to the associated original URL.
- When a request uses an unknown short URL, the system shall return an error response.

### Nonfunctional requirements

- None identified.

### Constraints

- None identified.

### Ambiguities

- The acceptable URL syntax and validation rules are unspecified.
- The required short URL format, length, and character set are unspecified.
- The uniqueness scope and persistence duration are unspecified.
- The required error type, HTTP status code, and response body for unknown short URLs are unspecified.
- The redirect status code and redirect behavior are unspecified.
- The storage, availability, scalability, security, and performance requirements are unspecified.

### Assumptions

- The service uses HTTP and represents short URLs as URL-addressable identifiers.
- Generated mappings remain available for subsequent redirect requests for at least the duration of the service's configured persistence.
- A short identifier must not map to more than one original URL.

### Acceptance criteria

- Given a valid long URL, when a shortening request is submitted, the system returns a short URL.
- For every successfully created short URL, the system stores a mapping to the submitted original URL.
- Given two successfully created mappings, their short identifiers are distinct.
- Given a previously created short URL, when it is requested, the system redirects the requester to the exact original URL associated with it.
- Given a short URL with no stored mapping, when it is requested, the system returns an error rather than redirecting.
- The system does not return an error for a known short URL solely because the original URL is long.

### Risks

- Insufficient uniqueness guarantees could cause one short URL to overwrite or incorrectly resolve to another mapping.
- Unspecified URL validation could allow malformed or unsafe destinations.
- Unspecified persistence behavior could cause previously generated short URLs to stop working.
- Ambiguous redirect and error semantics may lead to incompatible client behavior.
- Without performance and availability requirements, the service may not meet operational expectations at scale.

## Analysis lineage

1. Revision 0
   - Attempt: 1
   - Prompt: requirement-analysis-v1.1
   - Model: gpt-5.6-luna
   - Normalized problem: Build a service that accepts a long URL, creates a unique short URL associated with it, redirects requests for the short URL to the original URL, and reports an error when the short URL does not exist.
   - Ambiguities: The acceptable URL syntax and validation rules are unspecified.; The required short URL format, length, and character set are unspecified.; The uniqueness scope and persistence duration are unspecified.; The required error type, HTTP status code, and response body for unknown short URLs are unspecified.; The redirect status code and redirect behavior are unspecified.; The storage, availability, scalability, security, and performance requirements are unspecified.
   - Assumptions: The service uses HTTP and represents short URLs as URL-addressable identifiers.; Generated mappings remain available for subsequent redirect requests for at least the duration of the service's configured persistence.; A short identifier must not map to more than one original URL.

## Human requirement-review history

1. APPROVE
   - Revision: 0
