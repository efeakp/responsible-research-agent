"""HTTP access with an on-disk response cache and audit logging.

Every request is logged (URL, status, response hash). Responses are cached by
URL, so a run can be replayed exactly with `--offline`: same inputs, same
responses, same verdicts. That is what makes a run reproducible after the
live databases have changed.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from . import __version__
from .audit import AuditLog, sha256_text

USER_AGENT = f"ResponsibleResearchAgent/{__version__} (prototype)"


# Minimum seconds between live requests to a host, shared across the whole process.
# arXiv API terms of use: no more than one request every three seconds, one connection
# at a time (https://info.arxiv.org/help/api/tou.html). Cache hits are not throttled.
MIN_INTERVAL: dict[str, float] = {"export.arxiv.org": 3.0}
_last_request: dict[str, float] = {}
_host_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()
_clock = time.monotonic
_sleep = time.sleep


def _throttle(host: str) -> threading.Lock | None:
    """Wait until `host` may be called again. Returns the host's lock, held, so only one
    request to a throttled host is in flight at a time; the caller releases it."""
    interval = MIN_INTERVAL.get(host)
    if not interval:
        return None
    with _registry_lock:
        lock = _host_locks.setdefault(host, threading.Lock())
    lock.acquire()
    wait = _last_request.get(host, -1e9) + interval - _clock()
    if wait > 0:
        _sleep(wait)
    return lock


class OfflineMiss(Exception):
    pass


class BlockedURL(Exception):
    """A link in a manuscript points somewhere the checker must not fetch."""


def _resolve(host: str) -> list[str]:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})
    except (socket.gaierror, UnicodeError, OSError):
        return []


def check_public_url(url: str) -> None:
    """Refuse links to this machine or the local network. Manuscripts are untrusted input,
    and a link like http://localhost:11434/... must not make the server call itself."""
    u = httpx.URL(url)
    if u.scheme not in ("http", "https"):
        raise BlockedURL(f"only http and https links are checked (got {u.scheme or 'none'})")
    host = (u.host or "").lower()
    if not host:
        raise BlockedURL("link has no host")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
        raise BlockedURL(f"{host} is a local address")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        addresses = [ipaddress.ip_address(a.split("%")[0]) for a in _resolve(host)]
    for ip in addresses:
        if not ip.is_global:
            raise BlockedURL(f"{host} resolves to a private or local address ({ip})")


_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_OG_TITLE_RE = re.compile(rb"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)", re.IGNORECASE)
PROBE_BYTES = 256 * 1024
REDIRECTS = {301, 302, 303, 307, 308}


def _page_title(head: bytes) -> Optional[str]:
    m = _OG_TITLE_RE.search(head) or _TITLE_RE.search(head)
    if not m:
        return None
    t = html.unescape(m.group(1).decode("utf-8", errors="replace"))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:300] or None


class CachedClient:
    def __init__(self, cache_dir: Path, audit: AuditLog, offline: bool = False,
                 mailto: Optional[str] = None, transport: httpx.BaseTransport | None = None,
                 timeout: float = 20.0):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.audit = audit
        self.offline = offline
        ua = USER_AGENT + (f" mailto:{mailto}" if mailto else "")
        self.client = httpx.Client(headers={"User-Agent": ua}, timeout=timeout,
                                   transport=transport, follow_redirects=True)

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{sha256_text(url)[:32]}.json"

    def get(self, url: str, params: dict | None = None,
            headers: dict | None = None) -> tuple[int, str]:
        full = str(httpx.URL(url, params=params or {}))
        accept = (headers or {}).get("Accept")
        cp = self._cache_path(full + (f"|accept={accept}" if accept else ""))
        if cp.exists():
            cached = json.loads(cp.read_text(encoding="utf-8"))
            self.audit.log("http_get", url=full, status=cached["status"], cache="hit",
                           response_sha256=sha256_text(cached["body"]))
            return cached["status"], cached["body"]
        if self.offline:
            self.audit.log("http_get", url=full, status=None, cache="miss_offline")
            raise OfflineMiss(full)
        host = httpx.URL(full).host
        lock = _throttle(host)
        try:
            r = self.client.get(full, headers=headers)
            status, body = r.status_code, r.text
        except httpx.HTTPError as e:
            self.audit.log("http_error", url=full, error=type(e).__name__, detail=str(e)[:300])
            raise
        finally:
            if lock is not None:
                _last_request[host] = _clock()
                lock.release()
        # cache only definitive answers (2xx and 404); retry-worthy errors are not cached
        if status < 300 or status == 404:
            cp.write_text(json.dumps({"url": full, "status": status, "body": body}),
                          encoding="utf-8")
        self.audit.log("http_get", url=full, status=status, cache="miss",
                       response_sha256=sha256_text(body))
        return status, body

    def probe(self, url: str) -> dict:
        """Check that a link works, without downloading the document: status, final URL,
        content type and page title. Redirects are followed by hand so every hop is
        checked against private addresses. Cached and logged like every other request."""
        cp = self._cache_path("probe:" + url)
        if cp.exists():
            result = json.loads(cp.read_text(encoding="utf-8"))
            self.audit.log("http_probe", cache="hit", **result)
            return result
        if self.offline:
            self.audit.log("http_probe", url=url, cache="miss_offline")
            raise OfflineMiss(url)
        current, status, ctype, head, final = url, 0, "", b"", url
        try:
            for _ in range(6):
                check_public_url(current)
                host = httpx.URL(current).host
                lock = _throttle(host)
                try:
                    with self.client.stream("GET", current, follow_redirects=False, timeout=15,
                                            headers={"Accept": "text/html,application/pdf;q=0.9,*/*;q=0.5"}) as r:
                        if r.status_code in REDIRECTS and r.headers.get("location"):
                            current = str(r.url.join(r.headers["location"]))
                            continue
                        status, final = r.status_code, str(r.url)
                        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                        if status < 400 and ("html" in ctype or not ctype):
                            for chunk in r.iter_bytes():
                                head += chunk
                                if len(head) >= PROBE_BYTES or b"</title>" in head.lower():
                                    break
                    break
                finally:
                    if lock is not None:
                        _last_request[host] = _clock()
                        lock.release()
            else:
                raise httpx.TooManyRedirects("more than 5 redirects", request=None)
        except BlockedURL as e:
            self.audit.log("http_probe_blocked", url=current, reason=str(e))
            raise
        except httpx.HTTPError as e:
            self.audit.log("http_error", url=current, error=type(e).__name__, detail=str(e)[:300])
            raise
        result = {"url": url, "final_url": final, "status": status, "content_type": ctype,
                  "title": _page_title(head)}
        if status < 300 or status in (404, 410):
            cp.write_text(json.dumps(result), encoding="utf-8")
        self.audit.log("http_probe", cache="miss", **result)
        return result

    def post_json(self, url: str, payload: dict, headers: dict) -> tuple[int, str]:
        """Uncached POST (used for LLM calls; those are logged separately with prompt hashes)."""
        r = self.client.post(url, json=payload, headers=headers, timeout=600)  # local models on CPU can be slow
        return r.status_code, r.text
