import base64
import csv
import gc
import json
import logging
import os
import random
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import polars as pl
from . import movement as base
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


logger = base.logger
tqdm = base.tqdm

COMPLETE_LOG_LEVEL = 25
if logging.getLevelName(COMPLETE_LOG_LEVEL) == f"Level {COMPLETE_LOG_LEVEL}":
    logging.addLevelName(COMPLETE_LOG_LEVEL, "DONE")


def _complete(self, message, *args, **kwargs):
    if self.isEnabledFor(COMPLETE_LOG_LEVEL):
        self._log(COMPLETE_LOG_LEVEL, message, args, **kwargs)


if not hasattr(logging.Logger, "complete"):
    logging.Logger.complete = _complete


DEFAULT_AIS_POSITIONS_CONFIG = {
    "period": "all",
    "local_time": False,
    "out_dir": "ais_positions/test/",
    "headless": False,
    "login_user": None,
    "login_password": None,
    "log_level": "INFO",
    "check_status": False,
    "skip_if_exists": True,
    "skip_if_known_no_data": True,
    "remember_no_data": True,
    "show_progress": True,
    "encoding": "utf-8-sig",
    "log_steps": False,
    "periodic_rest_enabled": True,
    "work_session_hours": 6,
    "work_session_random_minutes": 30,
    "rest_session_hours": 1,
    "rest_session_random_minutes": 15,
    # Deprecated compatibility settings. Generic timeouts no longer trigger login.
    "timeout_streak_relogin_threshold": 2,
    "retry_timeout_once_after_relogin": True,
    "retry_timeout_immediately": False,
    "timeout_immediate_retry_attempts": 1,
    "next_page_ready_retry_attempts": 2,
    "period_window_retry_attempts": 1,
    "period_window_retry_delay_seconds": (2.0, 5.0),
    "period_apply_timeout": 45,
    "page_transition_timeout": 45,
    "period_settle_seconds": (0.3, 1.0),
    "between_period_windows_delay_seconds": (2.0, 5.0),
    "page_transition_settle_seconds": (0.5, 1.5),
    "event_based_page_transition_waits": True,
    "event_grid_stable_timeout": 5,
    "log_timing_metrics": True,
    "reuse_items_per_page_1000": True,
    "chunk_long_periods": True,
    "period_chunk_days": 30,
    "period_chunk_order": "newest_first",
    "resume_period_windows": True,
    "stop_on_llino_error": False,
    "retry_failed_llino_after_wait": True,
    "failed_llino_retry_wait_minutes": 30,
    "failed_llino_retry_random_minutes": 5,
    "failed_llino_retry_max_attempts": 2,
    "relogin_on_confirmed_session_loss": True,
    "session_relogin_attempts": 1,
    "stop_on_access_block": True,
    "webdriver_restart_attempts": 0,
    "max_consecutive_failed_llinos": 3,
}
DEFAULT_AIS_SOURCE_CONFIG = {
    "source_mode": "split_files",
    "source_vessel_file": None,
    "dir_vessel": "vessel",
    "vessel_type_list": ["bulk", "container", "cruise", "generalcargo", "roro", "tanker", "vehicle"],
    "file_template": "vessels_202504_{vessel_type}.csv",
    "source_statuses": ["Live"],
}

_ACTIVE_STATUS = {"Live", "Unconfirmed Existence", "Dead"}
_AIS_REQUIRED_HEADERS = {
    "Nearest Place",
    "Date/Time",
    "Lat/Lng Position",
    "AIS Destination",
    "Speed Over Ground",
}
_AIS_OUTPUT_COLUMNS = [
    "Nearest Place",
    "Distance (nm)",
    "Date/Time",
    "Lat",
    "Lng",
    "AIS Destination",
    "Heading",
    "Speed over ground",
    "Draught (m)",
    "Course over ground",
    "Source Type",
    "Navigation status",
]
_SOURCE_MODE_SINGLE_FILE = "single_file"
_SOURCE_MODE_SPLIT_FILES = "split_files"
_REQUIRED_VESSEL_SOURCE_COLUMNS = {"LLI NO", "Status", "LLI Vessel Type"}
_UNKNOWN_VESSEL_TYPE = "(Unknown)"
_AIS_NO_DATA_CACHE_LOCK = threading.Lock()
_CHROMEDRIVER_PATH_LOCK = threading.Lock()
_CHROMEDRIVER_PATH: str | None = None


class AISPaginationEnd(Exception):
    """The UI moved past the last real page; this is not a transport timeout."""


_ACCESS_BLOCK_MARKERS = (
    "access denied",
    "too many requests",
    "rate limit",
    "temporarily blocked",
    "unusual traffic",
    "verify you are human",
    "are you a robot",
    "captcha",
    "request forbidden",
)


def _resolve_chromedriver_path() -> str:
    global _CHROMEDRIVER_PATH
    if _CHROMEDRIVER_PATH:
        return _CHROMEDRIVER_PATH
    with _CHROMEDRIVER_PATH_LOCK:
        if not _CHROMEDRIVER_PATH:
            cached = base._find_cached_chromedriver()
            if cached:
                _CHROMEDRIVER_PATH = cached
            else:
                try:
                    _CHROMEDRIVER_PATH = ChromeDriverManager().install()
                except PermissionError:
                    cached = base._find_cached_chromedriver()
                    if cached:
                        logger.warning(
                            "webdriver_manager could not replace chromedriver; using cached executable: %s",
                            cached,
                        )
                        _CHROMEDRIVER_PATH = cached
                    else:
                        raise
    return _CHROMEDRIVER_PATH


def _format_llino(llino) -> str:
    return str(int(llino)).zfill(8)


def _slugify_token(value, fallback: str = "value") -> str:
    text = str(value or "").strip()
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in text).strip("_")
    return slug or fallback


def _is_summary_log_level(config=None) -> bool:
    if not config:
        return False
    return str(config.get("log_level", "")).upper() == "SUMMARY"


def _emit_summary_line(result: dict, worker_name: str = "worker") -> None:
    llino = result.get("llino")
    elapsed = result.get("elapsed")
    elapsed_text = f"{elapsed:.1f}s" if isinstance(elapsed, (int, float)) else "-"

    if result.get("ok") and result.get("skipped"):
        note = result.get("note", "skipped")
        message = f"[{worker_name}] llino={llino} skipped ({note}) elapsed={elapsed_text}"
    elif result.get("ok"):
        pages = result.get("pages", 0)
        message = f"[{worker_name}] llino={llino} completed ({pages} pages) elapsed={elapsed_text}"
    else:
        error = result.get("error") or result.get("error_detail") or "error"
        message = f"[{worker_name}] llino={llino} failed ({error}) elapsed={elapsed_text}"

    print(message, flush=True)


def _coerce_delay_range(value, default=(0.0, 0.0)) -> tuple[float, float]:
    if value is None:
        value = default

    if isinstance(value, dict):
        low = value.get("min", value.get("from", 0.0))
        high = value.get("max", value.get("to", low))
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        low, high = value[0], value[1]
    else:
        low = high = value

    try:
        low = max(0.0, float(low))
        high = max(0.0, float(high))
    except (TypeError, ValueError):
        low, high = default

    if high < low:
        low, high = high, low
    return low, high


def _sleep_configured_delay(config, key: str, label: str | None = None) -> float:
    cfg = config or {}
    default = DEFAULT_AIS_POSITIONS_CONFIG.get(key, (0.0, 0.0))
    low, high = _coerce_delay_range(cfg.get(key, default), default=default)
    delay = random.uniform(low, high) if high > low else low
    if delay <= 0:
        return 0.0

    if bool(cfg.get("log_steps", False)) and label:
        logger.info("%s: waiting %.1fs", label, delay)
    time.sleep(delay)
    return delay


def _add_timing_metric(metrics: dict, key: str, started_at: float) -> None:
    metrics[key] = metrics.get(key, 0.0) + max(0.0, time.monotonic() - started_at)


def _format_timing_metrics(metrics: dict) -> str:
    ordered_keys = ["open", "period", "page_size", "response", "pagination", "merge"]
    parts = []
    for key in ordered_keys:
        if key in metrics:
            parts.append(f"{key}={metrics[key]:.1f}s")
    for key in sorted(set(metrics) - set(ordered_keys)):
        parts.append(f"{key}={metrics[key]:.1f}s")
    return ", ".join(parts)


def _log_timing_metrics(config, llino, period_label: str, pages: int, elapsed: float, metrics: dict) -> None:
    if not bool((config or {}).get("log_timing_metrics", DEFAULT_AIS_POSITIONS_CONFIG["log_timing_metrics"])):
        return
    logger.complete(
        "llino %s AIS timing period=%s pages=%s total=%.1fs (%s)",
        llino,
        period_label,
        pages,
        elapsed,
        _format_timing_metrics(metrics),
    )


def _get_periodic_rest_settings(config=None) -> dict:
    cfg = config or {}
    enabled = bool(cfg.get("periodic_rest_enabled", DEFAULT_AIS_POSITIONS_CONFIG["periodic_rest_enabled"]))
    work_session_hours = float(cfg.get("work_session_hours", DEFAULT_AIS_POSITIONS_CONFIG["work_session_hours"]))
    work_session_random_minutes = float(
        cfg.get(
            "work_session_random_minutes",
            DEFAULT_AIS_POSITIONS_CONFIG["work_session_random_minutes"],
        )
    )
    rest_session_hours = float(cfg.get("rest_session_hours", DEFAULT_AIS_POSITIONS_CONFIG["rest_session_hours"]))
    rest_session_random_minutes = float(
        cfg.get(
            "rest_session_random_minutes",
            DEFAULT_AIS_POSITIONS_CONFIG["rest_session_random_minutes"],
        )
    )
    return {
        "enabled": enabled and work_session_hours > 0 and rest_session_hours > 0,
        "work_session_hours": max(0.0, work_session_hours),
        "work_session_random_minutes": max(0.0, work_session_random_minutes),
        "rest_session_hours": max(0.0, rest_session_hours),
        "rest_session_random_minutes": max(0.0, rest_session_random_minutes),
    }


def _get_timeout_relogin_settings(config=None) -> dict:
    cfg = config or {}
    threshold = int(
        cfg.get(
            "timeout_streak_relogin_threshold",
            DEFAULT_AIS_POSITIONS_CONFIG["timeout_streak_relogin_threshold"],
        )
    )
    return {
        "threshold": max(1, threshold),
        "retry_once": bool(
            cfg.get(
                "retry_timeout_once_after_relogin",
                DEFAULT_AIS_POSITIONS_CONFIG["retry_timeout_once_after_relogin"],
            )
        ),
    }


def _get_immediate_timeout_retry_settings(config=None) -> dict:
    cfg = config or {}
    return {
        "enabled": bool(
            cfg.get(
                "retry_timeout_immediately",
                DEFAULT_AIS_POSITIONS_CONFIG["retry_timeout_immediately"],
            )
        ),
        "attempts": max(
            0,
            int(
                cfg.get(
                    "timeout_immediate_retry_attempts",
                    DEFAULT_AIS_POSITIONS_CONFIG["timeout_immediate_retry_attempts"],
                )
            ),
        ),
    }


def _is_timeout_result(result) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("ok"):
        return False
    return (
        str(result.get("error", "")).strip().lower() == "timeout"
        or bool(result.get("chrome_page_error"))
        or bool(result.get("chrome_memory_error"))
        or bool(result.get("webdriver_command_timeout"))
        or bool(result.get("page_load_stuck"))
        or bool(result.get("renderer_timeout"))
    )


def _randomize_session_seconds(base_hours: float, random_minutes: float) -> float:
    base_seconds = max(0.0, float(base_hours)) * 3600.0
    jitter_seconds = max(0.0, float(random_minutes)) * 60.0
    if jitter_seconds <= 0:
        return base_seconds

    lower_bound = max(0.0, base_seconds - jitter_seconds)
    upper_bound = base_seconds + jitter_seconds
    return random.uniform(lower_bound, upper_bound)


def _randomize_minutes_seconds(base_minutes: float, random_minutes: float) -> float:
    base_seconds = max(0.0, float(base_minutes)) * 60.0
    jitter_seconds = max(0.0, float(random_minutes)) * 60.0
    if jitter_seconds <= 0:
        return base_seconds

    lower_bound = max(0.0, base_seconds - jitter_seconds)
    upper_bound = base_seconds + jitter_seconds
    return random.uniform(lower_bound, upper_bound)


def _initialize_periodic_rest_state(config=None) -> dict:
    rest_cfg = _get_periodic_rest_settings(config)
    return {
        "enabled": rest_cfg["enabled"],
        "continuous_work_started_at": time.monotonic(),
        "next_work_session_seconds": _randomize_session_seconds(
            rest_cfg["work_session_hours"],
            rest_cfg["work_session_random_minutes"],
        ) if rest_cfg["enabled"] else 0.0,
    }


