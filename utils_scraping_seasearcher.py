# =========================================================
# 基本ライブラリ
# =========================================================
import os
import gc
import sys
import re
import json
import base64
import shutil
import time
import random
import logging
import threading
from datetime import date, datetime
from pathlib import Path
from io import StringIO
from concurrent.futures import ThreadPoolExecutor, as_completed

# =========================================================
# データ操作
# =========================================================
import pandas as pd
import polars as pl

# =========================================================
# Selenium / WebDriver
# =========================================================
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    StaleElementReferenceException,
    NoSuchElementException,
    TimeoutException,
    ElementClickInterceptedException,
    ElementNotInteractableException,
    WebDriverException,
)

# =========================================================
# WebDriver マネージャ（webdriver-manager を利用）
# =========================================================
from webdriver_manager.chrome import ChromeDriverManager

# Optional progress bar (tqdm). Import if available, else fallback to simple prints.
try:
    from tqdm.auto import tqdm
except Exception:
    tqdm = None



# web操作関数
# ---------------------------
# ログのセットアップ
# ---------------------------
logger = logging.getLogger("scraping_seasearcher")
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(ch)
logger.setLevel(logging.INFO)
# Reduce verbose selenium logs
logging.getLogger('selenium').setLevel(logging.WARNING)

COMPLETE_LOG_LEVEL = 25
if logging.getLevelName(COMPLETE_LOG_LEVEL) == f"Level {COMPLETE_LOG_LEVEL}":
    logging.addLevelName(COMPLETE_LOG_LEVEL, "DONE")


def _complete(self, message, *args, **kwargs):
    if self.isEnabledFor(COMPLETE_LOG_LEVEL):
        self._log(COMPLETE_LOG_LEVEL, message, args, **kwargs)


if not hasattr(logging.Logger, "complete"):
    logging.Logger.complete = _complete

# Credentials are supplied by the notebook config or environment variables.
# Do not store SeaSearcher credentials in source files.
DEFAULT_SEASEARCHER_LOGIN_USER = os.getenv("SEASEARCHER_LOGIN_USER")
DEFAULT_SEASEARCHER_LOGIN_PASSWORD = os.getenv("SEASEARCHER_LOGIN_PASSWORD")
_ALL_MOVEMENT_STATUSES = ("Calls", "Passings", "Sightings")
_ACTIVE_STATUS = {"Live", "Unconfirmed Existence", "Dead"}


def slugify_token(value, fallback: str = "value") -> str:
    text = str(value or "").strip()
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in text).strip("_")
    return slug or fallback


def format_period_label(period_cfg) -> str:
    if period_cfg is None:
        return "all"
    if isinstance(period_cfg, str) and period_cfg.lower() == "all":
        return "all"

    def _to_token(value):
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


def _format_llino(llino) -> str:
    return str(int(llino)).zfill(8)


def resolve_movement_status_list(status_cfg) -> list[str]:
    if status_cfg is None:
        return list(_ALL_MOVEMENT_STATUSES)
    if isinstance(status_cfg, str):
        text = status_cfg.strip()
        if not text or text.lower() == "all":
            return list(_ALL_MOVEMENT_STATUSES)
        raw_values = [text]
    else:
        raw_values = [str(value).strip() for value in status_cfg or [] if str(value).strip()]
        if not raw_values:
            return list(_ALL_MOVEMENT_STATUSES)

    normalized = []
    seen = set()
    valid_by_lower = {status.lower(): status for status in _ALL_MOVEMENT_STATUSES}
    invalid = []
    for raw in raw_values:
        resolved = valid_by_lower.get(raw.lower())
        if resolved is None:
            invalid.append(raw)
            continue
        if resolved not in seen:
            seen.add(resolved)
            normalized.append(resolved)

    if invalid:
        raise ValueError(f"Unsupported movement status value(s): {invalid}")
    return normalized or list(_ALL_MOVEMENT_STATUSES)


def build_movement_status_slug(status_cfg) -> str:
    resolved = resolve_movement_status_list(status_cfg)
    return slugify_token("_".join(resolved), "status")


def _normalize_text(value: str) -> str:
    return " ".join((value or "").split())


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


def _clear_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.iterdir():
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except Exception:
            logger.debug("Failed to clear directory %s", path, exc_info=True)


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
                digits = re.sub(r"[^\d]", "", _normalize_text(element.text))
                if digits:
                    return int(digits)
            except StaleElementReferenceException:
                continue
            except Exception:
                continue
    return None


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


def _get_visible_grid_rows(driver) -> list:
    row_xpaths = [
        "//tr[contains(@class,'parent-row')]",
        "//tr[contains(@class,'lli-table__row')]",
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


def _capture_grid_state(driver) -> dict:
    visible_rows = _get_visible_grid_rows(driver)
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
    }


def _grid_state_changed(previous_state: dict | None, current_state: dict | None) -> bool:
    if previous_state is None or current_state is None:
        return False
    return (
        previous_state.get("row_count") != current_state.get("row_count")
        or previous_state.get("signature") != current_state.get("signature")
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
    _raise_if_chrome_page_error(driver)
    deadline = time.time() + timeout
    stable_state = None
    stable_hits = 0
    while time.time() < deadline:
        _raise_if_chrome_page_error(driver)
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
    _raise_if_chrome_page_error(driver)
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
    _raise_if_chrome_page_error(driver)
    # next_page() already confirms the page number changed.  Keep only a short
    # staleness check here so virtualized rows do not add up to 8 seconds/page.
    stale_observed = _wait_for_optional_staleness(previous_state.get("first_row"), timeout=min(1, timeout))
    current_state = _wait_for_loaded_grid(driver, timeout=timeout, min_rows=1)
    if current_state["row_count"] > 0 and (stale_observed or _grid_state_changed(previous_state, current_state)):
        return current_state

    deadline = time.time() + min(2, timeout)
    last_state = current_state
    while time.time() < deadline:
        time.sleep(0.5)
        _raise_if_chrome_page_error(driver)
        candidate = _capture_grid_state(driver)
        last_state = candidate
        if candidate["row_count"] == 0:
            continue
        if stale_observed or _grid_state_changed(previous_state, candidate):
            return candidate

    if last_state["row_count"] == 0:
        raise TimeoutException("Next page loaded without visible rows")
    raise TimeoutException("Pagination moved but grid contents did not change")


def _toolbar_ready(driver, timeout: int = 20) -> None:
    def _has_items_per_page_text(drv) -> bool:
        for element in drv.find_elements(By.XPATH, "//*[contains(normalize-space(.), 'items per page')]"):
            try:
                if element.is_displayed():
                    return True
            except StaleElementReferenceException:
                continue
        return False

    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.XPATH, "//*[@data-testid='tableSizeSelector']"))
    )
    WebDriverWait(driver, timeout).until(_has_items_per_page_text)


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
                lambda d: next((el for el in d.find_elements(By.XPATH, xpath) if el.is_displayed()), None)
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

    WebDriverWait(driver, timeout).until(_is_1000_selected)


def _clear_performance_log(driver) -> None:
    try:
        driver.get_log("performance")
    except Exception:
        logger.debug("Could not clear Chrome performance log", exc_info=True)


def _release_vessel_page(driver) -> None:
    """Drop the current page without closing Chrome or logging in again."""
    _clear_performance_log(driver)
    try:
        driver.get("about:blank")
    except Exception:
        # If the renderer is already unhealthy, the normal page-reload recovery
        # will handle it on the next attempt.
        logger.debug("Could not release the current Chrome page", exc_info=True)


def _movement_value(value, default="-") -> str:
    if value is None or value == "":
        return default
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _movement_datetime(value, *, date_only=False) -> str:
    if value in (None, ""):
        return "-"
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return str(value)
    return parsed.strftime("%d/%m/%Y" if date_only else "%H:%M GMT %d/%m/%Y")


def _movement_eta(value) -> str:
    if value in (None, ""):
        return ""
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    return str(value) if pd.isna(parsed) else parsed.strftime("%d %b %Y")


def _movement_distance(value) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.1f}nm"
    except (TypeError, ValueError):
        text = str(value)
        return text if text.lower().endswith("nm") else f"{text}nm"


def _movement_records_to_frame(records: list[dict], llino) -> pd.DataFrame:
    rows = []
    for record in records:
        place = record.get("place")
        place_name = place.get("name") if isinstance(place, dict) else place
        country_name = record.get("countryName")
        if not country_name and isinstance(place, dict):
            country = place.get("country")
            country_name = country.get("name") if isinstance(country, dict) else country

        status = _movement_value(record.get("status"), "").capitalize()
        distance = _movement_distance(record.get("distance"))

        destination = _movement_value(record.get("destination"), "")
        eta = _movement_eta(record.get("eta"))
        if eta and "ETA:" not in destination.upper():
            destination = f"{destination} ETA: {eta}".strip()

        rows.append(
            {
                "LLI NO": int(llino),
                "Place": _movement_value(place_name or record.get("port")),
                "Country/Region": _movement_value(country_name),
                "Type": _movement_value(record.get("type")),
                "Status and Distance": " ".join(part for part in (status, distance) if part),
                "From": _movement_datetime(record.get("from"), date_only=bool(record.get("noFromTime"))),
                "To": _movement_datetime(record.get("to"), date_only=bool(record.get("noToTime"))),
                "Duration": _movement_value(record.get("durationHumanized") or record.get("duration")),
                "Destination": destination or "-",
                "Passing details": _movement_value(record.get("details")),
                "Draught (m)": _movement_value(record.get("draught")),
                "Course over ground": _movement_value(record.get("cog")),
                "Speed over ground": _movement_value(record.get("sog")),
                "Sightings updates": _movement_value(record.get("updates")),
            }
        )
    return pd.DataFrame(rows)


def _read_current_movement_grid(
    driver,
    llino,
    expected_rows: int,
    expected_total: int,
    timeout: int = 30,
) -> pd.DataFrame:
    _raise_if_chrome_page_error(driver)
    deadline = time.time() + timeout
    pending_request_ids = set()
    seen_sizes = set()

    while time.time() < deadline:
        _raise_if_chrome_page_error(driver)
        for entry in driver.get_log("performance"):
            try:
                message = json.loads(entry["message"])["message"]
                if message.get("method") != "Network.responseReceived":
                    continue
                params = message.get("params", {})
                response = params.get("response", {})
                if "/api/vessel/ports" not in response.get("url", ""):
                    continue
                pending_request_ids.add(params["requestId"])
            except (KeyError, TypeError, ValueError):
                continue

        for request_id in list(pending_request_ids):
            try:
                response_body = driver.execute_cdp_cmd(
                    "Network.getResponseBody",
                    {"requestId": request_id},
                )
                body = response_body.get("body", "")
                if response_body.get("base64Encoded"):
                    body = base64.b64decode(body).decode("utf-8")
                payload = json.loads(body)
            except Exception:
                continue

            pending_request_ids.discard(request_id)
            records = payload.get("results") if isinstance(payload, dict) else None
            total_matches = payload.get("totalMatches") if isinstance(payload, dict) else None
            if not isinstance(records, list):
                continue
            seen_sizes.add(len(records))
            if len(records) != expected_rows or int(total_matches or 0) != expected_total:
                continue
            if any(not isinstance(record, dict) for record in records):
                raise ValueError("Movement response contains an unexpected row format")
            return _movement_records_to_frame(records, llino)

        time.sleep(0.25)

    raise TimeoutException(
        f"Movement response was not captured: expected_rows={expected_rows}, "
        f"expected_total={expected_total}, seen_sizes={sorted(seen_sizes)}"
    )


def _requested_period_ends_before_available_data(driver, period_cfg) -> bool:
    if not isinstance(period_cfg, dict) or not period_cfg.get("to"):
        return False
    from_input = _visible_by_xpath(driver, "//input[@placeholder='From']")
    if from_input is None:
        return False
    earliest_text = (from_input.get_attribute("value") or "").strip()
    requested_to_text = _format_date_for_input(period_cfg["to"])
    try:
        earliest = datetime.strptime(earliest_text, "%d/%m/%Y")
        requested_to = datetime.strptime(requested_to_text, "%d/%m/%Y")
    except (TypeError, ValueError):
        return False
    return earliest > requested_to


def _build_movement_output_path(llino, cfg=None) -> Path:
    cfg = cfg or {}
    out_dir = Path(cfg.get("out_dir", DEFAULT_SCRAPING_CONFIG["out_dir"]))
    period_label = format_period_label(cfg.get("period"))
    return out_dir / f"movement_{_format_llino(llino)}_{period_label}.csv"


def _collect_saved_movements_index(cfg=None) -> set[str]:
    cfg = cfg or {}
    out_dir = Path(cfg.get("out_dir", DEFAULT_SCRAPING_CONFIG["out_dir"]))
    if not out_dir.exists():
        return set()

    period_label = format_period_label(cfg.get("period"))
    pattern = re.compile(
        rf"^movement_(\d{{8}})_{re.escape(period_label)}\.csv$",
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
        "rows": 0,
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
        "rows": 0,
        "elapsed": 0.0,
        "note": "known_no_data",
    }


_MOVEMENT_NO_DATA_CACHE_LOCK = threading.Lock()


def _movement_no_data_cache_path(cfg=None) -> Path:
    out_dir = Path((cfg or {}).get("out_dir", DEFAULT_SCRAPING_CONFIG["out_dir"]))
    period_label = format_period_label((cfg or {}).get("period"))
    status_label = build_movement_status_slug((cfg or {}).get("status_list", DEFAULT_SCRAPING_CONFIG["status_list"]))
    return out_dir / f".movement_no_data_{period_label}_{status_label}.txt"


def _collect_known_no_data_index(cfg=None) -> set[str]:
    cache_path = _movement_no_data_cache_path(cfg)
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
    cache_path = _movement_no_data_cache_path(cfg)

    with _MOVEMENT_NO_DATA_CACHE_LOCK:
        if token in cached_index:
            return
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(f"{token}\n")
        cached_index.add(token)


def _get_timeout_relogin_settings(config=None) -> dict:
    cfg = config or {}
    threshold = int(
        cfg.get(
            "timeout_streak_relogin_threshold",
            DEFAULT_SCRAPING_CONFIG["timeout_streak_relogin_threshold"],
        )
    )
    return {
        "enabled": bool(
            cfg.get(
                "relogin_after_timeout_streak",
                DEFAULT_SCRAPING_CONFIG["relogin_after_timeout_streak"],
            )
        ),
        "threshold": max(1, threshold),
        "retry_once": bool(
            cfg.get(
                "retry_timeout_once_after_relogin",
                DEFAULT_SCRAPING_CONFIG["retry_timeout_once_after_relogin"],
            )
        ),
    }


