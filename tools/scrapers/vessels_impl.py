from __future__ import annotations

import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from . import movement as base
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait


logger = base.logger

VESSELS_URL = "https://www.seasearcher.com/vessels"

DEFAULT_VESSELS_FILTER_LABELS = [
    "Bulk Carrier",
    "General cargo",
    "Refrigerated bulk",
    "Container",
    "Ro-Ro",
    "Vehicle",
    "Chemical tanker",
    "Oil tanker",
    "Liquefied gas tanker",
    "Other liquids tankers",
    "Cruise",
]

FILTER_LABEL_TO_SLUG = {
    "Bulk Carrier": "bulk",
    "General cargo": "generalcargo",
    "Refrigerated bulk": "refrigeratedbulk",
    "Container": "container",
    "Ro-Ro": "roro",
    "Vehicle": "vehicle",
    "Chemical tanker": "chemicaltanker",
    "Oil tanker": "oiltanker",
    "Liquefied gas tanker": "liquefiedgastanker",
    "Other liquids tankers": "otherliquidstankers",
    "Cruise": "cruise",
}

DEFAULT_VESSELS_EXPORT_CONFIG = {
    "out_dir": "vessel",
    "file_label": datetime.now().strftime("%Y%m%d"),
    "encoding": "utf-8-sig",
    "headless": False,
    "overwrite": True,
    "keep_raw_pages": False,
    "timeout": 60,
    "horsepower_reference_label": "202605",
}


def _normalize(value: str) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _number(text: str) -> int | None:
    match = re.search(r"\d[\d,]*", text or "")
    return int(match.group(0).replace(",", "")) if match else None


def _visible(driver, xpath: str):
    for element in driver.find_elements(By.XPATH, xpath):
        try:
            if element.is_displayed():
                return element
        except StaleElementReferenceException:
            pass
    return None


def _click(driver, element) -> None:
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def _testid_number(driver, testid: str) -> int | None:
    element = _visible(driver, f"//*[@data-testid='{testid}']")
    return _number(element.text) if element is not None else None


def _current_page(driver) -> int | None:
    return _testid_number(driver, "tableCurrentPage")


def _total_pages(driver) -> int | None:
    return _testid_number(driver, "tableTotalPages")


def _total_rows(driver) -> int | None:
    return _testid_number(driver, "tableTotalResults")


def _first_vessel_text(driver) -> str:
    element = _visible(
        driver,
        "//*[@data-testid='vesselLinkContent' or @data-testid='vesselLink']",
    )
    return element.text.strip() if element is not None else ""


def _is_login_page(driver) -> bool:
    return bool(
        driver.find_elements(By.ID, "loginPage:loginForm:loginemail")
        or driver.find_elements(By.ID, "loginPage:loginForm:loginpassword")
    )


def initialize_driver(driver_opts=None, download_dir: str | Path | None = None):
    """Create the Chrome driver used by the vessels exporter."""
    return base.initialize_driver(driver_opts, download_dir=download_dir)


def _open_vessels_page(driver, timeout: int) -> None:
    driver.get(VESSELS_URL)
    WebDriverWait(driver, timeout).until(
        lambda drv: _is_login_page(drv)
        or (
            _visible(drv, "//*[@data-testid='vesselTypesAndSizes']") is not None
            and _visible(drv, "//*[@data-testid='tableTotalResults']") is not None
        )
    )
    if _is_login_page(driver):
        raise RuntimeError("SeaSearcher session is not logged in")


def _clear_filters(driver, timeout: int) -> None:
    button = _visible(driver, "//*[@data-testid='clearSearch']")
    if button is not None:
        _click(driver, button)
    WebDriverWait(driver, timeout).until(
        lambda drv: _total_rows(drv) is not None and _current_page(drv) == 1
    )


def _find_vessel_type_row(driver, filter_label: str):
    wanted = _normalize(filter_label)
    for label in driver.find_elements(By.XPATH, "//span[normalize-space()]"):
        try:
            if not label.is_displayed() or _normalize(label.text) != wanted:
                continue
            row = label.find_element(By.XPATH, "./parent::*[.//*[@role='checkbox']]")
            if row.is_displayed():
                return row
        except Exception:
            continue
    return None


def _select_vessel_type(driver, filter_label: str, timeout: int) -> None:
    before = (_total_rows(driver), _total_pages(driver))
    button = _visible(driver, "//*[@data-testid='vesselTypesAndSizes']")
    if button is None:
        raise RuntimeError("Vessel Type/Size button was not found")
    _click(driver, button)

    row = WebDriverWait(driver, timeout).until(
        lambda drv: _find_vessel_type_row(drv, filter_label)
    )
    checkbox = row.find_element(By.XPATH, ".//*[@role='checkbox']")
    if checkbox.get_attribute("aria-checked") != "true":
        _click(driver, checkbox)

    WebDriverWait(driver, timeout).until(
        lambda drv: (_total_rows(drv), _total_pages(drv)) != before
    )
    driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    WebDriverWait(driver, timeout).until(lambda drv: _current_page(drv) == 1)


