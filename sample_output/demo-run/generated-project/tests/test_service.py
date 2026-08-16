"""Executable tests for the generated URL-shortener project."""

from __future__ import annotations

import json
import unittest
from io import BytesIO
from typing import Any

from url_shortener.app import URLShortenerApplication
from url_shortener.service import InvalidURLError, UnknownShortCodeError, URLShortener


def invoke_wsgi(
    application: URLShortenerApplication,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
) -> tuple[str, dict[str, str], bytes]:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": BytesIO(body),
    }
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = dict(headers)

    response = b"".join(application(environ, start_response))
    return (
        str(captured["status"]),
        dict(captured["headers"]),  # type: ignore[arg-type]
        response,
    )


class URLShortenerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = URLShortener()

    def test_shortening_valid_url_returns_code(self) -> None:
        code = self.service.shorten("https://example.com/path")
        self.assertRegex(code, r"^[A-Za-z0-9_-]{8}$")

    def test_different_urls_receive_distinct_codes(self) -> None:
        first = self.service.shorten("https://example.com/first")
        second = self.service.shorten("https://example.com/second")
        self.assertNotEqual(first, second)

    def test_repeated_shortening_is_idempotent(self) -> None:
        url = "https://example.com/repeated"
        self.assertEqual(self.service.shorten(url), self.service.shorten(url))

    def test_known_code_resolves_to_original_url(self) -> None:
        url = "https://example.com/original"
        self.assertEqual(self.service.resolve(self.service.shorten(url)), url)

    def test_unknown_code_raises_domain_error(self) -> None:
        with self.assertRaises(UnknownShortCodeError):
            self.service.resolve("missing")

    def test_invalid_urls_are_rejected(self) -> None:
        for url in ("", "example.com", "/relative", "ftp://example.com", "https://bad host"):
            with self.subTest(url=url), self.assertRaises(InvalidURLError):
                self.service.shorten(url)


class URLShortenerHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = URLShortenerApplication(
            URLShortener(), base_url="http://short.test"
        )

    def test_post_shorten_returns_created_response(self) -> None:
        status, _, body = invoke_wsgi(
            self.application,
            "POST",
            "/shorten",
            {"url": "https://example.com/http"},
        )
        payload = json.loads(body)
        self.assertEqual(status, "201 Created")
        self.assertEqual(payload["short_url"], f"http://short.test/{payload['code']}")

    def test_post_shorten_rejects_invalid_url(self) -> None:
        status, _, body = invoke_wsgi(
            self.application,
            "POST",
            "/shorten",
            {"url": "ftp://example.com/not-supported"},
        )
        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(json.loads(body)["error"], "invalid_url")

    def test_get_shorten_rejects_wrong_method(self) -> None:
        status, _, body = invoke_wsgi(self.application, "GET", "/shorten")
        self.assertEqual(status, "405 Method Not Allowed")
        self.assertEqual(json.loads(body)["error"], "method_not_allowed")

    def test_get_known_code_redirects(self) -> None:
        url = "https://example.com/redirect"
        code = self.application.service.shorten(url)
        status, headers, body = invoke_wsgi(self.application, "GET", f"/{code}")
        self.assertEqual(status, "302 Found")
        self.assertEqual(headers["Location"], url)
        self.assertEqual(body, b"")

    def test_get_unknown_code_returns_not_found(self) -> None:
        status, _, body = invoke_wsgi(self.application, "GET", "/unknown")
        self.assertEqual(status, "404 Not Found")
        self.assertEqual(json.loads(body)["error"], "unknown_code")


if __name__ == "__main__":
    unittest.main()
