"""Thin dependency-free WSGI adapter for the URL-shortener service."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import unquote
from wsgiref.simple_server import make_server

from url_shortener.service import InvalidURLError, UnknownShortCodeError, URLShortener

StartResponse = Callable[[str, list[tuple[str, str]]], Any]


class URLShortenerApplication:
    """WSGI application exposing shorten and resolve operations."""

    def __init__(
        self,
        service: URLShortener | None = None,
        *,
        base_url: str = "http://127.0.0.1:8000",
    ) -> None:
        self.service = service or URLShortener()
        self.base_url = base_url.rstrip("/")

    def __call__(
        self, environ: dict[str, Any], start_response: StartResponse
    ) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))
        if path == "/shorten":
            if method == "POST":
                return self._shorten(environ, start_response)
            return _json_response(
                start_response,
                "405 Method Not Allowed",
                {"error": "method_not_allowed"},
            )
        if method == "GET" and path.startswith("/analytics/"):
            return self._analytics(
                unquote(path[len("/analytics/") :]), start_response
            )
        if method == "GET" and path.startswith("/") and path != "/":
            return self._resolve(unquote(path[1:]), start_response)
        return _json_response(
            start_response, "404 Not Found", {"error": "not_found"}
        )

    def _shorten(
        self, environ: dict[str, Any], start_response: StartResponse
    ) -> Iterable[bytes]:
        try:
            length = int(environ.get("CONTENT_LENGTH") or "0")
            if length < 1 or length > 1_000_000:
                raise ValueError
            stream = environ["wsgi.input"]
            payload = json.loads(stream.read(length).decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("url"), str):
                raise ValueError
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return _json_response(
                start_response,
                "400 Bad Request",
                {"error": "invalid_request", "message": "Expected JSON with a URL."},
            )
        try:
            code = self.service.shorten(payload["url"])
        except InvalidURLError as error:
            return _json_response(
                start_response,
                "400 Bad Request",
                {"error": "invalid_url", "message": str(error)},
            )
        return _json_response(
            start_response,
            "201 Created",
            {"code": code, "short_url": f"{self.base_url}/{code}"},
        )

    def _analytics(
        self, code: str, start_response: StartResponse
    ) -> Iterable[bytes]:
        try:
            redirect_count = self.service.redirect_count(code)
        except UnknownShortCodeError:
            return _json_response(
                start_response,
                "404 Not Found",
                {"error": "unknown_code", "code": code},
            )
        return _json_response(
            start_response,
            "200 OK",
            {"code": code, "redirect_count": redirect_count},
        )

    def _resolve(self, code: str, start_response: StartResponse) -> Iterable[bytes]:
        try:
            original_url = self.service.resolve(code)
        except UnknownShortCodeError:
            return _json_response(
                start_response,
                "404 Not Found",
                {"error": "unknown_code", "code": code},
            )
        start_response(
            "302 Found",
            [("Location", original_url), ("Content-Length", "0")],
        )
        return [b""]


def _json_response(
    start_response: StartResponse, status: str, payload: dict[str, object]
) -> list[bytes]:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]


def main() -> None:
    host = os.environ.get("URL_SHORTENER_HOST", "127.0.0.1")
    port = int(os.environ.get("URL_SHORTENER_PORT", "8000"))
    application = URLShortenerApplication(base_url=f"http://{host}:{port}")
    with make_server(host, port, application) as server:
        print(f"URL shortener listening on http://{host}:{port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