def _get_immediate_timeout_retry_settings(config=None) -> dict:
    cfg = config or {}
    return {
        "enabled": bool(
            cfg.get(
                "retry_timeout_immediately",
                DEFAULT_SCRAPING_CONFIG["retry_timeout_immediately"],
            )
        ),
        "attempts": max(
            0,
            int(
                cfg.get(
                    "timeout_immediate_retry_attempts",
                    DEFAULT_SCRAPING_CONFIG["timeout_immediate_retry_attempts"],
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
    )


def _is_renderer_timeout(value) -> bool:
    return "timed out receiving message from renderer" in str(value or "").lower()


def _is_webdriver_command_timeout(value) -> bool:
    text = str(value or "").lower()
    if "chromedriver command timed out" in text:
        return True
    return (
        "httpconnectionpool(host='localhost'" in text
        and ("read timed out" in text or "read timeout=" in text)
    )


def _is_page_load_stuck(value) -> bool:
    text = str(value or "").lower()
    return "movement page did not become ready" in text or "reload required" in text


def _is_chrome_memory_error(value) -> bool:
    text = str(value or "").lower()
    return any(
        marker in text
        for marker in (
            "out of memory",
            "memory pressure",
            "メモリ不足",
            "メモリが不足",
        )
    )


_CHROME_PAGE_ERROR_MARKERS = (
    "chrome-error://",
    "chrome error page detected",
    "this site can't be reached",
    "this site can’t be reached",
    "this page isn't working",
    "this page isn’t working",
    "this page is having a problem",
    "このサイトにアクセスできません",
    "このページは動作していません",
    "このページを開く際に問題が発生しました",
    "接続が拒否されました",
    "接続がリセットされました",
    "接続がタイムアウトしました",
)


def _looks_like_chrome_page_error(value) -> bool:
    text = str(value or "").lower()
    if _is_chrome_memory_error(text):
        return True
    if any(marker in text for marker in _CHROME_PAGE_ERROR_MARKERS):
        return True
    return re.search(r"\berr_[a-z0-9_]+\b", text) is not None


def _chrome_page_error_detail(driver, exception=None) -> str | None:
    """Return a short detail when Chrome is showing its own network/error page."""
    candidates = [("exception", str(exception or ""))]
    try:
        candidates.append(("url", str(driver.current_url or "")))
    except Exception:
        pass
    try:
        candidates.append(("title", str(driver.title or "")))
    except Exception:
        pass

    for label, value in candidates:
        if _looks_like_chrome_page_error(value):
            return f"{label}: {value[:240]}"

    # The URL can remain the original URL, so inspect the visible error text too.
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        body_text = (body.text or "").strip()
        if _looks_like_chrome_page_error(body_text):
            return f"body: {body_text[:240]}"
    except Exception:
        pass
    return None


def _raise_if_chrome_page_error(driver, exception=None) -> None:
    detail = _chrome_page_error_detail(driver, exception=exception)
    if detail:
        raise TimeoutException(f"Chrome error page detected ({detail})")


def _get_recoverable_error_retry_settings(config=None) -> dict:
    cfg = config or {}
    enabled = bool(
        cfg.get(
            "retry_recoverable_errors",
            DEFAULT_SCRAPING_CONFIG["retry_recoverable_errors"],
        )
    )
    attempts = max(
        0,
        int(
            cfg.get(
                "recoverable_error_retry_attempts",
                DEFAULT_SCRAPING_CONFIG["recoverable_error_retry_attempts"],
            )
        ),
    )
    return {"enabled": enabled and attempts > 0, "attempts": attempts}


def _is_recoverable_result(result) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("ok"):
        return False
    error_text = " ".join(
        str(result.get(key, "") or "")
        for key in ("error", "error_detail")
    ).lower()
    # Re-login only when the browser session itself is no longer usable.
    # UI timeouts and click/stale errors should keep the current session.
    recoverable_markers = (
        "chrome not reachable",
        "disconnected",
        "invalid session id",
        "no such window",
        "page crash",
    )
    return any(marker in error_text for marker in recoverable_markers)


def _get_periodic_rest_settings(config=None) -> dict:
    cfg = config or {}
    enabled = bool(
        cfg.get("periodic_rest_enabled", DEFAULT_SCRAPING_CONFIG["periodic_rest_enabled"])
    )
    work_session_hours = float(
        cfg.get("work_session_hours", DEFAULT_SCRAPING_CONFIG["work_session_hours"])
    )
    work_session_random_minutes = float(
        cfg.get(
            "work_session_random_minutes",
            DEFAULT_SCRAPING_CONFIG["work_session_random_minutes"],
        )
    )
    rest_session_hours = float(
        cfg.get("rest_session_hours", DEFAULT_SCRAPING_CONFIG["rest_session_hours"])
    )
    rest_session_random_minutes = float(
        cfg.get(
            "rest_session_random_minutes",
            DEFAULT_SCRAPING_CONFIG["rest_session_random_minutes"],
        )
    )
    return {
        "enabled": enabled and work_session_hours > 0 and rest_session_hours > 0,
        "work_session_hours": max(0.0, work_session_hours),
        "work_session_random_minutes": max(0.0, work_session_random_minutes),
        "rest_session_hours": max(0.0, rest_session_hours),
        "rest_session_random_minutes": max(0.0, rest_session_random_minutes),
    }


def _randomize_session_seconds(base_hours: float, random_minutes: float) -> float:
    base_seconds = max(0.0, float(base_hours)) * 3600.0
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

    continuous_elapsed = time.monotonic() - rest_state["continuous_work_started_at"]
    next_work_session_seconds = rest_state["next_work_session_seconds"]
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


def _apply_upfront_local_skip_filter(llino_list, cfg=None):
    llino_list = list(llino_list or [])
    use_saved_skip = bool((cfg or {}).get("skip_if_exists", True))
    use_known_no_data_skip = bool((cfg or {}).get("skip_if_known_no_data", True))
    if not use_saved_skip and not use_known_no_data_skip:
        return llino_list, [], set(), set()

    saved_outputs_index = _collect_saved_movements_index(cfg) if use_saved_skip else set()
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


def preview_existing_movement_outputs(llino_list, cfg=None) -> dict:
    llino_list = list(llino_list or [])
    pending_llinos, skipped_results, saved_outputs_index, known_no_data_index = _apply_upfront_local_skip_filter(
        llino_list,
        cfg,
    )
    return {
        "input_count": len(llino_list),
        "existing_count": len(skipped_results),
        "pending_count": len(pending_llinos),
        "existing_llinos": [res["llino"] for res in skipped_results],
        "pending_llinos": pending_llinos,
        "saved_outputs_index": saved_outputs_index,
        "known_no_data_index": known_no_data_index,
    }


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


def load_live_llinos_from_vessel_files(
    source_files,
    *,
    status_values=("Live",),
    llino_column="LLI NO",
    unique=True,
    sort=True,
):
    source_paths = [Path(path) for path in source_files or []]
    if not source_paths:
        raise ValueError("source_files must contain at least one CSV path")

    normalized_statuses = [str(status).strip() for status in status_values or [] if str(status).strip()]
    summary_rows = []
    live_frames = []

    for source_path in source_paths:
        if not source_path.exists():
            raise FileNotFoundError(f"Vessel CSV not found: {source_path}")

        frame = pl.read_csv(source_path, ignore_errors=True)
        if llino_column not in frame.columns:
            raise ValueError(f"Missing required column '{llino_column}' in {source_path}")

        selected = frame
        if "Status" in frame.columns and normalized_statuses:
            selected = frame.filter(pl.col("Status").cast(pl.Utf8, strict=False).is_in(normalized_statuses))

        selected = selected.with_columns(
            pl.lit(str(source_path)).alias("__source_file"),
            pl.lit(source_path.name).alias("__source_name"),
            pl.col(llino_column).cast(pl.Int64, strict=False).alias(llino_column),
        ).drop_nulls([llino_column])

        summary_rows.append(
            {
                "source_file": str(source_path),
                "source_name": source_path.name,
                "rows": int(frame.height),
                "selected_rows": int(selected.height),
            }
        )
        live_frames.append(selected)

    combined_live_df = pl.concat(live_frames, how="diagonal_relaxed") if live_frames else pl.DataFrame()
    llino_series = combined_live_df.select(pl.col(llino_column)).to_series()
    if unique:
        llino_series = llino_series.unique()

    targets = llino_series.to_list()
    if sort:
        targets = sorted(int(llino) for llino in targets)
    else:
        targets = [int(llino) for llino in targets]

    return {
        "source_files": [str(path) for path in source_paths],
        "source_summary": pl.DataFrame(summary_rows),
        "live_vessel_df": combined_live_df,
        "targets": targets,
        "target_count": len(targets),
        "status_values": normalized_statuses,
        "llino_column": llino_column,
    }


def _resolve_login_credentials(config=None, *, username=None, password=None):
    if username is not None or password is not None:
        if not username or not password:
            raise ValueError("username and password must both be provided when overriding login credentials")
        return str(username), str(password)

    cfg = config or {}
    login_user = cfg.get("login_user")
    login_password = cfg.get("login_password")
    has_login_user = login_user not in (None, "")
    has_login_password = login_password not in (None, "")

    if has_login_user or has_login_password:
        if not (has_login_user and has_login_password):
            raise ValueError("login_user and login_password must both be set in config when overriding credentials")
        return str(login_user), str(login_password)

    if not (DEFAULT_SEASEARCHER_LOGIN_USER and DEFAULT_SEASEARCHER_LOGIN_PASSWORD):
        raise ValueError(
            "SeaSearcher credentials are not configured. "
            "Set login_user/login_password in the notebook config or "
            "SEASEARCHER_LOGIN_USER/SEASEARCHER_LOGIN_PASSWORD environment variables."
        )
    return DEFAULT_SEASEARCHER_LOGIN_USER, DEFAULT_SEASEARCHER_LOGIN_PASSWORD


def cmd_or_ctrl():
    """macならCOMMAND、Windows/LinuxならCONTROL"""
    return Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL


def _find_cached_chromedriver() -> str | None:
    cache_root = Path.home() / ".wdm" / "drivers" / "chromedriver"
    if not cache_root.exists():
        return None
    candidates = []
    try:
        candidates = [
            path
            for path in cache_root.rglob("chromedriver.exe")
            if path.is_file() and path.stat().st_size > 0
        ]
    except OSError:
        return None
    if not candidates:
        return None
    return str(max(candidates, key=lambda path: path.stat().st_mtime))


def _resolve_chromedriver_path() -> str:
    """Reuse a cached executable before webdriver_manager tries to overwrite it."""
    cached = _find_cached_chromedriver()
    if cached:
        return cached
    try:
        return ChromeDriverManager().install()
    except PermissionError:
        cached = _find_cached_chromedriver()
        if cached:
            logger.warning("webdriver_manager could not replace chromedriver; using cached executable: %s", cached)
            return cached
        raise


def initialize_driver(driver_opts=None, download_dir: str | Path | None = None):
    """
    各スレッドで使用する WebDriver を初期化します。

    引数
    ----------
    driver_opts : dict | None
        例: {"headless": True}
    """
    options = Options()
    # The current SeaSearcher UI can keep background requests open after the
    # controls are already usable.  Waiting for the full load event eventually
    # wedges Chrome's renderer during a long run, so rely on the explicit UI
    # waits below once DOMContentLoaded has fired.
    options.page_load_strategy = (driver_opts or {}).get("page_load_strategy", "eager")
    options.add_experimental_option('excludeSwitches', ['enable-logging'])
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    headless = True if driver_opts is None else bool(driver_opts.get('headless', True))
    if headless:
        # 新しい Chrome は '--headless=new' をサポートしますが、'--headless' のほうが幅広い互換性があります
        options.add_argument('--headless')

    # 安定性を高めるためのオプション
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--window-size=1920,1080')

    # 任意: driver_opts から追加の引数を読み込みます
    if driver_opts and 'extra_args' in driver_opts:
        for arg in driver_opts['extra_args']:
            options.add_argument(arg)

    download_path = Path(download_dir or Path.cwd()).resolve()
    download_path.mkdir(parents=True, exist_ok=True)
    prefs = {
        "download.default_directory": str(download_path),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "profile.default_content_settings.popups": 0,
        "safebrowsing.enabled": True,
        "plugins.always_open_pdf_externally": True,
    }
    options.add_experimental_option("prefs", prefs)

    logger.info(f"Chrome WebDriver を初期化しています (headless={headless}, download_dir={download_path})")
    try:
        driver = webdriver.Chrome(service=ChromeService(_resolve_chromedriver_path()), options=options)
    except Exception as e:
        logger.error(f"WebDriver の初期化に失敗しました: {e}", exc_info=True)
        raise

    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(download_path)},
        )
    except Exception:
        try:
            driver.execute_cdp_cmd(
                "Browser.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": str(download_path)},
            )
        except Exception:
            logger.debug("Failed to set explicit download behavior", exc_info=True)

    logger.info("WebDriver の初期化が完了しました")
    return driver

def login_to_seasearcher(driver, *, username=None, password=None, config=None):
    """
    指定された driver で Seasearcher にログイン。
    """
    driver.get("https://www.seasearcher.com/")
    WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "Login")))

    USER, PASS = _resolve_login_credentials(config, username=username, password=password)

    login_button = WebDriverWait(driver, 30).until(EC.element_to_be_clickable((By.ID, "Login")))
    login_button.click()
    time.sleep(5)

    email_input = WebDriverWait(driver, 30).until(EC.visibility_of_element_located((By.ID, "loginPage:loginForm:loginemail")))
    email_input.send_keys(USER)

    password_input = WebDriverWait(driver, 30).until(EC.visibility_of_element_located((By.ID, "loginPage:loginForm:loginpassword")))
    password_input.send_keys(PASS)

    submit_button = WebDriverWait(driver, 30).until(EC.element_to_be_clickable((By.ID, "loginPage:loginForm:login-submit")))
    submit_button.click()

    WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "app")))
    logger.info("ログインに成功しました。")

    return driver

# movement
def open_movement(driver, llino, *, reload_current_page=False):
    logger.info(f"LLI {llino} の Movements を開きます")
    if not reload_current_page:
        wait_time = random.uniform(2, 5)
        time.sleep(wait_time)
    movement_url = f'https://www.seasearcher.com/vessel/{llino}/movements'
    try:
        if reload_current_page:
            driver.refresh()
        else:
            driver.get(movement_url)
    except Exception as exc:
        if _is_webdriver_command_timeout(exc):
            raise TimeoutException(
                f"ChromeDriver command timed out while opening {movement_url}: {exc}"
            ) from exc
        if _looks_like_chrome_page_error(exc):
            raise TimeoutException(f"Chrome error page detected while opening {movement_url}: {exc}") from exc
        raise
    _raise_if_chrome_page_error(driver)
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH, "//input[@placeholder='From']"))
        )
    except Exception as exc:
        if _is_webdriver_command_timeout(exc):
            raise TimeoutException(f"ChromeDriver command timed out while waiting for From: {exc}") from exc
        if isinstance(exc, TimeoutException):
            raise TimeoutException(
                f"Movement page did not become ready; reload required while waiting for From: {exc}"
            ) from exc
        raise

# --- 内部ヘルパー（関数数は外向け2つのまま） ---
def clear_input_safely(driver, input_el):
    """
    入力欄のクリアを堅牢に：
    1) JSで value='' + input/change 発火（最優先・最安定）
    2) それでも残るならキー操作で全選択+Delete（OS別の修飾キー）
    """
    # 1) JSでクリア（React/Vue向けにイベントも発火）
    driver.execute_script("""
        const el = arguments[0];
        if (el) {
            el.value = '';
            el.dispatchEvent(new Event('input',  {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
        }
    """, input_el)

    # 2) 念のためキー操作でもクリア（ページ実装によってはJSだけだとカレンダーが開かない場合）
    try:
        input_el.click()
    except Exception:
        pass
    input_el.send_keys(cmd_or_ctrl(), "a")
    input_el.send_keys(Keys.DELETE)

# def _clear_input_and_open_calendar(driver, placeholder: str, timeout=5):
#     """プレースホルダで入力欄を特定→クリア→カレンダーを開く"""
#     box = WebDriverWait(driver, timeout).until(
#         EC.presence_of_element_located((By.XPATH, f"//input[@placeholder='{placeholder}']"))
#     )
#     try:
#         box.click()
#     except Exception:
#         pass
#     # mac / win 両対応（どちらかが効く）
#     box.send_keys(Keys.COMMAND, "a")
#     box.send_keys(Keys.DELETE)
#     box.send_keys(Keys.CONTROL, "a")
#     box.send_keys(Keys.DELETE)
#     # 念のためもう一度クリックしてカレンダーを確実に開く
#     try:
#         box.click()
#     except Exception:
#         pass
#     return box

def _clear_input_and_open_calendar(driver, placeholder: str, timeout=5):
    box = WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.XPATH, f"//input[@placeholder='{placeholder}']"))
    )
    try:
        box.click()
    except Exception:
        pass

    clear_input_safely(driver, box)  # ← ここだけ差し替え

    # カレンダーを確実に開きたい場合はクリックを明示
    try:
        box.click()
    except Exception:
        pass
    return box

def _click_extreme_date(driver, pick_first: bool) -> bool:
    """選択可能日(aria-disabled='false')から先頭/末尾をクリック"""
    try:
        WebDriverWait(driver, 3).until(
            EC.presence_of_all_elements_located((By.XPATH, "//div[@aria-disabled='false']"))
        )
        dates = driver.find_elements(By.XPATH, "//div[@aria-disabled='false']")
        if not dates:
            return False
        target = dates[0] if pick_first else dates[-1]
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'nearest'});", target)
        except Exception:
            pass
        target.click()
        return True
    except TimeoutException:
        logger.debug("カレンダーの選択待ちでタイムアウトしました")
        return False


# --- ここから外向けの2関数（数は据え置き） ---
def select_earliest(driver, max_hops: int = 120):
    """From の最古日付を選択"""
    _clear_input_and_open_calendar(driver, "From")
    # 「Previous」を最大 max_hops 回クリック（端到達でTimeoutException→break）
    hops = 0
    while hops < max_hops:
        try:
            prev_btn = WebDriverWait(driver, 1.5).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(@aria-label, 'Previous')]"))
            )
            prev_btn.click()
            hops += 1
        except TimeoutException:
            logger.debug(f"select_earliest: {hops} 回移動して最古に到達しました")
            break
    # 端に着いたら先頭日(=最古)をクリック
    _click_extreme_date(driver, pick_first=True)


