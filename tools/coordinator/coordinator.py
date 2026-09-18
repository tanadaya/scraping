"""Public helpers for Google Sheets + Apps Script scrape coordination.

Normal operation is fail-closed: set SCRAPE_COORDINATOR_URL and
SCRAPE_COORDINATOR_TOKEN on every scraping PC.
"""
from __future__ import annotations

import argparse
import os
import threading
import time
from contextlib import contextmanager
from typing import Any

try:
    from dotenv import load_dotenv
except Exception:  # optional convenience only
    load_dotenv = None
if load_dotenv is not None:
    load_dotenv()

from scraping_coordinator_core import (
    AppsScriptTransport, CoordinatorError, CoordinatorIdentity,
    CoordinatorLeaseLostError, CoordinatorUnavailableError,
    ScrapeCoordinatorClient, _format_eta, _truthy,
)
from scraping_coordinator_job import BypassJobLease, JobLease

def infer_seasearcher_account_id(config: dict[str, Any] | None = None) -> str:
    """Resolve account_1/account_2 without storing credentials in the coordinator.

    High-level Movement notebooks pass their dedicated login_user, so they are
    matched automatically against SEASEARCHER_LOGIN_USER_1/_2.  AIS/Vessels can
    either use the same users or set SEASEARCHER_ACCOUNT_ID explicitly.
    """
    cfg = config or {}
    explicit = str(cfg.get("coordinator_account_id") or "").strip()
    if explicit:
        return explicit

    configured_user = str(cfg.get("login_user") or os.getenv("SEASEARCHER_LOGIN_USER") or "").strip()
    user_1 = str(os.getenv("SEASEARCHER_LOGIN_USER_1") or "").strip()
    user_2 = str(os.getenv("SEASEARCHER_LOGIN_USER_2") or "").strip()
    matches = []
    if configured_user and user_1 and configured_user == user_1:
        matches.append("account_1")
    if configured_user and user_2 and configured_user == user_2:
        matches.append("account_2")
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise CoordinatorError(
            "The configured SeaSearcher username matches both account slots. "
            "Set coordinator_account_id in config or SEASEARCHER_ACCOUNT_ID explicitly."
        )

    env_account = str(os.getenv("SEASEARCHER_ACCOUNT_ID") or "").strip()
    if env_account:
        return env_account

    raise CoordinatorError(
        "Could not determine which shared SeaSearcher account is being used. "
        "Set SEASEARCHER_ACCOUNT_ID=account_1 or account_2 (or pass "
        "coordinator_account_id in the scraper config)."
    )


@contextmanager
def track_unique_function_calls(
    module: Any,
    function_name: str,
    job: JobLease | BypassJobLease,
    *,
    total: int,
    initial_completed: int = 0,
    key_arg_index: int = 1,
):
    """Temporarily wrap a scraper function and report unique completed targets.

    This keeps coordination instrumentation outside the large scraper utility
    modules. Movement/AIS pass ``llino`` as argument 1, so retries of the same
    target do not inflate progress.  The original function is always restored.
    """
    original = getattr(module, function_name)
    seen = set()
    state_lock = threading.Lock()
    total = max(0, int(total))
    initial_completed = max(0, min(total, int(initial_completed)))
    job.progress_callback(
        completed=initial_completed,
        total=total,
        stage="local_skip",
        message=f"Local cache check complete: {initial_completed} already handled",
    )

    def wrapped(*args: Any, **kwargs: Any):
        result = None
        key = None
        if len(args) > key_arg_index:
            key = args[key_arg_index]
        elif "llino" in kwargs:
            key = kwargs["llino"]
        try:
            result = original(*args, **kwargs)
            return result
        finally:
            normalized_key = repr(key) if key is not None else f"call-{time.monotonic_ns()}"
            with state_lock:
                first_completion = normalized_key not in seen
                if first_completion:
                    seen.add(normalized_key)
                    completed = min(total, initial_completed + len(seen))
                else:
                    completed = min(total, initial_completed + len(seen))
            if first_completion:
                job.progress_callback(
                    completed=completed,
                    total=total,
                    result=result if isinstance(result, dict) else None,
                    stage="scrape",
                )

    setattr(module, function_name, wrapped)
    try:
        yield
    except Exception:
        raise
    else:
        job.progress_callback(
            completed=total,
            total=total,
            stage="complete",
            message="Scraping completed",
        )
    finally:
        setattr(module, function_name, original)

def coordinated_job(
    *,
    account_id: str,
    job_type: str,
    total: int | None = None,
    description: str | None = None,
    operator: str | None = None,
) -> JobLease | BypassJobLease:
    """Create a fail-closed shared job lease from environment configuration."""
    if _truthy(os.getenv("SCRAPE_COORDINATOR_BYPASS")):
        return BypassJobLease(account_id=account_id, job_type=job_type, total=total)
    client = ScrapeCoordinatorClient.from_env(operator=operator)
    return client.job(
        account_id=account_id,
        job_type=job_type,
        total=total,
        description=description,
    )


def _print_status(status: dict[str, Any]) -> None:
    accounts = status.get("accounts") or []
    queued = status.get("queued_jobs") or []
    print("Accounts")
    for account in accounts:
        total = account.get("total")
        completed = account.get("completed") or 0
        progress = f"{completed}/{total}" if total not in (None, "", 0, "0") else "-"
        print(
            f"  {account.get('account_id')}: {account.get('state')} | "
            f"{account.get('operator') or '-'} | {account.get('job_type') or '-'} | "
            f"{progress} | ETA {_format_eta(account.get('eta_at'))}"
        )
    if queued:
        print("Queue")
        for job in queued:
            print(
                f"  {job.get('account_id')} #{job.get('queue_position')}: "
                f"{job.get('operator')} / {job.get('job_type')}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SeaSearcher shared scrape coordinator")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Show current account locks and queue")
    args = parser.parse_args(argv)
    if args.command == "status":
        client = ScrapeCoordinatorClient.from_env()
        _print_status(client.status())
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
