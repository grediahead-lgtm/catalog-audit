"""Read-only audit of public product pages. Standard library only.

Rules: obey robots.txt (unknown = skip), never bypass protection (403/401 is "blocked", not "broken"), do not request
images from other hosts unless asked, stop at the request and time budget. Every field is an observation with its limits.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Optional

UA = "Mozilla/5.0 (compatible; catalog-audit/0.2; read-only)"
HTML_LIMIT = 1_000_000


class BudgetExceeded(Exception):
    pass


@dataclass
class Budget:
    max_requests: int = 200
    max_seconds: float = 120.0
    used: int = 0
    started: float = field(default_factory=time.monotonic)

    def spend(self):
        if self.used >= self.max_requests or time.monotonic() - self.started > self.max_seconds:
            raise BudgetExceeded("request or time budget exhausted")
        self.used += 1


class AddressRefused(Exception):
    pass


def check_address(url: str, allow_private: bool = False) -> None:
    """Only http(s), and no private, loopback, link-local, reserved or multicast addresses unless allow_private=True."""
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise AddressRefused(f"only http(s) urls: {url[:80]}")
    if allow_private:
        return
    for info in socket.getaddrinfo(p.hostname, p.port or 80, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise AddressRefused(f"private/local address refused: {p.hostname}")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect cannot lead to an internal address either."""

    def __init__(self, allow_private: bool):
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_address(newurl, self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    """HTTP access with a certificate-checking context; returns (status, headers, body, error). Never raises on HTTP errors.
    Every URL and every redirect target is checked: private and local addresses are refused unless allow_private=True."""

    def __init__(self, budget: Optional[Budget] = None, timeout: float = 10.0, cafile: Optional[str] = "/etc/ssl/cert.pem",
                 allow_private: bool = False):
        self.budget = budget or Budget()
        self.timeout = timeout
        self.allow_private = allow_private
        try:
            self.ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
        except OSError:
            self.ctx = ssl.create_default_context()
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self.ctx), _CheckedRedirects(allow_private))

    def request(self, url: str, method: str = "GET", headers: Optional[dict] = None, limit: int = HTML_LIMIT):
        self.budget.spend()
        req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **(headers or {})})
        try:
            check_address(url, self.allow_private)
            with self.opener.open(req, timeout=self.timeout) as r:
                body = r.read(limit + 1) if method == "GET" else b""
                return r.status, dict(r.headers), body, None
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), b"", None
        except Exception as e:  # noqa: BLE001 - timeouts, DNS, TLS: reported as error, not as a verdict about the shop
            return None, {}, b"", str(e)[:120]


