# Test Plan

Verify each requirement at the service boundary, then cover validation and repeat-request behavior.

## Cases

- **Valid URL shortening** — A valid long URL produces a usable short URL.
- **Unique short-code creation** — Distinct stored URLs do not receive colliding codes.
- **Redirect correctness** — A known short code redirects to its original URL.
- **Unknown short code** — An unknown code returns the defined error response.
- **Malformed input** — An invalid long URL is rejected clearly.
- **Repeated request** — Define and verify whether repeated submissions reuse an existing short code or generate a new one.