def select_latest(driver, max_hops: int = 120):
    """To の最新日付を選択"""
    _clear_input_and_open_calendar(driver, "To")
    # 「Next」を最大 max_hops 回クリック（端到達でTimeoutException→break）
    hops = 0
    while hops < max_hops:
        try:
            next_btn = WebDriverWait(driver, 1.0).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(@aria-label, 'Next')]"))
            )
            next_btn.click()
            hops += 1
        except TimeoutException:
            logger.debug(f"select_latest: {hops} 回移動して最新に到達しました")
            break
    # 端に着いたら末尾日(=最新)をクリック
    _click_extreme_date(driver, pick_first=False)


def clear_period_to_all(driver):
    """
    Clear or reset the Period filter so that the full period ('All') is selected.
    Prioritize the specific date-range control (class: 'lli-range-input lli-range-input--date lli-range-filter--selected date-range--gray')
    and click its '.lli-dropdown--cancel' (or a 'Clear' button) if present. Falls back to clearing the From/To inputs.
    """
    logger.info("Period を 'All' にリセットします (date-range 優先)")

    def _try_click(el):
        try:
            el.click()
            return True
        except Exception:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            except Exception:
                pass
            try:
                driver.execute_script("arguments[0].click();", el)
                return True
            except Exception:
                return False

    try:
        # 1) 優先: date-range 要素内の cancel を探す
        date_range_xpath = (
            "//div[contains(concat(' ', normalize-space(@class), ' '), ' lli-range-input ')"
            " and contains(concat(' ', normalize-space(@class), ' '), ' lli-range-input--date ')"
            " and contains(concat(' ', normalize-space(@class), ' '), ' lli-range-filter--selected ')"
            " and contains(concat(' ', normalize-space(@class), ' '), ' date-range--gray ')]"
        )
        for el in driver.find_elements(By.XPATH, date_range_xpath):
            try:
                if not el.is_displayed():
                    continue
                try:
                    cancel = el.find_element(By.CSS_SELECTOR, ".lli-dropdown--cancel")
                    if _try_click(cancel):
                        return True
                except Exception:
                    # 'Clear' ボタンを探す
                    try:
                        btn = el.find_element(By.XPATH, ".//button[contains(normalize-space(.),'Clear')]")
                        if _try_click(btn):
                            return True
                    except Exception:
                        pass
            except StaleElementReferenceException:
                continue

        # 2) 旧フォールバック: Period ラベルの近くを探す
        period_container = None
        for el in driver.find_elements(By.XPATH, "//p[normalize-space(text())='Period']/ancestor::div[1]"):
            try:
                if el.is_displayed():
                    period_container = el
                    break
            except StaleElementReferenceException:
                continue
        if period_container is not None:
            try:
                cancel = period_container.find_element(By.CSS_SELECTOR, ".lli-dropdown--cancel")
                if _try_click(cancel):
                    return True
            except Exception:
                try:
                    btn = period_container.find_element(By.XPATH, ".//button[contains(normalize-space(.),'Clear')]")
                    if _try_click(btn):
                        return True
                except Exception:
                    pass

        # 3) 最終フォールバック: From / To を直接クリア
        try:
            _clear_input_and_open_calendar(driver, "From")
            box = WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.XPATH, "//input[@placeholder='From']")))
            clear_input_safely(driver, box)
        except Exception:
            logger.debug("From のクリアに失敗しました", exc_info=True)
        try:
            _clear_input_and_open_calendar(driver, "To")
            box = WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.XPATH, "//input[@placeholder='To']")))
            clear_input_safely(driver, box)
        except Exception:
            logger.debug("To のクリアに失敗しました", exc_info=True)
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            body.click()
        except Exception:
            pass
        return True
    except Exception as e:
        logger.debug("clear_period_to_all failed", exc_info=True)
        raise TimeoutException(f"clear_period_to_all failed: {e}")


def _format_date_for_input(date_val):
    """Accept date/datetime/date-string and return a string in 'DD/MM/YYYY' if possible.
    If parsing fails, return the original string.
    """
    from datetime import date as _date, datetime as _dt
    if date_val is None:
        return None
    if isinstance(date_val, _date):
        return date_val.strftime("%d/%m/%Y")
    if isinstance(date_val, _dt):
        return date_val.date().strftime("%d/%m/%Y")
    if isinstance(date_val, str):
        s = date_val.strip()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
            try:
                parsed = _dt.strptime(s, fmt)
                return parsed.strftime("%d/%m/%Y")
            except Exception:
                continue
        # give up and return as-is
        return s
    return str(date_val)


def _set_input_value(driver, placeholder: str, value: str):
    """Set the input with given placeholder to value using JS and dispatch events, then blur.
    """
    try:
        box = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, f"//input[@placeholder='{placeholder}']"))
        )
    except Exception:
        raise NoSuchElementException(f"Input with placeholder '{placeholder}' not found")

    # clear first
    clear_input_safely(driver, box)
    # set via JS + event
    try:
        driver.execute_script("arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', {bubbles:true})); arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", box, value)
    except Exception:
        # fallback to send_keys
        try:
            box.click()
            box.send_keys(value)
        except Exception:
            raise
    # blur/apply
    try:
        box.send_keys(Keys.TAB)
    except Exception:
        try:
            driver.execute_script("arguments[0].blur();", box)
        except Exception:
            pass


def set_period_to_dates(
    driver,
    from_date=None,
    to_date=None,
    allow_from_earliest_fallback=True,
    allow_to_latest_fallback=True,
):
    """Set From/To period inputs to the specified dates (each can be None to skip).
    Preferably targets the two inputs inside the date-range control and sets them sequentially
    (From first, then To), verifying the value after each set and retrying if necessary.
    Dates are formatted to 'DD/MM/YYYY' when possible.
    """
    logger.info(f"Period を設定します: from={from_date}, to={to_date}")
    fr = _format_date_for_input(from_date)
    to = _format_date_for_input(to_date)

    # Find the primary date-range container if present
    date_range_xpath = (
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' lli-range-input ')"
        " and contains(concat(' ', normalize-space(@class), ' '), ' lli-range-input--date ')]"
    )
    date_container = None
    try:
        for el in driver.find_elements(By.XPATH, date_range_xpath):
            try:
                if el.is_displayed():
                    date_container = el
                    break
            except StaleElementReferenceException:
                continue
    except Exception:
        date_container = None

    # Helper to find inputs (prefer container-scoped)
    def _find_input(placeholder: str):
        if date_container is not None:
            try:
                return date_container.find_element(By.XPATH, f".//input[@placeholder='{placeholder}']")
            except Exception:
                pass
        try:
            return driver.find_element(By.XPATH, f"//input[@placeholder='{placeholder}']")
        except Exception:
            return None

    def _find_inputs():
        return _find_input("From"), _find_input("To")

    def _read_value(input_el):
        try:
            return (
                driver.execute_script(
                    "return arguments[0].value || arguments[0].getAttribute('value') || '';",
                    input_el,
                )
                or ""
            ).strip()
        except Exception:
            return ""

    def _verify_value(placeholder: str, expected: str) -> bool:
        if expected is None:
            return True
        input_el = _find_input(placeholder)
        if input_el is None:
            return False
        return _read_value(input_el) == expected

    def _wait_until_value(placeholder: str, expected: str, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _verify_value(placeholder, expected):
                return True
            time.sleep(0.15)
        return _verify_value(placeholder, expected)

    def _set_value_via_keyboard(input_el, expected: str) -> None:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", input_el)
        try:
            input_el.click()
        except Exception:
            driver.execute_script("arguments[0].focus();", input_el)
        input_el.send_keys(cmd_or_ctrl(), "a")
        input_el.send_keys(Keys.DELETE)
        input_el.send_keys(expected)
        input_el.send_keys(Keys.ENTER)
        input_el.send_keys(Keys.TAB)

    def _set_value_via_native_setter(input_el, expected: str) -> None:
        driver.execute_script(
            """
            const el = arguments[0];
            const value = arguments[1];
            const descriptor =
                Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value') ||
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
            if (descriptor && descriptor.set) {
                descriptor.set.call(el, value);
            } else {
                el.value = value;
            }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
            """,
            input_el,
            expected,
        )
        try:
            input_el.send_keys(Keys.TAB)
        except Exception:
            pass

    def _set_el_value(placeholder: str, expected: str):
        """Attempt several strategies to set the value on the given input element."""
        if expected is None:
            return True
        attempts = 4
        for _ in range(attempts):
            input_el = _find_input(placeholder)
            if input_el is None:
                break
            try:
                _set_value_via_keyboard(input_el, expected)
                if _wait_until_value(placeholder, expected):
                    return True
            except Exception:
                logger.debug("_set_el_value: keyboard attempt failed", exc_info=True)
            time.sleep(0.2)

            input_el = _find_input(placeholder)
            if input_el is None:
                break
            try:
                _set_value_via_native_setter(input_el, expected)
                if _wait_until_value(placeholder, expected):
                    return True
            except Exception:
                logger.debug("_set_el_value: native setter attempt failed", exc_info=True)
            time.sleep(0.2)
        return False

    def _fallback_from_to_earliest(original_expected: str) -> str | None:
        logger.warning(
            "From の設定に失敗しました: %s。選択可能な最古日付へフォールバックします。",
            original_expected,
        )
        try:
            select_earliest(driver)
            time.sleep(0.3)
            actual_from = _read_value(_find_input("From")) if _find_input("From") is not None else ""
            actual_from = (actual_from or "").strip()
            if actual_from:
                logger.info("From のフォールバック結果: %s", actual_from)
                return actual_from
        except Exception:
            logger.debug("From の最古日付フォールバックに失敗しました", exc_info=True)
        return None

    def _fallback_to_latest(original_expected: str) -> str | None:
        logger.warning(
            "To の設定に失敗しました: %s。選択可能な最新日付へフォールバックします。",
            original_expected,
        )
        try:
            select_latest(driver)
            time.sleep(0.3)
            to_input = _find_input("To")
            actual_to = _read_value(to_input) if to_input is not None else ""
            actual_to = (actual_to or "").strip()
            if actual_to:
                requested_date = datetime.strptime(original_expected, "%d/%m/%Y")
                actual_date = datetime.strptime(actual_to, "%d/%m/%Y")
                if actual_date <= requested_date:
                    logger.info("To のフォールバック結果: %s", actual_to)
                    return actual_to
                logger.warning(
                    "To fallback was rejected because it exceeds the requested end date: %s",
                    actual_to,
                )
        except Exception:
            logger.debug("To の最新日付フォールバックに失敗しました", exc_info=True)
        return None

    fr_el, to_el = _find_inputs()

    def _parse_input_date(value: str):
        try:
            return datetime.strptime((value or "").strip(), "%d/%m/%Y")
        except (TypeError, ValueError):
            return None

    requested_from_date = _parse_input_date(fr)
    requested_to_date = _parse_input_date(to)

    # Set From then To sequentially
    if fr is not None:
        if fr_el is None:
            raise NoSuchElementException("From input が見つかりません")
        ok = _set_el_value("From", fr)
        if not ok:
            if allow_from_earliest_fallback:
                fallback_from = _fallback_from_to_earliest(fr)
                if fallback_from:
                    fr = fallback_from
                else:
                    raise TimeoutException(f"Failed to set From to {fr}")
            else:
                logger.warning(
                    "From could not be set exactly and earliest fallback is disabled: %s",
                    fr,
                )
                raise TimeoutException(f"Failed to set From to {fr}")
        time.sleep(random.uniform(0.2, 0.6))

    def _requested_to_precedes_actual_from() -> bool:
        if to is None:
            return False
        from_input = _find_input("From")
        actual_from = _read_value(from_input) if from_input is not None else ""
        try:
            actual_from_date = datetime.strptime(actual_from, "%d/%m/%Y")
            requested_to_date = datetime.strptime(to, "%d/%m/%Y")
        except ValueError:
            return False
        return actual_from_date > requested_to_date

    if to is not None:
        if to_el is None:
            raise NoSuchElementException("To input が見つかりません")
        ok = _set_el_value("To", to)
        if not ok:
            if _requested_to_precedes_actual_from():
                logger.info("Requested period has no overlap with available Movement data")
                return False
            if allow_to_latest_fallback:
                fallback_to = _fallback_to_latest(to)
                if fallback_to:
                    to = fallback_to
                else:
                    raise TimeoutException(f"Failed to set To to {to}")
            else:
                logger.warning(f"To の設定に失敗しました: {to}")
                raise TimeoutException(f"Failed to set To to {to}")
        time.sleep(random.uniform(0.2, 0.6))

    # click outside to ensure UI applies
    try:
        driver.find_element(By.TAG_NAME, "body").click()
    except Exception:
        pass

    # final verification
    try:
        if fr is not None and not _verify_value("From", fr):
            actual_from = _read_value(_find_input("From")) if _find_input("From") is not None else ""
            requested_from = _parse_input_date(fr)
            clamped_from = _parse_input_date(actual_from)
            if allow_from_earliest_fallback and clamped_from and clamped_from >= requested_from:
                logger.info("From was clamped to the vessel's earliest date: %s", actual_from)
                fr = actual_from
            elif allow_from_earliest_fallback:
                fallback_from = _fallback_from_to_earliest(fr)
                fallback_from_date = _parse_input_date(fallback_from)
                if fallback_from_date and requested_from and fallback_from_date >= requested_from:
                    fr = fallback_from
                    if requested_to_date and fallback_from_date > requested_to_date:
                        logger.info("Requested period has no overlap with available Movement data")
                        return False
                else:
                    raise TimeoutException(f"From final verification failed (expected={fr}, actual={actual_from})")
            else:
                raise TimeoutException(f"From final verification failed (expected={fr}, actual={actual_from})")
        if to is not None and not _verify_value("To", to):
            actual_to = _read_value(_find_input("To")) if _find_input("To") is not None else ""
            if _requested_to_precedes_actual_from():
                logger.info("Requested period has no overlap with available Movement data")
                return False
            if allow_to_latest_fallback:
                fallback_to = _fallback_to_latest(to)
                if fallback_to and _verify_value("To", fallback_to):
                    to = fallback_to
                else:
                    raise TimeoutException(f"To の最終確認に失敗しました (expected={to}, actual={actual_to})")
            else:
                raise TimeoutException(f"To の最終確認に失敗しました (expected={to}, actual={actual_to})")
    except Exception:
        raise

    final_from = _read_value(_find_input("From")) if _find_input("From") is not None else ""
    final_to = _read_value(_find_input("To")) if _find_input("To") is not None else ""
    final_from_date = _parse_input_date(final_from)
    final_to_date = _parse_input_date(final_to)
    no_overlap = (
        (final_from_date and requested_to_date and final_from_date > requested_to_date)
        or (final_to_date and requested_from_date and final_to_date < requested_from_date)
        or (final_from_date and final_to_date and final_from_date > final_to_date)
    )
    if no_overlap:
        logger.info("Requested period has no overlap with available Movement data")
        return False

    return True


def set_period(
    driver,
    period_cfg,
    allow_from_earliest_fallback=True,
    allow_to_latest_fallback=True,
):
    """Dispatch period configuration.
    - period_cfg == 'all' or None -> clear to All
    - period_cfg is dict-like with 'from'/'to' keys -> set those dates
    """
    if period_cfg is None or (isinstance(period_cfg, str) and period_cfg.lower() == "all"):
        return clear_period_to_all(driver)
    # dict-like
    try:
        if isinstance(period_cfg, dict):
            return set_period_to_dates(
                driver,
                period_cfg.get('from'),
                period_cfg.get('to'),
                allow_from_earliest_fallback=allow_from_earliest_fallback,
                allow_to_latest_fallback=allow_to_latest_fallback,
            )
        # also accept tuple/list (from,to)
        if isinstance(period_cfg, (list, tuple)) and len(period_cfg) == 2:
            return set_period_to_dates(
                driver,
                period_cfg[0],
                period_cfg[1],
                allow_from_earliest_fallback=allow_from_earliest_fallback,
                allow_to_latest_fallback=allow_to_latest_fallback,
            )
        # unknown format: attempt to interpret as 'from' only
        return set_period_to_dates(
            driver,
            period_cfg,
            None,
            allow_from_earliest_fallback=allow_from_earliest_fallback,
            allow_to_latest_fallback=allow_to_latest_fallback,
        )
    except Exception as e:
        logger.debug("set_period failed", exc_info=True)
        raise TimeoutException(f"set_period failed: {e}")


def select_status(driver, select_list=('Calls',)):

    # --- 引数の正規化 ---
    cfg = {}
    llino = "unknown"
    target_set = set(resolve_movement_status_list(select_list))

    # --- helpers ---
    def close_cookie_if_any():
        try:
            WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.CLASS_NAME, "cookie-close-button"))
            ).click()
        except Exception:
            pass

    def _close_transient_overlays():
        """Attempt to remove or dismiss common transient overlays (cookie bar, toasts, chat iframes).
        This helps avoid ElementClickInterceptedException where such overlays cover targets."""
        try:
            driver.execute_script("""
                try {
                    const c = document.querySelector('.CookieConsent'); if (c) c.style.display='none';
                    const cs = Array.from(document.querySelectorAll('[data-sonner-toast] [data-close-button]'));
                    cs.forEach(b => { try { b.click(); } catch(e){} });
                    const ts = Array.from(document.querySelectorAll('[data-sonner-toaster], [data-sonner-toast]')); ts.forEach(t=>t.remove());
                    const launcher = document.getElementById('launcher'); if (launcher) launcher.style.display='none';
                    const intercom = document.getElementById('intercom-frame'); if (intercom) intercom.style.display='none';
                } catch (e) {}
            """)
        except Exception:
            # Best-effort; do not fail the main flow
            logger.debug("_close_transient_overlays failed", exc_info=True)

    def _element_center(el):
        try:
            r = driver.execute_script("const r = arguments[0].getBoundingClientRect(); return {x:r.x, y:r.y, w:r.width, h:r.height, cx:r.x + r.width/2, cy:r.y + r.height/2};", el)
            return r
        except Exception:
            return None

    def _safe_click(el, retries: int = 3, backoff: float = 0.2):
        last_exc = None
        for i in range(retries):
            try:
                el.click()
                return
            except ElementClickInterceptedException as e:
                last_exc = e
                logger.warning(f"[intercepted] クリックが遮られました（試行 {i+1}/{retries}）: {e}")
                # log elementFromPoint at element center
                try:
                    rc = _element_center(el)
                    if rc:
                        try:
                            top = driver.execute_script("const el = document.elementFromPoint(arguments[0], arguments[1]); return el ? (el.outerHTML || el.tagName) : null;", rc['cx'], rc['cy'])
                        except Exception:
                            top = None
                        logger.debug(f"要素中心の最上位要素: {top}")
                except Exception:
                    pass
                # オーバーレイを除去して JS クリック/スクロールで再試行
                _close_transient_overlays()
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                except Exception:
                    pass
                time.sleep(backoff)
                try:
                    driver.execute_script("arguments[0].click();", el)
                    return
                except Exception as e_js:
                    logger.debug(f"JSクリックに失敗: {e_js}", exc_info=True)
                time.sleep(backoff)
            except ElementNotInteractableException as e:
                last_exc = e
                logger.warning(f"要素が操作可能ではありません: {e}")
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                except Exception:
                    pass
                time.sleep(backoff)
            except Exception as e:
                last_exc = e
                logger.debug("クリック時の予期せぬ例外", exc_info=True)
                time.sleep(backoff)
        # If we reach here, re-raise the last known exception
        if last_exc:
            raise last_exc

    def get_visible_options():
        """画面上で可視な .lli-dropdown__options-wrapper をグローバルに取得（ポータル対応）"""
        # まずは通常の手段
        for el in driver.find_elements(By.CSS_SELECTOR, ".lli-dropdown__options-wrapper"):
            try:
                if el.is_displayed():
                    return el
            except StaleElementReferenceException:
                continue
        # JS フォールバック（offsetParent で簡易可視判定）
        el = driver.execute_script("""
            const list = Array.from(document.querySelectorAll('.lli-dropdown__options-wrapper'));
            return list.find(e => e && e.offsetParent !== null) || null;
        """)
        return el or None

    def ensure_open_and_get_options():
        trigger = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.CLASS_NAME, "lli-dropdown__trigger"))
        )
        opts = get_visible_options()
        if not opts:  # 閉じているので開く
            # クリック妨害を避けるため、安全なクリックを使用
            _safe_click(trigger)
            WebDriverWait(driver, 5).until(lambda d: get_visible_options() is not None)
            opts = get_visible_options()
        return trigger, opts

    def _xpath_text_literal(s: str) -> str:
        if "'" not in s:
            return f"'{s}'"
        parts = s.split("'")
        # 例: ab'c -> concat('ab', "'", 'c')
        return "concat(" + ", ".join([f"'{p}'" for p in parts[:-1]] + ["\"'\"", f"'{parts[-1]}'"]) + ")"

    def row_xpath_by_name(name: str) -> str:
        lit = _xpath_text_literal(name.strip())
        return (
            ".//label[contains(@class,'lli-dropdown__label')]"
            "[.//span[contains(@class,'lli-dropdown__label-text') and normalize-space(text())=" + lit + "]]"
        )

    def current_checked(options_el, name: str) -> bool:
        row = options_el.find_element(By.XPATH, row_xpath_by_name(name))
        cb = row.find_element(By.CSS_SELECTOR, "input.lli-checkbox__input")
        return cb.is_selected()

    def set_checked(options_el, name: str, want_on: bool, retries: int = 2) -> bool:
        for attempt in range(retries + 1):
            try:
                row = options_el.find_element(By.XPATH, row_xpath_by_name(name))
                # スクロール（必要なら）
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'nearest', inline:'nearest'});", row)
                except Exception:
                    pass
                is_on = row.find_element(By.CSS_SELECTOR, "input.lli-checkbox__input").is_selected()
                if is_on == want_on:
                    return False  # 変更なし
                # ラベルクリック（安全なクリックヘルパーを使用）
                lbl = row.find_element(By.CLASS_NAME, "lli-dropdown__label-text")
                _safe_click(lbl)
                # 状態反映を“再取得”で待機（stale 対策）
                WebDriverWait(driver, 5).until(lambda d: current_checked(options_el, name) == want_on)
                return True
            except (StaleElementReferenceException, NoSuchElementException):
                if attempt == retries:
                    raise
            except TimeoutException:
                # 最後に直読みで確認
                try:
                    if current_checked(options_el, name) == want_on:
                        return True
                except Exception:
                    pass
                if attempt == retries:
                    raise
        return False

    # --- main flow ---
    close_cookie_if_any()

    # 1) ドロップダウンを開いて可視 options を取得（ポータル対応）
    trigger, options = ensure_open_and_get_options()

    # 2) 現在のラベル一覧
    labels = options.find_elements(By.CSS_SELECTOR, "label.lli-dropdown__label")
    names_now = []
    for lb in labels:
        try:
            names_now.append(lb.find_element(By.CLASS_NAME, "lli-dropdown__label-text").text.strip())
        except StaleElementReferenceException:
            # 一度だけ取り直し
            labels = options.find_elements(By.CSS_SELECTOR, "label.lli-dropdown__label")
            names_now = [el.find_element(By.CLASS_NAME, "lli-dropdown__label-text").text.strip() for el in labels]
            break
    names_set = set(names_now)

    # 与えられた名前が一つも UI に無いときは安全のため例外にする（好みに応じて無視にしてOK）
    if not (target_set & names_set):
        raise NoSuchElementException(f"None of {sorted(target_set)} were found in dropdown: {sorted(names_now)}")

    # 3) target_set を ON
    for name in (n for n in target_set if n in names_set):
        try:
            changed = set_checked(options, name, True)
        except ElementClickInterceptedException as e:
            # クリック遮蔽時の診断を収集
            try:
                debug_base = Path(cfg.get('out_dir', 'movement/test')) / 'debug'
                debug_base.mkdir(parents=True, exist_ok=True)
                ts = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
                png_path = debug_base / f"llino_{llino}_select_status_click_intercept_{name}_{ts}.png"
                html_path = debug_base / f"llino_{llino}_select_status_click_intercept_{name}_{ts}.html"
                try:
                    driver.save_screenshot(str(png_path))
                    logger.info(f"スクリーンショットを保存しました: {png_path}")
                except Exception as e_s:
                    logger.debug(f"スクリーンショットの保存に失敗しました: {e_s}", exc_info=True)
                try:
                    page_html = driver.execute_script("return document.documentElement.outerHTML;")
                    html_path.write_text(page_html, encoding='utf-8')
                    logger.info(f"ページHTMLを保存しました: {html_path}")
                except Exception as e_h:
                    logger.debug(f"ページHTMLの保存に失敗しました: {e_h}", exc_info=True)
            except Exception:
                logger.debug("クリック遮蔽時の診断収集に失敗しました", exc_info=True)
            raise TimeoutException(f"Click intercepted when toggling '{name}': {e}") from e
        # print(f"{'✓' if changed else '='} {name}: ON")

    # 4) その他を OFF
    for name in (n for n in names_now if n not in target_set):
        try:
            changed = set_checked(options, name, False)
        except ElementClickInterceptedException as e:
            # クリック遮蔽時の診断を収集
            try:
                debug_base = Path(cfg.get('out_dir', 'movement/test')) / 'debug'
                debug_base.mkdir(parents=True, exist_ok=True)
                ts = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
                png_path = debug_base / f"llino_{llino}_select_status_click_intercept_{name}_{ts}.png"
                html_path = debug_base / f"llino_{llino}_select_status_click_intercept_{name}_{ts}.html"
                try:
                    driver.save_screenshot(str(png_path))
                    logger.info(f"スクリーンショットを保存しました: {png_path}")
                except Exception as e_s:
                    logger.debug(f"スクリーンショットの保存に失敗しました: {e_s}", exc_info=True)
                try:
                    page_html = driver.execute_script("return document.documentElement.outerHTML;")
                    html_path.write_text(page_html, encoding='utf-8')
                    logger.info(f"ページHTMLを保存しました: {html_path}")
                except Exception as e_h:
                    logger.debug(f"ページHTMLの保存に失敗しました: {e_h}", exc_info=True)
            except Exception:
                logger.debug("クリック遮蔽時の診断収集に失敗しました", exc_info=True)
            raise TimeoutException(f"Click intercepted when toggling '{name}': {e}") from e
        # print(f"{'✗' if changed else '='} {name}: OFF")

    # 5) 選択したステータスのフィルタにある「キャンセル / クリア」ボタンをクリックして確定する（存在するなら）
    try:
        for container in driver.find_elements(By.CSS_SELECTOR, ".lli-dropdown, .lli-filter, .lli-dropdown--filter-bar, .lli-range-input"):
            try:
                # ラベル要素（例: Calls）
                lbl = container.find_element(By.CSS_SELECTOR, ".filter-label__flex")
                txt = lbl.text.strip()
                if txt in target_set:
                    try:
                        cancel = container.find_element(By.CSS_SELECTOR, ".lli-dropdown--cancel")
                        try:
                            # 安全クリックを試す
                            _safe_click(cancel)
                            logger.info(f"フィルタのキャンセルボタンをクリックしました: {txt}")
                        except Exception as e:
                            logger.debug(f"キャンセルボタンクリックに失敗しました ({txt}): {e}", exc_info=True)
                    except Exception:
                        # キャンセル要素が見つからない場合は無視
                        pass
            except Exception:
                continue
    except Exception:
        logger.debug("フィルタキャンセル要素の検索/クリック処理に失敗しました", exc_info=True)