def _take_periodic_rest_if_needed(
    *,
    worker_name: str,
    config=None,
    rest_state: dict,
    processed: int,
    total: int,
) -> dict:
    rest_cfg = _get_periodic_rest_settings(config)
    if not rest_cfg["enabled"]:
        return rest_state
    if processed >= total:
        return rest_state

    continuous_work_started_at = rest_state["continuous_work_started_at"]
    next_work_session_seconds = rest_state["next_work_session_seconds"]
    continuous_elapsed = time.monotonic() - continuous_work_started_at
    if continuous_elapsed < next_work_session_seconds:
        return rest_state

    work_hours = continuous_elapsed / 3600.0
    rest_seconds = _randomize_session_seconds(
        rest_cfg["rest_session_hours"],
        rest_cfg["rest_session_random_minutes"],
    )
    rest_hours = rest_seconds / 3600.0
    next_target_hours = next_work_session_seconds / 3600.0
    logger.complete(
        "[%s] taking a scheduled rest after %.2f hours of continuous scraping (target %.2f hours, rest %.2f hours)",
        worker_name,
        work_hours,
        next_target_hours,
        rest_hours,
    )
    time.sleep(rest_seconds)

    next_work_session_seconds = _randomize_session_seconds(
        rest_cfg["work_session_hours"],
        rest_cfg["work_session_random_minutes"],
    )
    logger.complete(
        "[%s] scheduled rest finished; next rest target is %.2f hours",
        worker_name,
        next_work_session_seconds / 3600.0,
    )
    return {
        "enabled": True,
        "continuous_work_started_at": time.monotonic(),
        "next_work_session_seconds": next_work_session_seconds,
    }


def _make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _period_label(period_cfg) -> str:
    if period_cfg is None:
        return "all"
    if isinstance(period_cfg, str) and period_cfg.lower() == "all":
        return "all"

    def _to_token(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.strftime("%Y%m%d")
        if isinstance(value, date):
            return value.strftime("%Y%m%d")
        if isinstance(value, str):
            candidate = value.strip()
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d"):
                try:
                    return datetime.strptime(candidate, fmt).strftime("%Y%m%d")
                except ValueError:
                    continue
        return None

    if isinstance(period_cfg, dict):
        start = _to_token(period_cfg.get("from"))
        end = _to_token(period_cfg.get("to"))
    elif isinstance(period_cfg, (list, tuple)) and len(period_cfg) == 2:
        start = _to_token(period_cfg[0])
        end = _to_token(period_cfg[1])
    else:
        start = _to_token(period_cfg)
        end = None

    if start and end:
        return f"{start}_{end}"
    if start:
        return start
    return "custom"


def _parse_period_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def _period_bounds(period_cfg) -> tuple[date, date] | None:
    if isinstance(period_cfg, dict):
        start = _parse_period_date(period_cfg.get("from"))
        end = _parse_period_date(period_cfg.get("to"))
    elif isinstance(period_cfg, (list, tuple)) and len(period_cfg) == 2:
        start = _parse_period_date(period_cfg[0])
        end = _parse_period_date(period_cfg[1])
    else:
        return None

    if start is None or end is None:
        return None
    if start > end:
        raise ValueError(f"AIS period start must be on or before end: {start} > {end}")
    return start, end


def build_ais_period_windows(period_cfg, max_days: int = 30, order: str = "newest_first") -> list[dict[str, str]]:
    bounds = _period_bounds(period_cfg)
    if bounds is None:
        return []

    start, end = bounds
    chunk_days = max(1, int(max_days))
    windows: list[dict[str, str]] = []
    current_end = end
    while current_end >= start:
        current_start = max(start, current_end - timedelta(days=chunk_days - 1))
        windows.append({"from": current_start.isoformat(), "to": current_end.isoformat()})
        current_end = current_start - timedelta(days=1)

    order_key = str(order or "newest_first").strip().lower()
    if order_key in {"newest_first", "newest", "desc", "descending"}:
        return windows
    if order_key in {"oldest_first", "oldest", "asc", "ascending"}:
        return list(reversed(windows))
    raise ValueError(f"Unsupported AIS period_chunk_order: {order}")


def _normalize_vessel_df(vessel_df: pl.DataFrame) -> pl.DataFrame:
    if "LLI Vessel Type" not in vessel_df.columns:
        vessel_df = vessel_df.with_columns(pl.lit(_UNKNOWN_VESSEL_TYPE).alias("LLI Vessel Type"))

    missing_columns = sorted(_REQUIRED_VESSEL_SOURCE_COLUMNS - set(vessel_df.columns))
    if missing_columns:
        raise ValueError(f"Missing required columns in source data: {missing_columns}")

    return vessel_df.with_columns(
        pl.col("LLI Vessel Type").cast(pl.Utf8, strict=False).fill_null(_UNKNOWN_VESSEL_TYPE)
    )


def _normalize_source_statuses(statuses) -> list[str] | None:
    if statuses is None:
        return ["Live"]
    if isinstance(statuses, str):
        candidate = statuses.strip()
        if candidate.lower() == "all":
            return None
        return [candidate] if candidate else ["Live"]

    normalized = [str(status).strip() for status in statuses if str(status).strip()]
    return normalized or ["Live"]


def load_ais_vessel_source(source_config=None) -> dict:
    cfg = {**DEFAULT_AIS_SOURCE_CONFIG, **(source_config or {})}
    source_mode = str(cfg.get("source_mode", _SOURCE_MODE_SPLIT_FILES)).strip().lower()

    if source_mode == _SOURCE_MODE_SINGLE_FILE:
        source_vessel_file = cfg.get("source_vessel_file")
        if not source_vessel_file:
            raise ValueError("source_vessel_file is required when source_mode='single_file'")
        source_path = Path(source_vessel_file)
        if not source_path.exists():
            raise FileNotFoundError(f"Vessel CSV not found: {source_path}")
        vessel_df = pl.read_csv(source_path, ignore_errors=True)
        source_description = str(source_path)
        source_name = source_path.stem
        file_path_dict = {}
    elif source_mode == _SOURCE_MODE_SPLIT_FILES:
        dir_vessel = Path(cfg["dir_vessel"])
        vessel_type_list = list(cfg["vessel_type_list"])
        file_template = str(cfg["file_template"])
        file_path_dict = {
            vessel_type: str(dir_vessel / file_template.format(vessel_type=vessel_type))
            for vessel_type in vessel_type_list
        }
        vessel_frames = []
        for vessel_type, file_path in file_path_dict.items():
            source_path = Path(file_path)
            if not source_path.exists():
                raise FileNotFoundError(f"Vessel CSV not found: {source_path}")
            frame = pl.read_csv(source_path, ignore_errors=True)
            if "LLI Vessel Type" not in frame.columns:
                frame = frame.with_columns(pl.lit(vessel_type).alias("LLI Vessel Type"))
            vessel_frames.append(frame)
        vessel_df = pl.concat(vessel_frames, how="diagonal_relaxed") if vessel_frames else pl.DataFrame()
        source_description = f"split_files ({len(file_path_dict)} files)"
        source_name = "split_files"
    else:
        raise ValueError(
            f"source_mode must be '{_SOURCE_MODE_SINGLE_FILE}' or '{_SOURCE_MODE_SPLIT_FILES}', got: {cfg.get('source_mode')}"
        )

    vessel_df = _normalize_vessel_df(vessel_df)
    source_statuses = _normalize_source_statuses(cfg.get("source_statuses"))
    live_vessel_df = vessel_df.filter(pl.col("Status") == "Live").with_columns(
        pl.col("LLI Vessel Type").fill_null(_UNKNOWN_VESSEL_TYPE)
    )
    target_vessel_df = vessel_df
    if source_statuses is not None:
        target_vessel_df = target_vessel_df.filter(pl.col("Status").is_in(source_statuses))
    target_vessel_df = target_vessel_df.with_columns(
        pl.col("LLI Vessel Type").fill_null(_UNKNOWN_VESSEL_TYPE)
    )
    vessel_type_list = (
        target_vessel_df
        .select("LLI Vessel Type")
        .unique()
        .sort("LLI Vessel Type")
        .to_series()
        .to_list()
    )
    llino_list_dict = {}
    for vessel_type in vessel_type_list:
        llino_list_dict[vessel_type] = (
            target_vessel_df
            .filter(pl.col("LLI Vessel Type") == vessel_type)
            .select(["LLI NO"])
            .sort("LLI NO")
            .to_series()
            .to_list()
        )

    return {
        "source_mode": source_mode,
        "source_name": source_name,
        "source_name_slug": _slugify_token(source_name, source_mode),
        "source_description": source_description,
        "file_path_dict": file_path_dict,
        "vessel_df": vessel_df,
        "live_vessel_df": live_vessel_df,
        "target_vessel_df": target_vessel_df,
        "source_statuses": source_statuses,
        "vessel_type_list": vessel_type_list,
        "llino_list_dict": llino_list_dict,
    }


def prepare_ais_run_context(source_config=None, run_vessel_type: str = "all", run_start: int = 0, run_end=None, out_dir=None) -> dict:
    source_ctx = load_ais_vessel_source(source_config)
    valid_run_vessel_types = ["all", *source_ctx["vessel_type_list"]]
    if run_vessel_type not in valid_run_vessel_types:
        raise ValueError(
            f"run_vessel_type must be one of {valid_run_vessel_types}, got: {run_vessel_type}"
        )

    if run_vessel_type == "all":
        selected_llinos = (
            source_ctx["target_vessel_df"]
            .select(["LLI NO"])
            .sort("LLI NO")
            .to_series()
            .to_list()
        )
    else:
        selected_llinos = source_ctx["llino_list_dict"][run_vessel_type]

    targets = selected_llinos[run_start:run_end]
    resolved_out_dir = out_dir or (
        f"ais_positions/test_{source_ctx['source_name_slug']}_{_slugify_token(run_vessel_type, 'all')}/"
    )
    summary = {
        "source_mode": source_ctx["source_mode"],
        "source_description": source_ctx["source_description"],
        "rows": source_ctx["vessel_df"].height,
        "live_rows": source_ctx["live_vessel_df"].height,
        "target_statuses": source_ctx["source_statuses"] or "all",
        "target_status_rows": source_ctx["target_vessel_df"].height,
        "vessel_type_count": len(source_ctx["vessel_type_list"]),
        "run_vessel_type": run_vessel_type,
        "selected_count": len(selected_llinos),
        "target_count": len(targets),
        "out_dir": resolved_out_dir,
    }

    return {
        **source_ctx,
        "run_vessel_type": run_vessel_type,
        "run_start": run_start,
        "run_end": run_end,
        "valid_run_vessel_types": valid_run_vessel_types,
        "selected_llinos": selected_llinos,
        "targets": targets,
        "out_dir": resolved_out_dir,
        "summary": summary,
    }


def _safe_click(driver, element) -> None:
    try:
        element.click()
        return
    except (ElementClickInterceptedException, ElementNotInteractableException, StaleElementReferenceException):
        pass

    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
    except Exception:
        pass

    try:
        driver.execute_script("arguments[0].click();", element)
    except Exception as exc:
        raise TimeoutException(f"Failed to click element: {exc}") from exc


def _normalize_text(value: str) -> str:
    return " ".join((value or "").split())


def _dismiss_transient_ui(driver) -> None:
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        try:
            body.send_keys(Keys.ESCAPE)
        except Exception:
            pass
        try:
            body.click()
        except Exception:
            pass
    except Exception:
        pass


def _save_debug_artifacts(driver, debug_base: Path, stem: str) -> dict[str, str]:
    debug_base.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, str] = {}

    screenshot_path = debug_base / f"{stem}.png"
    html_path = debug_base / f"{stem}.html"

    try:
        driver.save_screenshot(str(screenshot_path))
        artifacts["screenshot"] = str(screenshot_path)
    except Exception:
        logger.debug("Failed to save screenshot", exc_info=True)

    try:
        page_html = driver.execute_script("return document.documentElement.outerHTML;")
        html_path.write_text(page_html, encoding="utf-8")
        artifacts["html"] = str(html_path)
    except Exception:
        logger.debug("Failed to save page HTML", exc_info=True)

    return artifacts


def _driver_text_snapshot(driver, limit: int = 5000) -> tuple[str, str, str]:
    """Return URL, title and visible body text without exposing cookies or credentials."""
    try:
        current_url = str(driver.current_url or "")
    except Exception:
        current_url = ""
    try:
        title = str(driver.title or "")
    except Exception:
        title = ""
    try:
        body_text = str(
            driver.execute_script(
                "return document.body ? document.body.innerText.slice(0, arguments[0]) : '';",
                max(0, int(limit)),
            )
            or ""
        )
    except Exception:
        body_text = ""
    return current_url, title, body_text