def robots_state(fetcher: Fetcher, url: str):
    """('allow'|'deny'|'unknown', reason). Only 200 (parsed) and 404 (no robots file) are decisive; 401/403/5xx/error = unknown."""
    p = urllib.parse.urlsplit(url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    status, _, body, err = fetcher.request(robots_url)
    if err or status is None:
        return "unknown", f"robots.txt unavailable ({err})"
    if status == 404:
        return "allow", "no robots.txt (404)"
    if status != 200:
        return "unknown", f"robots.txt status {status}"
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(body.decode("utf-8", "replace").splitlines())
    return ("allow", "robots.txt allows") if rp.can_fetch(UA, url) else ("deny", "robots.txt disallows")


def classify_image(fetcher: Fetcher, url: str):
    """HEAD first; on 405/501 a tiny ranged GET. Only 404/410 count as 'not_found'; 401/403 are 'blocked' (check in a browser)."""
    status, headers, _, err = fetcher.request(url, "HEAD")
    if status in (405, 501):
        status, headers, _, err = fetcher.request(url, "GET", {"Range": "bytes=0-0"}, limit=1)
    if err or status is None:
        return "error", err
    if status in (404, 410):
        return "not_found", status
    if status in (401, 403):
        return "blocked", status
    if 200 <= status < 300 or status == 206:
        ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
        return ("ok", status) if ctype.startswith("image/") or not ctype else ("not_image", ctype)
    return "other", status


class _Text(HTMLParser):
    """Collects text of elements whose class or id mentions 'description'; ignores script/style/nav/header/footer/aside."""
    SKIP = {"script", "style", "nav", "header", "footer", "aside", "noscript"}

    def __init__(self):
        super().__init__()
        self.skip = 0
        self.stack = []
        self.desc = []
        self.depth_desc = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        is_desc = bool(re.search(r"descr", (a.get("class", "") + " " + a.get("id", "")), re.I)) and tag not in self.SKIP
        if tag in self.SKIP:
            self.skip += 1
        self.stack.append((tag, tag in self.SKIP, is_desc))
        if is_desc:
            self.depth_desc += 1

    def handle_endtag(self, tag):
        while self.stack:
            t, skip, is_desc = self.stack.pop()
            if skip:
                self.skip -= 1
            if is_desc:
                self.depth_desc -= 1
            if t == tag:
                break

    def handle_data(self, data):
        if self.depth_desc and not self.skip and data.strip():
            self.desc.append(data.strip())


def extract_description(html: str):
    """Characters of the text inside an element marked as description (class/id contains 'descr'); None if no such element."""
    p = _Text()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001
        return None
    return len(" ".join(p.desc)) if p.desc else None


def _walk(node, out):
    if isinstance(node, dict):
        t = node.get("@type")
        out.append(t if isinstance(t, list) else [t])
        for v in node.values():
            _walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out)


def find_product_schema(html: str):
    """True if a JSON-LD block declares @type Product (string or list, also nested in @graph). False means 'not detected'."""
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        types = []
        _walk(data, types)
        if any("Product" in (t or []) for t in types):
            return True
    return False


def audit_page(url: str, fetcher: Optional[Fetcher] = None, allow_external_images: bool = False, max_images: int = 6):
    f = fetcher or Fetcher()
    out = {"url": url}
    try:
        state, why = robots_state(f, url)
        out["robots"] = {"state": state, "why": why}
        if state != "allow":
            out["skipped"] = f"robots: {state}"
            return out
        status, _, body, err = f.request(url)
        if err or status is None:
            out["error"] = err
            return out
        out["http"] = status
        if status != 200:
            return out
        truncated = len(body) > HTML_LIMIT
        html = body[:HTML_LIMIT].decode("utf-8", "replace")
        out["html_truncated"] = truncated
        t = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        out["title"] = re.sub(r"\s+", " ", t.group(1)).strip()[:120] if t else None
        m = re.search(r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']*)', html, re.I)
        out["meta_description_chars"] = len(m.group(1)) if m else None   # None = no meta tag; does NOT prove an empty description
        out["description_chars"] = extract_description(html)             # None = no marked description element; verify manually
        out["product_schema_detected"] = find_product_schema(html)       # False = not detected by this method
        host = urllib.parse.urlsplit(url).netloc
        imgs, seen = [], set()
        for src in re.findall(r'<img[^>]+src=["\']([^"\']+)', html, re.I):
            full = urllib.parse.urljoin(url, src)
            if src.startswith("data:") or full in seen:
                continue
            seen.add(full)
            imgs.append(full)
        out["images_found"] = len(imgs)
        checked = []
        for img in imgs[:max_images]:
            if urllib.parse.urlsplit(img).netloc != host and not allow_external_images:
                checked.append({"url": img[:120], "result": "skipped_external_host"})
                continue
            rs, _ = robots_state(f, img)
            if rs != "allow":
                checked.append({"url": img[:120], "result": f"skipped_robots_{rs}"})
                continue
            res, detail = classify_image(f, img)
            checked.append({"url": img[:120], "result": res, "detail": detail})
        out["images_checked"] = checked
        out["images_not_found"] = [c["url"] for c in checked if c["result"] == "not_found"]
    except BudgetExceeded as e:
        out["stopped"] = str(e)
    return out