def _set_1000_items_per_page(driver, timeout: int) -> None:
    selector = _visible(driver, "//*[@data-testid='tableSizeSelector']")
    if selector is None:
        raise RuntimeError("Items-per-page selector was not found")
    if _number(selector.text) == 1000:
        return

    _click(driver, selector)
    option = WebDriverWait(driver, timeout).until(
        lambda drv: _visible(
            drv,
            "//*[@role='menuitem' or @role='option'][normalize-space()='1000']"
            " | //*[normalize-space()='1000' and self::button]",
        )
    )
    _click(driver, option)
    WebDriverWait(driver, timeout).until(
        lambda drv: (
            (el := _visible(drv, "//*[@data-testid='tableSizeSelector']")) is not None
            and _number(el.text) == 1000
        )
    )


def _wait_for_download(download_dir: Path, old_names: set[str], timeout: int) -> Path:
    deadline = time.time() + timeout
    while time.time() < deadline:
        partial = list(download_dir.glob("*.crdownload"))
        new_files = [
            path
            for path in download_dir.glob("*.csv")
            if path.name not in old_names
        ]
        if new_files and not partial:
            newest = max(new_files, key=lambda path: path.stat().st_mtime)
            size = newest.stat().st_size
            time.sleep(0.5)
            if newest.stat().st_size == size:
                return newest
        time.sleep(0.5)
    raise TimeoutException("CSV download timed out")


def _download_page(
    driver,
    download_dir: Path,
    pages_dir: Path,
    page_number: int,
    timeout: int,
) -> Path:
    old_names = {path.name for path in download_dir.iterdir() if path.is_file()}

    for attempt in range(2):
        try:
            menu_button = _visible(driver, "//*[@data-testid='moreActionsButton']")
            if menu_button is None:
                raise RuntimeError("More Actions button was not found")
            _click(driver, menu_button)
            export_button = WebDriverWait(driver, timeout).until(
                lambda drv: _visible(drv, "//*[normalize-space()='Export Table Data']")
            )
            _click(driver, export_button)
            break
        except StaleElementReferenceException:
            if attempt == 1:
                raise
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.2)

    downloaded = _wait_for_download(download_dir, old_names, timeout)
    pages_dir.mkdir(parents=True, exist_ok=True)
    destination = pages_dir / f"page_{page_number:04d}.csv"
    shutil.move(str(downloaded), destination)
    return destination


def _go_to_next_page(driver, timeout: int) -> None:
    old_page = _current_page(driver)
    old_first = _first_vessel_text(driver)
    button = _visible(driver, "//*[@data-testid='tableNextPageButton']")
    if button is None or button.get_attribute("disabled") is not None:
        raise RuntimeError("Next page button was not available")
    _click(driver, button)

    WebDriverWait(driver, timeout).until(
        lambda drv: (
            _current_page(drv) is not None
            and old_page is not None
            and _current_page(drv) > old_page
            and _first_vessel_text(drv) not in ("", old_first)
        )
    )


def _read_csv(path: Path) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            pass
    return pd.read_csv(path)


def _add_engine_horsepower(
    frame: pd.DataFrame,
    reference_path: Path | None = None,
) -> pd.DataFrame:
    if "Engine Horsepower" in frame.columns:
        return frame

    old_values = {}
    old_power_values = {}
    if reference_path is not None and reference_path.exists():
        old = _read_csv(reference_path)
        if (
            "LLI NO" in old.columns
            and "Engine Horsepower" in old.columns
            and "Engine Power (kW)" in old.columns
        ):
            keys = old["LLI NO"].fillna("").astype(str).str.strip()
            old_values = dict(zip(keys, old["Engine Horsepower"]))
            old_power_values = dict(zip(keys, old["Engine Power (kW)"].astype(str)))

    keys = frame["LLI NO"].fillna("").astype(str).str.strip()
    horsepower = keys.map(old_values).astype("string")
    missing = horsepower.isna() | horsepower.str.strip().str.lower().isin(
        {"", "-", "nan", "none"}
    )
    power_kw = pd.to_numeric(frame["Engine Power (kW)"], errors="coerce")
    calculated = (power_kw * 1.34102209).round().astype("Int64").astype("string")
    old_power_kw = pd.to_numeric(keys.map(old_power_values), errors="coerce")
    old_value_is_usable = (
        ~missing
        & (power_kw.isna() | old_power_kw.isna() | power_kw.eq(old_power_kw))
    )
    horsepower = horsepower.where(old_value_is_usable, calculated).fillna("-")

    insert_at = frame.columns.get_loc("Engine Power (kW)") + 1
    frame.insert(insert_at, "Engine Horsepower", horsepower)
    return frame


