"""Runnable standard-library HTTP service for the URL shortener prototype."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from typing import Any

from state_engine import (
    ExpiredCodeError,
    InvalidExpirationError,
    InvalidURLError,
    Mapping,
    MappingStore,
    UnknownCodeError,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
STORE = MappingStore()


class URLShortenerHandler(BaseHTTPRequestHandler):
    """Serve creation, redirect, and analytics requests."""

    protocol_version = "HTTP/1.1"
    server_version = "LocalURLShortener/0.1"

    def do_POST(self) -> None:
        if self._request_path() != "/api/urls":
            self._send_method_not_allowed()
            return

        if self.headers.get_content_type().lower() != "application/json":
            self._send_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "Content-Type must be application/json",
            )
            return

        try:
            content_length = self._content_length()
            raw_body = self.rfile.read(content_length)
            representation = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "Request body must be valid JSON",
            )
            return

        if not isinstance(representation, dict):
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "Request body must be a JSON object",
            )
            return

        try:
            if "expires_at" in representation:
                mapping, created = STORE.create_or_get(
                    representation.get("url"),
                    representation["expires_at"],
                )
            else:
                mapping, created = STORE.create_or_get(representation.get("url"))
        except InvalidURLError:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_url",
                "A valid absolute HTTP or HTTPS URL is required",
            )
            return
        except InvalidExpirationError:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_expiration",
                "expires_at must be a future UTC timestamp in YYYY-MM-DDTHH:MM:SSZ format",
            )
            return

        status = HTTPStatus.CREATED if created else HTTPStatus.OK
        self._send_json(status, self._mapping_representation(mapping))

    def do_GET(self) -> None:
        path = self._request_path()

        if path == "/api/urls":
            self._send_method_not_allowed()
            return

        analytics_prefix = "/api/urls/"
        if path.startswith(analytics_prefix):
            code = path[len(analytics_prefix) :]
            if not code or "/" in code:
                self._send_not_found()
                return
            self._serve_analytics(code)
            return

        if path.startswith("/"):
            code = path[1:]
            if code and "/" not in code:
                self._serve_redirect(code)
                return

        self._send_not_found()

    def __getattr__(self, name: str) -> Any:
        """Return a JSON 405 handler for every otherwise unsupported method."""

        if name.startswith("do_"):
            return self._send_method_not_allowed
        raise AttributeError(name)

    def _serve_analytics(self, code: str) -> None:
        try:
            mapping = STORE.get_mapping(code)
        except UnknownCodeError:
            self._send_not_found()
            return

        self._send_json(HTTPStatus.OK, self._mapping_representation(mapping))

    def _serve_redirect(self, code: str) -> None:
        try:
            mapping = STORE.record_redirect(code)
        except UnknownCodeError:
            self._send_not_found()
            return
        except ExpiredCodeError:
            self._send_error(
                HTTPStatus.GONE,
                "expired_url",
                "Short URL has expired",
            )
            return

        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", mapping.url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _content_length(self) -> int:
        value = self.headers.get("Content-Length")
        if value is None:
            raise ValueError("Content-Length is required")

        length = int(value)
        if length < 0:
            raise ValueError("Content-Length must not be negative")
        return length

    def _request_path(self) -> str:
        return self.path.partition("?")[0]

    def _send_not_found(self) -> None:
        self._send_error(
            HTTPStatus.NOT_FOUND,
            "not_found",
            "No mapping exists for the requested code",
        )

    def _send_method_not_allowed(self) -> None:
        self._send_error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "method_not_allowed",
            "The HTTP method is not supported for this resource",
        )

    def _send_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _send_json(self, status: HTTPStatus, value: object) -> None:
        body = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _mapping_representation(mapping: Mapping) -> dict[str, object]:
        """Serialize only fields present on the concrete mapping snapshot."""

        return asdict(mapping)


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Run the local service until interrupted."""

    server = HTTPServer((host, port), URLShortenerHandler)
    print(f"URL shortener listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local URL shortener service")
    parser.add_argument("--host", default=DEFAULT_HOST, help="local bind address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="local bind port")
    arguments = parser.parse_args()
    run(arguments.host, arguments.port)


if __name__ == "__main__":
    main()
