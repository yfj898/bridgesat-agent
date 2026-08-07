from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class FetchError(RuntimeError):
    """Raised for blocked or failed remote fetches."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


@dataclass(frozen=True, slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    local_path: str
    sha256: str
    size_bytes: int
    content_type: str
    fetched_at: str
    reused: bool = False


class SafeFetcher:
    def __init__(
        self,
        *,
        user_agent: str = "BridgeSATDataBot/0.1 (governed educational acquisition)",
        timeout_seconds: float = 30.0,
        max_redirects: int = 5,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_redirects = max_redirects
        self._last_fetch_by_host: dict[str, float] = {}
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    @staticmethod
    def _validate_url(url: str, allowed_hosts: set[str]) -> urllib.parse.ParseResult:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise FetchError("only absolute HTTPS URLs are permitted")
        host = parsed.hostname.lower()
        if host not in allowed_hosts:
            raise FetchError(f"host is not in the source allowlist: {host}")
        try:
            addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise FetchError(f"DNS resolution failed for {host}") from exc
        for address_info in addresses:
            address = ipaddress.ip_address(address_info[4][0])
            if any((
                address.is_private,
                address.is_loopback,
                address.is_link_local,
                address.is_reserved,
                address.is_multicast,
                address.is_unspecified,
            )):
                raise FetchError(f"blocked non-public destination: {address}")
        return parsed

    def _wait(self, host: str, interval_seconds: float) -> None:
        previous = self._last_fetch_by_host.get(host)
        if previous is not None:
            remaining = interval_seconds - (time.monotonic() - previous)
            if remaining > 0:
                time.sleep(remaining)
        self._last_fetch_by_host[host] = time.monotonic()

    def _open(
        self,
        url: str,
        *,
        allowed_hosts: set[str],
        interval_seconds: float,
    ) -> tuple[object, str]:
        current = url
        for _ in range(self.max_redirects + 1):
            parsed = self._validate_url(current, allowed_hosts)
            self._wait(parsed.hostname or "", interval_seconds)
            request = urllib.request.Request(
                current,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json, application/gzip, application/zip, text/plain, text/csv, text/html, */*;q=0.5",
                },
            )
            try:
                return self._opener.open(request, timeout=self.timeout_seconds), current
            except urllib.error.HTTPError as exc:
                if 300 <= exc.code < 400 and exc.headers.get("Location"):
                    current = urllib.parse.urljoin(current, exc.headers["Location"])
                    continue
                raise FetchError(f"HTTP {exc.code} while fetching {current}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise FetchError(f"network error while fetching {current}: {exc}") from exc
        raise FetchError(f"too many redirects while fetching {url}")

    def download(
        self,
        url: str,
        destination: str | Path,
        *,
        allowed_hosts: set[str],
        max_bytes: int,
        interval_seconds: float = 1.0,
        reuse_existing: bool = True,
    ) -> FetchResult:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if reuse_existing and destination.exists():
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            return FetchResult(
                requested_url=url,
                final_url=url,
                local_path=str(destination),
                sha256=digest,
                size_bytes=destination.stat().st_size,
                content_type="application/octet-stream",
                fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(destination.stat().st_mtime)),
                reused=True,
            )

        response, final_url = self._open(url, allowed_hosts=allowed_hosts, interval_seconds=interval_seconds)
        temporary = destination.with_suffix(destination.suffix + ".part")
        digest = hashlib.sha256()
        size = 0
        try:
            with response, temporary.open("wb") as output:
                content_type = response.headers.get("Content-Type", "application/octet-stream")
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise FetchError(f"response exceeds {max_bytes} bytes: {url}")
                    digest.update(chunk)
                    output.write(chunk)
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

        return FetchResult(
            requested_url=url,
            final_url=final_url,
            local_path=str(destination),
            sha256=digest.hexdigest(),
            size_bytes=size,
            content_type=content_type,
            fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