def _merge_pages(
    page_paths: list[Path],
    output_path: Path,
    encoding: str,
    reference_path: Path | None = None,
) -> int:
    frames = [_read_csv(path) for path in page_paths]
    merged = pd.concat(frames, ignore_index=True)
    merged = _add_engine_horsepower(merged, reference_path)
    merged.to_csv(output_path, index=False, encoding=encoding)
    return len(merged)


def _resolve_filter_labels(filter_labels: list[str]) -> list[str]:
    if not filter_labels:
        raise ValueError("Specify at least one vessel type in FILTER_LABELS")

    by_key = {_normalize(label): label for label in FILTER_LABEL_TO_SLUG}
    resolved = []
    for label in filter_labels:
        key = _normalize(label)
        if key not in by_key:
            valid = ", ".join(FILTER_LABEL_TO_SLUG)
            raise ValueError(f"Unsupported vessel type: {label}. Valid values: {valid}")
        resolved.append(by_key[key])
    return resolved


def export_vessels_filters(
    filter_labels: list[str],
    config: dict | None = None,
) -> list[dict]:
    """Export every 1000-row page for each requested vessel type.

    Only Vessel Type/Size is filtered. Status is left unfiltered, so Live, Dead,
    On Order, and the other statuses present in SeaSearcher are all included.
    The browser logs in once and is not automatically restarted or re-logged in.
    """
    cfg = {**DEFAULT_VESSELS_EXPORT_CONFIG, **(config or {})}
    labels = _resolve_filter_labels(filter_labels)
    out_dir = Path(cfg["out_dir"])
    download_dir = out_dir / "_downloads" / "vessels"
    run_dir = out_dir / "_vessels_pages" / datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)

    timeout = int(cfg["timeout"])
    encoding = str(cfg["encoding"])
    overwrite = bool(cfg["overwrite"])
    keep_raw_pages = bool(cfg["keep_raw_pages"])
    results = []
    driver = initialize_driver(
        {"headless": bool(cfg["headless"])},
        download_dir=download_dir,
    )

    try:
        base.login_to_seasearcher(driver, config=cfg)

        for filter_label in labels:
            slug = FILTER_LABEL_TO_SLUG[filter_label]
            output_path = out_dir / f"vessels_{cfg['file_label']}_{slug}.csv"
            reference_path = out_dir / (
                f"vessels_{cfg['horsepower_reference_label']}_{slug}.csv"
            )
            if output_path.exists() and not overwrite:
                results.append(
                    {
                        "filter_label": filter_label,
                        "rows": None,
                        "pages": None,
                        "output_path": str(output_path),
                        "ok": True,
                    }
                )
                continue

            _open_vessels_page(driver, timeout)
            _clear_filters(driver, timeout)
            _select_vessel_type(driver, filter_label, timeout)
            _set_1000_items_per_page(driver, timeout)

            total_rows = _total_rows(driver)
            total_pages = _total_pages(driver)
            if total_rows is None or total_pages is None:
                raise RuntimeError("Could not read vessel count or page count")

            logger.info(
                "Vessels export: %s, %s rows, %s pages",
                filter_label,
                total_rows,
                total_pages,
            )

            pages_dir = run_dir / slug
            page_paths = []
            for page_number in range(1, total_pages + 1):
                current_page = _current_page(driver)
                if current_page != page_number:
                    raise RuntimeError(
                        f"Unexpected page number: expected {page_number}, got {current_page}"
                    )
                page_paths.append(
                    _download_page(
                        driver,
                        download_dir,
                        pages_dir,
                        page_number,
                        timeout,
                    )
                )
                logger.info("%s: page %s/%s downloaded", filter_label, page_number, total_pages)
                if page_number < total_pages:
                    _go_to_next_page(driver, timeout)

            rows = _merge_pages(
                page_paths,
                output_path,
                encoding,
                reference_path=reference_path,
            )
            if rows != total_rows:
                logger.warning(
                    "%s: table reported %s rows but CSV contains %s rows",
                    filter_label,
                    total_rows,
                    rows,
                )

            results.append(
                {
                    "filter_label": filter_label,
                    "rows": rows,
                    "pages": total_pages,
                    "output_path": str(output_path),
                    "ok": True,
                }
            )

            if not keep_raw_pages:
                shutil.rmtree(pages_dir)
    finally:
        driver.quit()

    if run_dir.exists() and not any(run_dir.iterdir()):
        run_dir.rmdir()
    return results
