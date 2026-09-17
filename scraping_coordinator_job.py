"""Lease lifecycle, heartbeat and ETA tracking for shared scrape jobs."""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from scraping_coordinator_core import (
    CoordinatorError, CoordinatorUnavailableError, _format_eta, _utc_now_iso,
)
if TYPE_CHECKING:
    from scraping_coordinator_core import ScrapeCoordinatorClient

class BypassJobLease:
    """Explicit emergency bypass.  It is never selected automatically."""

    bypassed = True

    def __init__(self, account_id: str, job_type: str, total: int | None = None):
        self.account_id = account_id
        self.job_type = job_type
        self.total = total
        self.job_id = f"bypass-{uuid.uuid4()}"

    def __enter__(self) -> "BypassJobLease":
        print(
            "WARNING: SCRAPE_COORDINATOR_BYPASS=1; shared account locking is disabled "
            f"for {self.account_id}."
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def progress_callback(self, **kwargs: Any) -> None:
        return None

    def set_message(self, message: str) -> None:
        return None

    def assert_healthy(self) -> None:
        return None


class JobLease:
    bypassed = False

    def __init__(
        self,
        *,
        client: ScrapeCoordinatorClient,
        account_id: str,
        job_type: str,
        total: int | None = None,
        description: str | None = None,
    ):
        self.client = client
        self.account_id = str(account_id).strip()
        self.job_type = str(job_type).strip()
        self.total = int(total) if total is not None else None
        self.description = str(description or "").strip()
        if not self.account_id:
            raise ValueError("account_id is required")
        if not self.job_type:
            raise ValueError("job_type is required")

        self.job_id = str(uuid.uuid4())
        self._acquired = False
        self._stop_event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._completed = 0
        self._message = self.description
        self._eta_at: str | None = None
        self._started_monotonic: float | None = None
        self._eta_baseline_completed = 0
        self._last_progress_push = 0.0
        self._heartbeat_failures = 0
        self._health_error: Exception | None = None

    def __enter__(self) -> "JobLease":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        final_status = "completed" if exc_type is None else "failed"
        message = self._message or ("Completed" if exc_type is None else "Failed")
        error = None if exc is None else f"{type(exc).__name__}: {exc}"
        try:
            self.release(status=final_status, message=message, error=error)
        except Exception as release_exc:
            print(f"WARNING: failed to release coordinator job {self.job_id}: {release_exc}")
        return False

    def acquire(self) -> None:
        identity = self.client.identity
        response = self.client.transport.post(
            "reserve",
            job_id=self.job_id,
            account_id=self.account_id,
            operator=identity.operator,
            host=identity.host,
            pid=identity.pid,
            job_type=self.job_type,
            total=self.total,
            message=self.description,
        )
        self._wait_until_running(response)
        self._acquired = True
        self._started_monotonic = time.monotonic()
        self._eta_baseline_completed = 0
        self._start_heartbeat_thread()
        self._send_heartbeat(force=True)

    def _wait_until_running(self, response: dict[str, Any]) -> None:
        last_summary: tuple[Any, ...] | None = None
        while True:
            job = response.get("job") or {}
            status = str(job.get("status") or "").lower()
            if status == "running":
                print(
                    f"Coordinator lock acquired: account={self.account_id}, "
                    f"job={self.job_type}, job_id={self.job_id}"
                )
                return
            if status not in {"queued", "waiting"}:
                raise CoordinatorError(f"Unexpected coordinator job status: {status or 'unknown'}")

            current = response.get("current_job") or {}
            queue_position = response.get("queue_position")
            summary = (
                queue_position,
                current.get("operator"),
                current.get("job_type"),
                current.get("completed"),
                current.get("total"),
                current.get("eta_at"),
            )
            if summary != last_summary:
                holder = current.get("operator") or "unknown"
                current_type = current.get("job_type") or "unknown"
                progress = ""
                if current.get("total") not in (None, "", 0, "0"):
                    progress = f", progress={current.get('completed', 0)}/{current.get('total')}"
                eta = _format_eta(current.get("eta_at"))
                print(
                    f"Account {self.account_id} is busy: {holder} / {current_type}{progress}, "
                    f"ETA={eta}. Queue position={queue_position}. Waiting..."
                )
                last_summary = summary

            try:
                time.sleep(self.client.poll_interval_seconds)
            except KeyboardInterrupt:
                try:
                    self.client.transport.post(
                        "release",
                        job_id=self.job_id,
                        status="cancelled",
                        message="Cancelled while waiting",
                    )
                finally:
                    raise
            response = self.client.transport.post("poll", job_id=self.job_id)

    def _start_heartbeat_thread(self) -> None:
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"scrape-coordinator-{self.account_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self.client.heartbeat_interval_seconds):
            try:
                self._send_heartbeat(force=True)
                self._heartbeat_failures = 0
            except Exception as exc:
                self._heartbeat_failures += 1
                print(
                    f"WARNING: coordinator heartbeat failed "
                    f"({self._heartbeat_failures}/{self.client.heartbeat_failure_limit}): {exc}"
                )
                if self._heartbeat_failures >= self.client.heartbeat_failure_limit:
                    self._health_error = CoordinatorUnavailableError(
                        "Coordinator heartbeat repeatedly failed; stopping is required before the lease expires"
                    )

    def _snapshot(self) -> tuple[int, int | None, str | None, str]:
        with self._state_lock:
            return self._completed, self.total, self._eta_at, self._message

    def _send_heartbeat(self, *, force: bool = False) -> None:
        if not self._acquired and not force:
            return
        completed, total, eta_at, message = self._snapshot()
        self.client.transport.post(
            "heartbeat",
            job_id=self.job_id,
            completed=completed,
            total=total,
            eta_at=eta_at,
            message=message,
        )
        self._last_progress_push = time.monotonic()

    def assert_healthy(self) -> None:
        if self._health_error is not None:
            raise self._health_error

    def set_message(self, message: str) -> None:
        with self._state_lock:
            self._message = str(message or "")

    def progress_callback(
        self,
        *,
        completed: int | None = None,
        total: int | None = None,
        result: dict[str, Any] | None = None,
        stage: str | None = None,
        message: str | None = None,
        **_: Any,
    ) -> None:
        """Update in-memory progress and periodically push it to the Sheet."""
        self.assert_healthy()
        now_mono = time.monotonic()
        with self._state_lock:
            if total is not None:
                self.total = max(0, int(total))
            if completed is not None:
                self._completed = max(0, int(completed))
            if message is not None:
                self._message = str(message)
            elif result is not None:
                llino = result.get("llino")
                if llino is not None:
                    self._message = f"Processed LLI {llino}"

            if stage == "local_skip":
                self._eta_baseline_completed = self._completed
                self._started_monotonic = now_mono
                self._eta_at = None
            elif self._started_monotonic is not None:
                work_completed = self._completed - self._eta_baseline_completed
                elapsed = now_mono - self._started_monotonic
                if (
                    self.total is not None
                    and work_completed >= 2
                    and elapsed >= 10.0
                    and self.total > self._completed
                ):
                    seconds_per_item = elapsed / work_completed
                    remaining = self.total - self._completed
                    eta = datetime.now(timezone.utc) + timedelta(seconds=seconds_per_item * remaining)
                    self._eta_at = eta.isoformat().replace("+00:00", "Z")
                elif self.total is not None and self._completed >= self.total:
                    self._eta_at = _utc_now_iso()

        should_push = (
            stage == "local_skip"
            or (self.total is not None and self._completed >= self.total)
            or now_mono - self._last_progress_push >= self.client.progress_push_interval_seconds
        )
        if should_push:
            self._send_heartbeat(force=True)

    def release(
        self,
        *,
        status: str,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        self._stop_event.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.0)
        if not self._acquired:
            return
        completed, total, eta_at, stored_message = self._snapshot()
        self.client.transport.post(
            "release",
            job_id=self.job_id,
            status=status,
            completed=completed,
            total=total,
            eta_at=eta_at,
            message=message if message is not None else stored_message,
            error=error,
        )
        self._acquired = False
