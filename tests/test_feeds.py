"""Source normalization, provenance, parsing, and loopback transport tests.

The HTTP fixture is a local, nonbillable server. No test contacts an external
provider or uses real credentials.
"""

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from cazimi.feeds import MAX_OBSERVATIONS, MAX_RESPONSE_BYTES, fetch_source, validate_source


NOW = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)


def configured(location, **overrides):
    source = {
        "id": "reports",
        "kind": "json",
        "location": str(location),
        "category": "company_filings_ir",
        "quality": "primary",
    }
    source.update(overrides)
    return source


def metric(**overrides):
    observation = {
        "id": "revenue-q3",
        "ticker": "example",
        "kind": "metric",
        "as_of": "2026-10-22T08:00:00-05:00",
        "metric": "revenue_growth",
        "value": 0.12,
        "unit": "ratio",
        "period": "2026Q3",
        "citation": "https://example.test/filing/2026-q3",
        "title": "Quarterly results",
    }
    observation.update(overrides)
    return observation


class FeedParsingTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path.cwd() / ".test-tmp"
        temporary_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def write(self, contents, name="feed.json"):
        path = self.directory / name
        if isinstance(contents, bytes):
            path.write_bytes(contents)
        else:
            path.write_text(contents, encoding="utf-8")
        return path

    def fetch_json(self, observations, **source_overrides):
        path = self.write(json.dumps({"observations": observations}))
        return fetch_source(configured(path, **source_overrides), NOW)

    def fetch_xml(self, contents, **source_overrides):
        path = self.write(contents, "feed.xml")
        return fetch_source(configured(path, kind="rss", ticker="example", **source_overrides), NOW)

    def test_source_resolution_defaults_and_no_mutation(self):
        raw = configured("reports with spaces & Unicode Δ.json")
        result = validate_source(raw, base_dir=self.directory)
        self.assertEqual(result["location"], str((self.directory / raw["location"]).resolve()))
        self.assertEqual(result["timeout"], 15)
        self.assertEqual(result["request_cost"], 0)
        self.assertEqual(result["header_env"], {})
        self.assertEqual(raw["location"], "reports with spaces & Unicode Δ.json")
        self.assertNotIn("timeout", raw)

    def test_supported_urls_and_secret_names_are_preserved_without_resolution(self):
        for url in ("https://example.test/feed?symbol=EXAMPLE", "http://localhost:8080/feed", "http://127.0.0.1/feed", "http://[::1]/feed"):
            with self.subTest(url=url):
                raw = configured(url, header_env={"Authorization": "CAZIMI_TEST_FEED_TOKEN"})
                self.assertEqual(validate_source(raw)["location"], url)
                self.assertEqual(validate_source(raw)["header_env"], raw["header_env"])

    def test_transport_rejects_insecure_credentials_and_invalid_urls(self):
        invalid = (
            "http://example.test/feed", "https://user:secret@example.test/feed",
            "https://example.test/feed#fragment", "https://example.test:bad/feed",
            "https://example.test:0/feed", "https:///feed", "ftp://example.test/feed",
            "https://example.test/line\nSECRET", "https://example.test\\@attacker.test/feed",
            "file:///tmp/example.json", "https:example.test/feed",
        )
        for location in invalid:
            with self.subTest(location=location):
                with self.assertRaises(ValueError):
                    validate_source(configured(location))

    def test_invalid_configuration_is_rejected(self):
        invalid = (
            {"id": "../escape"}, {"id": ""}, {"kind": "html"},
            {"category": "rumors"}, {"quality": "official-ish"},
            {"timeout": True}, {"timeout": 0}, {"timeout": 61},
            {"timeout": float("nan")}, {"request_cost": -1}, {"request_cost": float("inf")},
            {"header_env": {"Authorization": "actual secret value"}},
            {"header_env": {"Host": "HOST_ENV"}},
            {"header_env": {"Authorization\nInjected": "TOKEN"}},
            {"header_env": {"Authorization": "TOKEN", "authorization": "OTHER"}},
            {"headers": {"Authorization": "secret"}},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    validate_source(configured("https://example.test/feed", **overrides))
        with self.assertRaises(ValueError):
            validate_source(configured("local.json", header_env={"Authorization": "TOKEN"}))
        with self.assertRaises(ValueError):
            validate_source(configured("https://example.test/feed", kind="rss"))

    def test_metric_is_normalized_and_registered_provenance_wins(self):
        incoming = metric(source_id="spoofed", source_quality="user", source_category="internal_research")
        result, = self.fetch_json([incoming])
        self.assertEqual(result["id"], "reports:revenue-q3")
        self.assertEqual(result["ticker"], "EXAMPLE")
        self.assertEqual(result["as_of"], "2026-10-22T13:00:00+00:00")
        self.assertEqual(result["source_id"], "reports")
        self.assertEqual(result["source_quality"], "primary")
        self.assertEqual(result["source_category"], "company_filings_ir")
        self.assertEqual(result["value"], 0.12)
        self.assertEqual(result["citation"], incoming["citation"])

    def test_source_ticker_is_a_default_and_cannot_be_overridden(self):
        observation = metric()
        del observation["ticker"]
        self.assertEqual(self.fetch_json([observation], ticker="ex.ample")[0]["ticker"], "EX.AMPLE")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.fetch_json([metric(ticker="OTHER")], ticker="EXAMPLE")

    def test_price_requires_positive_value_and_currency_and_has_canonical_fields(self):
        result, = self.fetch_json([metric(kind="price", value=123.4, unit="usd", metric="ignored", period="ignored")])
        self.assertEqual((result["metric"], result["period"], result["unit"]), ("price", "spot", "USD"))
        for overrides in ({"value": 0}, {"value": -1}, {"unit": "%"}, {"unit": "USD/share"}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    self.fetch_json([metric(kind="price", **overrides)])

    def test_documents_never_acquire_numeric_metrics(self):
        result, = self.fetch_json([metric(kind="document", value=None)])
        self.assertEqual((result["metric"], result["unit"], result["period"], result["value"]), ("", "", "", None))
        with self.assertRaisesRegex(ValueError, "cannot contain"):
            self.fetch_json([metric(kind="document", value=12)])

    def test_missing_malformed_future_or_naive_dates_fail(self):
        for date in (None, "", "2026-10-24T00:00:00Z", "2026-10-22T08:00:00", "2026-13-22T00:00:00Z", "tomorrow", "2026-10-22"):
            with self.subTest(date=date):
                with self.assertRaises(ValueError):
                    self.fetch_json([metric(as_of=date)])
        result, = self.fetch_json([metric(as_of=NOW.isoformat())])
        self.assertEqual(result["as_of"], NOW.isoformat())

    def test_run_requires_aware_datetime(self):
        source = configured(self.write('{"observations": []}'))
        for run_time in (None, "2026-10-23", datetime(2026, 10, 23)):
            with self.subTest(run_time=run_time):
                with self.assertRaisesRegex(ValueError, "timezone-aware"):
                    fetch_source(source, run_time)

    def test_invalid_observation_fails_whole_source(self):
        invalid = (
            {"id": ""}, {"ticker": ""}, {"kind": "opinion"}, {"metric": ""},
            {"period": ""}, {"unit": ""}, {"value": True}, {"value": "12"},
            {"value": float("nan")}, {"value": float("inf")},
            {"citation": ""}, {"citation": "reference\nmalicious"}, {"title": "x" * 1025},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    self.fetch_json([metric(id="valid"), metric(**overrides)])
        with self.assertRaises(ValueError):
            self.fetch_json([None])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.fetch_json([metric(), metric(value=99)])

    def test_json_requires_explicit_envelope_and_record_limit(self):
        for content in ("[]", "{}", '{"observations": {}}', "null", "not json secret", '{"observations": [NaN]}'):
            with self.subTest(content=content):
                source = configured(self.write(content))
                with self.assertRaises(ValueError) as error:
                    fetch_source(source, NOW)
                self.assertNotIn("not json secret", str(error.exception))
        self.assertEqual(self.fetch_json([]), [])
        with self.assertRaisesRegex(ValueError, "256"):
            self.fetch_json([metric(id=str(index)) for index in range(MAX_OBSERVATIONS + 1)])

    def test_bounded_files_and_missing_paths(self):
        oversized = self.write(b" " * (MAX_RESPONSE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "2 MiB"):
            fetch_source(configured(oversized), NOW)
        for path in (self.directory, self.directory / "missing.json"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "could not be read"):
                    fetch_source(configured(path), NOW)

    def test_rss_documents_use_publication_date_and_do_not_infer_metrics(self):
        result, = self.fetch_xml("""<?xml version="1.0"?><rss version="2.0"><channel>
          <item><guid>release-1</guid><title>Revenue up 20% &amp; margin higher</title>
          <link>https://example.test/release</link><pubDate>Thu, 22 Oct 2026 08:00:00 -0500</pubDate></item>
          <item><title>Undated article is skipped</title></item>
          </channel></rss>""")
        self.assertEqual(result["id"], "reports:release-1")
        self.assertEqual(result["as_of"], "2026-10-22T13:00:00+00:00")
        self.assertEqual(result["kind"], "document")
        self.assertEqual(result["title"], "Revenue up 20% & margin higher")
        self.assertIsNone(result["value"])
        self.assertEqual(result["metric"], "")

    def test_atom_namespace_updated_date_and_alternate_link(self):
        result, = self.fetch_xml("""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <id>tag:example.test,2026:release</id><title type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">Results <b>released</b></div></title>
          <published>2026-10-20T00:00:00Z</published><updated>2026-10-22T00:00:00Z</updated>
          <link rel="self" href="https://example.test/feed/entry" />
          <link rel="alternate" href="https://example.test/results" />
          </entry></feed>""")
        self.assertEqual(result["as_of"], "2026-10-22T00:00:00+00:00")
        self.assertEqual(result["citation"], "https://example.test/results")
        self.assertEqual(result["title"], "Results released")

    def test_rss_ids_are_stable_without_guid_and_long_ids_are_bounded(self):
        xml = '<rss><channel><item><title>Results</title><pubDate>2026-10-22T00:00:00Z</pubDate></item></channel></rss>'
        first, = self.fetch_xml(xml)
        second, = self.fetch_xml(xml)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["citation"], str(self.directory / "feed.xml"))
        result, = self.fetch_xml(xml.replace("<title>", "<guid>" + "x" * 300 + "</guid><title>"))
        self.assertLessEqual(len(result["id"]), len("reports:") + 128)

    def test_unsafe_malformed_xml_and_invalid_stated_dates_fail(self):
        contents = (
            '<!DOCTYPE rss [<!ENTITY x "secret">]><rss><channel /></rss>',
            '<!DOCTYPE rss SYSTEM "https://example.test/entity"><rss />',
            '<rss><channel /></rss>'.encode("utf-16"),
            '<rss><channel>', '<html><body>not a feed</body></html>',
            '<rss><channel><item><pubDate>not a date</pubDate></item></channel></rss>',
            '<rss><channel><item><pubDate>2026-10-24T00:00:00Z</pubDate></item></channel></rss>',
        )
        for content in contents:
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    self.fetch_xml(content)

    def test_rss_record_limit_and_duplicate_ids(self):
        entry = '<item><guid>same</guid><pubDate>2026-10-22T00:00:00Z</pubDate></item>'
        with self.assertRaisesRegex(ValueError, "256"):
            self.fetch_xml(f'<rss><channel>{entry * (MAX_OBSERVATIONS + 1)}</channel></rss>')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.fetch_xml(f'<rss><channel>{entry * 2}</channel></rss>')

    def test_transport_exceptions_are_sanitized(self):
        for error in (URLError("secret-token"), IncompleteRead(b"secret-token"), ValueError("secret-token"), UnicodeError("secret-token")):
            with self.subTest(error=type(error).__name__):
                with patch("cazimi.feeds.build_opener", side_effect=error):
                    with self.assertRaises(ValueError) as raised:
                        fetch_source(configured("https://example.test/feed?key=secret-token"), NOW)
                self.assertNotIn("secret-token", str(raised.exception))


class _FeedHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append({"path": self.path, "headers": dict(self.headers)})
        self.send_response(self.server.response_status)
        for key, value in self.server.response_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(self.server.response_body)))
        self.end_headers()
        try:
            self.wfile.write(self.server.response_body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


class FeedTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/feed"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.server.requests = []
        self.server.response_status = 200
        self.server.response_headers = {}
        self.server.response_body = json.dumps({"observations": [metric()]}).encode()

    def test_loopback_fetch_ignores_environment_proxy_and_loads_rotated_credentials(self):
        source = configured(self.url, header_env={"Authorization": "CAZIMI_TEST_FEED_TOKEN"})
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": "", "CAZIMI_TEST_FEED_TOKEN": "Bearer first-test-value"}):
            normalized = validate_source(source)
            first, = fetch_source(normalized, NOW)
            os.environ["CAZIMI_TEST_FEED_TOKEN"] = "Bearer rotated-test-value"
            fetch_source(normalized, NOW)
        self.assertEqual(first["ticker"], "EXAMPLE")
        self.assertEqual([request["headers"]["Authorization"] for request in self.server.requests], ["Bearer first-test-value", "Bearer rotated-test-value"])
        self.assertEqual(normalized["header_env"], {"Authorization": "CAZIMI_TEST_FEED_TOKEN"})
        self.assertNotIn("Bearer", repr(normalized))

    def test_missing_or_header_injecting_credentials_do_not_send_requests_or_echo_secret(self):
        source = configured(self.url, header_env={"Authorization": "CAZIMI_TEST_FEED_TOKEN"})
        for value in ("", "secret\r\nInjected: yes", "secret\x01"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"CAZIMI_TEST_FEED_TOKEN": value}):
                    with self.assertRaises(ValueError) as error:
                        fetch_source(source, NOW)
                self.assertNotIn("secret", str(error.exception))
        self.assertEqual(self.server.requests, [])

    def test_redirect_is_not_followed_and_error_body_is_not_exposed(self):
        self.server.response_status = 302
        self.server.response_headers = {"Location": self.url + "/redirected?secret=token"}
        self.server.response_body = b"private-response-body"
        with self.assertRaisesRegex(ValueError, "redirect refused") as error:
            fetch_source(configured(self.url), NOW)
        self.assertEqual(len(self.server.requests), 1)
        self.assertNotIn("token", str(error.exception))
        self.assertNotIn("private-response-body", str(error.exception))

    def test_http_error_contains_status_only(self):
        self.server.response_status = 401
        self.server.response_body = b"private-response-body"
        with self.assertRaisesRegex(ValueError, "401") as error:
            fetch_source(configured(self.url + "?token=private-query"), NOW)
        self.assertNotIn("private", str(error.exception))

    def test_remote_response_is_bounded(self):
        self.server.response_body = b" " * (MAX_RESPONSE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "2 MiB"):
            fetch_source(configured(self.url), NOW)

    def test_remote_atom_relative_link_is_resolved(self):
        self.server.response_body = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>entry-1</id><title>Results</title><updated>2026-10-22T00:00:00Z</updated><link href="/results" /></entry></feed>'
        result, = fetch_source(configured(self.url, kind="rss", ticker="example"), NOW)
        self.assertEqual(result["citation"], self.url.rsplit("/", 1)[0] + "/results")


if __name__ == "__main__":
    unittest.main()