def _is_login_page(driver) -> bool:
    current_url, title, _ = _driver_text_snapshot(driver, limit=1000)
    url_text = current_url.lower()
    title_text = title.strip().lower()
    if "signin" in url_text or "sign-in" in url_text:
        return True
    if title_text in {"sign in", "login", "log in"}:
        return True

    login_ids = (
        "loginPage:loginForm:loginemail",
        "loginPage:loginForm:loginpassword",
        "loginPage:loginForm:login-submit",
    )
    displayed_markers = 0
    for element_id in login_ids:
        try:
            for element in driver.find_elements(By.ID, element_id):
                if element.is_displayed():
                    displayed_markers += 1
                    break
        except Exception:
            continue
    return displayed_markers >= 2


def _detect_access_block(driver) -> str | None:
    current_url, title, body_text = _driver_text_snapshot(driver)
    haystack = "\n".join((current_url, title, body_text)).lower()
    for marker in _ACCESS_BLOCK_MARKERS:
        if marker in haystack:
            return marker
    if re.search(r"(?:^|\D)(?:403|429)(?:\D|$)", title):
        return title.strip() or "HTTP access restriction"
    return None


def _classify_driver_failure(driver) -> tuple[str, str | None]:
    access_marker = _detect_access_block(driver)
    if access_marker:
        return "access_blocked", access_marker
    if _is_login_page(driver):
        return "session_expired", "login page detected"
    return "transient_timeout", None


_AIS_UNRESPONSIVE_BROWSER_FLAGS = (
    "chrome_page_error",
    "chrome_memory_error",
    "renderer_timeout",
    "webdriver_command_timeout",
    "page_load_stuck",
)


def _ais_browser_failure_flags(driver, exc=None) -> dict[str, bool]:
    text = str(exc or "")
    is_chrome_page_error = bool(
        getattr(base, "_looks_like_chrome_page_error", lambda value: False)(text)
    )
    is_chrome_memory_error = bool(
        getattr(base, "_is_chrome_memory_error", lambda value: False)(text)
    )
    is_renderer_timeout = bool(getattr(base, "_is_renderer_timeout", lambda value: False)(text))
    is_webdriver_command_timeout = bool(
        getattr(base, "_is_webdriver_command_timeout", lambda value: False)(text)
    )
    is_page_load_stuck = bool(getattr(base, "_is_page_load_stuck", lambda value: False)(text))

    # Do not issue another WebDriver command once the exception text already
    # proves that Chrome/the renderer is unresponsive.  The worker can refresh
    # the same browser immediately instead.
    chrome_page_error = is_chrome_page_error
    if not any(
        (
            is_chrome_page_error,
            is_chrome_memory_error,
            is_renderer_timeout,
            is_webdriver_command_timeout,
            is_page_load_stuck,
        )
    ):
        try:
            chrome_page_error = chrome_page_error or bool(
                getattr(base, "_chrome_page_error_detail", lambda *_args, **_kwargs: None)(driver, exc)
            )
        except Exception:
            pass
    return {
        "chrome_page_error": chrome_page_error,
        "chrome_memory_error": is_chrome_memory_error,
        "renderer_timeout": is_renderer_timeout,
        "webdriver_command_timeout": is_webdriver_command_timeout,
        "page_load_stuck": is_page_load_stuck,
    }


def _ais_browser_is_unresponsive(failure_flags: dict[str, bool]) -> bool:
    return any(bool(failure_flags.get(key)) for key in _AIS_UNRESPONSIVE_BROWSER_FLAGS)


def _get_visible_grid_rows(driver) -> list:
    row_xpaths = [
        "//tr[contains(@class,'parent-row')]",
        "//tr[contains(@class,'lli-table__row')]",
        "//div[contains(@class,'lli-table__row')]",
    ]
    for xpath in row_xpaths:
        visible_rows = []
        for row in driver.find_elements(By.XPATH, xpath):
            try:
                if row.is_displayed():
                    visible_rows.append(row)
            except StaleElementReferenceException:
                continue
            except Exception:
                continue
        if visible_rows:
            return visible_rows
    return []


def _read_csv_with_fallback(path: Path) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path)


def _count_rows(driver) -> int:
    try:
        return len(_get_visible_grid_rows(driver))
    except Exception:
        return 0


def _read_total_found_count(driver) -> int | None:
    candidate_xpaths = [
        "//*[@data-testid='itemsTotalCount']",
        "//*[contains(@class,'lli-gridz__total-count')]",
    ]
    for xpath in candidate_xpaths:
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if not element.is_displayed():
                    continue
                text = _normalize_text(element.text)
                count_text = re.sub(r"(?<=\d)[,\s\u00a0](?=\d{3}\b)", "", text)
                of_match = re.search(r"\bof\s+(\d+)\b", count_text, flags=re.IGNORECASE)
                if of_match:
                    return int(of_match.group(1))
                matches = re.findall(r"\d+", count_text)
                if matches:
                    return int(matches[-1])
            except StaleElementReferenceException:
                continue
            except Exception:
                continue
    return None


def _read_pagination_state(driver) -> dict[str, int | None]:
    state = {"current_page": None, "total_pages": None}
    candidate_xpaths = [
        "//input[contains(@class,'lli-grid-pager__input')]",
        "//input[@type='number' and contains(@value,'')]",
    ]
    for xpath in candidate_xpaths:
        try:
            inputs = driver.find_elements(By.XPATH, xpath)
        except Exception:
            continue
        for page_input in inputs:
            try:
                if not page_input.is_displayed():
                    continue
                current_text = str(page_input.get_attribute("value") or "").strip()
                if not current_text.isdigit():
                    continue
                parent = page_input.find_element(By.XPATH, "..")
                parent_text = _normalize_text(parent.text)
                total_match = re.search(r"\bof\s+(\d+)\b", parent_text, flags=re.IGNORECASE)
                if total_match is None:
                    continue
                state["current_page"] = int(current_text)
                state["total_pages"] = int(total_match.group(1))
                return state
            except (NoSuchElementException, StaleElementReferenceException, ValueError):
                continue
            except Exception:
                continue
    return state


def _grid_reports_no_data(driver) -> bool:
    if _read_total_found_count(driver) == 0:
        return True

    no_data_markers = [
        "//*[contains(normalize-space(.), 'There is no data to display.')]",
        "//*[contains(normalize-space(.), 'No data to display')]",
    ]
    for xpath in no_data_markers:
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if element.is_displayed():
                    return True
            except StaleElementReferenceException:
                continue
            except Exception:
                continue
    return False


def _has_next_page_for_ais(driver, current_grid_state: dict | None, page_index: int) -> bool:
    next_button = _get_next_ais_page_button(driver)
    next_available = next_button is not None and not _is_ais_pager_button_disabled(next_button)
    if not current_grid_state:
        return next_available

    current_page = current_grid_state.get("current_page")
    total_pages = current_grid_state.get("total_pages")
    if isinstance(total_pages, int) and total_pages >= 0:
        effective_page = current_page if isinstance(current_page, int) else page_index
        return effective_page < total_pages

    total_count = current_grid_state.get("total_count")
    if isinstance(total_count, int) and total_count >= 0:
        needs_next_page = total_count > page_index * 1000
        if needs_next_page and not next_available:
            raise TimeoutException(
                f"AIS grid reports {total_count} rows, but Next Page is unavailable at page {page_index}"
            )
        return needs_next_page

    row_count = current_grid_state.get("row_count")
    if isinstance(row_count, int) and row_count < 1000:
        return False

    return next_available


def _grid_state_confirms_no_data(driver, grid_state: dict | None) -> bool:
    if grid_state and grid_state.get("total_count") == 0:
        return True
    return _grid_reports_no_data(driver)


def _get_ais_pager_button(driver, label: str):
    candidate_xpaths = [
        (
            "//li[contains(@class,'lli-grid-pager__number')"
            f" and normalize-space()='{label}']//button"
        ),
        f"//button[@aria-label='{label}']",
        f"//*[@role='button' and @aria-label='{label}']",
        f"//button[normalize-space()='{label}']",
    ]
    for xpath in candidate_xpaths:
        try:
            for button in driver.find_elements(By.XPATH, xpath):
                try:
                    if button.is_displayed():
                        return button
                except StaleElementReferenceException:
                    continue
        except Exception:
            continue
    return None


def _get_next_ais_page_button(driver):
    button = _get_ais_pager_button(driver, "Next Page")
    if button is None:
        button = _get_ais_pager_button(driver, "Next")
    return button


def _is_ais_pager_button_disabled(button) -> bool:
    if button is None:
        return True
    cls = button.get_attribute("class") or ""
    aria = (button.get_attribute("aria-disabled") or "").lower()
    disabled_attr = button.get_attribute("disabled")
    parent_cls = ""
    parent_aria = ""
    try:
        parent = button.find_element(By.XPATH, "./ancestor::*[self::li or self::button or @role='button'][1]")
        parent_cls = parent.get_attribute("class") or ""
        parent_aria = (parent.get_attribute("aria-disabled") or "").lower()
    except Exception:
        pass
    return (
        "lli-grid-pager__link--disabled" in cls
        or "lli-grid-pager__link--disabled" in parent_cls
        or aria == "true"
        or parent_aria == "true"
        or disabled_attr is not None
    )


def _click_ais_pager_button(driver, button) -> bool:
    if button is None or _is_ais_pager_button_disabled(button):
        return False
    try:
        WebDriverWait(driver, 5).until(EC.element_to_be_clickable(button))
    except TimeoutException:
        if _is_ais_pager_button_disabled(button):
            return False

    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'nearest'});", button)
    except Exception:
        pass

    try:
        button.click()
        return True
    except (ElementClickInterceptedException, StaleElementReferenceException):
        try:
            driver.execute_script("arguments[0].click();", button)
            return True
        except Exception:
            return False


def _click_next_ais_page(driver) -> bool:
    return _click_ais_pager_button(driver, _get_next_ais_page_button(driver))


def _return_to_first_ais_page(driver, current_grid_state: dict | None = None, config=None, max_hops: int = 100) -> dict:
    cfg = {**DEFAULT_AIS_POSITIONS_CONFIG, **(config or {})}
    transition_timeout = int(cfg.get("page_transition_timeout", DEFAULT_AIS_POSITIONS_CONFIG["page_transition_timeout"]))
    state = current_grid_state or _capture_grid_state(driver)
    for _ in range(max(1, int(max_hops))):
        previous_button = _get_ais_pager_button(driver, "Previous Page")
        if previous_button is None:
            previous_button = _get_ais_pager_button(driver, "Previous")
        if previous_button is None or _is_ais_pager_button_disabled(previous_button):
            return state

        before_click_state = state or _capture_grid_state(driver)
        if not _click_ais_pager_button(driver, previous_button):
            return state
        state = _wait_for_next_page_ready(
            driver,
            previous_state=before_click_state,
            timeout=transition_timeout,
        )
        stable_state = _wait_after_page_transition(driver, cfg)
        if stable_state is not None:
            state = stable_state

    raise TimeoutException("Could not return AIS grid to the first page")


def _capture_grid_state(driver) -> dict:
    visible_rows = _get_visible_grid_rows(driver)
    pagination_state = _read_pagination_state(driver)

    signature = []
    for row in visible_rows[:3]:
        try:
            signature.append(_normalize_text(row.text))
        except StaleElementReferenceException:
            signature.append("")

    return {
        "row_count": len(visible_rows),
        "signature": tuple(signature),
        "first_row": visible_rows[0] if visible_rows else None,
        "total_count": _read_total_found_count(driver),
        "current_page": pagination_state.get("current_page"),
        "total_pages": pagination_state.get("total_pages"),
    }


def _grid_state_changed(previous_state: dict | None, current_state: dict | None) -> bool:
    if previous_state is None or current_state is None:
        return False
    return (
        previous_state.get("row_count") != current_state.get("row_count")
        or previous_state.get("signature") != current_state.get("signature")
        or (
            previous_state.get("current_page") is not None
            and current_state.get("current_page") is not None
            and previous_state.get("current_page") != current_state.get("current_page")
        )
    )


def _wait_for_optional_staleness(row_element, timeout: int = 5) -> bool:
    if row_element is None:
        return False
    try:
        WebDriverWait(row_element.parent, timeout).until(EC.staleness_of(row_element))
        return True
    except TimeoutException:
        return False
    except Exception:
        return False


