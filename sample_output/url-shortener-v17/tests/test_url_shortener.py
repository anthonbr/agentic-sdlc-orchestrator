"""Automated behavior tests for the local URL shortener prototype."""

from __future__ import annotations

import ast
import http.client
from http import HTTPStatus
import json
from pathlib import Path
import socket
import string
import sys
from threading import Thread
import unittest
from unittest.mock import patch
import urllib.request

import server
from state_engine import InvalidURLError, MappingStore, UnknownCodeError, validate_url


class APITestCase(unittest.TestCase):
    """Exercise the public HTTP API against an isolated in-process server."""

    def setUp(self) -> None:
        self.original_store = server.STORE
        server.STORE = MappingStore()
        self.httpd = server.HTTPServer(("127.0.0.1", 0), server.URLShortenerHandler)
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.httpd.server_address

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()
        server.STORE = self.original_store

    def request(
        self,
        method: str,
        path: str,
        body: bytes | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            response_body = response.read()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, response_headers, response_body
        finally:
            connection.close()

    def post_json(self, value: object) -> tuple[int, dict[str, str], bytes]:
        return self.request(
            "POST",
            "/api/urls",
            json.dumps(value).encode("utf-8"),
            {"Content-Type": "application/json"},
        )

    def create(self, url: str) -> dict[str, object]:
        status, headers, body = self.post_json({"url": url})
        self.assertEqual(HTTPStatus.CREATED, status)
        self.assertEqual("application/json; charset=utf-8", headers["content-type"])
        return json.loads(body)

    def assert_error_schema(self, status: int, body: bytes, expected_status: int) -> None:
        self.assertEqual(expected_status, status)
        representation = json.loads(body)
        self.assertEqual({"error"}, set(representation))
        self.assertIsInstance(representation["error"], dict)
        self.assertEqual({"code", "message"}, set(representation["error"]))
        self.assertIsInstance(representation["error"]["code"], str)
        self.assertIsInstance(representation["error"]["message"], str)

    def analytics(self, code: str) -> dict[str, object]:
        status, _, body = self.request("GET", f"/api/urls/{code}")
        self.assertEqual(HTTPStatus.OK, status)
        return json.loads(body)

    def test_new_creation_and_exact_duplicate(self) -> None:
        url = "https://example.com/path?value=One%20Two#fragment"
        created = self.create(url)

        self.assertEqual({"code", "short_url", "url", "redirect_count"}, set(created))
        self.assertEqual(url, created["url"])
        self.assertEqual(0, created["redirect_count"])
        code = created["code"]
        self.assertIsInstance(code, str)
        self.assertEqual(8, len(code))
        self.assertTrue(all(character in string.ascii_letters + string.digits for character in code))
        self.assertEqual(f"http://127.0.0.1:8000/{code}", created["short_url"])

        status, _, body = self.post_json({"url": url})
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual(created, json.loads(body))
        self.assertEqual(1, len(server.STORE))

    def test_textually_different_urls_have_independent_mappings(self) -> None:
        first = self.create("https://example.com/path")
        second = self.create("https://example.com/path/")

        self.assertNotEqual(first["code"], second["code"])
        self.assertEqual(2, len(server.STORE))

    def test_redirect_analytics_duplicate_and_count_isolation(self) -> None:
        first_url = "https://example.com/first?x=1"
        second_url = "http://example.net:8080/second"
        first = self.create(first_url)
        second = self.create(second_url)

        status, headers, body = self.request("GET", f"/{first['code']}")
        self.assertEqual(HTTPStatus.FOUND, status)
        self.assertEqual(first_url, headers["location"])
        self.assertEqual(b"", body)

        self.assertEqual(1, self.analytics(str(first["code"]))["redirect_count"])
        self.assertEqual(0, self.analytics(str(second["code"]))["redirect_count"])

        status, _, body = self.post_json({"url": first_url})
        self.assertEqual(HTTPStatus.OK, status)
        duplicate = json.loads(body)
        self.assertEqual(first["code"], duplicate["code"])
        self.assertEqual(1, duplicate["redirect_count"])

        status, _, _ = self.request("GET", f"/{first['code']}")
        self.assertEqual(HTTPStatus.FOUND, status)
        self.assertEqual(2, self.analytics(str(first["code"]))["redirect_count"])
        self.assertEqual(0, self.analytics(str(second["code"]))["redirect_count"])

    def test_unknown_redirect_and_analytics_return_json_404_without_mutation(self) -> None:
        known = self.create("https://example.com/known")

        for path in ("/NoSuch01", "/api/urls/NoSuch01"):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assert_error_schema(status, body, HTTPStatus.NOT_FOUND)

        self.assertEqual(0, self.analytics(str(known["code"]))["redirect_count"])
        self.assertEqual(1, len(server.STORE))

    def test_malformed_json_and_missing_url_create_no_mapping(self) -> None:
        cases = (
            (b'{"url":', {"Content-Type": "application/json"}),
            (json.dumps({}).encode("utf-8"), {"Content-Type": "application/json"}),
        )
        for body_value, headers in cases:
            with self.subTest(body=body_value):
                status, _, body = self.request("POST", "/api/urls", body_value, headers)
                self.assert_error_schema(status, body, HTTPStatus.BAD_REQUEST)
                self.assertEqual(0, len(server.STORE))

    def test_invalid_url_boundaries_return_400_without_creation(self) -> None:
        invalid_urls = (
            "",
            "ftp://example.com/file",
            "https:///missing-host",
            "https://example.com/a path",
            "https://example.com/line\nfeed",
            "https://example.com:",
            "https://example.com:abc/path",
            "https://example.com:0/path",
            "https://example.com:65536/path",
        )
        for url in invalid_urls:
            with self.subTest(url=repr(url)):
                status, _, body = self.post_json({"url": url})
                self.assert_error_schema(status, body, HTTPStatus.BAD_REQUEST)
                self.assertEqual(0, len(server.STORE))

    def test_required_valid_url_boundaries_are_accepted(self) -> None:
        accepted_urls = (
            "http://example.com",
            "https://example.com/path",
            "http://example.com:1/low",
            "https://example.com:65535/high",
        )
        for url in accepted_urls:
            with self.subTest(url=url):
                status, _, body = self.post_json({"url": url})
                self.assertEqual(HTTPStatus.CREATED, status)
                self.assertEqual(url, json.loads(body)["url"])

    def test_unsupported_media_type_and_method_use_error_schema(self) -> None:
        status, _, body = self.request(
            "POST",
            "/api/urls",
            b'{"url":"https://example.com"}',
            {"Content-Type": "text/plain"},
        )
        self.assert_error_schema(status, body, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        self.assertEqual(0, len(server.STORE))

        status, _, body = self.request("PUT", "/api/urls")
        self.assert_error_schema(status, body, HTTPStatus.METHOD_NOT_ALLOWED)
        self.assertEqual(0, len(server.STORE))


class MappingStoreTestCase(unittest.TestCase):
    """Verify state-engine invariants that need deterministic collaborators."""

    def test_collision_retries_without_changing_existing_mapping(self) -> None:
        candidates = iter(("AAAAAAAA", "AAAAAAAA", "BBBBBBBB"))
        store = MappingStore(code_generator=lambda: next(candidates))
        first, first_created = store.create_or_get("https://example.com/first")
        store.record_redirect(first.code)

        second, second_created = store.create_or_get("https://example.com/second")

        self.assertTrue(first_created)
        self.assertTrue(second_created)
        self.assertEqual("AAAAAAAA", first.code)
        self.assertEqual("BBBBBBBB", second.code)
        preserved = store.get_mapping(first.code)
        self.assertEqual("https://example.com/first", preserved.url)
        self.assertEqual(1, preserved.redirect_count)
        self.assertEqual(2, len(store))

    def test_state_lasts_for_store_lifetime_and_fresh_store_is_empty(self) -> None:
        store = MappingStore(code_generator=lambda: "Lifetime")
        mapping, _ = store.create_or_get("https://example.com/lifetime")
        store.record_redirect(mapping.code)

        self.assertEqual(1, store.get_mapping(mapping.code).redirect_count)
        fresh_store = MappingStore()
        self.assertEqual(0, len(fresh_store))
        with self.assertRaises(UnknownCodeError):
            fresh_store.get_mapping(mapping.code)

    def test_validation_is_local_and_makes_no_external_request(self) -> None:
        with (
            patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS attempted")),
            patch.object(urllib.request, "urlopen", side_effect=AssertionError("network attempted")),
        ):
            self.assertEqual(
                "https://does-not-need-to-exist.invalid/path",
                validate_url("https://does-not-need-to-exist.invalid/path"),
            )

    def test_non_string_url_is_rejected_without_state_change(self) -> None:
        store = MappingStore()
        for value in (None, 123, [], {}):
            with self.subTest(value=value), self.assertRaises(InvalidURLError):
                store.create_or_get(value)
        self.assertEqual(0, len(store))


class ImportConstraintTestCase(unittest.TestCase):
    """Guard the application and test suite against third-party imports."""

    def test_python_files_import_only_standard_library_or_project_modules(self) -> None:
        root = Path(__file__).resolve().parents[1]
        files = (root / "server.py", root / "state_engine.py", Path(__file__).resolve())
        project_modules = {"server", "state_engine"}
        violations: list[str] = []

        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                imported_roots: list[str] = []
                if isinstance(node, ast.Import):
                    imported_roots.extend(alias.name.partition(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_roots.append(node.module.partition(".")[0])

                for imported_root in imported_roots:
                    if (
                        imported_root not in sys.stdlib_module_names
                        and imported_root not in project_modules
                    ):
                        violations.append(f"{path.name}: {imported_root}")

        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
