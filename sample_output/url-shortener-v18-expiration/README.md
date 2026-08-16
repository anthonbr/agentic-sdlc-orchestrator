# Local URL Shortener Prototype

A small, local-only URL shortener implemented with Python 3.12 and the Python standard library. It creates eight-character short codes, optionally expires mappings at an absolute UTC time, redirects active short URLs, and tracks successful redirects for the lifetime of the running process.

## Requirements

- Python 3.12
- No third-party packages
- No database, external service, cloud resource, credential, API key, or other infrastructure

Run all commands from the project root, which contains `server.py` and `state_engine.py`.

## Setup

Confirm that Python 3.12 is available:

```sh
python3.12 --version
```

No package installation is required. The application and tests use only the Python standard library.

If your system exposes Python 3.12 under a different command, replace `python3.12` in the commands below with that interpreter's path.

## Start the service

```sh
python3.12 server.py
```

The service listens by default at `http://127.0.0.1:8000`. Stop it with Ctrl-C.

The entry point also accepts local bind options:

```sh
python3.12 server.py --host 127.0.0.1 --port 8000
```

Use the default host and port for the examples below and for short URLs represented as `http://127.0.0.1:8000/{code}`.

### Optional orchestrator-layout interpreter

If this project remains in its original `projects/<project-name>/` directory and the orchestrator's root `.venv` exists two levels above the project, the same entry point can optionally be invoked with:

```sh
../../.venv/bin/python server.py
```

This relative path is optional and layout-dependent. If the project is copied or moved elsewhere, use the portable Python 3.12 setup and start commands above instead.

## Run the automated tests

From the project root:

```sh
python3.12 -m unittest discover -s tests -p 'test_*.py'
```

The tests also use only the Python standard library and require no running service, database, network service, or credentials.

## Minimal API usage

Start the service in one terminal, then use another terminal for these examples.

### Create a non-expiring short URL

```sh
curl -i \
  -X POST \
  -H 'Content-Type: application/json' \
  --data '{"url":"https://example.com/path?value=demo"}' \
  http://127.0.0.1:8000/api/urls
```

A newly created non-expiring mapping returns HTTP `201 Created` and exactly the existing four-field JSON shape:

```json
{
  "code": "ABCd1234",
  "short_url": "http://127.0.0.1:8000/ABCd1234",
  "url": "https://example.com/path?value=demo",
  "redirect_count": 0
}
```

The generated code will differ. Requests containing only `url` continue to create non-expiring mappings and preserve the baseline behavior. Repeating a request with the exact same URL string and omitted expiration returns the existing mapping with HTTP `200 OK`. Textually different URL strings are treated as different URLs, even when they are semantically equivalent.

### Create an expiring short URL

`POST /api/urls` accepts an optional `expires_at` field:

```sh
curl -i \
  -X POST \
  -H 'Content-Type: application/json' \
  --data '{"url":"https://example.com/temporary","expires_at":"2099-12-31T23:59:59Z"}' \
  http://127.0.0.1:8000/api/urls
```

The example expiration must still be in the future when submitted. An accepted `expires_at` value must be:

- a JSON string;
- an absolute UTC timestamp, not a relative duration;
- a real date and time within Python's supported datetime range;
- strictly later than the service process's current UTC time when validated; and
- in the exact whole-second ASCII form `YYYY-MM-DDTHH:MM:SSZ`, including uppercase `T` and trailing `Z`.

Null, booleans, numbers, malformed or out-of-range timestamps, fractional seconds, non-UTC offsets, and timestamps equal to or earlier than current UTC time are rejected. Expiration validation occurs before duplicate lookup, so an expiration that is no longer in the future is rejected even if a mapping with the same URL and expiration already exists.

A newly created expiring mapping returns HTTP `201 Created` and the four baseline fields plus `expires_at`:

```json
{
  "code": "EfGH5678",
  "short_url": "http://127.0.0.1:8000/EfGH5678",
  "url": "https://example.com/temporary",
  "redirect_count": 0,
  "expires_at": "2099-12-31T23:59:59Z"
}
```

