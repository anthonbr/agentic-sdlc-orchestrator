"""Automated behavior tests for the local URL shortener prototype."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
import http.client
from http import HTTPStatus
import json
from pathlib import Path
import socket
import string
import sys
from threading import Lock, Thread
import unittest
from unittest.mock import patch
import urllib.request

import server
from state_engine import (
    ExpiredCodeError,
    InvalidExpirationError,
    InvalidURLError,
    MappingStore,
    UnknownCodeError,
    validate_expires_at,
    validate_url,
)

CURRENT = datetime(2029, 1, 2, 3, 4, 5, tzinfo=UTC)
EXPIRATION = "2030-01-02T03:04:05Z"
INVALID_EXPIRATION_BODY = (
    b'{"error":{"code":"invalid_expiration","message":'
    b'"expires_at must be a future UTC timestamp in '
    b'YYYY-MM-DDTHH:MM:SSZ format"}}'
)
EXPIRED_BODY = (
    b'{"error":{"code":"expired_url","message":"Short URL has expired"}}'
)
NOT_FOUND_BODY = (
    b'{"error":{"code":"not_found","message":'
    b'"No mapping exists for the requested code"}}'
)


class MutableClock:
    """Thread-safe manually controlled UTC clock for deterministic tests."""

    def __init__(self, value: datetime = CURRENT) -> None:
        self._value = value
        self._lock = Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self._value

    def set(self, value: datetime) -> None:
        with self._lock:
            self._value = value


class APITestCase(unittest.TestCase):
    """Exercise the public HTTP API against an isolated in-process server."""

    def setUp(self) -> None:
        self.clock = MutableClock()
        self.original_store = server.STORE
        server.STORE = MappingStore(utc_now=self.clock)
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
            response_headers = {
                key.lower(): value for key, value in response.getheaders()
            }
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

    def create(
        self, url: str, expires_at: str | None = None
    ) -> dict[str, object]:
        request: dict[str, object] = {"url": url}
        if expires_at is not None:
            request["expires_at"] = expires_at
        status, headers, body = self.post_json(request)
        self.assertEqual(HTTPStatus.CREATED, status)
        self.assertEqual("application/json; charset=utf-8", headers["content-type"])
        return json.loads(body)

    def analytics(self, code: object) -> dict[str, object]:
        status, _, body = self.request("GET", f"/api/urls/{code}")
        self.assertEqual(HTTPStatus.OK, status)
        return json.loads(body)

    def test_non_expiring_creation_duplicate_and_representation_regression(self) -> None:
        url = "https://example.com/path?value=One%20Two#fragment"
        created = self.create(url)

        self.assertEqual(
            {"code", "short_url", "url", "redirect_count"}, set(created)
        )
        self.assertEqual(url, created["url"])
        self.assertEqual(0, created["redirect_count"])
        code = created["code"]
        self.assertIsInstance(code, str)
        self.assertEqual(8, len(code))
        self.assertTrue(
            all(character in string.ascii_letters + string.digits for character in code)
        )
        self.assertEqual(f"http://127.0.0.1:8000/{code}", created["short_url"])

        status, _, body = self.post_json({"url": url})
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual(created, json.loads(body))
        self.assertEqual(1, len(server.STORE))

    def test_valid_expiration_creation_and_conditional_representation(self) -> None:
        expiring = self.create("https://example.com/expiring", EXPIRATION)
        baseline = self.create("https://example.com/baseline")

        self.assertEqual(
            {"code", "short_url", "url", "redirect_count", "expires_at"},
            set(expiring),
        )
        self.assertEqual(EXPIRATION, expiring["expires_at"])
        self.assertEqual(
            {"code", "short_url", "url", "redirect_count"}, set(baseline)
        )

    def test_invalid_expiration_forms_return_exact_400_without_mutation(self) -> None:
        known = self.create("https://example.com/known")
        status, _, _ = self.request("GET", f"/{known['code']}")
        self.assertEqual(HTTPStatus.FOUND, status)

        invalid_values = (
            None,
            True,
            False,
            1,
            1.5,
            [],
            {},
            "",
            "2030-01-02",
            "2030-01-02T03:04:05",
            "2030-01-02T03:04:05z",
            "2030-01-02 03:04:05Z",
            "2030-01-02T03:04:05+00:00",
            "2030-01-02T04:04:05+01:00",
            "2030-01-02T03:04:05.0Z",
            "2030-02-30T03:04:05Z",
            "2030-01-02T24:00:00Z",
            "10000-01-02T03:04:05Z",
            "２０３０-01-02T03:04:05Z",
            "2029-01-02T03:04:05Z",
            "2029-01-02T03:04:04Z",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                status, headers, body = self.post_json(
                    {"url": "https://example.com/rejected", "expires_at": value}
                )
                self.assertEqual(HTTPStatus.BAD_REQUEST, status)
                self.assertEqual(INVALID_EXPIRATION_BODY, body)
                self.assertEqual(
                    "application/json; charset=utf-8", headers["content-type"]
                )
                self.assertEqual(1, len(server.STORE))
                self.assertEqual(1, self.analytics(known["code"])["redirect_count"])

    def test_redirect_immediately_before_at_and_after_expiration(self) -> None:
        destination = "https://example.com/boundary?exact=yes"
        mapping = self.create(destination, EXPIRATION)
        boundary = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)

        self.clock.set(boundary - timedelta(microseconds=1))
        status, headers, body = self.request("GET", f"/{mapping['code']}")
        self.assertEqual(HTTPStatus.FOUND, status)
        self.assertEqual(destination, headers["location"])
        self.assertEqual(b"", body)
        self.assertEqual(1, self.analytics(mapping["code"])["redirect_count"])

        self.clock.set(boundary)
        status, headers, body = self.request("GET", f"/{mapping['code']}")
        self.assertEqual(HTTPStatus.GONE, status)
        self.assertNotIn("location", headers)
        self.assertEqual(EXPIRED_BODY, body)
        self.assertEqual(1, self.analytics(mapping["code"])["redirect_count"])

        self.clock.set(boundary + timedelta(microseconds=1))
        status, headers, body = self.request("GET", f"/{mapping['code']}")
        self.assertEqual(HTTPStatus.GONE, status)
        self.assertNotIn("location", headers)
        self.assertEqual(EXPIRED_BODY, body)
        self.assertEqual(1, self.analytics(mapping["code"])["redirect_count"])

    def test_expired_mapping_remains_in_store_and_analytics(self) -> None:
        mapping = self.create("https://example.com/retained", EXPIRATION)
        self.clock.set(datetime(2031, 1, 1, tzinfo=UTC))

        analytics = self.analytics(mapping["code"])
        self.assertEqual(mapping, analytics)
        self.assertEqual(EXPIRATION, analytics["expires_at"])
        self.assertEqual(0, analytics["redirect_count"])
        self.assertEqual(1, len(server.STORE))

    def test_expiration_validation_precedes_duplicate_handling(self) -> None:
        mapping = self.create("https://example.com/duplicate", EXPIRATION)
        self.clock.set(datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC))

        status, _, body = self.post_json(
            {"url": mapping["url"], "expires_at": EXPIRATION}
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, status)
        self.assertEqual(INVALID_EXPIRATION_BODY, body)
        self.assertEqual(1, len(server.STORE))
        self.assertEqual(mapping, self.analytics(mapping["code"]))

    def test_composite_deduplication_and_independent_mappings(self) -> None:
        url = "https://example.com/shared"
        first = self.create(url, "2030-01-02T03:04:05Z")

        status, _, body = self.post_json(
            {"url": url, "expires_at": "2030-01-02T03:04:05Z"}
        )
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual(first, json.loads(body))

        second = self.create(url, "2031-01-02T03:04:05Z")
        baseline = self.create(url)
        self.assertEqual(3, len(server.STORE))
        self.assertEqual(3, len({first["code"], second["code"], baseline["code"]}))

        status, _, body = self.post_json({"url": url})
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual(baseline, json.loads(body))
        self.assertEqual(3, len(server.STORE))
        self.assertEqual(first, self.analytics(first["code"]))

    def test_non_expiring_redirects_continue_after_expiration_dates(self) -> None:
        mapping = self.create("https://example.com/never-expires")
        self.clock.set(datetime.max.replace(tzinfo=UTC))

        for expected_count in (1, 2):
            status, headers, body = self.request("GET", f"/{mapping['code']}")
            self.assertEqual(HTTPStatus.FOUND, status)
            self.assertEqual(mapping["url"], headers["location"])
            self.assertEqual(b"", body)
            self.assertEqual(
                expected_count,
                self.analytics(mapping["code"])["redirect_count"],
            )

    def test_unknown_codes_return_exact_404_without_mutation(self) -> None:
        known = self.create("https://example.com/known")

        for path in ("/NoSuch01", "/api/urls/NoSuch01"):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(HTTPStatus.NOT_FOUND, status)
                self.assertEqual(NOT_FOUND_BODY, body)
                self.assertNotIn("location", headers)

        self.assertEqual(0, self.analytics(known["code"])["redirect_count"])
        self.assertEqual(1, len(server.STORE))

    def test_malformed_requests_content_type_and_methods_do_not_mutate(self) -> None:
        cases = (
            (
                "POST",
                "/api/urls",
                b'{"url":',
                {"Content-Type": "application/json"},
                HTTPStatus.BAD_REQUEST,
            ),
            (
                "POST",
                "/api/urls",
                json.dumps({}).encode("utf-8"),
                {"Content-Type": "application/json"},
                HTTPStatus.BAD_REQUEST,
            ),
            (
                "POST",
                "/api/urls",
                b'{"url":"https://example.com"}',
                {"Content-Type": "text/plain"},
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            ),
            ("PUT", "/api/urls", None, None, HTTPStatus.METHOD_NOT_ALLOWED),
            ("GET", "/api/urls", None, None, HTTPStatus.METHOD_NOT_ALLOWED),
        )
        for method, path, body_value, headers, expected in cases:
            with self.subTest(method=method, path=path, body=body_value):
                status, _, response_body = self.request(
                    method, path, body_value, headers
                )
                self.assertEqual(expected, status)
                representation = json.loads(response_body)
                self.assertEqual({"error"}, set(representation))
                self.assertEqual(
                    {"code", "message"}, set(representation["error"])
                )
                self.assertEqual(0, len(server.STORE))

    def test_url_validation_and_exact_string_preservation_regression(self) -> None:
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
                status, _, _ = self.post_json({"url": url})
                self.assertEqual(HTTPStatus.BAD_REQUEST, status)
                self.assertEqual(0, len(server.STORE))

        accepted_urls = (
            "http://example.com",
            "https://example.com/path",
            "http://example.com:1/low",
            "https://example.com:65535/high",
        )
        for url in accepted_urls:
            with self.subTest(url=url):
                created = self.create(url)
                self.assertEqual(url, created["url"])

        first = self.create("https://example.com/text")
        second = self.create("https://example.com/text/")
        self.assertNotEqual(first["code"], second["code"])


class MappingStoreTestCase(unittest.TestCase):
    """Verify state-engine invariants with deterministic collaborators."""

    def test_validate_expiration_accepts_only_future_canonical_utc(self) -> None:
        text, parsed = validate_expires_at(EXPIRATION, CURRENT)
        self.assertEqual(EXPIRATION, text)
        self.assertEqual(datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC), parsed)

        for value in (
            None,
            True,
            123,
            "2030-01-02T03:04:05.1Z",
            "2030-01-02T03:04:05+00:00",
            "2029-01-02T03:04:05Z",
        ):
            with self.subTest(value=value), self.assertRaises(
                InvalidExpirationError
            ):
                validate_expires_at(value, CURRENT)

    def test_store_boundary_behavior_and_retention(self) -> None:
        clock = MutableClock()
        store = MappingStore(code_generator=lambda: "Boundary", utc_now=clock)
        mapping, created = store.create_or_get(
            "https://example.com/boundary", EXPIRATION
        )
        self.assertTrue(created)
        boundary = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)

        clock.set(boundary - timedelta(microseconds=1))
        self.assertEqual(1, store.record_redirect(mapping.code).redirect_count)

        for moment in (boundary, boundary + timedelta(microseconds=1)):
            clock.set(moment)
            with self.subTest(moment=moment), self.assertRaises(ExpiredCodeError):
                store.record_redirect(mapping.code)
            retained = store.get_mapping(mapping.code)
            self.assertEqual(1, retained.redirect_count)
            self.assertEqual(EXPIRATION, retained.expires_at)
            self.assertEqual(1, len(store))

    def test_redirect_counting_is_atomic_before_and_at_boundary(self) -> None:
        clock = MutableClock()
        store = MappingStore(code_generator=lambda: "Atomic01", utc_now=clock)
        mapping, _ = store.create_or_get("https://example.com/atomic", EXPIRATION)
        failures: list[BaseException] = []
        failures_lock = Lock()

        def successful_worker() -> None:
            try:
                for _ in range(20):
                    store.record_redirect(mapping.code)
            except BaseException as exc:
                with failures_lock:
                    failures.append(exc)

        threads = [Thread(target=successful_worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.assertEqual([], failures)
        self.assertEqual(160, store.get_mapping(mapping.code).redirect_count)

        clock.set(datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC))
        rejected: list[bool] = []
        rejected_lock = Lock()

        def expired_worker() -> None:
            try:
                store.record_redirect(mapping.code)
            except ExpiredCodeError:
                with rejected_lock:
                    rejected.append(True)

        threads = [Thread(target=expired_worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(16, len(rejected))
        self.assertEqual(160, store.get_mapping(mapping.code).redirect_count)

    def test_expiration_identity_and_immutable_snapshots(self) -> None:
        clock = MutableClock()
        candidates = iter(("Expire01", "Expire02", "NoExpiry"))
        store = MappingStore(code_generator=lambda: next(candidates), utc_now=clock)
        url = "https://example.com/identity"

        first, created = store.create_or_get(url, EXPIRATION)
        duplicate, duplicate_created = store.create_or_get(url, EXPIRATION)
        second, second_created = store.create_or_get(
            url, "2031-01-02T03:04:05Z"
        )
        baseline, baseline_created = store.create_or_get(url)

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first, duplicate)
        self.assertTrue(second_created)
        self.assertTrue(baseline_created)
        self.assertEqual(3, len(store))
        self.assertEqual(EXPIRATION, store.get_mapping(first.code).expires_at)
        self.assertNotEqual(first.code, second.code)
        self.assertNotEqual(first.code, baseline.code)
        with self.assertRaises(FrozenInstanceError):
            first.expires_at = "2035-01-01T00:00:00Z"

    def test_invalid_expiration_does_not_mutate_existing_state(self) -> None:
        clock = MutableClock()
        store = MappingStore(code_generator=lambda: "Preserve", utc_now=clock)
        mapping, _ = store.create_or_get("https://example.com/preserve")
        store.record_redirect(mapping.code)

        for value in (None, False, 1, "2029-01-02T03:04:05Z"):
            with self.subTest(value=value), self.assertRaises(
                InvalidExpirationError
            ):
                store.create_or_get("https://example.com/rejected", value)
            self.assertEqual(1, len(store))
            self.assertEqual(1, store.get_mapping(mapping.code).redirect_count)

    def test_collision_retry_and_mapping_isolation_regression(self) -> None:
        candidates = iter(("AAAAAAAA", "AAAAAAAA", "BBBBBBBB"))
        store = MappingStore(code_generator=lambda: next(candidates))
        first, first_created = store.create_or_get("https://example.com/first")
        store.record_redirect(first.code)
        second, second_created = store.create_or_get("https://example.com/second")

        self.assertTrue(first_created)
        self.assertTrue(second_created)
        self.assertEqual("AAAAAAAA", first.code)
        self.assertEqual("BBBBBBBB", second.code)
        self.assertEqual(1, store.get_mapping(first.code).redirect_count)
        self.assertEqual(0, store.get_mapping(second.code).redirect_count)
        self.assertEqual(2, len(store))

    def test_process_reset_and_unknown_code_regression(self) -> None:
        store = MappingStore(code_generator=lambda: "Lifetime")
        mapping, _ = store.create_or_get("https://example.com/lifetime")
        store.record_redirect(mapping.code)
        self.assertEqual(1, store.get_mapping(mapping.code).redirect_count)

        fresh_store = MappingStore()
        self.assertEqual(0, len(fresh_store))
        with self.assertRaises(UnknownCodeError):
            fresh_store.get_mapping(mapping.code)

    def test_validation_is_local_and_non_string_urls_are_rejected(self) -> None:
        with (
            patch.object(
                socket, "getaddrinfo", side_effect=AssertionError("DNS attempted")
            ),
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=AssertionError("network attempted"),
            ),
        ):
            url = "https://does-not-need-to-exist.invalid/path"
            self.assertEqual(url, validate_url(url))

        store = MappingStore()
        for value in (None, 123, [], {}):
            with self.subTest(value=value), self.assertRaises(InvalidURLError):
                store.create_or_get(value)
        self.assertEqual(0, len(store))


class ImportConstraintTestCase(unittest.TestCase):
    """Guard the application and tests against third-party imports."""

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
                    imported_roots.extend(
                        alias.name.partition(".")[0] for alias in node.names
                    )
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
