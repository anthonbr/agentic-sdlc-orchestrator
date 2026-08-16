# Local URL Shortener Prototype

A small, local-only URL shortener implemented with Python 3.12 and the Python standard library. It creates eight-character short codes, redirects short URLs, and tracks successful redirects for the lifetime of the running process.

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

### Create a short URL

```sh
curl -i \
  -X POST \
  -H 'Content-Type: application/json' \
  --data '{"url":"https://example.com/path?value=demo"}' \
  http://127.0.0.1:8000/api/urls
```

A newly created mapping returns HTTP `201 Created` and a JSON object like:

```json
{
  "code": "ABCd1234",
  "short_url": "http://127.0.0.1:8000/ABCd1234",
  "url": "https://example.com/path?value=demo",
  "redirect_count": 0
}
```

The generated code will differ. Repeating the request with the exact same URL string returns the existing mapping with HTTP `200 OK`. Textually different URL strings are treated as different URLs, even when they are semantically equivalent.

### Follow or inspect a redirect

Replace `ABCd1234` with the returned code. To inspect the HTTP `302 Found` response without following it:

```sh
curl -i http://127.0.0.1:8000/ABCd1234
```

To follow the redirect:

```sh
curl -i -L http://127.0.0.1:8000/ABCd1234
```

Each successful request to the short URL increments that mapping's redirect count.

### Read analytics

```sh
curl -i http://127.0.0.1:8000/api/urls/ABCd1234
```

The response contains the code, default short URL, exact submitted destination URL, and current `redirect_count`.

## In-memory lifecycle

All mappings and redirect counts exist only in application memory. They remain available without expiration while that one service process is running. Stopping or restarting the process discards all mappings and counts, and every fresh start begins with an empty data set. State is not persisted and is not shared across multiple processes.

## Significant prototype limitations

- This is an intentionally small, local-only demonstration, not a production service.
- There is no persistence, database, expiration, authentication, API-key requirement, deployment integration, background processing, or multi-process state sharing.
- Destination validation is syntactic and local. The service does not contact destinations or check DNS, reachability, safety, private-network access, or blocklists. It can therefore redirect to unreachable, unsafe, private-network, or otherwise undesirable HTTP or HTTPS destinations.
- Submitted URL strings are preserved exactly. URLs containing credentials, tokens, personal data, or other sensitive query-string or user-information values remain in process memory and are returned by the API.
- Anyone able to access the locally bound service can create mappings, use redirects, and read analytics because authentication is intentionally absent.
- The default short-URL representation is fixed to `http://127.0.0.1:8000/{code}`; running with alternative bind arguments does not change that represented default.
