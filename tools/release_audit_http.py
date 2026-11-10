"""Bounded GET-only transport for two fixed release evidence hosts."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import http.client
import os
import time
from typing import Protocol

from tools.release_audit_model import JSON, AuditError
from tools.release_audit_parse import integer, load, mapping, sequence


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: str


class Fetcher(Protocol):
    def get(
        self, host: str, path: str, accept: str = "application/vnd.github+json"
    ) -> Response: ...


class HTTPFetcher:
    def __init__(self, timeout: float = 20, deadline_seconds: float = 300) -> None:
        if timeout <= 0 or deadline_seconds <= 0:
            raise AuditError("timeout and deadline must be positive")
        self.timeout = timeout
        self.deadline = time.monotonic() + deadline_seconds

    def headers(self, host: str, accept: str) -> dict[str, str]:
        headers = {"Accept": accept, "User-Agent": "release-audit/1"}
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if host == "api.github.com" and token:
            headers["Authorization"] = "Bearer " + token
        return headers

    def get(self, host: str, path: str, accept: str = "application/vnd.github+json") -> Response:
        if host not in {"api.github.com", "pypi.org"} or not path.startswith("/"):
            raise AuditError("only fixed GitHub/PyPI HTTPS hosts are allowed")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AuditError("overall collection deadline exceeded")
        try:
            with closing(
                http.client.HTTPSConnection(host, timeout=min(self.timeout, remaining))
            ) as c:
                c.request("GET", path, headers=self.headers(host, accept))
                with c.getresponse() as response:
                    chunks: list[bytes] = []
                    size = 0
                    while True:
                        remaining = self.deadline - time.monotonic()
                        if remaining <= 0:
                            raise AuditError("overall collection deadline exceeded")
                        if c.sock:
                            c.sock.settimeout(min(self.timeout, remaining))
                        block = response.read1(65536)
                        if not block:
                            break
                        size += len(block)
                        if size > 32 * 1024 * 1024:
                            raise AuditError("response exceeds 32 MiB evidence limit")
                        chunks.append(block)
                    return Response(response.status, b"".join(chunks).decode("utf-8"))
        except (OSError, http.client.HTTPException, UnicodeError):
            raise AuditError(f"HTTPS GET failed for {host}; diagnostics redacted") from None


def get_json(fetcher: Fetcher, host: str, path: str, *, provenance: bool = False) -> JSON:
    accept = (
        "application/vnd.pypi.integrity.v1+json" if provenance else "application/vnd.github+json"
    )
    response = fetcher.get(host, path, accept)
    if response.status == 404 and provenance:
        return None
    if response.status in {403, 429}:
        raise AuditError("HTTP rate limit/authorization error; check GH_TOKEN or GITHUB_TOKEN")
    if response.status != 200:
        raise AuditError(f"HTTP {response.status} from {host}; redirects are not followed")
    return load(response.body)


def pages(fetcher: Fetcher, path: str, key: str = "") -> list[JSON]:
    result: list[JSON] = []
    for page in range(1, 51):
        raw = get_json(fetcher, "api.github.com", f"{path}?per_page=100&page={page}")
        data = mapping(raw) if key else None
        items = sequence(data[key] if data is not None else raw)
        if data is not None and integer(data["total_count"]) > 5000:
            raise AuditError("pagination exceeds 50 pages of 100")
        result.extend(items)
        if len(items) < 100:
            if data is not None and len(result) != integer(data["total_count"]):
                raise AuditError("paginated inventory total_count changed or was truncated")
            return result
    raise AuditError("pagination exceeds 50 pages of 100")
