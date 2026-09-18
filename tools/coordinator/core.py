"""HTTP transport and client configuration for the shared scraper coordinator."""
from __future__ import annotations

import getpass
import json
import os
import platform
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class CoordinatorError(RuntimeError):
    """Base coordinator error."""


class CoordinatorUnavailableError(CoordinatorError):
    """Raised when the shared coordinator cannot be reached."""


class CoordinatorLeaseLostError(CoordinatorError):
    """Raised when this process no longer owns the shared account lease."""


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _format_eta(value: Any) -> str:
    dt = _parse_iso(value)
    if dt is None:
        return "unknown"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


class AppsScriptTransport:
    """Tiny JSON-over-HTTP transport using only the Python standard library."""

    def __init__(self, url: str, token: str, timeout: float = 20.0):
        self.url = str(url or "").strip()
        self.token = str(token or "").strip()
        self.timeout = float(timeout)
        if not self.url:
            raise CoordinatorError("SCRAPE_COORDINATOR_URL is not configured")
        if not self.token:
            raise CoordinatorError("SCRAPE_COORDINATOR_TOKEN is not configured")

    def post(self, action: str, **payload: Any) -> dict[str, Any]:
        body = {
            "action": action,
            "token": self.token,
            **payload,
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise CoordinatorUnavailableError(f"Coordinator request failed: {exc}") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CoordinatorUnavailableError(
                f"Coordinator returned non-JSON response: {raw[:200]!r}"
            ) from exc

        if not isinstance(parsed, dict):
            raise CoordinatorUnavailableError("Coordinator returned an invalid response")
        if not parsed.get("ok", False):
            code = parsed.get("error_code") or "coordinator_error"
            message = parsed.get("error") or "unknown coordinator error"
            if code in {"lease_lost", "job_not_running", "job_not_found"}:
                raise CoordinatorLeaseLostError(message)
            raise CoordinatorError(message)
        return parsed


@dataclass
class CoordinatorIdentity:
    operator: str
    host: str
    pid: int

    @classmethod
    def current(cls, operator: str | None = None) -> "CoordinatorIdentity":
        resolved_operator = (
            str(operator or os.getenv("SCRAPE_OPERATOR") or "").strip()
            or getpass.getuser()
            or "unknown"
        )
        host = socket.gethostname() or platform.node() or "unknown-host"
        return cls(operator=resolved_operator, host=host, pid=os.getpid())


class ScrapeCoordinatorClient:
    def __init__(
        self,
        transport: AppsScriptTransport,
        identity: CoordinatorIdentity | None = None,
        *,
        poll_interval_seconds: float = 20.0,
        heartbeat_interval_seconds: float = 60.0,
        progress_push_interval_seconds: float = 15.0,
        heartbeat_failure_limit: int = 3,
    ):
        self.transport = transport
        self.identity = identity or CoordinatorIdentity.current()
        self.poll_interval_seconds = max(2.0, float(poll_interval_seconds))
        self.heartbeat_interval_seconds = max(5.0, float(heartbeat_interval_seconds))
        self.progress_push_interval_seconds = max(2.0, float(progress_push_interval_seconds))
        self.heartbeat_failure_limit = max(1, int(heartbeat_failure_limit))

    @classmethod
    def from_env(cls, *, operator: str | None = None) -> "ScrapeCoordinatorClient":
        url = os.getenv("SCRAPE_COORDINATOR_URL", "").strip()
        token = os.getenv("SCRAPE_COORDINATOR_TOKEN", "").strip()
        return cls(
            AppsScriptTransport(url, token),
            CoordinatorIdentity.current(operator),
            poll_interval_seconds=float(os.getenv("SCRAPE_COORDINATOR_POLL_SECONDS", "20")),
            heartbeat_interval_seconds=float(os.getenv("SCRAPE_COORDINATOR_HEARTBEAT_SECONDS", "60")),
            progress_push_interval_seconds=float(os.getenv("SCRAPE_COORDINATOR_PROGRESS_PUSH_SECONDS", "15")),
        )

    def job(
        self,
        *,
        account_id: str,
        job_type: str,
        total: int | None = None,
        description: str | None = None,
    ) -> "JobLease":
        from .job import JobLease
        return JobLease(
            client=self,
            account_id=account_id,
            job_type=job_type,
            total=total,
            description=description,
        )

    def status(self) -> dict[str, Any]:
        return self.transport.post("status")