def set_local_time_checkbox(driver, value=True):
    """
    'Local Time' チェックボックスを指定の状態 (True=ON / False=OFF) に設定する
    """
    def js_set_checked(input_el, want=True):
        # JSでcheckedを書き換え + change/inputイベント発火（React対応）
        driver.execute_script("""
            const cb = arguments[0];
            const want = arguments[1];
            if (cb.checked !== want) {
                cb.checked = want;
                cb.dispatchEvent(new Event('input', {bubbles:true}));
                cb.dispatchEvent(new Event('change', {bubbles:true}));
            }
        """, input_el, bool(want))

    # 該当チェックボックスを特定
    try:
        box = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.lli-grid-toolbar__item input[name='isLocalTime']"))
        )
    except Exception:
        raise NoSuchElementException("Local Time チェックボックスが見つかりません。")

    # 現在の状態を確認
    try:
        current = box.is_selected()
    except StaleElementReferenceException:
        box = driver.find_element(By.CSS_SELECTOR, "div.lli-grid-toolbar__item input[name='isLocalTime']")
        current = box.is_selected()

    # 望む状態でなければ切り替え
    if current != bool(value):
        try:
            # まず通常クリック
            label = box.find_element(By.XPATH, "./ancestor::label")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", label)
            label.click()
        except (ElementNotInteractableException, Exception):
            # クリックできなければJSで直接セット
            js_set_checked(box, bool(value))

    # # 反映確認（再ロケート）
    # box = driver.find_element(By.CSS_SELECTOR, "div.lli-grid-toolbar__item input[name='isLocalTime']")
    # state = box.is_selected()
    # print(f"Local Time checkbox set to {state}")
    # return state


def select_all_columns(driver):
    """
    Columns ドロップダウン内の全チェックボックスを ON（表示）にする。
    - body直下ポータル描画に対応（可視な .lli-dropdown__list をグローバル取得）
    - クリック不可でも JS で強制ON＋イベント発火
    - 再描画(stale)にリトライ
    """

    def get_visible_list():
        # 可視な ul (.lli-dropdown__list) を返す
        for el in driver.find_elements(By.CSS_SELECTOR, ".lli-dropdown__list"):
            try:
                if el.is_displayed():
                    return el
            except StaleElementReferenceException:
                continue
        return driver.execute_script("""
            const els = Array.from(document.querySelectorAll('.lli-dropdown__list'));
            return els.find(e => e && e.offsetParent !== null) || null;
        """)

    def open_columns_menu():
        # Columnsボタンを押して開く（既に開いていたら何もしない）
        if get_visible_list() is None:
            btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(normalize-space(.), 'Columns')]"))
            )
            btn.click()
            WebDriverWait(driver, 5).until(lambda d: get_visible_list() is not None)
        return get_visible_list()

    def try_enable(cb_el):
        # ラベル要素
        label = cb_el.find_element(By.XPATH, "./ancestor::label")
        driver.execute_script("arguments[0].scrollIntoView({block:'nearest', inline:'nearest'});", label)

        # ① ラベルを JS click（重なりや非表示でも実行される）
        driver.execute_script("arguments[0].click();", label)

        # 状態確認（再 locate）
        def is_on():
            try:
                return label.find_element(By.CSS_SELECTOR, "input.lli-dropdown__checkbox").is_selected()
            except StaleElementReferenceException:
                # 再取得
                return False

        if is_on():
            return True

        # ② 直接 checked を true、各種イベント発火（React/Vue対策）
        driver.execute_script("""
            const cb = arguments[0];
            if (!cb.checked) {
                cb.checked = true;
                cb.dispatchEvent(new Event('click',  {bubbles:true}));
                cb.dispatchEvent(new Event('input',  {bubbles:true}));
                cb.dispatchEvent(new Event('change', {bubbles:true}));
                const lbl = cb.closest('label');
                if (lbl) lbl.dispatchEvent(new MouseEvent('click', {bubbles:true}));
            }
        """, cb_el)

        # 最終確認
        try:
            return label.find_element(By.CSS_SELECTOR, "input.lli-dropdown__checkbox").is_selected()
        except StaleElementReferenceException:
            return False

    # --- 実行 ---
    ul = open_columns_menu()
    if ul is None:
        raise RuntimeError("Columns dropdown not found.")

    # 2パスで確実にON（途中で再描画が起きても拾う）
    for _ in range(2):
        checkboxes = ul.find_elements(By.CSS_SELECTOR, "input.lli-dropdown__checkbox")
        pending = []
        for cb in checkboxes:
            try:
                if not cb.is_selected():
                    ok = try_enable(cb)
                    if not ok:
                        pending.append(cb)
            except StaleElementReferenceException:
                # 再描画→次パスで拾う
                pending.append(cb)
                continue
        if not pending:
            break

    # （任意で閉じるなら）
    # try:
    #     btn = driver.find_element(By.XPATH, "//button[contains(normalize-space(.), 'Columns')]")
    #     if get_visible_list() is not None:
    #         btn.click()
    # except Exception:
    #     pass



def _get_next_button(driver, timeout=5):
    """ページャの 'Next Page' ボタン要素を返す（見つからなければ None）"""
    # テーブル（リスト）描画待ち
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//table")))

    # “Next Page” の button を直接探す（li内のbutton or aria-label）
    x = (
        "//li[contains(@class,'lli-grid-pager__number')"
        "     and normalize-space(.)='Next Page']"
        "//button | //button[@aria-label='Next Page']"
    )
    try:
        # 可視のものを一つ
        for btn in driver.find_elements(By.XPATH, x):
            try:
                if btn.is_displayed():
                    return btn
            except StaleElementReferenceException:
                continue
    except Exception:
        pass
    return None