def _wait_for_loaded_grid(driver, timeout: int = 20, min_rows: int = 1) -> dict:
    deadline = time.time() + timeout
    stable_state = None
    stable_hits = 0
    while time.time() < deadline:
        candidate = _capture_grid_state(driver)
        if candidate.get("total_count") == 0 or _grid_reports_no_data(driver):
            return candidate
        if candidate["row_count"] < min_rows:
            stable_state = candidate
            stable_hits = 0
            time.sleep(0.35)
            continue
        if (
            stable_state is not None
            and candidate["row_count"] == stable_state["row_count"]
            and candidate["signature"] == stable_state["signature"]
        ):
            stable_hits += 1
            if stable_hits >= 2:
                return candidate
        else:
            stable_state = candidate
            stable_hits = 1
        time.sleep(0.35)
    return _capture_grid_state(driver)


def _wait_for_items_per_page_apply(driver, previous_state: dict | None, timeout: int = 20) -> dict:
    stale_observed = _wait_for_optional_staleness(
        previous_state.get("first_row") if previous_state else None,
        timeout=min(5, timeout),
    )
    current_state = _wait_for_loaded_grid(driver, timeout=timeout, min_rows=1)
    if current_state["row_count"] == 0:
        return current_state
    if stale_observed or _grid_state_changed(previous_state, current_state):
        return current_state

    deadline = time.time() + min(4, timeout)
    stable_hits = 0
    baseline = current_state
    last_candidate = current_state
    while time.time() < deadline:
        time.sleep(0.5)
        candidate = _capture_grid_state(driver)
        last_candidate = candidate
        if candidate["row_count"] == 0:
            return candidate
        if candidate["row_count"] == baseline["row_count"] and candidate["signature"] == baseline["signature"]:
            stable_hits += 1
            if stable_hits >= 2:
                return candidate
        else:
            baseline = candidate
            stable_hits = 1

    return last_candidate


def _wait_for_next_page_ready(driver, previous_state: dict, timeout: int = 20) -> dict:
    stale_observed = _wait_for_optional_staleness(previous_state.get("first_row"), timeout=min(1, timeout))
    current_state = _wait_for_loaded_grid(driver, timeout=timeout, min_rows=1)
    if current_state["row_count"] > 0 and (stale_observed or _grid_state_changed(previous_state, current_state)):
        return current_state

    deadline = time.time() + min(2, timeout)
    last_state = current_state
    while time.time() < deadline:
        time.sleep(0.5)
        candidate = _capture_grid_state(driver)
        last_state = candidate
        if candidate["row_count"] == 0:
            continue
        if stale_observed or _grid_state_changed(previous_state, candidate):
            return candidate

    current_page = last_state.get("current_page")
    total_pages = last_state.get("total_pages")
    if (
        last_state["row_count"] == 0
        and isinstance(current_page, int)
        and isinstance(total_pages, int)
        and current_page > total_pages
    ):
        raise AISPaginationEnd(f"AIS pagination moved past the last page ({current_page} of {total_pages})")
    if last_state["row_count"] == 0:
        raise TimeoutException("Next page loaded without visible rows")
    raise TimeoutException("Pagination moved but grid contents did not change")


def _has_saved_ais_positions_for_llino(llino, cfg=None) -> bool:
    cached_index = (cfg or {}).get("_saved_outputs_index")
    if isinstance(cached_index, set):
        return _format_llino(llino) in cached_index

    out_dir = Path((cfg or {}).get("out_dir", DEFAULT_AIS_POSITIONS_CONFIG["out_dir"]))
    if not out_dir.exists():
        return False
    token = _format_llino(llino)
    period_label = _period_label((cfg or {}).get("period"))
    legacy_pattern = f"ais_positions_{token}_{period_label}_*.csv"
    current_path = out_dir / f"ais_positions_{token}_{period_label}.csv"
    return current_path.exists() or any(out_dir.glob(legacy_pattern))


def _collect_saved_ais_positions_index(cfg=None) -> set[str]:
    out_dir = Path((cfg or {}).get("out_dir", DEFAULT_AIS_POSITIONS_CONFIG["out_dir"]))
    if not out_dir.exists():
        return set()

    period_label = _period_label((cfg or {}).get("period"))
    pattern = re.compile(
        rf"^ais_positions_(\d{{8}})_{re.escape(period_label)}(?:_.+)?\.csv$",
        re.IGNORECASE,
    )

    saved_tokens = set()
    try:
        for path in out_dir.iterdir():
            if not path.is_file():
                continue
            match = pattern.match(path.name)
            if match:
                saved_tokens.add(match.group(1))
    except FileNotFoundError:
        return set()

    return saved_tokens


def _build_already_exists_result(llino) -> dict:
    return {
        "llino": llino,
        "ok": True,
        "skipped": True,
        "status": None,
        "pages": 0,
        "elapsed": 0.0,
        "note": "already_exists",
    }


def _build_known_no_data_result(llino) -> dict:
    return {
        "llino": llino,
        "ok": True,
        "skipped": True,
        "status": None,
        "pages": 0,
        "elapsed": 0.0,
        "note": "known_no_data",
    }


def _ais_no_data_cache_path(cfg=None) -> Path:
    out_dir = Path((cfg or {}).get("out_dir", DEFAULT_AIS_POSITIONS_CONFIG["out_dir"]))
    period_label = _period_label((cfg or {}).get("period"))
    return out_dir / f".ais_positions_no_data_{period_label}.txt"


def _collect_known_no_data_index(cfg=None) -> set[str]:
    cache_path = _ais_no_data_cache_path(cfg)
    if not cache_path.exists():
        return set()

    known_tokens = set()
    try:
        with cache_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                token = line.strip()
                if re.fullmatch(r"\d{8}", token):
                    known_tokens.add(token)
    except FileNotFoundError:
        return set()

    return known_tokens


def _has_known_no_data_llino(llino, cfg=None) -> bool:
    cached_index = (cfg or {}).get("_known_no_data_index")
    if isinstance(cached_index, set):
        return _format_llino(llino) in cached_index
    return _format_llino(llino) in _collect_known_no_data_index(cfg)


def _remember_known_no_data_llino(llino, cfg=None) -> None:
    if not bool((cfg or {}).get("remember_no_data", True)):
        return

    token = _format_llino(llino)
    cached_index = (cfg or {}).setdefault("_known_no_data_index", set())
    cache_path = _ais_no_data_cache_path(cfg)

    with _AIS_NO_DATA_CACHE_LOCK:
        if token in cached_index:
            return
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(f"{token}\n")
        cached_index.add(token)


def _apply_upfront_local_skip_filter(llino_list, cfg=None):
    llino_list = list(llino_list or [])
    use_saved_skip = bool((cfg or {}).get("skip_if_exists", True))
    use_known_no_data_skip = bool((cfg or {}).get("skip_if_known_no_data", True))
    if not use_saved_skip and not use_known_no_data_skip:
        return llino_list, [], set(), set()

    saved_outputs_index = _collect_saved_ais_positions_index(cfg) if use_saved_skip else set()
    known_no_data_index = _collect_known_no_data_index(cfg) if use_known_no_data_skip else set()
    if not saved_outputs_index and not known_no_data_index:
        return llino_list, [], saved_outputs_index, known_no_data_index

    pending_llinos = []
    skipped_results = []
    for llino in llino_list:
        token = _format_llino(llino)
        if token in saved_outputs_index:
            skipped_results.append(_build_already_exists_result(llino))
        elif token in known_no_data_index:
            skipped_results.append(_build_known_no_data_result(llino))
        else:
            pending_llinos.append(llino)

    return pending_llinos, skipped_results, saved_outputs_index, known_no_data_index


def _order_results_like_input(llino_list, results):
    llino_list = list(llino_list or [])
    if not llino_list:
        return []

    result_queues = {}
    for result in results or []:
        llino = result.get("llino") if isinstance(result, dict) else None
        if llino is None:
            continue
        result_queues.setdefault(llino, []).append(result)

    ordered_results = []
    remainder = []
    for llino in llino_list:
        queue = result_queues.get(llino)
        if queue:
            ordered_results.append(queue.pop(0))

    for queue in result_queues.values():
        if queue:
            remainder.extend(queue)

    return ordered_results + remainder


def _remove_empty_parents(path: Path, stop_at: Path) -> None:
    current = path
    stop_at = stop_at.resolve()
    while True:
        try:
            if not current.exists():
                pass
            elif current.resolve() == stop_at:
                return
            elif any(current.iterdir()):
                return
            else:
                current.rmdir()
        except Exception:
            return
        current = current.parent


def initialize_driver(driver_opts=None, download_dir: str | Path | None = None):
    options = Options()
    options.page_load_strategy = (driver_opts or {}).get("page_load_strategy", "eager")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    headless = True if driver_opts is None else bool(driver_opts.get("headless", True))
    if headless:
        options.add_argument("--headless")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")

    if driver_opts and "extra_args" in driver_opts:
        for arg in driver_opts["extra_args"]:
            options.add_argument(arg)

    logger.info(
        "Starting Chrome WebDriver for AIS Positions (headless=%s)",
        headless,
    )
    # Let Selenium Manager select/download a driver that matches the
    # installed Chrome version.  Passing a cached webdriver-manager path
    # can silently select an obsolete driver after Chrome auto-updates.
    driver = webdriver.Chrome(options=options)

    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        logger.debug("Failed to enable Chrome Network events", exc_info=True)

    return driver


def open_ais_positions_tab(driver, timeout: int = 20) -> None:
    candidate_xpaths = [
        "//button[normalize-space()='AIS Positions']",
        "//a[normalize-space()='AIS Positions']",
        "//*[@role='tab' and normalize-space()='AIS Positions']",
        "//*[normalize-space()='AIS Positions']",
    ]

    target = None
    for xpath in candidate_xpaths:
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if element.is_displayed():
                    target = element
                    break
            except StaleElementReferenceException:
                continue
        if target is not None:
            break

    if target is None:
        raise NoSuchElementException("AIS Positions tab not found")

    _safe_click(driver, target)
    _dismiss_transient_ui(driver)

    def _tab_ready(drv) -> bool:
        visible_headers = set()
        for header in drv.find_elements(By.XPATH, "//table//th"):
            try:
                if header.is_displayed():
                    text = _normalize_text(header.text)
                    if text:
                        visible_headers.add(text)
            except StaleElementReferenceException:
                continue

        if not (visible_headers & _AIS_REQUIRED_HEADERS):
            return False

        for tab in drv.find_elements(By.XPATH, "//*[normalize-space()='AIS Positions']"):
            try:
                cls = (tab.get_attribute("class") or "").lower()
                aria_selected = (tab.get_attribute("aria-selected") or "").lower()
                if tab.is_displayed() and ("active" in cls or "selected" in cls or aria_selected == "true"):
                    return True
            except StaleElementReferenceException:
                continue
        return False

    WebDriverWait(driver, timeout).until(
        _tab_ready
    )


def _toolbar_ready(driver, timeout: int = 20) -> None:
    def _has_items_per_page_text(drv) -> bool:
        for element in drv.find_elements(By.XPATH, "//*[contains(normalize-space(.), 'items per page')]"):
            try:
                if element.is_displayed():
                    return True
            except StaleElementReferenceException:
                continue
        return False

    WebDriverWait(driver, timeout).until(_has_items_per_page_text)


def _wait_for_grid_stable(driver, timeout: int = 5, min_rows: int = 1) -> dict:
    return _wait_for_loaded_grid(driver, timeout=max(1, int(timeout)), min_rows=min_rows)


def _wait_after_page_transition(driver, cfg: dict) -> dict | None:
    if not bool(
        cfg.get(
            "event_based_page_transition_waits",
            DEFAULT_AIS_POSITIONS_CONFIG["event_based_page_transition_waits"],
        )
    ):
        _sleep_configured_delay(cfg, "page_transition_settle_seconds", "AIS next page transition")
        return None

    stable_state = _wait_for_grid_stable(
        driver,
        timeout=int(cfg.get("event_grid_stable_timeout", DEFAULT_AIS_POSITIONS_CONFIG["event_grid_stable_timeout"])),
        min_rows=1,
    )
    _sleep_configured_delay(cfg, "page_transition_settle_seconds", "AIS next page transition")
    return stable_state


