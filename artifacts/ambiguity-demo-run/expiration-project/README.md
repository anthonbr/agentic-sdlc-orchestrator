# Governed URL Shortener Demo

This dependency-free Python URL shortener was originally produced by the governed
greenfield scenario, enhanced by the governed brownfield analytics scenario, and
then enhanced by the governed ambiguity-resolution scenario. The third workflow
blocked planning for an underspecified expiration request, preserved the human
clarification and analysis revision lineage, and transactionally modified three
existing files only after the revised requirement became authoritative.

## Architecture

`URLShortener` contains the domain rules and process-local in-memory mappings.
`URLShortenerApplication` is a thin WSGI adapter. The service accepts absolute
HTTP(S) URLs, generates stable collision-checked short codes, resolves active codes,
counts successful resolutions, exposes per-code analytics, and reports typed errors
for invalid URLs and unknown or expired codes.

Each code records a timezone-aware creation time and expires exactly 24 hours later.
The fixed TTL starts at creation and is checked on redirect or analytics access. At
or after the boundary, both operations return HTTP 404 through the existing adapter.
Expiration and analytics state are process-local and in-memory: neither survives a
restart. There is no configuration, database, migration, cleanup scheduler, or
background expiration job.

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

Tests inject a mutable timezone-aware clock, run entirely in-process, wait no real
time, and require no network access.

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

An active code returns `302 Found`; an unknown or expired code returns
`404 Not Found`. Each successful pre-expiration redirect increments that code's
process-local count.

Inspect analytics without incrementing the count:

```bash
curl -i http://127.0.0.1:8000/analytics/<code>
```

An active code returns `200 OK` with JSON such as
`{"code": "abc12345", "redirect_count": 7}`. An unknown or expired analytics
code returns `404 Not Found`. Analytics lookup itself does not increment the count.

## Assumptions and trade-offs

- Repeated shortening of the same URL returns the existing code and does not reset
  its creation time or redirect count.
- SHA-256-derived candidates plus collision checking provide deterministic codes.
- Redirect counts and expiration timestamps are process-local, in-memory, and reset
  when the process restarts.
- Persistence, configurable TTLs, background cleanup, migration, authentication,
  and repository promotion remain outside this prototype.
- The orchestrator does not execute generated source or tests. Reviewer tooling
  validates this exported copy only after governed execution and integrity checks.