def _is_disabled(btn):
    """‘Next Page’ ボタンが押せない状態か判定"""
    if btn is None:
        return True
    cls = (btn.get_attribute("class") or "")
    aria = (btn.get_attribute("aria-disabled") or "").lower()
    disabled_attr = btn.get_attribute("disabled")
    parent_cls = ""
    parent_aria = ""
    try:
        parent = btn.find_element(By.XPATH, "./ancestor::*[self::li or self::button or @role='button'][1]")
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

def have_next_page(driver) -> bool:
    """次ページが存在するかを確実に True/False で返す"""
    btn = _get_next_button(driver)
    return False if btn is None else (not _is_disabled(btn))

def next_page(driver) -> bool:
    """次ページへ遷移。成功なら True を返す。"""
    btn = _get_next_button(driver)
    if btn is None or _is_disabled(btn):
        return False
    try:
        # クリック可能まで待つ
        WebDriverWait(driver, 5).until(EC.element_to_be_clickable(btn))
    except TimeoutException:
        # staleness で失敗しやすいので取り直し
        btn = _get_next_button(driver)
        if btn is None or _is_disabled(btn):
            return False

    # スクロールしてからクリック
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'nearest'});", btn)
    except Exception:
        pass

    try:
        btn.click()
        return True
    except (ElementClickInterceptedException, StaleElementReferenceException):
        # 最後の手段: JS クリック
        try:
            driver.execute_script("arguments[0].click();", btn)
            return True
        except Exception:
            return False

def wait_for_data_load(driver, timeout: int = 10, min_rows: int = 1) -> bool:
    try:
        state = _wait_for_loaded_grid(driver, timeout=timeout, min_rows=min_rows)
        return bool(state.get("row_count", 0) >= min_rows or state.get("total_count", 0) == 0)
    except Exception as e:
        logger.debug(f"wait_for_data_load: タイムアウトまたは行数未達: {e}", exc_info=True)
        return False

def save_table(driver, llino, index: int,
               out_dir: str = "movement/test",
               encoding: str = "utf-8-sig") -> Path | None:
    """
    高速・安定版:
    - 全parent-rowをJSで一括展開
    - 親<tr>とその直下の<tr><div class="lli-berth-calling">...</div></tr>をペアとして処理
    - BerthテーブルをFrom/To付きでレコード追加
    """
    import pandas as pd
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, StaleElementReferenceException
    from pathlib import Path
    from datetime import date
    from io import StringIO
    import logging, time

    # --- 親テーブル待機 ---
    table_xpath = ("//table[contains(@class,'lli-table') and "
                   "(.//tr[contains(@class,'lli-table__row')] or .//tr[contains(@class,'parent-row')])]")
    try:
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.XPATH, table_xpath)))
    except TimeoutException:
        logging.warning(f"[skip:no-table] llino {llino} page {index}: table not found")
        return None

    # --- 親テーブルHTML取得 ---
    table_el = driver.find_element(By.XPATH, table_xpath)
    html_main = driver.execute_script("return arguments[0].outerHTML;", table_el)

    # --- 一括トグル展開 ---
    driver.execute_script("""
        document.querySelectorAll('tr.parent-row .toggle-row-collapsable.flaticon-arrow-right')
        .forEach(e => { e.scrollIntoView({block:'center'}); e.click(); });
    """)
    time.sleep(1.5)  # 描画待ち

    # --- 親<tr>一覧取得 ---
    parent_rows = driver.find_elements(By.XPATH, "//tr[contains(@class,'parent-row')]")

    berth_records = []

    for pr in parent_rows:
        try:
            # 親行の基本情報（place, country, type, from, to）
            def safe_text(xpath):
                try:
                    return pr.find_element(By.XPATH, xpath).text.strip()
                except Exception:
                    return ""

            place = safe_text(".//td[3]")  # <a>を含む
            country = safe_text(".//td[4]")
            typ = safe_text(".//td[5]")
            from_ = safe_text(".//td[8]")
            to_ = safe_text(".//td[9]")

            # 直後の兄弟<tr>にberthテーブルがあるか確認
            try:
                berth_wrap = pr.find_element(By.XPATH, "./following-sibling::tr[1]//div[contains(@class,'lli-berth-calling')]")
            except Exception:
                continue

            berth_tables = berth_wrap.find_elements(By.XPATH, ".//table[contains(@class,'lli-table')]")
            for bt in berth_tables:
                html = driver.execute_script("return arguments[0].outerHTML;", bt)
                tbs = pd.read_html(StringIO(html))
                if not tbs:
                    continue
                dfb = tbs[0]
                if isinstance(dfb.columns, pd.MultiIndex):
                    dfb.columns = [" ".join([str(x) for x in tup if str(x) != "nan"]).strip() for tup in dfb.columns]

                # 列名正規化
                cmap = {}
                for c in dfb.columns:
                    lc = str(c).lower().strip()
                    if "terminal" in lc: cmap[c] = "Terminal Name"
                    elif "berth" in lc: cmap[c] = "Berth Name"
                    elif "arrived" in lc: cmap[c] = "Arrived At"
                    elif "sailed" in lc: cmap[c] = "Sailed At"
                dfb.rename(columns=cmap, inplace=True)

                for _, r in dfb.iterrows():
                    berth_records.append({
                        "LLI NO": llino,
                        "Place": place,
                        "Country/Region": country,
                        "Type": "Berth",
                        "Terminal Name": r.get("Terminal Name", None),
                        "Berth Name": r.get("Berth Name", None),
                        "From": r.get("Arrived At", None),
                        "To": r.get("Sailed At", None),
                        "Status And Distance": None,
                    })
        except Exception:
            continue

    # --- 親テーブルのDataFrame化 ---
    tbs_main = pd.read_html(StringIO(html_main))
    if not tbs_main:
        logging.warning(f"[skip:parse] llino {llino} page {index}: parsed 0 tables")
        return None

    df_main = tbs_main[0]
    if isinstance(df_main.columns, pd.MultiIndex):
        df_main.columns = [" ".join([str(x) for x in tup if str(x) != "nan"]).strip() for tup in df_main.columns]
    df_main.insert(0, "LLI NO", llino)

    # --- 結合 ---
    df_berth = pd.DataFrame(berth_records)
    all_cols = list(dict.fromkeys(list(df_main.columns) + list(df_berth.columns)))
    df_out = pd.concat(
        [df_main.reindex(columns=all_cols), df_berth.reindex(columns=all_cols)],
        ignore_index=True
    )

    # --- ここから追加: LLI NO を整数型に戻す ---
    if "LLI NO" in df_out.columns:
        # 万一 NaN が混ざっていても、全部この llino で埋める
        df_out["LLI NO"] = (
            df_out["LLI NO"]
            .where(df_out["LLI NO"].notna(), other=llino)
            .astype("Int64")  # 欠損にも耐える整数型
        )
    # ----------------------------------------

    # --- 保存 ---
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    fpath = out_path / f"movement_{date.today():%Y%m%d}_{str(llino).zfill(8)}_{str(index).zfill(3)}.csv"
    df_out.to_csv(fpath, index=False, encoding=encoding)

    return fpath

DEFAULT_SCRAPING_CONFIG = {
    "max_hops": 10,
    "status_list": "all",
    "period": "all",
    "local_time": False,
    "out_dir": "movement/test/",
    "login_user": None,
    "login_password": None,
    "headless": False,
    "log_level": "DONE",
    "log_steps": False,
    "skip_if_exists": True,
    "skip_if_known_no_data": True,
    "remember_no_data": True,
    "show_progress": True,
    "periodic_rest_enabled": True,
    "work_session_hours": 6.0,
    "work_session_random_minutes": 20.0,
    "rest_session_hours": 1.5,
    "rest_session_random_minutes": 15.0,
    "check_status": False,
    "encoding": "utf-8-sig",
    "relogin_after_timeout_streak": False,
    "timeout_streak_relogin_threshold": 5,
    "retry_timeout_once_after_relogin": False,
    "retry_timeout_immediately": False,
    "timeout_immediate_retry_attempts": 1,
    "retry_recoverable_errors": True,
    "recoverable_error_retry_attempts": 1,
}

def scraping_movement(driver, llino, config=None):
    """
    指定 LLI No. の Movement データを全ページ取得してCSV保存。
    成功・失敗・スキップをすべてログ出力し、所要時間を計測する。

    config keys:
      - max_hops: int        … カレンダー過去遡りの最大回数
      - status_list: [str]   … 例: ["Calls","Passings","Sightings"]
      - local_time: bool     … Local Time チェックのON/OFF
      - out_dir: str         … CSV出力先ディレクトリ
    """
    # 設定をマージ（未指定はデフォルト）
    cfg = {**DEFAULT_SCRAPING_CONFIG, **(config or {})}

    # 各主要ステップで info ログを出すかどうか
    log_steps = bool(cfg.get('log_steps', False))

    start = time.time()
    saved_pages = 0

    # check_status=False のときに status が未定義になるのを防ぐため、デフォルトで None に初期化
    status = None

    logger.info(f"Start scraping_movement for llino={llino}")

    # 事前チェック: movement フォルダに該当 LLI のファイルがあればスキップ
    try:
        if bool(cfg.get('skip_if_exists', True)) and has_saved_movements_for_llino(llino, cfg):
            logger.info(f"llino {llino}: 移動ファイルが既に存在するためスキップします (skip_if_exists=True)。")
            return {"llino": llino, "ok": True, "skipped": True, "status": None, "pages": 0, "elapsed": 0.0, "note": "already_exists"}
    except Exception:
        logger.debug("pre-skip check failed", exc_info=True)

    try:
        # --- 1) Movement画面を開く ---
        try:
            open_movement(driver, llino)
        except TimeoutException as e:
            elapsed = time.time() - start
            logger.warning(f"[timeout][open_movement] llino {llino}: {e} url={getattr(driver, 'current_url', 'n/a')} ({elapsed:.1f}s)")
            raise
        # オープン成功ログ（オプション）
        if log_steps:
            logger.info(f"open_movement 完了 for llino {llino}")

        # --- 2) ステータス確認 (optional, controlled by cfg['check_status']) ---
        if cfg.get('check_status', True):
            try:
                status_elem = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "lli-status__label"))
                )
            except TimeoutException as e:
                elapsed = time.time() - start
                url = getattr(driver, 'current_url', 'n/a')
                logger.warning(f"[timeout][status_check] llino {llino}: {e} url={url} ({elapsed:.1f}s)")

                # --- 診断キャプチャ: スクリーンショット、ページHTML、タイトル、クッキー ---
                try:
                    debug_base = Path(cfg.get('out_dir', 'movement/test')) / 'debug'
                    debug_base.mkdir(parents=True, exist_ok=True)
                    ts = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
                    png_path = debug_base / f"llino_{llino}_status_check_{ts}.png"
                    html_path = debug_base / f"llino_{llino}_status_check_{ts}.html"

                    try:
                        driver.save_screenshot(str(png_path))
                        logger.info(f"スクリーンショットを保存しました: {png_path}")
                    except Exception as e_s:
                        logger.debug(f"スクリーンショットの保存に失敗しました: {e_s}", exc_info=True)

                    try:
                        page_html = driver.execute_script("return document.documentElement.outerHTML;")
                        html_path.write_text(page_html, encoding='utf-8')
                        logger.info(f"ページHTMLを保存しました: {html_path}")
                    except Exception as e_h:
                        logger.debug(f"ページHTMLの保存に失敗しました: {e_h}", exc_info=True)

                    try:
                        title = driver.title
                        logger.info(f"タイムアウト時のページタイトル: {title}")
                    except Exception:
                        pass

                    try:
                        cookies = driver.get_cookies()
                        logger.debug(f"タイムアウト時のクッキー: {cookies}")
                    except Exception:
                        pass

                    # 追加の診断情報: document.readyState、ステータスセレクタの存在、ブラウザコンソールログ等
                    try:
                        diagnostics = driver.execute_script("""
                            try {
                                return {
                                    readyState: document.readyState,
                                    hasStatusSelector: !!document.querySelector('.lli-status__label'),
                                    statusHTML: (document.querySelector('.lli-status__label')||{outerHTML:''}).outerHTML,
                                    bodyTextSample: document.body ? document.body.innerText.slice(0,1000) : ''
                                };
                            } catch (e) {
                                return {error: String(e)};
                            }
                        """)
                        diag_path = debug_base / f"llino_{llino}_status_check_{ts}.json"
                        import json
                        diag_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding='utf-8')
                        logger.info(f"診断 JSON を保存しました: {diag_path}")
                    except Exception as e_d:
                        logger.debug(f"JS診断の取得に失敗しました: {e_d}", exc_info=True)

                    try:
                        # ブラウザのコンソールログ（ドライバにより空の場合あり）
                        logs = []
                        try:
                            logs = driver.get_log('browser')
                            log_path = debug_base / f"llino_{llino}_status_check_{ts}_browser_logs.json"
                            log_path.write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding='utf-8')
                            logger.info(f"ブラウザログを保存しました: {log_path}")
                        except Exception as e_l:
                            logger.debug(f"ブラウザログの取得に失敗しました: {e_l}")
                    except Exception:
                        pass

                except Exception as diag_e:
                    logger.debug(f"診断キャプチャに失敗しました: {diag_e}", exc_info=True)
                raise

            status = status_elem.text.strip()
            if log_steps:
                logger.info(f"ステータス確認完了: {status}")
            if status not in ("Live", "Unconfirmed Existence", "Dead"):
                msg = f"llino {llino} は live ではありません (status={status})。スキップされました。"
                logger.info(msg)
                return {
                    "llino": llino, "ok": True, "skipped": True,
                    "status": status, "pages": 0, "elapsed": 0.0
                }
        else:
            logger.info(f"設定により llino {llino} のステータスチェックをスキップします")

        # --- 3) 各種設定（タイムアウト時に個別ステップをログ出力） ---
        try:
            select_status(driver, cfg["status_list"])
        except TimeoutException as e:
            elapsed = time.time() - start
            logger.warning(f"[timeout][select_status] llino {llino}: {e} url={getattr(driver, 'current_url', 'n/a')} ({elapsed:.1f}s)")
            raise
        if log_steps:
            logger.info("select_status 完了")

        try:
            # Period の扱い: config の 'period' に従う (例: 'all' / {'from':'2025-06-21','to':'2025-12-21'})
            period_cfg = cfg.get('period', 'all')
            set_period(driver, period_cfg)
        except TimeoutException as e:
            elapsed = time.time() - start
            logger.warning(f"[timeout][set_period] llino {llino}: {e} url={getattr(driver, 'current_url', 'n/a')} ({elapsed:.1f}s)")
            raise
        if log_steps:
            logger.info("set_period 完了")

        try:
            set_local_time_checkbox(driver, cfg["local_time"])
        except TimeoutException as e:
            elapsed = time.time() - start
            logger.warning(f"[timeout][set_local_time_checkbox] llino {llino}: {e} url={getattr(driver, 'current_url', 'n/a')} ({elapsed:.1f}s)")
            raise
        if log_steps:
            logger.info("set_local_time_checkbox 完了")

        try:
            pass
        except TimeoutException as e:
            elapsed = time.time() - start
            logger.warning(f"[timeout][select_all_columns] llino {llino}: {e} url={getattr(driver, 'current_url', 'n/a')} ({elapsed:.1f}s)")
            raise

        try:
            wait_for_data_load(driver)
        except TimeoutException as e:
            elapsed = time.time() - start
            logger.warning(f"[timeout][wait_for_data_load] llino {llino}: {e} url={getattr(driver, 'current_url', 'n/a')} ({elapsed:.1f}s)")
            raise

        # # --- 4) テーブル保存（1ページ目） ---
        # index = 1
        # save_table(driver, llino, index, out_dir=cfg["out_dir"])
        # saved_pages += 1

        # # --- 5) ページネーション ---
        # while have_next_page(driver):
        #     index += 1
        #     next_page(driver)
        #     # データ行の再出現を待つ（再描画対策）
        #     WebDriverWait(driver, 10).until(
        #         EC.presence_of_element_located((By.CLASS_NAME, "lli-table__row"))
        #     )
        #     wait_for_data_load(driver)
        #     save_table(driver, llino, index, out_dir=cfg["out_dir"])
        #     saved_pages += 1

        index = 1
        path = save_table(driver, llino, index, out_dir=cfg["out_dir"])
        if path is not None:
            saved_pages += 1
        else:
            # 1ページ目からテーブルが無い場合はスキップ扱いにして終了
            elapsed = time.time() - start
            msg = f"[skip:no-table] llino {llino}: no table on first page"
            print(msg); logging.warning(msg)
            return {"llino": llino, "ok": True, "skipped": True,
                    "status": status, "pages": saved_pages, "elapsed": elapsed, "note": "no-table"}

        # 2ページ目以降
        while have_next_page(driver):
            index += 1
            next_page(driver)
            wait_for_data_load(driver)
            path = save_table(driver, llino, index, out_dir=cfg["out_dir"])
            if path is not None:
                saved_pages += 1
            else:
                # ページ途中でテーブルが取れない場合も静かに抜ける
                break

        # --- 6) 正常終了 ---
        elapsed = time.time() - start
        msg = f"llino {llino} の処理が正常に完了しました（{elapsed:.2f}s、{saved_pages} ページ）"
        logger.info(msg)
        logger.debug({"llino": llino, "elapsed": elapsed, "pages": saved_pages})
        return {
            "llino": llino, "ok": True, "skipped": False,
            "status": status, "pages": saved_pages, "elapsed": elapsed
        }

    except TimeoutException as e:
        # ここでも“1行だけ”にする（スタックトレース無し）
        elapsed = time.time() - start
        msg = f"[timeout] llino {llino}: 操作がタイムアウトしました ({elapsed:.1f}s)"
        logger.warning(msg)
        return {"llino": llino, "ok": False, "skipped": False,
                "status": None, "pages": saved_pages, "elapsed": elapsed, "error": "timeout"}

    except Exception as e:
        elapsed = time.time() - start
        msg = f"Error processing llino {llino}: {e}"
        logger.error(msg, exc_info=True)
        return {
            "llino": llino, "ok": False, "skipped": False,
            "status": None, "pages": saved_pages, "elapsed": elapsed,
            "error": str(e)
        }