def _items_per_page_1000_selected(driver) -> bool:
    for select_el in driver.find_elements(By.XPATH, "//select[.//option[normalize-space()='1000']]"):
        try:
            if not select_el.is_displayed():
                continue
            selected = Select(select_el).first_selected_option
            if (selected.text or "").strip() == "1000":
                return True
        except Exception:
            continue

    trigger_xpaths = [
        "//*[contains(@class,'lli-grid-pager__select')]//*[contains(@class,'lli-dropdown__trigger')]",
        "//span[contains(normalize-space(.), 'items per page')]/preceding::*[contains(@class,'lli-dropdown__trigger')][1]",
        "//*[contains(normalize-space(.), 'items per page')]/preceding::*[contains(@class,'lli-dropdown__trigger')][1]",
        "//button[following-sibling::*[contains(normalize-space(.), 'items per page')]]",
        "//button[preceding-sibling::*[contains(normalize-space(.), 'items per page')]]",
        "//*[contains(normalize-space(.), 'items per page')]/preceding::button[1]",
        "//*[contains(normalize-space(.), 'items per page')]/following::button[1]",
        "//*[@role='button'][following-sibling::*[contains(normalize-space(.), 'items per page')]]",
        "//*[@role='button'][preceding-sibling::*[contains(normalize-space(.), 'items per page')]]",
    ]
    for xpath in trigger_xpaths:
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if element.is_displayed() and (element.text or "").strip() == "1000":
                    return True
            except StaleElementReferenceException:
                continue
            except Exception:
                continue
    return False


def ensure_items_per_page_1000(driver, cfg=None, timeout: int = 20) -> bool:
    cfg = cfg or {}
    if _items_per_page_1000_selected(driver):
        return False
    set_items_per_page_1000(driver, timeout=timeout)
    return True


def set_items_per_page_1000(driver, timeout: int = 20) -> None:
    native_selects = driver.find_elements(By.XPATH, "//select[.//option[normalize-space()='1000']]")
    for select_el in native_selects:
        try:
            if not select_el.is_displayed():
                continue
            Select(select_el).select_by_visible_text("1000")
            return
        except Exception:
            logger.debug("Native items-per-page select failed", exc_info=True)

    trigger_xpaths = [
        "//*[contains(@class,'lli-grid-pager__select')]//*[contains(@class,'lli-dropdown__trigger')]",
        "//span[contains(normalize-space(.), 'items per page')]/preceding::*[contains(@class,'lli-dropdown__trigger')][1]",
        "//*[contains(normalize-space(.), 'items per page')]/preceding::*[contains(@class,'lli-dropdown__trigger')][1]",
        "//button[following-sibling::*[contains(normalize-space(.), 'items per page')]]",
        "//button[preceding-sibling::*[contains(normalize-space(.), 'items per page')]]",
        "//*[contains(normalize-space(.), 'items per page')]/preceding::button[1]",
        "//*[contains(normalize-space(.), 'items per page')]/following::button[1]",
        "//*[@role='button'][following-sibling::*[contains(normalize-space(.), 'items per page')]]",
        "//*[@role='button'][preceding-sibling::*[contains(normalize-space(.), 'items per page')]]",
    ]

    trigger = None
    for xpath in trigger_xpaths:
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if element.is_displayed():
                    trigger = element
                    break
            except StaleElementReferenceException:
                continue
        if trigger is not None:
            break

    if trigger is None:
        raise NoSuchElementException("Items-per-page control not found")

    if (trigger.text or "").strip() == "1000":
        return

    _safe_click(driver, trigger)

    option_xpaths = [
        "//li[normalize-space()='1000']",
        "//button[normalize-space()='1000']",
        "//*[@role='option' and normalize-space()='1000']",
        "//div[normalize-space()='1000']",
        "//span[normalize-space()='1000']",
    ]

    option = None
    for xpath in option_xpaths:
        try:
            option = WebDriverWait(driver, 5).until(
                lambda d: next(
                    (
                        el
                        for el in d.find_elements(By.XPATH, xpath)
                        if el.is_displayed()
                    ),
                    None,
                )
            )
        except TimeoutException:
            option = None
        if option is not None:
            break

    if option is None:
        raise TimeoutException("Could not find the 1000 items-per-page option")

    _safe_click(driver, option)
    _dismiss_transient_ui(driver)

    def _is_1000_selected(drv) -> bool:
        for xpath in trigger_xpaths:
            for element in drv.find_elements(By.XPATH, xpath):
                try:
                    if element.is_displayed() and (element.text or "").strip() == "1000":
                        return True
                except StaleElementReferenceException:
                    continue
        try:
            return "1000" in (trigger.text or "")
        except StaleElementReferenceException:
            return False

    WebDriverWait(driver, timeout).until(
        _is_1000_selected
    )


def _ais_response_url_matches(url: str) -> bool:
    """Match the normal AIS Positions data response, not an export request."""
    value = str(url or "").lower()
    return (
        ("ais" in value or "position" in value)
        and "export" not in value
        and "download" not in value
    )


def _ais_payload_rows(payload) -> list[dict] | None:
    """Read the small set of response shapes used by the AIS grid."""
    if isinstance(payload, list):
        return payload if all(isinstance(item, dict) for item in payload) else None
    if not isinstance(payload, dict):
        return None

    for key in ("results", "data", "rows", "items", "records", "positions", "aisPositions"):
        value = payload.get(key)
        if isinstance(value, list):
            return value if all(isinstance(item, dict) for item in value) else None
        if isinstance(value, dict):
            nested = _ais_payload_rows(value)
            if nested is not None:
                return nested
    return None