`expires_at` is stored in canonical form and is immutable after creation. The API provides no operation to add, remove, extend, or shorten an expiration.

#### Invalid expiration response

Invalid `expires_at` input returns HTTP `400 Bad Request` with exactly:

```json
{"error":{"code":"invalid_expiration","message":"expires_at must be a future UTC timestamp in YYYY-MM-DDTHH:MM:SSZ format"}}
```

The rejected request does not create or modify any mapping or redirect count.

### Follow or inspect a redirect

Replace `ABCd1234` with the returned code. To inspect the HTTP `302 Found` response without following it:

```sh
curl -i http://127.0.0.1:8000/ABCd1234
```

To follow the redirect:

```sh
curl -i -L http://127.0.0.1:8000/ABCd1234
```

A non-expiring mapping continues to redirect normally. An expiring mapping also returns HTTP `302 Found` before its expiration. Each successful request to an active short URL increments that mapping's `redirect_count` exactly once.

Expiration is evaluated using the service process's current UTC time on each relevant operation. A mapping is active immediately before `expires_at` and expired when current UTC time is equal to or later than `expires_at`.

At or after expiration, requesting the short URL returns HTTP `410 Gone` with exactly:

```json
{"error":{"code":"expired_url","message":"Short URL has expired"}}
```

The HTTP `410` response has no `Location` header and does not increment `redirect_count`.

### Read analytics

```sh
curl -i http://127.0.0.1:8000/api/urls/ABCd1234
```

Analytics return HTTP `200 OK` with the mapping's code, default short URL, exact submitted destination URL, and current `redirect_count`. A non-expiring mapping retains exactly the four-field shape shown above. An expiring mapping also includes its stored `expires_at` value.

Analytics remain available after a mapping expires. Expired mappings retain their last successful `redirect_count`; failed HTTP `410` requests do not change it.

## Deduplication and mapping identity

Creation deduplication uses the combination of:

1. the exact submitted destination URL string; and
2. the optional expiration value, with omitted expiration distinct from every supplied expiration.

An active mapping with an exact URL and `expires_at` match is returned with HTTP `200 OK`. A new mapping is created with HTTP `201 Created` when the expiration differs or when one request omits expiration and the other supplies it. These mappings are independent; creating one does not alter another mapping for the same URL.

Expired mappings are retained but are not eligible deduplication matches. A request can create a new mapping when all exact matches are expired, provided its submitted `expires_at` is itself still strictly in the future. Because expiration validation happens first, resubmitting the already elapsed expiration receives the exact HTTP `400` validation response rather than creating or returning a mapping.

## In-memory lifecycle

All mappings, expiration values, and redirect counts exist only in application memory for one service process. Non-expiring mappings remain active until that process stops. Expired mappings remain resident and available through analytics until the process stops; there is no background or lazy deletion.

Stopping or restarting the process discards every mapping, expiration value, and redirect count, and every fresh start begins with an empty data set. State is not persisted or coordinated across processes. Running multiple service processes would give each process separate state.

## Significant prototype limitations

- This is an intentionally small, local-only demonstration, not a production service.
- There is no persistence, database, authentication, API-key requirement, deployment integration, external time service, background processing, cleanup job, or multi-process state sharing or coordination.
- Current UTC time comes from the local process environment; clock changes can affect expiration validation and redirect decisions.
- Expired mappings are deliberately retained until restart. A long-running process can therefore consume unbounded memory as mappings accumulate.
- Destination validation is syntactic and local. The service does not contact destinations or check DNS, reachability, safety, private-network access, or blocklists. It can therefore redirect to unreachable, unsafe, private-network, or otherwise undesirable HTTP or HTTPS destinations.
- Submitted URL strings are preserved exactly. URLs containing credentials, tokens, personal data, or other sensitive query-string or user-information values remain in process memory and are returned by the API.
- Anyone able to access the locally bound service can create mappings, use redirects, and read analytics because authentication is intentionally absent.
- The default short-URL representation is fixed to `http://127.0.0.1:8000/{code}`; running with alternative bind arguments does not change that represented default.