# --- ユーティリティ ---

def chunk_round_robin(items, k):
    """kワーカーにラウンドロビンで均等配分"""
    chunks = [[] for _ in range(k)]
    for i, x in enumerate(items):
        chunks[i % k].append(x)
    return chunks

def _safe_sleep_jitter(a=0.2, b=0.8):
    """礼儀として微小スリープ"""
    time.sleep(random.uniform(a, b))

def _ensure_logged_in(driver):
    """セッション生存を軽く確認。必要なら再ログイン"""
    try:
        # 例: 既にログイン済みならユーザーアイコンがある…等の軽いチェック
        # サイト都合に合わせて小改造してください
        driver.execute_script("return document.readyState")
        # 必要なら軽い要素確認やCookie確認を入れる
        return True
    except Exception:
        return False


# --- movement ファイル存在チェック ---
def has_saved_movements_for_llino(llino, cfg=None) -> bool:
    """movement フォルダ（と cfg['out_dir']）内に該当 LLI のファイルがあるか確認する。
    ファイル名に LLI の数値トークンが含まれる場合に True を返します。
    """
    try:
        import re
        token = str(int(llino))
        pat = re.compile(rf"(?<!\d){re.escape(token)}(?!\d)")

        search_paths = set()
        # cfg の out_dir を優先
        if cfg and cfg.get('out_dir'):
            p = Path(cfg.get('out_dir'))
            if p.exists():
                search_paths.add(p)
            else:
                # try parent movement dir
                search_paths.add(Path('movement'))
        else:
            search_paths.add(Path('movement'))

        # 確実に top-level movement も検索
        if Path('movement').exists():
            search_paths.add(Path('movement'))

        for base in list(search_paths):
            try:
                for f in base.rglob('*'):
                    if f.is_file():
                        if pat.search(f.name):
                            return True
            except Exception:
                continue
    except Exception:
        logger.debug("has_saved_movements_for_llino: failed", exc_info=True)
    return False

# --- ワーカー ---
def worker_thread(llino_list, *, driver_opts=None, retry_driver_once=True, config=None):
    """
    1スレッド用ワーカー。
    - 1 WebDriver を生成しログイン
    - llino_list を順次処理
    - 途中でドライバが死んだら1回だけ再起動 → 残りを続行
    戻り値: list[dict] （scraping_movement の戻り値をそのまま並べたもの）

    引数
    ----------
    llino_list : list[int]
    driver_opts : any
        initialize_driver に渡す追加オプション（任意）
    retry_driver_once : bool
        WebDriverException 時に 1 回だけドライバ再起動して続行
    config : dict | None
        一括設定（例）
          {
            "max_hops": 200,
            "status_list": ["Calls","Passings","Sightings"],
            "local_time": False,
            "out_dir": "movement/test",
          }
    """
    results = []

    show_progress = bool(config.get('show_progress', True)) if config else True
    pbar = None
    use_tqdm = (tqdm is not None) and show_progress and len(llino_list) > 1

    def _new_driver_and_login():
        drv = initialize_driver(driver_opts) if driver_opts else initialize_driver()
        try:
            drv.set_page_load_timeout(45)
            drv.set_script_timeout(45)
        except Exception:
            pass
        drv = login_to_seasearcher(drv, config=config)
        return drv

    driver = None
    try:
        driver = _new_driver_and_login()

        # 念のためログイン生存確認
        if not _ensure_logged_in(driver):
            driver = login_to_seasearcher(driver, config=config)

        # プログレスバー初期化（tqdm が使える場合）
        if use_tqdm:
            try:
                pbar = tqdm(total=len(llino_list), desc="LLIs", unit="llino")
            except Exception:
                pbar = None
        processed = 0

        for llino in llino_list:
            try:
                # ✅ config をそのまま渡す
                res = scraping_movement(driver, llino, config=config)
                results.append(res)
            except WebDriverException as we:
                logger.error(f"[worker] WebDriverException at llino={llino}: {we}", exc_info=True)
                if retry_driver_once:
                    retry_driver_once = False
                    try:
                        if driver:
                            try:
                                driver.quit()
                            except Exception:
                                pass
                        driver = _new_driver_and_login()
                        # ✅ 再試行時も config を確実に渡す
                        res = scraping_movement(driver, llino, config=config)
                        results.append(res)
                    except Exception as e2:
                        logger.error(f"[worker] Retry failed at llino={llino}: {e2}", exc_info=True)
                        results.append({"llino": llino, "ok": False, "error": f"retry_failed: {e2}"})
                else:
                    results.append({"llino": llino, "ok": False, "error": f"webdriver_error: {we}"})
            except Exception as e:
                logger.error(f"[worker] Error at llino={llino}: {e}", exc_info=True)
                results.append({"llino": llino, "ok": False, "error": str(e)})
            finally:
                processed += 1
                if pbar is not None:
                    try:
                        pbar.update(1)
                    except Exception:
                        pass
                elif show_progress:
                    # fallback: simple print progress
                    try:
                        sys.stdout.write(f"Processed {processed}/{len(llino_list)}\r")
                        sys.stdout.flush()
                    except Exception:
                        pass

        if pbar is not None:
            try:
                pbar.close()
            except Exception:
                pass

            _safe_sleep_jitter()

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return results


# --- 並列制御 ---

def parallel_scraping(llino_list, max_workers=1, driver_opts=None, config=None):
    """
    llino_list を並列スクレイピング。
    戻り値: list[dict] （各 llino の結果をすべて結合）

    引数
    ----------
    llino_list : list[int]
    max_workers : int
    driver_opts : any
    config : dict | None
        一括設定（worker_thread にそのまま渡される）
    """
    # 明示的に指定されていない場合は、config から driver オプション (例: headless) を伝搬します
    if driver_opts is None:
        driver_opts = {}
    if config and 'headless' in config:
        driver_opts.setdefault('headless', config['headless'])

    # もし指定があればログレベルを適用します
    if config and 'log_level' in config:
        lvl_name = str(config['log_level']).upper()
        lvl = getattr(logging, lvl_name, None)
        if isinstance(lvl, int):
            logger.setLevel(lvl)
            logging.getLogger().setLevel(lvl)

    logger.info(f"parallel_scraping を開始します: ワーカー数={max_workers}, headless={driver_opts.get('headless')}")

    chunks = chunk_round_robin(llino_list, max_workers)
    all_results = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # ✅ 各スレッドに同一 config を配布
        futures = [
            executor.submit(worker_thread, chunk, driver_opts=driver_opts, config=config)
            for chunk in chunks
        ]
        # option: show a consolidated progress bar based on number of llino processed across chunks
        show_progress = bool(config.get('show_progress', True)) if config else True
        total_llinos = len(llino_list)
        total_processed = 0
        main_pbar = None
        use_tqdm_main = (tqdm is not None) and show_progress and max_workers > 1 and total_llinos > 1
        if use_tqdm_main:
            try:
                main_pbar = tqdm(total=total_llinos, desc="Total LLIs", unit="llino")
            except Exception:
                main_pbar = None

        for fut in as_completed(futures):
            try:
                res = fut.result()
                all_results.extend(res)
                # res is a list of per-llino results; increment progress accordingly
                inc = len(res) if isinstance(res, list) else 1
                total_processed += inc
                if main_pbar is not None:
                    try:
                        main_pbar.update(inc)
                    except Exception:
                        pass
                elif show_progress:
                    try:
                        sys.stdout.write(f"Total processed {total_processed}/{total_llinos}\r")
                        sys.stdout.flush()
                    except Exception:
                        pass
            except Exception as e:
                logging.error(f"[parallel] Error in thread: {e}", exc_info=True)
        if main_pbar is not None:
            try:
                main_pbar.close()
            except Exception:
                pass

    return all_results


# --- Current Movement table flow overrides ---

def has_saved_movements_for_llino(llino, cfg=None) -> bool:
    cached_index = (cfg or {}).get("_saved_outputs_index")
    if isinstance(cached_index, set):
        return _format_llino(llino) in cached_index
    return _build_movement_output_path(llino, cfg).exists()


