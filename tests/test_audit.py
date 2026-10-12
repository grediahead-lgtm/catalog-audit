import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from catalog_audit import Budget, Fetcher, audit_page, classify_image, extract_description, find_product_schema, robots_state  # noqa: E402


class Site:
    """Tiny local site: routes[(method, path)] = (status, content_type, body). Records every request."""

    def __init__(self, routes):
        self.routes, self.hits = routes, []
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def handle_one(self):
                site.hits.append((self.command, self.path))
                status, ctype, body = site.routes.get((self.command, self.path)) or site.routes.get(("*", self.path)) or (404, "text/plain", b"")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body) if self.command != "HEAD" else 0))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = do_HEAD = handle_one

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def fetcher(**kw):
    return Fetcher(Budget(**kw) if kw else None, timeout=3, cafile=None)


PAGE = b"""<html><head><title>Lounge</title><meta name="description" content="short"></head><body>
<nav>menu menu menu menu menu menu menu menu</nav>
<div class="product-description">Real product text.</div>
<footer>Long footer text that must not count as a description. Long footer text that must not count.</footer>
<img src="/img/a.jpg"></body></html>"""


class Robots(unittest.TestCase):
    def check(self, robots, expect):
        s = Site({("GET", "/robots.txt"): robots} if robots else {})
        try:
            self.assertEqual(robots_state(fetcher(), s.base + "/p")[0], expect)
        finally:
            s.close()

    def test_allow_deny_and_missing(self):
        self.check((200, "text/plain", b"User-agent: *\nDisallow:\n"), "allow")
        self.check((200, "text/plain", b"User-agent: *\nDisallow: /\n"), "deny")
        self.check(None, "allow")                                     # 404: no robots file

    def test_blocked_or_broken_robots_is_unknown_and_page_skipped(self):
        for code in (401, 403, 500, 503):
            s = Site({("GET", "/robots.txt"): (code, "text/plain", b""), ("GET", "/p"): (200, "text/html", PAGE)})
            try:
                self.assertEqual(robots_state(fetcher(), s.base + "/p")[0], "unknown", code)
                r = audit_page(s.base + "/p", fetcher())
                self.assertIn("skipped", r)
                self.assertNotIn(("GET", "/p"), s.hits)               # the page itself was never requested
            finally:
                s.close()


class Images(unittest.TestCase):
    def run_case(self, routes):
        s = Site(routes)
        try:
            return classify_image(fetcher(), s.base + "/img/a.jpg")
        finally:
            s.close()

    def test_head_405_then_get_200_is_not_broken(self):
        r = self.run_case({("HEAD", "/img/a.jpg"): (405, "text/plain", b""), ("GET", "/img/a.jpg"): (206, "image/jpeg", b"x")})
        self.assertEqual(r[0], "ok")

    def test_404_is_not_found_403_is_blocked(self):
        self.assertEqual(self.run_case({("HEAD", "/img/a.jpg"): (404, "text/plain", b"")})[0], "not_found")
        self.assertEqual(self.run_case({("HEAD", "/img/a.jpg"): (403, "text/plain", b"")})[0], "blocked")

    def test_non_image_content_type_flagged(self):
        self.assertEqual(self.run_case({("HEAD", "/img/a.jpg"): (200, "text/html", b"")})[0], "not_image")


class Extraction(unittest.TestCase):
    def test_description_ignores_footer_and_nav(self):
        n = extract_description(PAGE.decode())
        self.assertEqual(n, len("Real product text."))

    def test_no_description_element_is_none_not_zero(self):
        self.assertIsNone(extract_description("<html><body><footer>lots of text here</footer></body></html>"))

    def test_product_schema_variants(self):
        wrap = lambda d: f'<script type="application/ld+json">{json.dumps(d)}</script>'  # noqa: E731
        self.assertTrue(find_product_schema(wrap({"@type": "Product"})))
        self.assertTrue(find_product_schema(wrap({"@type": ["Product", "Thing"]})))
        self.assertTrue(find_product_schema(wrap({"@graph": [{"@type": "WebSite"}, {"@type": "Product"}]})))
        self.assertFalse(find_product_schema(wrap({"@type": "Organization"})))
        self.assertFalse(find_product_schema('<script type="application/ld+json">{broken</script>'))


class Pages(unittest.TestCase):
    def test_full_audit_reports_observations_with_limits(self):
        s = Site({("GET", "/robots.txt"): (404, "text/plain", b""), ("GET", "/p"): (200, "text/html", PAGE),
                  ("HEAD", "/img/a.jpg"): (404, "text/plain", b"")})
        try:
            r = audit_page(s.base + "/p", fetcher())
        finally:
            s.close()
        self.assertEqual((r["http"], r["title"], r["description_chars"]), (200, "Lounge", len("Real product text.")))
        self.assertEqual(r["meta_description_chars"], 5)
        self.assertFalse(r["product_schema_detected"])
        self.assertEqual(len(r["images_not_found"]), 1)

    def test_external_image_host_is_not_requested_by_default(self):
        other = Site({("HEAD", "/x.jpg"): (200, "image/jpeg", b"")})
        page = f'<html><body><img src="{other.base}/x.jpg"></body></html>'.encode()
        s = Site({("GET", "/robots.txt"): (404, "text/plain", b""), ("GET", "/p"): (200, "text/html", page)})
        try:
            r = audit_page(s.base + "/p", fetcher())
            self.assertEqual(r["images_checked"][0]["result"], "skipped_external_host")
            self.assertEqual(other.hits, [])
            audit_page(s.base + "/p", fetcher(), allow_external_images=True)
            self.assertTrue(any(h[0] == "HEAD" and h[1] == "/x.jpg" for h in other.hits))
        finally:
            s.close()
            other.close()

    def test_request_budget_stops_the_audit(self):
        s = Site({("GET", "/robots.txt"): (404, "text/plain", b""), ("GET", "/p"): (200, "text/html", PAGE)})
        try:
            r = audit_page(s.base + "/p", fetcher(max_requests=2))
        finally:
            s.close()
        self.assertIn("stopped", r)
        self.assertLessEqual(len(s.hits), 2)

    def test_non_200_page_is_reported_without_further_requests(self):
        s = Site({("GET", "/robots.txt"): (404, "text/plain", b""), ("GET", "/p"): (403, "text/html", b"")})
        try:
            r = audit_page(s.base + "/p", fetcher())
        finally:
            s.close()
        self.assertEqual(r["http"], 403)
        self.assertNotIn("images_checked", r)


if __name__ == "__main__":
    unittest.main()
