# Governed URL Shortener Demo

This dependency-free Python URL shortener was originally produced by the
governed greenfield scenario and then enhanced by the governed brownfield analytics
scenario. The brownfield workflow seeded the verified existing six-file application
into a disposable isolated workspace, reasoned over bounded repository context, and
transactionally modified four existing files to add successful-redirect analytics
while preserving existing shortening and redirect behavior.

## Architecture

`URLShortener` contains the domain rules and process-local in-memory mappings.
`URLShortenerApplication` is a thin WSGI adapter. The service accepts absolute
HTTP(S) URLs, generates stable collision-checked short codes, resolves known
codes, counts successful resolutions, exposes per-code analytics, and reports typed
errors for invalid URLs and unknown codes.

Storage is deliberately in-memory and is lost when the process exits. URL
expiration remains unresolved by the approved specification and is not
implemented.

## Run

Python 3.11 or newer is required. No third-party runtime dependency is needed.

```bash
PYTHONPATH=src python -m url_shortener.app
```

The server binds to `127.0.0.1:8000` by default. Override it with
`URL_SHORTENER_HOST` and `URL_SHORTENER_PORT`.

## Test

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Tests run entirely in-process and require no network access.

## HTTP API

Shorten a URL:

```bash
curl -i -X POST http://127.0.0.1:8000/shorten \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/path"}'
```

The `201 Created` JSON response contains `code` and `short_url`. Resolve the code:

```bash
curl -i http://127.0.0.1:8000/<code>
```

A known code returns `302 Found`; an unknown code returns `404 Not Found`.
Each successful redirect increments that code's process-local count.

Inspect analytics without incrementing the count:

```bash
curl -i http://127.0.0.1:8000/analytics/<code>
```

A known code returns `200 OK` with JSON such as
`{"code": "abc12345", "redirect_count": 7}`. An unknown analytics code
returns `404 Not Found`. Analytics lookup itself does not increment the count.

## Assumptions and trade-offs

- Repeated shortening of the same URL returns the existing code.
- SHA-256-derived candidates plus collision checking provide deterministic codes.
- Redirect counts are process-local, in-memory, and reset when the process
  restarts. Persistence, expiration, authentication, and repository promotion
  remain outside this prototype.
- The orchestrator does not execute this generated source or its tests. Execution
  commands above are manual reviewer/development actions against the exported copy.