def scraping_movement(
    driver,
    llino,
    config=None,
    *,
    download_dir=None,
    reload_current_page=False,
):
    cfg = {**DEFAULT_SCRAPING_CONFIG, **(config or {})}
    cfg["status_list"] = resolve_movement_status_list(cfg.get("status_list"))
    log_steps = bool(cfg.get("log_steps", False))
    start = time.time()
    saved_pages = 0
    saved_rows = 0
    status = None
    run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
    llino_token = _format_llino(llino)

    out_dir = Path(cfg["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    debug_base = out_dir / "debug"
    output_path = _build_movement_output_path(llino, cfg)

    if log_steps:
        logger.info("Start scraping_movement for llino=%s", llino)

    try:
        if bool(cfg.get("skip_if_exists", True)) and has_saved_movements_for_llino(llino, cfg):
            logger.complete(
                "llino %s: skipped because Movement output already exists (skip_if_exists=True)",
                llino,
            )
            return {
                "llino": llino,
                "ok": True,
                "skipped": True,
                "status": None,
                "pages": 0,
                "rows": 0,
                "elapsed": 0.0,
                "note": "already_exists",
                "output_path": str(output_path),
            }
        if bool(cfg.get("skip_if_known_no_data", True)) and _has_known_no_data_llino(llino, cfg):
            logger.complete(
                "llino %s: skipped because Movement no-data cache exists (skip_if_known_no_data=True)",
                llino,
            )
            res = _build_known_no_data_result(llino)
            res["output_path"] = str(output_path)
            return res
    except Exception:
        logger.debug("Movement pre-skip check failed", exc_info=True)

    try:
        open_movement(driver, llino, reload_current_page=reload_current_page)
        _toolbar_ready(driver)
        _clear_performance_log(driver)
        if log_steps:
            logger.info("Movement page opened for llino %s", llino)

        if cfg.get("check_status", False):
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
                    "rows": 0,
                    "elapsed": elapsed,
                    "note": "inactive-status",
                    "output_path": str(output_path),
                }

        select_status(driver, cfg["status_list"])
        if log_steps:
            logger.info("status filters applied for llino %s: %s", llino, cfg["status_list"])

        period_cfg = cfg.get("period", "all")
        period_applied = set_period(
            driver,
            period_cfg,
            allow_from_earliest_fallback=True,
            allow_to_latest_fallback=True,
        )
        if period_applied is False:
            elapsed = time.time() - start
            _remember_known_no_data_llino(llino, cfg)
            logger.complete(
                "llino %s finished with no Movement rows in the requested period (%.2fs)",
                llino,
                elapsed,
            )
            return {
                "llino": llino,
                "ok": True,
                "skipped": True,
                "status": status,
                "pages": 0,
                "rows": 0,
                "elapsed": elapsed,
                "note": "no-data-in-requested-period",
                "output_path": str(output_path),
            }
        if log_steps:
            logger.info("period applied for llino %s", llino)

        set_local_time_checkbox(driver, cfg["local_time"])
        if log_steps:
            logger.info("local_time applied for llino %s", llino)

        grid_state_before_page_size = _wait_for_loaded_grid(driver, timeout=20, min_rows=1)
        if grid_state_before_page_size.get("total_count") == 0 or _grid_reports_no_data(driver):
            # No rows are already confirmed; skip the 1000-row selector and its wait.
            current_grid_state = grid_state_before_page_size
        else:
            set_items_per_page_1000(driver)
            _dismiss_transient_ui(driver)
            current_grid_state = _wait_for_items_per_page_apply(
                driver,
                previous_state=grid_state_before_page_size,
                timeout=20,
            )

        if current_grid_state.get("total_count") == 0 or _grid_reports_no_data(driver):
            elapsed = time.time() - start
            _remember_known_no_data_llino(llino, cfg)
            logger.complete(
                "llino %s finished with no Movement rows (%.2fs)",
                llino,
                elapsed,
            )
            return {
                "llino": llino,
                "ok": True,
                "skipped": True,
                "status": status,
                "pages": 0,
                "rows": 0,
                "elapsed": elapsed,
                "note": "no-data",
                "output_path": str(output_path),
            }

        total_count = int(current_grid_state.get("total_count") or 0)
        total_pages = (total_count + 999) // 1000
        page_frames = []
        page_markers = set()

        for page_index in range(1, total_pages + 1):
            expected_rows = min(1000, total_count - ((page_index - 1) * 1000))
            frame = _read_current_movement_grid(
                driver,
                llino,
                expected_rows=expected_rows,
                expected_total=total_count,
                timeout=30,
            )
            page_marker = tuple(frame.iloc[0].astype(str))
            if page_marker in page_markers:
                raise ValueError(f"Movement page {page_index} repeated the previous page")
            page_markers.add(page_marker)
            page_frames.append(frame)
            saved_rows += len(frame)
            saved_pages += 1
            if page_index >= total_pages:
                break

            next_button = _get_next_button(driver, timeout=10)
            if next_button is None:
                raise TimeoutException("Next page button not found")
            _clear_performance_log(driver)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", next_button)
            previous_grid_state = current_grid_state
            if not next_page(driver):
                raise TimeoutException("Could not move to the next Movement page")
            current_grid_state = _wait_for_next_page_ready(
                driver,
                previous_state=previous_grid_state,
                timeout=30,
            )

        merged = pd.concat(page_frames, ignore_index=True)
        if saved_rows != total_count:
            raise ValueError(f"Movement row count mismatch: expected={total_count}, saved={saved_rows}")
        duplicate_rows = int(merged.duplicated().sum())
        if duplicate_rows:
            raise ValueError(f"Movement output contains {duplicate_rows} duplicate rows")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        merged.to_csv(output_path, index=False, encoding=cfg["encoding"])

        cached_index = cfg.get("_saved_outputs_index")
        if isinstance(cached_index, set):
            cached_index.add(llino_token)

        elapsed = time.time() - start
        logger.complete(
            "llino %s completed successfully (%.2fs, %s pages, %s rows, output=%s)",
            llino,
            elapsed,
            saved_pages,
            saved_rows,
            output_path,
        )
        return {
            "llino": llino,
            "ok": True,
            "skipped": False,
            "status": status,
            "pages": saved_pages,
            "rows": saved_rows,
            "elapsed": elapsed,
            "output_path": str(output_path),
            "run_id": run_id,
        }

    except TimeoutException as exc:
        elapsed = time.time() - start
        renderer_timeout = _is_renderer_timeout(exc)
        webdriver_command_timeout = _is_webdriver_command_timeout(exc)
        page_load_stuck = _is_page_load_stuck(exc)
        chrome_page_error = _looks_like_chrome_page_error(exc)
        chrome_memory_error = _is_chrome_memory_error(exc)
        # A wedged renderer makes both screenshot and page-source commands wait
        # for the full script timeout.  There is no usable page to diagnose in
        # that case, so avoid adding roughly 90 seconds to every failure.
        artifacts = {}
        if not renderer_timeout:
            artifacts = _save_debug_artifacts(
                driver,
                debug_base,
                f"llino_{llino_token}_movement_timeout_{run_id}",
            )
        if chrome_page_error:
            logger.warning("Chrome error page while scraping Movement for llino %s: %s", llino, exc)
        else:
            logger.warning("Timeout while scraping Movement for llino %s: %s", llino, exc)
        return {
            "llino": llino,
            "ok": False,
            "skipped": False,
            "status": status,
            "pages": saved_pages,
            "rows": saved_rows,
            "elapsed": elapsed,
            "error": "timeout",
            "error_detail": str(exc),
            "renderer_timeout": renderer_timeout,
            "webdriver_command_timeout": webdriver_command_timeout,
            "page_load_stuck": page_load_stuck,
            "chrome_page_error": chrome_page_error,
            "chrome_memory_error": chrome_memory_error,
            "debug": artifacts,
            "output_path": str(output_path),
            "run_id": run_id,
        }

    except Exception as exc:
        elapsed = time.time() - start
        webdriver_command_timeout = _is_webdriver_command_timeout(exc)
        page_load_stuck = _is_page_load_stuck(exc)
        chrome_page_error = _looks_like_chrome_page_error(exc)
        chrome_memory_error = _is_chrome_memory_error(exc)
        artifacts = _save_debug_artifacts(
            driver,
            debug_base,
            f"llino_{llino_token}_movement_error_{run_id}",
        )
        if chrome_page_error:
            logger.warning("Chrome error page while scraping Movement for llino %s: %s", llino, exc)
        else:
            logger.error("Error while scraping Movement for llino %s: %s", llino, exc, exc_info=True)
        return {
            "llino": llino,
            "ok": False,
            "skipped": False,
            "status": status,
            "pages": saved_pages,
            "rows": saved_rows,
            "elapsed": elapsed,
            "error": "timeout" if chrome_page_error else str(exc),
            "error_detail": str(exc),
            "webdriver_command_timeout": webdriver_command_timeout,
            "page_load_stuck": page_load_stuck,
            "chrome_page_error": chrome_page_error,
            "chrome_memory_error": chrome_memory_error,
            "debug": artifacts,
            "output_path": str(output_path),
            "run_id": run_id,
        }


def worker_thread(
    llino_list,
    *,
    worker_name: str = "worker_01",
    driver_opts=None,
    retry_driver_once: bool = False,
    config=None,
):
    results = []

    show_progress = bool(config.get("show_progress", True)) if config else True
    pbar = None
    use_tqdm = (tqdm is not None) and show_progress and len(llino_list) > 1

    out_dir = Path((config or {}).get("out_dir", DEFAULT_SCRAPING_CONFIG["out_dir"]))
    worker_download_dir = out_dir / "_downloads" / worker_name
    timeout_relogin_cfg = _get_timeout_relogin_settings(config)
    immediate_timeout_retry_cfg = _get_immediate_timeout_retry_settings(config)
    recoverable_retry_cfg = _get_recoverable_error_retry_settings(config)

    def _new_driver_and_login():
        _clear_directory(worker_download_dir)
        effective_driver_opts = dict(driver_opts or {})
        effective_driver_opts.setdefault(
            "headless",
            bool((config or {}).get("headless", DEFAULT_SCRAPING_CONFIG["headless"])),
        )
        drv = initialize_driver(effective_driver_opts, download_dir=worker_download_dir)
        try:
            # Do not wait 120 seconds for a hung ChromeDriver command.
            drv.command_executor.client_config.timeout = 45
            drv.set_page_load_timeout(45)
            drv.set_script_timeout(45)
        except Exception:
            pass
        return login_to_seasearcher(drv, config=config)

    def _recreate_driver():
        nonlocal driver
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        driver = _new_driver_and_login()
        return driver

    def _handle_timeout_recovery(timeout_streak_entries):
        if not timeout_relogin_cfg["enabled"]:
            return []

        streak_count = len(timeout_streak_entries)
        if streak_count < timeout_relogin_cfg["threshold"]:
            return timeout_streak_entries

        current_llino = timeout_streak_entries[-1]["llino"]
        retry_entries = [entry for entry in timeout_streak_entries if entry.get("retry_allowed", True)]
        logger.warning(
            "[worker=%s] Timeout streak reached %s at llino=%s; recreating driver and re-login",
            worker_name,
            streak_count,
            current_llino,
        )

        try:
            _recreate_driver()
        except Exception as relogin_exc:
            logger.error(
                "[worker=%s] Failed to recreate driver after timeout streak at llino=%s: %s",
                worker_name,
                current_llino,
                relogin_exc,
                exc_info=True,
            )
            for entry in timeout_streak_entries:
                results[entry["index"]] = {
                    "llino": entry["llino"],
                    "ok": False,
                    "error": f"timeout_relogin_failed: {relogin_exc}",
                }
            return []

        if not timeout_relogin_cfg["retry_once"]:
            return []

        logger.warning(
            "[worker=%s] Retrying %s timeouted llino(s) once after timeout recovery re-login",
            worker_name,
            len(retry_entries),
        )

        trailing_timeout_entries = []
        for entry in timeout_streak_entries:
            llino = entry["llino"]
            if entry.get("retry_allowed", True):
                logger.warning(
                    "[worker=%s] Retrying llino=%s once after timeout recovery re-login",
                    worker_name,
                    llino,
                )
                try:
                    retry_res = scraping_movement(
                        driver,
                        llino,
                        config=config,
                        download_dir=worker_download_dir,
                    )
                except Exception as retry_exc:
                    logger.error(
                        "[worker=%s] Retry after timeout recovery failed at llino=%s: %s",
                        worker_name,
                        llino,
                        retry_exc,
                        exc_info=True,
                    )
                    retry_res = {
                        "llino": llino,
                        "ok": False,
                        "error": f"timeout_retry_failed: {retry_exc}",
                    }
                results[entry["index"]] = retry_res
            else:
                retry_res = results[entry["index"]]

            if _is_timeout_result(retry_res):
                if entry.get("retry_allowed", True):
                    logger.warning(
                        "[worker=%s] Retry after timeout recovery also timed out at llino=%s",
                        worker_name,
                        llino,
                    )
                trailing_timeout_entries.append(
                    {"llino": llino, "index": entry["index"], "retry_allowed": False}
                )
            else:
                trailing_timeout_entries = []

        return trailing_timeout_entries

    def _retry_timeout_immediately(llino, res):
        current_res = res
        if not immediate_timeout_retry_cfg["enabled"]:
            return current_res

        for attempt in range(immediate_timeout_retry_cfg["attempts"]):
            if not _is_timeout_result(current_res):
                break

            logger.warning(
                "[worker=%s] Immediate timeout retry for llino=%s (%s/%s); recreating driver and re-login",
                worker_name,
                llino,
                attempt + 1,
                immediate_timeout_retry_cfg["attempts"],
            )
            try:
                _recreate_driver()
                _clear_directory(worker_download_dir)
                current_res = scraping_movement(
                    driver,
                    llino,
                    config=config,
                    download_dir=worker_download_dir,
                )
            except Exception as retry_exc:
                logger.error(
                    "[worker=%s] Immediate timeout retry failed at llino=%s: %s",
                    worker_name,
                    llino,
                    retry_exc,
                    exc_info=True,
                )
                current_res = {
                    "llino": llino,
                    "ok": False,
                    "error": "timeout",
                    "error_detail": f"immediate_retry_failed: {retry_exc}",
                }

            if not _is_timeout_result(current_res):
                logger.info(
                    "[worker=%s] Immediate timeout retry succeeded at llino=%s",
                    worker_name,
                    llino,
                )
                break

        return current_res

    def _retry_timeout_after_reload(llino, res):
        if not _is_timeout_result(res):
            return res

        browser_reload_error = bool(
            res.get("chrome_memory_error")
            or res.get("webdriver_command_timeout")
            or res.get("page_load_stuck")
        )
        browser_reload_already_attempted = bool(
            res.get("chrome_memory_retry") or res.get("browser_reload_retry")
        )
        if browser_reload_error and not browser_reload_already_attempted:
            if res.get("chrome_memory_error"):
                reason = "Chrome Out of Memory"
            elif res.get("page_load_stuck"):
                reason = "Movement page loading timeout"
            else:
                reason = "ChromeDriver communication timeout"
            logger.warning(
                "[worker=%s] %s at llino=%s; waiting 5 seconds and reloading the same page once without closing Chrome",
                worker_name,
                reason,
                llino,
            )
            time.sleep(5.0)
            try:
                _clear_directory(worker_download_dir)
                retry_res = scraping_movement(
                    driver,
                    llino,
                    config=config,
                    download_dir=worker_download_dir,
                    reload_current_page=True,
                )
                if isinstance(retry_res, dict):
                    retry_res["browser_reload_retry"] = True
                return retry_res
            except Exception as retry_exc:
                logger.error(
                    "[worker=%s] Reload retry failed at llino=%s: %s",
                    worker_name,
                    llino,
                    retry_exc,
                    exc_info=True,
                )
                return {
                    "llino": llino,
                    "ok": False,
                    "error": "timeout",
                    "error_detail": str(retry_exc),
                    "chrome_page_error": True,
                    "chrome_memory_error": bool(res.get("chrome_memory_error")),
                    "webdriver_command_timeout": bool(res.get("webdriver_command_timeout")),
                    "page_load_stuck": bool(res.get("page_load_stuck")),
                    "browser_reload_retry": True,
                }

        if res.get("chrome_page_error"):
            logger.warning(
                "[worker=%s] Chrome error page at llino=%s; waiting 15 seconds and reopening the same page once without re-login",
                worker_name,
                llino,
            )
        else:
            logger.warning(
                "[worker=%s] Timeout at llino=%s; waiting 15 seconds and reloading the same page once without re-login",
                worker_name,
                llino,
            )
        time.sleep(15.0)
        try:
            _clear_directory(worker_download_dir)
            return scraping_movement(
                driver,
                llino,
                config=config,
                download_dir=worker_download_dir,
            )
        except Exception as retry_exc:
            logger.error(
                "[worker=%s] Same-page retry failed at llino=%s: %s",
                worker_name,
                llino,
                retry_exc,
                exc_info=True,
            )
            return {
                "llino": llino,
                "ok": False,
                "error": "timeout",
                "error_detail": str(retry_exc),
                "renderer_timeout": _is_renderer_timeout(retry_exc),
            }

    def _retry_recoverable_result(llino, res):
        current_res = res
        if not recoverable_retry_cfg["enabled"]:
            return current_res

        for attempt in range(recoverable_retry_cfg["attempts"]):
            if not _is_recoverable_result(current_res) or _is_timeout_result(current_res):
                break

            logger.warning(
                "[worker=%s] Recoverable error retry for llino=%s (%s/%s); recreating driver and re-login",
                worker_name,
                llino,
                attempt + 1,
                recoverable_retry_cfg["attempts"],
            )
            try:
                _recreate_driver()
                _clear_directory(worker_download_dir)
                current_res = scraping_movement(
                    driver,
                    llino,
                    config=config,
                    download_dir=worker_download_dir,
                )
            except Exception as retry_exc:
                logger.error(
                    "[worker=%s] Recoverable retry failed at llino=%s: %s",
                    worker_name,
                    llino,
                    retry_exc,
                    exc_info=True,
                )
                current_res = {
                    "llino": llino,
                    "ok": False,
                    "error": f"recoverable_retry_failed: {retry_exc}",
                }

            if not _is_recoverable_result(current_res):
                logger.info(
                    "[worker=%s] Recoverable error retry succeeded at llino=%s",
                    worker_name,
                    llino,
                )
                break

        return current_res

    driver = None
    try:
        driver = _new_driver_and_login()

        if not _ensure_logged_in(driver):
            driver = login_to_seasearcher(driver, config=config)

        if use_tqdm:
            try:
                pbar = tqdm(total=len(llino_list), desc=f"{worker_name} LLIs", unit="llino")
            except Exception:
                pbar = None

        processed = 0
        timeout_streak_entries = []
        rest_state = _initialize_periodic_rest_state(config)
        if rest_state["enabled"]:
            logger.complete(
                "[%s] periodic rest enabled; first rest target is %.2f hours",
                worker_name,
                rest_state["next_work_session_seconds"] / 3600.0,
            )
        for llino in llino_list:
            res = None
            try:
                _clear_directory(worker_download_dir)
                res = scraping_movement(
                    driver,
                    llino,
                    config=config,
                    download_dir=worker_download_dir,
                )
            except WebDriverException as exc:
                logger.error("[worker=%s] WebDriverException at llino=%s: %s", worker_name, llino, exc, exc_info=True)
                if (
                    _looks_like_chrome_page_error(exc)
                    or _is_webdriver_command_timeout(exc)
                    or _is_page_load_stuck(exc)
                ):
                    res = {
                        "llino": llino,
                        "ok": False,
                        "error": "timeout",
                        "error_detail": str(exc),
                        "chrome_page_error": _looks_like_chrome_page_error(exc),
                        "chrome_memory_error": _is_chrome_memory_error(exc),
                        "webdriver_command_timeout": _is_webdriver_command_timeout(exc),
                        "page_load_stuck": _is_page_load_stuck(exc),
                    }
                elif retry_driver_once:
                    retry_driver_once = False
                    try:
                        _recreate_driver()
                        res = scraping_movement(
                            driver,
                            llino,
                            config=config,
                            download_dir=worker_download_dir,
                        )
                    except Exception as retry_exc:
                        logger.error(
                            "[worker=%s] Retry failed at llino=%s: %s",
                            worker_name,
                            llino,
                            retry_exc,
                            exc_info=True,
                        )
                        res = {"llino": llino, "ok": False, "error": f"retry_failed: {retry_exc}"}
                else:
                    res = {"llino": llino, "ok": False, "error": f"webdriver_error: {exc}"}
            except Exception as exc:
                if (
                    _looks_like_chrome_page_error(exc)
                    or _is_webdriver_command_timeout(exc)
                    or _is_page_load_stuck(exc)
                ):
                    logger.warning("[worker=%s] Chrome page/driver issue at llino=%s: %s", worker_name, llino, exc)
                    res = {
                        "llino": llino,
                        "ok": False,
                        "error": "timeout",
                        "error_detail": str(exc),
                        "chrome_page_error": _looks_like_chrome_page_error(exc),
                        "chrome_memory_error": _is_chrome_memory_error(exc),
                        "webdriver_command_timeout": _is_webdriver_command_timeout(exc),
                        "page_load_stuck": _is_page_load_stuck(exc),
                    }
                else:
                    logger.error("[worker=%s] Error at llino=%s: %s", worker_name, llino, exc, exc_info=True)
                    res = {"llino": llino, "ok": False, "error": str(exc)}
            finally:
                if res is None:
                    res = {"llino": llino, "ok": False, "error": "unknown_error"}
                if _is_timeout_result(res):
                    res = _retry_timeout_after_reload(llino, res)
                elif _is_recoverable_result(res):
                    res = _retry_recoverable_result(llino, res)
                result_index = len(results)
                results.append(res)
                if res.get("ok"):
                    _release_vessel_page(driver)
                gc.collect()
                if (
                    _is_timeout_result(res)
                    and not res.get("chrome_memory_error")
                    and not res.get("webdriver_command_timeout")
                    and not res.get("page_load_stuck")
                ):
                    timeout_streak_entries.append(
                        {"llino": llino, "index": result_index, "retry_allowed": True}
                    )
                    timeout_streak_entries = _handle_timeout_recovery(timeout_streak_entries)
                else:
                    # Browser-level failures are handled by refreshing the current
                    # page; do not turn them into generic timeout-streak relogin.
                    timeout_streak_entries = []
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
                rest_state = _take_periodic_rest_if_needed(
                    worker_name=worker_name,
                    config=config,
                    rest_state=rest_state,
                    processed=processed,
                    total=len(llino_list),
                )

        if pbar is not None:
            try:
                pbar.close()
            except Exception:
                pass

            _safe_sleep_jitter()

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        try:
            shutil.rmtree(worker_download_dir, ignore_errors=True)
        except Exception:
            logger.debug("Failed to remove worker download dir %s", worker_download_dir, exc_info=True)

    return results


def parallel_scraping(llino_list, max_workers=1, driver_opts=None, config=None):
    runtime_config = {**(config or {})}
    llino_list = list(llino_list or [])
    requested_max_workers = max(1, int(max_workers))
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
        "parallel_scraping starting: workers=%s, headless=%s",
        max_workers,
        driver_opts.get("headless"),
    )
    if requested_max_workers != 1:
        logger.complete(
            "Movement scraping forces max_workers=1 for stability (requested=%s)",
            requested_max_workers,
        )

    pending_llinos, upfront_skipped_results, saved_outputs_index, known_no_data_index = _apply_upfront_local_skip_filter(
        llino_list,
        runtime_config,
    )
    if bool(runtime_config.get("skip_if_exists", True)):
        runtime_config["_saved_outputs_index"] = saved_outputs_index
    if bool(runtime_config.get("skip_if_known_no_data", True)):
        runtime_config["_known_no_data_index"] = known_no_data_index
    if bool(runtime_config.get("skip_if_exists", True)) or bool(runtime_config.get("skip_if_known_no_data", True)):
        logger.complete(
            "Movement upfront local cache check: %s skipped, %s pending",
            len(upfront_skipped_results),
            len(pending_llinos),
        )

    chunks = [chunk for chunk in chunk_round_robin(pending_llinos, max_workers) if chunk]
    all_results = []

    if chunks:
        if max_workers == 1 and len(chunks) == 1:
            all_results.extend(
                worker_thread(
                    chunks[0],
                    worker_name="worker_01",
                    driver_opts=driver_opts,
                    config=runtime_config,
                )
            )
            combined_results = upfront_skipped_results + all_results
            return _order_results_like_input(llino_list, combined_results)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    worker_thread,
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
                    main_pbar = tqdm(total=total_llinos, desc="Total LLIs", unit="llino")
                except Exception:
                    main_pbar = None

            for future in as_completed(futures):
                try:
                    res = future.result()
                    all_results.extend(res)
                    increment = len(res) if isinstance(res, list) else 1
                    total_processed += increment
                    if main_pbar is not None:
                        try:
                            main_pbar.update(increment)
                        except Exception:
                            pass
                    elif show_progress:
                        try:
                            sys.stdout.write(f"Total processed {total_processed}/{total_llinos}\r")
                            sys.stdout.flush()
                        except Exception:
                            pass
                except Exception as exc:
                    logger.error("[parallel] Error in thread: %s", exc, exc_info=True)

            if main_pbar is not None:
                try:
                    main_pbar.close()
                except Exception:
                    pass

    combined_results = upfront_skipped_results + all_results
    return _order_results_like_input(llino_list, combined_results)