def _ais_payload_total(payload) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("totalMatches", "totalCount", "total", "count"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    for key in ("pagination", "page", "meta", "data", "results"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            total = _ais_payload_total(nested)
            if total is not None:
                return total
    return None


def _ais_nested_value(record: dict, *paths):
    for path in paths:
        current = record
        for key in path.split("."):
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if current not in (None, ""):
            return current
    return None


def _ais_scalar(value, default="-") -> str:
    if value is None or value == "":
        return default
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _ais_datetime(value) -> str:
    if value in (None, ""):
        return "-"
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return str(value)
    return parsed.strftime("%H:%M:%S GMT %d/%m/%Y")


def _ais_eta(value) -> str:
    if value in (None, ""):
        return ""
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    return str(value) if pd.isna(parsed) else parsed.strftime("%d %b %Y")


def _ais_records_to_frame(records: list[dict]) -> pd.DataFrame:
    rows = []
    for record in records:
        position = _ais_nested_value(record, "position", "location")
        position = position if isinstance(position, dict) else {}
        lat_value = _ais_nested_value(
            record,
            "lat",
            "latitude",
            "position.lat",
            "position.latitude",
            "location.lat",
            "location.latitude",
        )
        if lat_value in (None, ""):
            lat_value = position.get("lat", position.get("latitude"))
        lng_value = _ais_nested_value(
            record,
            "lng",
            "lon",
            "longitude",
            "position.lng",
            "position.lon",
            "position.longitude",
            "location.lng",
            "location.lon",
            "location.longitude",
        )
        if lng_value in (None, ""):
            lng_value = position.get("lng", position.get("lon", position.get("longitude")))
        destination = _ais_scalar(
            _ais_nested_value(
                record,
                "aisDestination",
                "ais_destination",
                "destination.name",
                "destination",
            )
        )
        eta = _ais_eta(
            _ais_nested_value(record, "eta", "ETA", "estimatedTimeOfArrival", "estimated_time_of_arrival")
        )
        if eta and eta not in destination:
            destination = f"{destination} ETA: {eta}" if destination != "-" else f"ETA: {eta}"
        rows.append(
            {
                "Nearest Place": _ais_scalar(
                    _ais_nested_value(
                        record,
                        "nearestPlace.name",
                        "nearest_place.name",
                        "place.name",
                        "nearestPlace",
                        "nearest_place",
                        "place",
                    )
                ),
                "Distance (nm)": _ais_scalar(
                    _ais_nested_value(
                        record,
                        "distance",
                        "distanceNm",
                        "distance_nm",
                        "distanceNauticalMiles",
                        "nearestPlace.distance",
                        "nearestPlace.distanceNm",
                    )
                ),
                "Date/Time": _ais_datetime(
                    _ais_nested_value(record, "dateTime", "date_time", "timestamp", "reportedAt", "time")
                ),
                "Lat": _ais_scalar(lat_value),
                "Lng": _ais_scalar(lng_value),
                "AIS Destination": destination,
                "Heading": _ais_scalar(_ais_nested_value(record, "heading", "trueHeading")),
                "Speed over ground": _ais_scalar(
                    _ais_nested_value(record, "speedOverGround", "speed_over_ground", "sog", "speed")
                ),
                "Draught (m)": _ais_scalar(
                    _ais_nested_value(record, "draught", "draft", "draughtM", "draught_m")
                ),
                "Course over ground": _ais_scalar(
                    _ais_nested_value(record, "courseOverGround", "course_over_ground", "cog", "course")
                ),
                "Source Type": _ais_scalar(
                    _ais_nested_value(record, "sourceType.name", "source_type.name", "source.name", "sourceType", "source_type", "source")
                ),
                "Navigation status": _ais_scalar(
                    _ais_nested_value(
                        record,
                        "navigationStatus.name",
                        "navigation_status.name",
                        "navStatus.name",
                        "status.name",
                        "navigationStatus",
                        "navigation_status",
                        "navStatus",
                        "status",
                    )
                ),
            }
        )
    return pd.DataFrame(rows, columns=_AIS_OUTPUT_COLUMNS)


def _read_current_ais_grid(
    driver,
    expected_rows: int,
    expected_total: int | None,
    timeout: int = 30,
) -> pd.DataFrame:
    """Read the AIS grid response caused by the latest normal UI action."""
    deadline = time.time() + timeout
    pending_request_ids = set()
    seen_row_counts = set()

    while time.time() < deadline:
        base._raise_if_chrome_page_error(driver)
        for entry in driver.get_log("performance"):
            try:
                message = json.loads(entry["message"])["message"]
                if message.get("method") != "Network.responseReceived":
                    continue
                params = message.get("params", {})
                response = params.get("response", {})
                url = response.get("url", "")
                if not _ais_response_url_matches(url):
                    continue
                request_id = params.get("requestId")
                if request_id:
                    pending_request_ids.add(request_id)
            except (KeyError, TypeError, ValueError):
                continue

        for request_id in list(pending_request_ids):
            base._raise_if_chrome_page_error(driver)
            try:
                response_body = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
                body = response_body.get("body", "")
                if response_body.get("base64Encoded"):
                    body = base64.b64decode(body).decode("utf-8")
                payload = json.loads(body)
                records = _ais_payload_rows(payload)
                total = _ais_payload_total(payload)
            except Exception:
                continue

            pending_request_ids.discard(request_id)
            if records is None or total is None:
                continue
            seen_row_counts.add(len(records))
            if len(records) != expected_rows:
                continue
            if expected_total is not None and total != expected_total:
                continue
            frame = _ais_records_to_frame(records)
            frame.attrs["total_count"] = total
            return frame

        time.sleep(0.15)

    raise TimeoutException(
        f"AIS response was not captured: expected_rows={expected_rows}, "
        f"expected_total={expected_total}, seen_sizes={sorted(seen_row_counts)}"
    )


def _ais_page_signature(frame: pd.DataFrame):
    return tuple(pd.util.hash_pandas_object(frame, index=False).tolist())


def _expected_ais_page_rows(total_count: int | None, page_index: int, fallback_row_count: int = 0) -> int:
    if total_count is None:
        return int(fallback_row_count or 0)
    return min(1000, int(total_count) - (int(page_index) - 1) * 1000)


def _append_ais_page(frame: pd.DataFrame, page_index: int, page_frames: list[pd.DataFrame], page_markers: set) -> None:
    marker = _ais_page_signature(frame)
    if marker in page_markers:
        raise ValueError(f"AIS page {page_index} repeated the previous page")
    page_markers.add(marker)
    page_frames.append(frame)


def _merge_ais_frames(page_frames: list[pd.DataFrame], expected_total: int) -> pd.DataFrame:
    merged = pd.concat(page_frames, ignore_index=True)
    if len(merged) != expected_total:
        raise ValueError(f"AIS row count mismatch: expected={expected_total}, saved={len(merged)}")
    duplicate_rows = int(merged.duplicated().sum())
    if duplicate_rows:
        raise ValueError(f"AIS output contains {duplicate_rows} duplicate rows")
    return merged


def _write_ais_frame(frame: pd.DataFrame, output_path: Path, encoding: str) -> Path:
    frame = frame.loc[:, _AIS_OUTPUT_COLUMNS]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_path, index=False, encoding=encoding)
    return output_path


def _stream_merge_ais_window_files(
    window_paths: list[Path],
    output_path: Path,
    encoding: str,
) -> int:
    """Merge disjoint period-window CSVs without loading all years into RAM."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.name}.tmp")
    total_rows = 0

    try:
        with temp_path.open("w", encoding=encoding, newline="") as output_file:
            writer = csv.writer(output_file)
            writer.writerow(_AIS_OUTPUT_COLUMNS)

            for path in window_paths:
                input_file = None
                for source_encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                    try:
                        input_file = path.open("r", encoding=source_encoding, newline="")
                        header = next(csv.reader(input_file), None)
                        input_file.seek(0)
                        break
                    except UnicodeDecodeError:
                        if input_file is not None:
                            input_file.close()
                        input_file = None

                if input_file is None:
                    raise UnicodeDecodeError("unknown", b"", 0, 1, f"Could not decode {path}")

                with input_file:
                    reader = csv.reader(input_file)
                    header = next(reader, None)
                    if header is None:
                        raise ValueError(f"AIS period-window CSV has no header: {path}")
                    normalized_header = [str(value).lstrip("\ufeff") for value in header]
                    if set(normalized_header) != set(_AIS_OUTPUT_COLUMNS):
                        raise ValueError(f"AIS period-window CSV header mismatch: {path}")
                    column_indexes = [normalized_header.index(column) for column in _AIS_OUTPUT_COLUMNS]

                    # Each file covers at most one non-overlapping period window.
                    # Keeping only that window's keys preserves duplicate checking
                    # without retaining the full multi-year dataset in memory.
                    window_rows = set()
                    for row in reader:
                        if not row:
                            continue
                        if len(row) != len(normalized_header):
                            raise ValueError(f"AIS period-window CSV row width mismatch: {path}")
                        ordered_row = tuple(row[index] for index in column_indexes)
                        if ordered_row in window_rows:
                            raise ValueError(f"AIS period-window CSV contains duplicate rows: {path}")
                        window_rows.add(ordered_row)
                        writer.writerow(ordered_row)
                        total_rows += 1
                    del window_rows

        os.replace(temp_path, output_path)
        return total_rows
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _is_usable_csv_file(path: Path) -> bool:
    try:
        if not path.exists() or path.stat().st_size <= 0:
            return False
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                pd.read_csv(path, encoding=encoding, nrows=1)
                return True
            except UnicodeDecodeError:
                continue
        pd.read_csv(path, nrows=1)
        return True
    except Exception:
        logger.debug("AIS resume CSV is not usable: %s", path, exc_info=True)
        return False


def _period_window_no_data_marker(window_out_dir: Path, llino_token: str, window_label: str) -> Path:
    return window_out_dir / f".ais_positions_{llino_token}_{window_label}_no_data"


def scraping_ais_positions(driver, llino, config=None, *, download_dir=None, reload_current_page=False):
    cfg = {**DEFAULT_AIS_POSITIONS_CONFIG, **(config or {})}
    start = time.time()
    run_id = _make_run_id()
    llino_token = _format_llino(llino)
    period_label = _period_label(cfg.get("period"))

    out_dir = Path(cfg["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / f"ais_positions_{llino_token}_{period_label}.csv"

    period_windows = []
    if bool(cfg.get("chunk_long_periods", True)):
        period_windows = build_ais_period_windows(
            cfg.get("period"),
            max_days=int(cfg.get("period_chunk_days", DEFAULT_AIS_POSITIONS_CONFIG["period_chunk_days"])),
            order=cfg.get("period_chunk_order", DEFAULT_AIS_POSITIONS_CONFIG["period_chunk_order"]),
        )

    result_period_windows = [dict(window) for window in period_windows]
    window_count = len(period_windows) if period_windows else 1

    if not period_windows or len(period_windows) == 1:
        result = _scraping_ais_positions_single_period(
            driver,
            llino,
            config=cfg,
            reload_current_page=reload_current_page,
        )
        if isinstance(result, dict):
            result.setdefault("windows", window_count)
            result.setdefault("period_windows", result_period_windows)
            result.setdefault("output_path", str(output_path))
        return result

    logger.info(
        "Start chunked scraping_ais_positions for llino=%s (%s period windows, newest first)",
        llino,
        len(period_windows),
    )

    try:
        if bool(cfg.get("skip_if_exists", True)) and _has_saved_ais_positions_for_llino(llino, cfg):
            logger.complete(
                "llino %s: skipped because AIS Positions output already exists (skip_if_exists=True)",
                llino,
            )
            return {
                "llino": llino,
                "ok": True,
                "skipped": True,
                "status": None,
                "pages": 0,
                "elapsed": 0.0,
                "note": "already_exists",
                "windows": len(period_windows),
                "period_windows": result_period_windows,
                "output_path": str(output_path),
            }
        if bool(cfg.get("skip_if_known_no_data", True)) and _has_known_no_data_llino(llino, cfg):
            logger.complete(
                "llino %s: skipped because AIS Positions no-data cache exists (skip_if_known_no_data=True)",
                llino,
            )
            result = _build_known_no_data_result(llino)
            result.update(
                {
                    "windows": len(period_windows),
                    "period_windows": result_period_windows,
                    "output_path": str(output_path),
                }
            )
            return result
    except Exception:
        logger.debug("AIS Positions pre-skip check failed", exc_info=True)

    resume_period_windows = bool(
        cfg.get("resume_period_windows", DEFAULT_AIS_POSITIONS_CONFIG["resume_period_windows"])
    )
    if resume_period_windows:
        temp_out_dir = out_dir / "_tmp" / f"ais_positions_{llino_token}_{period_label}_period_windows"
    else:
        temp_out_dir = out_dir / "_tmp" / f"{run_id}_{llino_token}_period_windows"
    completed_chunks = []
    pages_total = 0
    status = None
    page_ready_for_next_window = False
    period_window_retry_attempts = max(
        0,
        int(
            cfg.get(
                "period_window_retry_attempts",
                DEFAULT_AIS_POSITIONS_CONFIG["period_window_retry_attempts"],
            )
        ),
    )

    for index, window in enumerate(period_windows, start=1):
        stop_event = cfg.get("_stop_event")
        if stop_event is not None and stop_event.is_set():
            return {
                "llino": llino,
                "ok": False,
                "skipped": False,
                "error": "run_stopped",
                "error_type": "run_stopped",
                "fatal": True,
                "windows": len(period_windows),
                "period_windows": result_period_windows,
                "output_path": str(output_path),
                "resume_tmp_dir": str(temp_out_dir),
            }
        window_label = _period_label(window)
        window_out_dir = temp_out_dir / _slugify_token(window_label, "period")
        window_output_path = window_out_dir / f"ais_positions_{llino_token}_{window_label}.csv"
        no_data_marker = _period_window_no_data_marker(window_out_dir, llino_token, window_label)
        logger.complete(
            "llino %s AIS period window %s/%s: %s",
            llino,
            index,
            len(period_windows),
            window,
        )
        if resume_period_windows and _is_usable_csv_file(window_output_path):
            logger.complete(
                "llino %s AIS period window %s/%s already downloaded; reusing %s",
                llino,
                index,
                len(period_windows),
                window_output_path,
            )
            completed_chunks.append(
                {
                    "window": dict(window),
                    "output_path": window_output_path,
                }
            )
            page_ready_for_next_window = False
            continue

        if resume_period_windows and no_data_marker.exists():
            logger.complete(
                "llino %s AIS period window %s/%s already confirmed no-data; skipping",
                llino,
                index,
                len(period_windows),
            )
            page_ready_for_next_window = False
            continue

        sub_cfg = {
            **cfg,
            "period": dict(window),
            "out_dir": str(window_out_dir),
            "skip_if_exists": False,
            "skip_if_known_no_data": False,
            "remember_no_data": False,
            "chunk_long_periods": False,
        }
        sub_cfg.pop("_saved_outputs_index", None)
        sub_cfg.pop("_known_no_data_index", None)
        result = None
        for window_attempt in range(1, period_window_retry_attempts + 2):
            result = _scraping_ais_positions_single_period(
                driver,
                llino,
                config=sub_cfg,
                page_ready=page_ready_for_next_window if window_attempt == 1 else False,
                initial_status=status,
                reload_current_page=reload_current_page if index == 1 and window_attempt == 1 else False,
            )

            if isinstance(result, dict) and result.get("ok"):
                break

            if isinstance(result, dict) and result.get("error_type") in {
                "access_blocked",
                "session_expired",
                "webdriver_failure",
                "run_stopped",
            }:
                break

            if window_attempt > period_window_retry_attempts:
                break

            error_text = None
            if isinstance(result, dict):
                error_text = result.get("error_detail") or result.get("error")
            if not error_text:
                error_text = "unknown_chunk_result"

            page_ready_for_next_window = False
            logger.warning(
                "llino %s AIS period window %s/%s failed; retrying same window (%s/%s): %s",
                llino,
                index,
                len(period_windows),
                window_attempt,
                period_window_retry_attempts,
                error_text,
            )
            _sleep_configured_delay(
                cfg,
                "period_window_retry_delay_seconds",
                "AIS period window retry",
            )

        if isinstance(result, dict):
            status = result.get("status") or status
            if result.get("ok") and result.get("note") != "inactive-status":
                page_ready_for_next_window = True

        if not isinstance(result, dict) or not result.get("ok"):
            if isinstance(result, dict):
                result.update(
                    {
                        "windows": len(period_windows),
                        "period_windows": result_period_windows,
                        "output_path": str(output_path),
                        "failed_period_window": dict(window),
                        "failed_period_window_index": index,
                        "resume_tmp_dir": str(temp_out_dir),
                    }
                )
                return result
            return {
                "llino": llino,
                "ok": False,
                "skipped": False,
                "status": status,
                "pages": pages_total,
                "elapsed": time.time() - start,
                "error": "unknown_chunk_result",
                "windows": len(period_windows),
                "period_windows": result_period_windows,
                "output_path": str(output_path),
                "failed_period_window": dict(window),
                "failed_period_window_index": index,
                "resume_tmp_dir": str(temp_out_dir),
            }

        if result.get("note") == "inactive-status":
            result.update(
                {
                    "windows": len(period_windows),
                    "period_windows": result_period_windows,
                    "output_path": str(output_path),
                }
            )
            shutil.rmtree(temp_out_dir, ignore_errors=True)
            _remove_empty_parents(temp_out_dir.parent, out_dir)
            return result

        if result.get("note") == "no-data":
            if resume_period_windows:
                no_data_marker.parent.mkdir(parents=True, exist_ok=True)
                no_data_marker.touch()
            continue

        chunk_output_path = result.get("output_path")
        if not chunk_output_path:
            result.update(
                {
                    "ok": False,
                    "error": "missing_chunk_output_path",
                    "windows": len(period_windows),
                    "period_windows": result_period_windows,
                    "output_path": str(output_path),
                }
            )
            return result

        pages_total += int(result.get("pages") or 0)
        completed_chunks.append(
            {
                "window": dict(window),
                "output_path": Path(chunk_output_path),
            }
        )

    if not completed_chunks:
        elapsed = time.time() - start
        _remember_known_no_data_llino(llino, cfg)
        shutil.rmtree(temp_out_dir, ignore_errors=True)
        _remove_empty_parents(temp_out_dir.parent, out_dir)
        logger.complete(
            "llino %s finished with no AIS Positions rows across %s period window(s) (%.2fs)",
            llino,
            len(period_windows),
            elapsed,
        )
        return {
            "llino": llino,
            "ok": True,
            "skipped": True,
            "status": status,
            "pages": 0,
            "elapsed": elapsed,
            "note": "no-data",
            "windows": len(period_windows),
            "period_windows": result_period_windows,
            "output_path": str(output_path),
            "run_id": run_id,
        }

    completed_chunks.sort(
        key=lambda item: _parse_period_date(item["window"].get("from")) or date.min
    )
    chunk_paths = [item["output_path"] for item in completed_chunks]
    merged_rows = _stream_merge_ais_window_files(
        chunk_paths,
        output_path,
        cfg["encoding"],
    )

    cached_index = cfg.get("_saved_outputs_index")
    if isinstance(cached_index, set):
        cached_index.add(llino_token)

    shutil.rmtree(temp_out_dir, ignore_errors=True)
    _remove_empty_parents(temp_out_dir.parent, out_dir)

    elapsed = time.time() - start
    logger.complete(
        "llino %s completed chunked AIS scrape successfully (%.2fs, %s pages, %s windows, output=%s)",
        llino,
        elapsed,
        pages_total,
        len(period_windows),
        output_path,
    )
    return {
        "llino": llino,
        "ok": True,
        "skipped": False,
        "status": status,
        "pages": pages_total,
        "rows": merged_rows,
        "elapsed": elapsed,
        "output_path": str(output_path),
        "run_id": run_id,
        "windows": len(period_windows),
        "period_windows": result_period_windows,
    }


def _scraping_ais_positions_single_period(
    driver,
    llino,
    config=None,
    *,
    download_dir=None,
    page_ready: bool = False,
    initial_status=None,
    reload_current_page: bool = False,
):
    cfg = {**DEFAULT_AIS_POSITIONS_CONFIG, **(config or {})}
    log_steps = bool(cfg.get("log_steps", False))
    start = time.time()
    metrics = {}
    saved_pages = 0
    status = initial_status
    run_id = _make_run_id()
    llino_token = _format_llino(llino)
    period_label = _period_label(cfg.get("period"))
    next_page_ready_retry_attempts = int(
        cfg.get(
            "next_page_ready_retry_attempts",
            DEFAULT_AIS_POSITIONS_CONFIG["next_page_ready_retry_attempts"],
        )
    )
    period_apply_timeout = int(
        cfg.get("period_apply_timeout", DEFAULT_AIS_POSITIONS_CONFIG["period_apply_timeout"])
    )
    page_transition_timeout = int(
        cfg.get("page_transition_timeout", DEFAULT_AIS_POSITIONS_CONFIG["page_transition_timeout"])
    )

    out_dir = Path(cfg["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    debug_base = out_dir / "debug"

    logger.info("Start scraping_ais_positions for llino=%s", llino)

    try:
        if bool(cfg.get("skip_if_exists", True)) and _has_saved_ais_positions_for_llino(llino, cfg):
            logger.complete(
                "llino %s: skipped because AIS Positions output already exists (skip_if_exists=True)",
                llino,
            )
            return {
                "llino": llino,
                "ok": True,
                "skipped": True,
                "status": None,
                "pages": 0,
                "elapsed": 0.0,
                "note": "already_exists",
            }
        if bool(cfg.get("skip_if_known_no_data", True)) and _has_known_no_data_llino(llino, cfg):
            logger.complete(
                "llino %s: skipped because AIS Positions no-data cache exists (skip_if_known_no_data=True)",
                llino,
            )
            return _build_known_no_data_result(llino)
    except Exception:
        logger.debug("AIS Positions pre-skip check failed", exc_info=True)

    try:
        phase_started = time.monotonic()
        if page_ready:
            _toolbar_ready(driver)
            _sleep_configured_delay(cfg, "between_period_windows_delay_seconds", "AIS period window transition")
            if log_steps:
                logger.info("AIS Positions page reused for llino %s", llino)
        else:
            base.open_movement(driver, llino, reload_current_page=reload_current_page)
            if log_steps:
                logger.info("open_movement completed for llino %s", llino)

            if cfg.get("check_status", True):
                status_elem = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "lli-status__label"))
                )
                status = status_elem.text.strip()
                if log_steps:
                    logger.info("status check completed for llino %s: %s", llino, status)
                if status not in _ACTIVE_STATUS:
                    elapsed = time.time() - start
                    logger.complete(
                        "llino %s finished with inactive status (%s, %.2fs)",
                        llino,
                        status,
                        elapsed,
                    )
                    return {
                        "llino": llino,
                        "ok": True,
                        "skipped": True,
                        "status": status,
                        "pages": 0,
                        "elapsed": elapsed,
                        "note": "inactive-status",
                    }

            open_ais_positions_tab(driver)
            _toolbar_ready(driver)
            if log_steps:
                logger.info("AIS Positions tab opened for llino %s", llino)
        _add_timing_metric(metrics, "open", phase_started)

        phase_started = time.monotonic()
        if not page_ready:
            base._clear_performance_log(driver)
            base.set_local_time_checkbox(driver, cfg["local_time"])
            if log_steps:
                logger.info("local_time applied for llino %s", llino)

        base._clear_performance_log(driver)
        grid_state_before_period = _capture_grid_state(driver)
        base.set_period(
            driver,
            cfg["period"],
            allow_from_earliest_fallback=False,
        )
        if log_steps:
            logger.info("period applied for llino %s", llino)
        _sleep_configured_delay(cfg, "period_settle_seconds", "AIS period apply settle")

        grid_state_after_period = _wait_for_items_per_page_apply(
            driver,
            previous_state=grid_state_before_period,
            timeout=period_apply_timeout,
        )
        grid_state_after_period = _return_to_first_ais_page(
            driver,
            current_grid_state=grid_state_after_period,
            config=cfg,
        )
        _add_timing_metric(metrics, "period", phase_started)

        if grid_state_after_period.get("row_count", 0) == 0:
            if not _grid_state_confirms_no_data(driver, grid_state_after_period):
                raise TimeoutException(
                    "AIS grid has no visible rows, but no confirmed no-data indicator or total_count=0 was found"
                )
            elapsed = time.time() - start
            _remember_known_no_data_llino(llino, cfg)
            logger.complete("llino %s finished with no AIS Positions rows (%.2fs)", llino, elapsed)
            _log_timing_metrics(cfg, llino, period_label, 0, elapsed, metrics)
            return {
                "llino": llino,
                "ok": True,
                "skipped": True,
                "status": status,
                "pages": 0,
                "elapsed": elapsed,
                "note": "no-data",
            }

        phase_started = time.monotonic()
        grid_state_before_page_size = grid_state_after_period
        page_size_changed = False
        if not _items_per_page_1000_selected(driver):
            base._clear_performance_log(driver)
            page_size_changed = ensure_items_per_page_1000(driver, cfg=cfg)
        _dismiss_transient_ui(driver)
        if log_steps:
            if page_size_changed:
                logger.info("items per page set to 1000 for llino %s", llino)
            else:
                logger.info("items per page already 1000 for llino %s", llino)

        if page_size_changed:
            current_grid_state = _wait_for_items_per_page_apply(
                driver,
                previous_state=grid_state_before_page_size,
                timeout=page_transition_timeout,
            )
        else:
            current_grid_state = _capture_grid_state(driver)
        _add_timing_metric(metrics, "page_size", phase_started)
        page_frames: list[pd.DataFrame] = []
        page_markers = set()
        page_index = 1
        expected_total = current_grid_state.get("total_count")
        expected_rows = _expected_ais_page_rows(
            expected_total,
            page_index,
            fallback_row_count=int(current_grid_state.get("row_count") or 0),
        )
        if expected_rows <= 0:
            raise TimeoutException(f"AIS page {page_index} has no expected rows")
        phase_started = time.monotonic()
        frame = _read_current_ais_grid(
            driver,
            expected_rows=expected_rows,
            expected_total=expected_total,
            timeout=page_transition_timeout,
        )
        _add_timing_metric(metrics, "response", phase_started)
        if expected_total is None:
            expected_total = int(frame.attrs.get("total_count") or 0)
        _append_ais_page(frame, page_index, page_frames, page_markers)
        saved_pages += 1

        total_count = int(expected_total or 0)
        total_pages = (total_count + 999) // 1000
        while page_index < total_pages:
            phase_started = time.monotonic()
            grid_state_before_next_page = current_grid_state
            base._clear_performance_log(driver)
            if not _click_next_ais_page(driver):
                raise TimeoutException(f"AIS Next Page could not be clicked at page {page_index}")
            page_index += 1
            last_pagination_exc = None
            pagination_exhausted = False
            for pagination_attempt in range(1, max(1, next_page_ready_retry_attempts) + 1):
                try:
                    current_grid_state = _wait_for_next_page_ready(
                        driver,
                        previous_state=grid_state_before_next_page,
                        timeout=page_transition_timeout,
                    )
                    stable_grid_state = _wait_after_page_transition(driver, cfg)
                    if stable_grid_state is not None:
                        current_grid_state = stable_grid_state
                    break
                except AISPaginationEnd as exc:
                    logger.info("llino %s reached AIS pagination boundary: %s", llino, exc)
                    pagination_exhausted = True
                    break
                except TimeoutException as exc:
                    last_pagination_exc = exc
                    if pagination_attempt >= max(1, next_page_ready_retry_attempts):
                        raise
                    logger.warning(
                        "AIS pagination wait retry for llino %s page %s (%s/%s): %s",
                        llino,
                        page_index,
                        pagination_attempt,
                        max(1, next_page_ready_retry_attempts),
                        exc,
                    )
                    time.sleep(1.0)
            else:
                raise last_pagination_exc or TimeoutException("AIS pagination wait failed")
            if pagination_exhausted:
                break
            _add_timing_metric(metrics, "pagination", phase_started)
            expected_rows = _expected_ais_page_rows(total_count, page_index)
            if expected_rows <= 0:
                raise TimeoutException(f"AIS page {page_index} has no expected rows")
            phase_started = time.monotonic()
            frame = _read_current_ais_grid(
                driver,
                expected_rows=expected_rows,
                expected_total=total_count,
                timeout=page_transition_timeout,
            )
            _add_timing_metric(metrics, "response", phase_started)
            _append_ais_page(frame, page_index, page_frames, page_markers)
            saved_pages += 1

        output_path = out_dir / f"ais_positions_{llino_token}_{period_label}.csv"
        phase_started = time.monotonic()
        merged = _merge_ais_frames(page_frames, total_count)
        _write_ais_frame(merged, output_path, cfg["encoding"])
        output_rows = len(merged)
        page_frames.clear()
        del frame, merged
        base._clear_performance_log(driver)
        gc.collect()
        _add_timing_metric(metrics, "merge", phase_started)
        cached_index = cfg.get("_saved_outputs_index")
        if isinstance(cached_index, set):
            cached_index.add(llino_token)

        elapsed = time.time() - start
        logger.complete(
            "llino %s completed successfully (%.2fs, %s pages, output=%s)",
            llino,
            elapsed,
            saved_pages,
            output_path,
        )
        _log_timing_metrics(cfg, llino, period_label, saved_pages, elapsed, metrics)
        return {
            "llino": llino,
            "ok": True,
            "skipped": False,
            "status": status,
            "pages": saved_pages,
            "rows": output_rows,
            "elapsed": elapsed,
            "output_path": str(output_path),
            "run_id": run_id,
        }

    except TimeoutException as exc:
        elapsed = time.time() - start
        failure_flags = _ais_browser_failure_flags(driver, exc)
        browser_unresponsive = _ais_browser_is_unresponsive(failure_flags)
        if browser_unresponsive:
            error_type = "transient_timeout"
            classification_detail = None
            artifacts = {}
        else:
            error_type, classification_detail = _classify_driver_failure(driver)
            artifacts = _save_debug_artifacts(
                driver,
                debug_base,
                f"llino_{llino_token}_ais_positions_timeout_{run_id}",
            )
        logger.warning(
            "Timeout while scraping AIS Positions for llino %s (type=%s): %s",
            llino,
            error_type,
            exc,
        )
        return {
            "llino": llino,
            "ok": False,
            "skipped": False,
            "status": status,
            "pages": saved_pages,
            "elapsed": elapsed,
            "error": "timeout" if error_type == "transient_timeout" else error_type,
            "error_type": error_type,
            "error_detail": classification_detail or str(exc),
            "fatal": error_type == "access_blocked",
            "debug": artifacts,
            **failure_flags,
        }

    except Exception as exc:
        elapsed = time.time() - start
        failure_flags = _ais_browser_failure_flags(driver, exc)
        browser_unresponsive = _ais_browser_is_unresponsive(failure_flags)
        if browser_unresponsive:
            error_type = "transient_timeout"
            error_value = "timeout"
            classification_detail = None
            artifacts = {}
        elif isinstance(exc, WebDriverException):
            classified_type, classification_detail = _classify_driver_failure(driver)
            if classified_type in {"access_blocked", "session_expired"}:
                error_type = classified_type
                error_value = classified_type
            else:
                error_type = "webdriver_failure"
                error_value = str(exc)
            artifacts = _save_debug_artifacts(
                driver,
                debug_base,
                f"llino_{llino_token}_ais_positions_error_{run_id}",
            )
        else:
            error_type = "scrape_error"
            classification_detail = None
            error_value = str(exc)
            artifacts = _save_debug_artifacts(
                driver,
                debug_base,
                f"llino_{llino_token}_ais_positions_error_{run_id}",
            )
        logger.error("Error while scraping AIS Positions for llino %s: %s", llino, exc, exc_info=True)
        return {
            "llino": llino,
            "ok": False,
            "skipped": False,
            "status": status,
            "pages": saved_pages,
            "elapsed": elapsed,
            "error": error_value,
            "error_type": error_type,
            "error_detail": classification_detail or str(exc),
            "fatal": error_type == "access_blocked",
            "debug": artifacts,
            **failure_flags,
        }


def worker_thread_ais_positions(
    llino_list,
    *,
    worker_name: str = "worker_01",
    driver_opts=None,
    retry_driver_once: bool = True,
    config=None,
):
    """Scrape a worker chunk while preserving one authenticated browser session."""
    cfg = {**DEFAULT_AIS_POSITIONS_CONFIG, **(config or {})}
    llino_list = list(llino_list or [])
    results = []
    show_progress = bool(cfg.get("show_progress", True))
    pbar = None
    use_tqdm = (tqdm is not None) and show_progress and len(llino_list) > 1
    stop_event = cfg.get("_stop_event")
    if stop_event is None:
        stop_event = threading.Event()
        cfg["_stop_event"] = stop_event

    out_dir = Path(cfg["out_dir"])
    retry_failed = bool(cfg.get("retry_failed_llino_after_wait", True))
    max_attempts = max(1, int(cfg.get("failed_llino_retry_max_attempts", 2) or 2))
    retry_wait_minutes = float(cfg.get("failed_llino_retry_wait_minutes", 30))
    retry_random_minutes = float(cfg.get("failed_llino_retry_random_minutes", 5))
    relogin_enabled = bool(cfg.get("relogin_on_confirmed_session_loss", True))
    relogin_attempt_limit = max(0, int(cfg.get("session_relogin_attempts", 1)))
    restart_attempt_limit = max(0, int(cfg.get("webdriver_restart_attempts", 0)))
    max_consecutive_failures = max(1, int(cfg.get("max_consecutive_failed_llinos", 3)))

    driver = None
    relogin_count = 0
    retry_count = 0
    restart_count = 0
    consecutive_failures = 0
    processed = 0

    def _new_driver_and_login():
        drv = initialize_driver(driver_opts)
        try:
            drv.command_executor.client_config.timeout = 45
            drv.set_page_load_timeout(45)
            drv.set_script_timeout(45)
        except Exception:
            pass
        return base.login_to_seasearcher(drv, config=cfg)

    def _replace_driver():
        nonlocal driver
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        driver = _new_driver_and_login()

    def _finish_result(result: dict) -> None:
        nonlocal processed
        result.setdefault("retry_count", retry_count)
        result.setdefault("relogin_count", relogin_count)
        results.append(result)
        processed += 1
        if pbar is not None:
            try:
                pbar.update(1)
            except Exception:
                pass
        elif show_progress:
            try:
                sys.stdout.write(f"{worker_name}: processed {processed}/{len(llino_list)}\r")
                sys.stdout.flush()
            except Exception:
                pass

    try:
        driver = _new_driver_and_login()
        logger.complete("[%s] initial login completed", worker_name)

        if use_tqdm:
            try:
                pbar = tqdm(total=len(llino_list), desc=f"{worker_name} LLIs", unit="llino")
            except Exception:
                pbar = None

        rest_state = _initialize_periodic_rest_state(cfg)
        if rest_state["enabled"]:
            logger.complete(
                "[%s] periodic rest enabled; first rest target is %.2f hours",
                worker_name,
                rest_state["next_work_session_seconds"] / 3600.0,
            )

        for llino_index, llino in enumerate(llino_list):
            if stop_event.is_set():
                for pending_llino in llino_list[llino_index:]:
                    _finish_result(
                        {
                            "llino": pending_llino,
                            "ok": False,
                            "skipped": True,
                            "error": "run_stopped",
                            "error_type": "run_stopped",
                            "fatal": True,
                        }
                    )
                break

            llino_attempt = 0
            incident_relogin_attempts = 0
            browser_reload_attempted = False
            while llino_attempt < max_attempts:
                llino_attempt += 1
                try:
                    res = scraping_ais_positions(
                        driver,
                        llino,
                        config=cfg,
                    )
                except WebDriverException as exc:
                    res = {
                        "llino": llino,
                        "ok": False,
                        "error": str(exc),
                        "error_type": "webdriver_failure",
                        "fatal": restart_attempt_limit <= restart_count,
                    }
                except Exception as exc:
                    logger.error("[worker=%s] Error at llino=%s: %s", worker_name, llino, exc, exc_info=True)
                    res = {
                        "llino": llino,
                        "ok": False,
                        "error": str(exc),
                        "error_type": "scrape_error",
                        "fatal": False,
                    }

                if not isinstance(res, dict):
                    res = {
                        "llino": llino,
                        "ok": False,
                        "error": "unknown_error",
                        "error_type": "scrape_error",
                        "fatal": False,
                    }

                browser_reload_error = bool(
                    res.get("chrome_memory_error")
                    or res.get("webdriver_command_timeout")
                    or res.get("page_load_stuck")
                    or res.get("chrome_page_error")
                    or res.get("renderer_timeout")
                )
                if _is_timeout_result(res) and browser_reload_error and not browser_reload_attempted:
                    browser_reload_attempted = True
                    logger.warning(
                        "[worker=%s] AIS page issue at llino=%s; waiting 5 seconds and refreshing the same browser once",
                        worker_name,
                        llino,
                    )
                    time.sleep(5.0)
                    try:
                        retry_res = scraping_ais_positions(
                            driver,
                            llino,
                            config=cfg,
                            reload_current_page=True,
                        )
                        if isinstance(retry_res, dict):
                            retry_res["browser_reload_retry"] = True
                            res = retry_res
                    except Exception as retry_exc:
                        res = {
                            "llino": llino,
                            "ok": False,
                            "error": "timeout",
                            "error_type": "transient_timeout",
                            "error_detail": str(retry_exc),
                            "browser_reload_retry": True,
                            **_ais_browser_failure_flags(driver, retry_exc),
                        }

                if res.get("ok"):
                    base._release_vessel_page(driver)
                    consecutive_failures = 0
                gc.collect()
                if res.get("ok"):
                    _finish_result(res)
                    rest_state = _take_periodic_rest_if_needed(
                        worker_name=worker_name,
                        config=cfg,
                        rest_state=rest_state,
                        processed=processed,
                        total=len(llino_list),
                    )
                    break

                error_type = str(res.get("error_type") or "scrape_error")
                if error_type == "access_blocked":
                    res["fatal"] = True
                    logger.critical(
                        "[worker=%s] Access restriction detected at llino=%s; stopping all AIS workers without re-login",
                        worker_name,
                        llino,
                    )
                    if bool(cfg.get("stop_on_access_block", True)):
                        stop_event.set()
                    _finish_result(res)
                    break

                if error_type == "session_expired":
                    if (
                        relogin_enabled
                        and incident_relogin_attempts < relogin_attempt_limit
                        and not stop_event.is_set()
                    ):
                        incident_relogin_attempts += 1
                        logger.warning(
                            "[worker=%s] Confirmed login page at llino=%s; re-login in the existing browser (%s/%s)",
                            worker_name,
                            llino,
                            incident_relogin_attempts,
                            relogin_attempt_limit,
                        )
                        try:
                            driver = base.login_to_seasearcher(driver, config=cfg)
                            relogin_count += 1
                            retry_count += 1
                            llino_attempt -= 1
                            continue
                        except Exception as exc:
                            res.update(
                                {
                                    "error": f"confirmed_session_relogin_failed: {exc}",
                                    "error_type": "session_relogin_failed",
                                    "fatal": True,
                                }
                            )
                    else:
                        res["fatal"] = True
                    stop_event.set()
                    _finish_result(res)
                    break

                if error_type == "webdriver_failure":
                    if restart_count < restart_attempt_limit and not stop_event.is_set():
                        restart_count += 1
                        logger.warning(
                            "[worker=%s] WebDriver failed at llino=%s; restarting browser (%s/%s)",
                            worker_name,
                            llino,
                            restart_count,
                            restart_attempt_limit,
                        )
                        try:
                            _replace_driver()
                            retry_count += 1
                            llino_attempt -= 1
                            continue
                        except Exception as exc:
                            res["error"] = f"webdriver_restart_failed: {exc}"
                    res["fatal"] = True
                    stop_event.set()
                    _finish_result(res)
                    break

                if not retry_failed or llino_attempt >= max_attempts:
                    consecutive_failures += 1
                    _finish_result(res)
                    if consecutive_failures >= max_consecutive_failures:
                        logger.critical(
                            "[worker=%s] %s consecutive LLI failures reached; stopping all AIS workers",
                            worker_name,
                            consecutive_failures,
                        )
                        stop_event.set()
                    break

                wait_seconds = _randomize_minutes_seconds(retry_wait_minutes, retry_random_minutes)
                retry_count += 1
                logger.warning(
                    "[worker=%s] llino=%s failed with %s; keeping the current session and retrying after %.1f minutes (%s/%s)",
                    worker_name,
                    llino,
                    error_type,
                    wait_seconds / 60.0,
                    llino_attempt + 1,
                    max_attempts,
                )
                time.sleep(wait_seconds)

        logger.complete(
            "[%s] worker finished: processed=%s, retries=%s, re-logins=%s, browser-restarts=%s",
            worker_name,
            processed,
            retry_count,
            relogin_count,
            restart_count,
        )
    finally:
        if pbar is not None:
            try:
                pbar.close()
            except Exception:
                pass
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    return results


def parallel_scraping_ais_positions(llino_list, max_workers=1, driver_opts=None, config=None):
    runtime_config = {**(config or {})}
    runtime_config.setdefault("_stop_event", threading.Event())
    llino_list = list(llino_list or [])
    requested_max_workers = max(1, int(max_workers))
    # One authenticated browser is enough here.  Multiple Chrome renderers
    # make long AIS runs consume memory quickly and add unnecessary site load.
    max_workers = 1
    if driver_opts is None:
        driver_opts = {}
    else:
        driver_opts = dict(driver_opts)
    if "headless" in runtime_config:
        driver_opts.setdefault("headless", runtime_config["headless"])

    if "log_level" in runtime_config:
        level_name = str(runtime_config["log_level"]).upper()
        if level_name in {"DONE", "COMPLETE"}:
            level = COMPLETE_LOG_LEVEL
        else:
            level = getattr(logging, level_name, None)
        if isinstance(level, int):
            logger.setLevel(level)
            logging.getLogger().setLevel(level)

    logger.info(
        "parallel_scraping_ais_positions starting: workers=%s, headless=%s",
        max_workers,
        driver_opts.get("headless"),
    )
    if requested_max_workers != 1:
        logger.complete(
            "AIS scraping forces max_workers=1 for memory and site-load stability (requested=%s)",
            requested_max_workers,
        )

    def _run_parallel_scrape_pass(target_llinos, *, pass_label: str):
        target_llinos = list(target_llinos or [])
        (
            pending_llinos,
            upfront_skipped_results,
            saved_outputs_index,
            known_no_data_index,
        ) = _apply_upfront_local_skip_filter(
            target_llinos,
            runtime_config,
        )
        if bool(runtime_config.get("skip_if_exists", True)):
            runtime_config["_saved_outputs_index"] = saved_outputs_index
        if bool(runtime_config.get("skip_if_known_no_data", True)):
            runtime_config["_known_no_data_index"] = known_no_data_index
        if saved_outputs_index or known_no_data_index:
            logger.complete(
                "%s upfront local AIS cache check: %s skipped, %s pending",
                pass_label,
                len(upfront_skipped_results),
                len(pending_llinos),
            )

        chunks = [chunk for chunk in base.chunk_round_robin(pending_llinos, max_workers) if chunk]
        pass_results = []

        if chunks:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(
                        worker_thread_ais_positions,
                        chunk,
                        worker_name=f"worker_{index + 1:02d}",
                        driver_opts=driver_opts,
                        config=runtime_config,
                    )
                    for index, chunk in enumerate(chunks)
                ]

                show_progress = bool(runtime_config.get("show_progress", True))
                total_llinos = len(pending_llinos)
                total_processed = 0
                main_pbar = None
                use_tqdm_main = (tqdm is not None) and show_progress and max_workers > 1 and total_llinos > 1
                if use_tqdm_main:
                    try:
                        main_pbar = tqdm(total=total_llinos, desc="Total AIS LLIs", unit="llino")
                    except Exception:
                        main_pbar = None

                for future in as_completed(futures):
                    try:
                        result = future.result()
                        pass_results.extend(result)
                        increment = len(result) if isinstance(result, list) else 1
                        total_processed += increment
                        if main_pbar is not None:
                            try:
                                main_pbar.update(increment)
                            except Exception:
                                pass
                        elif show_progress:
                            try:
                                sys.stdout.write(f"Total AIS processed {total_processed}/{total_llinos}\r")
                                sys.stdout.flush()
                            except Exception:
                                pass
                    except Exception as exc:
                        logger.error("[parallel] Error in AIS worker thread: %s", exc, exc_info=True)

                if main_pbar is not None:
                    try:
                        main_pbar.close()
                    except Exception:
                        pass

        combined_results = upfront_skipped_results + pass_results
        return _order_results_like_input(target_llinos, combined_results)

    return _run_parallel_scrape_pass(llino_list, pass_label="Initial pass")


__all__ = [
    "DEFAULT_AIS_POSITIONS_CONFIG",
    "initialize_driver",
    "build_ais_period_windows",
    "scraping_ais_positions",
    "parallel_scraping_ais_positions",
    "worker_thread_ais_positions",
]
