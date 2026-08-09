# Test Plan

Verify each approved requirement at the service boundary; V0.4 plans the work but does not execute these tests.

## Cases

- **Valid URL shortening** — A valid long URL produces a usable short URL.
- **Unique short-code creation** — Distinct stored URLs do not receive colliding codes.
- **Redirect correctness** — A known short code redirects to its original URL.
- **Unknown short code** — An unknown code returns the defined error response.