def select_status(driver, select_list=('Calls',)):
    target_statuses = set(resolve_movement_status_list(select_list))

    def _visible_wrappers():
        wrappers = []
        for wrapper in driver.find_elements(By.CSS_SELECTOR, ".lli-dropdown__options-wrapper"):
            try:
                if wrapper.is_displayed():
                    wrappers.append(wrapper)
            except StaleElementReferenceException:
                continue
        return wrappers

    def _wrapper_status_names(wrapper):
        names = []
        for label in wrapper.find_elements(By.CSS_SELECTOR, "label.lli-dropdown__label"):
            try:
                text = label.find_element(By.CSS_SELECTOR, ".lli-dropdown__label-text").text.strip()
                if text:
                    names.append(text)
            except Exception:
                continue
        return names

    def _find_status_wrapper():
        wrappers = _visible_wrappers()
        for wrapper in wrappers:
            names = set(_wrapper_status_names(wrapper))
            if names & set(_ALL_MOVEMENT_STATUSES):
                return wrapper
        return None

    def _open_status_dropdown():
        wrapper = _find_status_wrapper()
        if wrapper is not None:
            return wrapper

        triggers = []
        for trigger in driver.find_elements(By.CSS_SELECTOR, ".lli-dropdown__trigger"):
            try:
                if trigger.is_displayed():
                    triggers.append(trigger)
            except StaleElementReferenceException:
                continue

        for trigger in triggers:
            try:
                _safe_click(driver, trigger)
                WebDriverWait(driver, 3).until(lambda d: _find_status_wrapper() is not None)
                wrapper = _find_status_wrapper()
                if wrapper is not None:
                    return wrapper
            except TimeoutException:
                _dismiss_transient_ui(driver)
                continue

        raise TimeoutException("Status dropdown not found")

    def _find_label_by_name(name: str):
        wrapper = _find_status_wrapper()
        if wrapper is None:
            wrapper = _open_status_dropdown()
        xpath = (
            ".//label[contains(@class,'lli-dropdown__label')]"
            f"[.//span[contains(@class,'lli-dropdown__label-text') and normalize-space(text())='{name}']]"
        )
        return wrapper.find_element(By.XPATH, xpath)

    def _label_checked_by_name(name: str) -> bool:
        label = _find_label_by_name(name)
        input_el = label.find_element(By.CSS_SELECTOR, "input")
        return input_el.is_selected()

    def _set_label_checked(name: str, want_on: bool):
        for _ in range(3):
            try:
                label = _find_label_by_name(name)
                driver.execute_script(
                    """
                    const label = arguments[0];
                    const wantOn = arguments[1];
                    const input = label.querySelector('input');
                    if (!input) return false;

                    label.scrollIntoView({block: 'center', inline: 'nearest'});
                    if (input.checked !== wantOn) {
                        label.click();
                    }

                    if (input.checked !== wantOn) {
                        input.checked = wantOn;
                        input.dispatchEvent(new Event('input', {bubbles: true}));
                        input.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    return input.checked === wantOn;
                    """,
                    label,
                    bool(want_on),
                )
                return
            except StaleElementReferenceException:
                time.sleep(0.2)
                continue
        raise TimeoutException(f"Failed to toggle movement status '{name}'")

    _dismiss_transient_ui(driver)


# Current SeaSearcher movement controls (Radix/data-testid UI).
def _visible_by_xpath(driver, xpath: str):
    for element in driver.find_elements(By.XPATH, xpath):
        try:
            if element.is_displayed():
                return element
        except StaleElementReferenceException:
            continue
    return None


def _read_total_found_count(driver) -> int | None:
    element = _visible_by_xpath(
        driver,
        "//*[@data-testid='tableTotalResults' or @data-testid='itemsTotalCount']",
    )
    if element is None:
        return None
    digits = re.sub(r"[^\d]", "", _normalize_text(element.text))
    return int(digits) if digits else None


def _get_visible_grid_rows(driver) -> list:
    rows = driver.find_elements(By.XPATH, "//*[@data-testid='baseTable']//tbody/tr")
    visible_rows = []
    for row in rows:
        try:
            if row.is_displayed():
                visible_rows.append(row)
        except StaleElementReferenceException:
            continue
    return visible_rows


def _find_open_menu(driver, expected_labels=None):
    wanted = set(expected_labels or [])
    for menu in driver.find_elements(By.XPATH, "//*[@role='menu']"):
        try:
            if not menu.is_displayed():
                continue
            if not wanted:
                return menu
            menu_text = _normalize_text(menu.text)
            if any(label in menu_text for label in wanted):
                return menu
        except StaleElementReferenceException:
            continue
    return None


def select_status(driver, select_list=('Calls',)):
    target_statuses = set(resolve_movement_status_list(select_list))

    def open_menu():
        menu = _find_open_menu(driver, _ALL_MOVEMENT_STATUSES)
        if menu is not None:
            return menu
        for button in driver.find_elements(By.XPATH, "//button[@role='combobox']"):
            try:
                if not button.is_displayed():
                    continue
                _safe_click(driver, button)
                menu = WebDriverWait(driver, 3).until(
                    lambda drv: _find_open_menu(drv, _ALL_MOVEMENT_STATUSES)
                )
                return menu
            except TimeoutException:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        raise TimeoutException("Movement status menu not found")

    for name in _ALL_MOVEMENT_STATUSES:
        menu = open_menu()
        label = _visible_by_xpath(driver, f"//*[@role='menu']//span[normalize-space(.)='{name}']")
        if label is None:
            raise NoSuchElementException(f"Movement status option not found: {name}")
        item = label.find_element(By.XPATH, "./parent::*//*[@role='checkbox']")
        checked = (
            item.get_attribute("aria-checked") == "true"
            or item.get_attribute("data-state") == "checked"
        )
        if checked != (name in target_statuses):
            _safe_click(driver, item)

    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)


def set_local_time_checkbox(driver, value=True):
    checkbox = WebDriverWait(driver, 5).until(
        EC.presence_of_element_located((By.ID, "places-and-passings-local-time"))
    )
    current = checkbox.get_attribute("aria-checked") == "true"
    if current != bool(value):
        _safe_click(driver, checkbox)
        WebDriverWait(driver, 5).until(
            lambda drv: (
                drv.find_element(By.ID, "places-and-passings-local-time").get_attribute("aria-checked")
                == str(bool(value)).lower()
            )
        )


def select_all_columns(driver):
    button = WebDriverWait(driver, 5).until(
        EC.element_to_be_clickable((By.XPATH, "//*[@data-testid='columnsMenuButton']"))
    )

    for _ in range(20):
        menu = _find_open_menu(driver)
        if menu is None:
            _safe_click(driver, button)
            menu = WebDriverWait(driver, 3).until(lambda drv: _find_open_menu(drv))

        unchecked = None
        for item in menu.find_elements(By.XPATH, ".//*[@role='menuitemcheckbox' or @role='checkbox']"):
            if item.get_attribute("aria-checked") != "true" and item.get_attribute("data-state") != "checked":
                unchecked = item
                break
        if unchecked is None:
            break
        _safe_click(driver, unchecked)

    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)


def set_items_per_page_1000(driver, timeout: int = 20) -> None:
    button = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.XPATH, "//*[@data-testid='tableSizeSelector']"))
    )
    if re.sub(r"[^\d]", "", button.text or "") == "1000":
        return
    _safe_click(driver, button)
    option = WebDriverWait(driver, 5).until(
        lambda drv: _visible_by_xpath(
            drv,
            "//*[@role='menuitem' or @role='option'][normalize-space(.)='1000']",
        )
    )
    _safe_click(driver, option)
    WebDriverWait(driver, timeout).until(
        lambda drv: "1000" in drv.find_element(By.XPATH, "//*[@data-testid='tableSizeSelector']").text
    )


def _get_next_button(driver, timeout=5):
    try:
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, "//*[@data-testid='tableNextPageButton']"))
        )
    except TimeoutException:
        return None


def _table_page_number(driver, testid: str) -> int | None:
    element = _visible_by_xpath(driver, f"//*[@data-testid='{testid}']")
    if element is None:
        return None
    digits = re.sub(r"[^\d]", "", element.text or "")
    return int(digits) if digits else None


def have_next_page(driver) -> bool:
    current = _table_page_number(driver, "tableCurrentPage")
    total = _table_page_number(driver, "tableTotalPages")
    return current is not None and total is not None and current < total


def next_page(driver) -> bool:
    before = _table_page_number(driver, "tableCurrentPage")
    if before is None or not have_next_page(driver):
        return False
    button = _get_next_button(driver)
    if button is None or _is_disabled(button):
        return False
    _safe_click(driver, button)
    WebDriverWait(driver, 10).until(
        lambda drv: (_table_page_number(drv, "tableCurrentPage") or 0) > before
    )
    return True


def _parse_period_input(value):
    try:
        return datetime.strptime((value or "").strip(), "%d/%m/%Y")
    except (TypeError, ValueError):
        return None


def _set_period_input_once(
    driver,
    placeholder: str,
    requested_text: str,
    *,
    search_direction: str,
    boundary=None,
) -> str:
    target = _parse_period_input(requested_text)
    if target is None:
        raise ValueError(f"Invalid {placeholder} date: {requested_text}")
    if search_direction not in {"forward", "backward"}:
        raise ValueError(f"Invalid date search direction: {search_direction}")

    boundary_date = boundary
    if isinstance(boundary, str):
        boundary_date = _parse_period_input(boundary)
    if boundary_date is not None:
        if search_direction == "forward" and target > boundary_date:
            raise ValueError(f"{placeholder} search range is invalid: {requested_text} > {boundary}")
        if search_direction == "backward" and target < boundary_date:
            raise ValueError(f"{placeholder} search range is invalid: {requested_text} < {boundary}")

    box = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.XPATH, f"//input[@placeholder='{placeholder}']"))
    )
    box.click()

    def visible_month_text():
        month = _visible_by_xpath(
            driver,
            "//*[contains(concat(' ', normalize-space(@class), ' '), ' react-datepicker__current-month ')]",
        )
        return (month.text or "").strip() if month is not None else ""

    month_text = WebDriverWait(driver, 10).until(lambda drv: visible_month_text())
    current = datetime.strptime(month_text, "%B %Y")
    month_delta = (target.year - current.year) * 12 + target.month - current.month
    navigation_direction = "Next" if month_delta > 0 else "Previous"

    for _ in range(abs(month_delta)):
        old_month = visible_month_text()
        button = _visible_by_xpath(
            driver,
            f"//button[contains(@aria-label, '{navigation_direction} Month')]",
        )
        if button is None or not button.is_enabled():
            break
        _safe_click(driver, button)
        WebDriverWait(driver, 5).until(lambda drv: visible_month_text() != old_month)

    def enabled_days():
        return [
            day
            for day in driver.find_elements(
                By.XPATH,
                "//*[contains(concat(' ', normalize-space(@class), ' '), ' react-datepicker__day ')"
                " and not(contains(@class,'react-datepicker__day--outside-month'))"
                " and not(contains(@class,'react-datepicker__day--disabled'))"
                " and @aria-disabled!='true']",
            )
            if day.is_displayed()
        ]

    def move_month(direction):
        old_month = visible_month_text()
        button = _visible_by_xpath(
            driver,
            f"//button[contains(@aria-label, '{direction} Month')]",
        )
        if button is None or not button.is_enabled():
            return False
        _safe_click(driver, button)
        WebDriverWait(driver, 5).until(lambda drv: visible_month_text() != old_month)
        return True

    # Fromは新しい日付へ、Toは古い日付へ、指定期間内だけを探索する。
    while True:
        visible_month = datetime.strptime(visible_month_text(), "%B %Y")
        selectable = []
        for day in enabled_days():
            try:
                day_number = int((day.text or "").strip())
            except ValueError:
                continue
            candidate = visible_month.replace(day=day_number)
            if search_direction == "forward":
                in_range = candidate >= target and (
                    boundary_date is None or candidate <= boundary_date
                )
            else:
                in_range = candidate <= target and (
                    boundary_date is None or candidate >= boundary_date
                )
            if in_range:
                selectable.append((candidate, day))

        if selectable:
            if search_direction == "forward":
                _, selected = min(selectable, key=lambda item: item[0])
            else:
                _, selected = max(selectable, key=lambda item: item[0])
            _safe_click(driver, selected)
            actual = WebDriverWait(driver, 10).until(
                lambda drv: (
                    drv.find_element(By.XPATH, f"//input[@placeholder='{placeholder}']").get_attribute("value") or ""
                ).strip()
            )
            return actual

        if boundary_date is not None:
            boundary_month = boundary_date.replace(day=1)
            if search_direction == "forward" and visible_month >= boundary_month:
                break
            if search_direction == "backward" and visible_month <= boundary_month:
                break

        next_direction = "Next" if search_direction == "forward" else "Previous"
        if not move_month(next_direction):
            break

    relation = "on or after" if search_direction == "forward" else "on or before"
    raise TimeoutException(
        f"No selectable {placeholder} date found {relation} {requested_text} within the requested period"
    )


def set_period_to_dates(
    driver,
    from_date=None,
    to_date=None,
    allow_from_earliest_fallback=True,
    allow_to_latest_fallback=True,
):
    requested_from_text = _format_date_for_input(from_date)
    requested_to_text = _format_date_for_input(to_date)
    requested_from = _parse_period_input(requested_from_text)
    requested_to = _parse_period_input(requested_to_text)

    actual_from_text = _set_period_input_once(
        driver,
        "From",
        requested_from_text,
        search_direction="forward",
        boundary=requested_to,
    )
    actual_from = _parse_period_input(actual_from_text)
    if actual_from_text != requested_from_text:
        logger.info("From was adjusted to the nearest selectable date: %s", actual_from_text)
    if actual_from and requested_to and actual_from > requested_to:
        logger.info("Requested period has no overlap with available Movement data")
        return False

    actual_to_text = _set_period_input_once(
        driver,
        "To",
        requested_to_text,
        search_direction="backward",
        boundary=actual_from or requested_from,
    )
    actual_to = _parse_period_input(actual_to_text)
    if actual_to_text != requested_to_text:
        logger.info("To was adjusted to the nearest selectable date: %s", actual_to_text)
    if (
        (actual_to and requested_from and actual_to < requested_from)
        or (actual_from and actual_to and actual_from > actual_to)
    ):
        logger.info("Requested period has no overlap with available Movement data")
        return False
    return True


def set_period(
    driver,
    period_cfg,
    allow_from_earliest_fallback=True,
    allow_to_latest_fallback=True,
):
    if period_cfg is None or (isinstance(period_cfg, str) and period_cfg.lower() == "all"):
        return clear_period_to_all(driver)
    if not isinstance(period_cfg, dict):
        raise ValueError("period must be 'all' or {'from': ..., 'to': ...}")
    return set_period_to_dates(driver, period_cfg.get("from"), period_cfg.get("to"))
