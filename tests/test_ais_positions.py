import threading
import unittest
import json
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import pandas as pd

from tools.scrapers import ais_positions as ais


class FakeElement:
    def __init__(self, *, text="", value=None, displayed=True, parent=None):
        self.text = text
        self._value = value
        self._displayed = displayed
        self._parent = parent

    def is_displayed(self):
        return self._displayed

    def get_attribute(self, name):
        if name == "value":
            return self._value
        return None

    def find_element(self, by, value):
        if value == ".." and self._parent is not None:
            return self._parent
        raise RuntimeError("element not found")


class FakeDriver:
    def __init__(self, *, url="https://www.seasearcher.com/vessel/1/movements", title="Vessel", body=""):
        self.current_url = url
        self.title = title
        self.body = body
        self.quit_calls = 0

    def execute_script(self, script, *args):
        if "document.body" in script:
            return self.body
        return "complete"

    def find_elements(self, by, value):
        return []

    def set_page_load_timeout(self, value):
        pass

    def set_script_timeout(self, value):
        pass

    def quit(self):
        self.quit_calls += 1


class NetworkResponseDriver:
    PERIOD = {"from": "2026-02-01", "to": "2026-02-28"}

    def __init__(self, payload, **kwargs):
        self._logs = []
        self._bodies = {}
        self.body_request_ids = []
        self.add_response(payload, **kwargs)

    def add_response(self, payload, *, llino=1, period=None, page_index=1, page_size=1000):
        period = self.PERIOD if period is None else period
        filters = {"vesselIds": [str(llino)]}
        if period != "all":
            filters["dateRange"] = {
                "from": period["from"] + "T00:00:00.000Z",
                "to": period["to"] + "T23:59:59.999Z",
            }
        query = {"PageNumber": page_index - 1, "PageSize": page_size, "Filters": filters}
        url = "https://www.seasearcher.com/api/vessel/aismessages?" + urlencode({"query": json.dumps(query)})
        request_id = f"ais-request-{len(self._bodies) + 1}"
        for method, detail in (("Network.requestWillBeSent", {"request": {"url": url}}),
                               ("Network.responseReceived", {"response": {"url": url}})):
            self._logs.append({"message": json.dumps({"message": {
                "method": method, "params": {"requestId": request_id, **detail},
            }})})
        self._bodies[request_id] = {"body": json.dumps(payload), "base64Encoded": False}

    def get_log(self, name):
        logs, self._logs = self._logs, []
        return logs

    def execute_cdp_cmd(self, name, params):
        self._last_cdp_call = (name, params)
        self.body_request_ids.append(params["requestId"])
        return self._bodies[params["requestId"]]


class PaginationTests(unittest.TestCase):
    def test_reads_current_seasearcher_testid_pagination_and_count(self):
        current_page = FakeElement(text="1")
        total_pages = FakeElement(text="2")
        found_count = FakeElement(text="1,592")
        driver = Mock()

        def find_elements(by, xpath):
            if "tableCurrentPage" in xpath:
                return [current_page]
            if "tableTotalPages" in xpath:
                return [total_pages]
            if "tableTotalResults" in xpath:
                return [found_count]
            return []

        driver.find_elements.side_effect = find_elements

        self.assertEqual(ais._read_pagination_state(driver), {"current_page": 1, "total_pages": 2})
        self.assertEqual(ais._read_total_found_count(driver), 1592)

    def test_reads_current_and_total_pages(self):
        parent = FakeElement(text="Page of 3")
        page_input = FakeElement(value="3", parent=parent)
        driver = Mock()
        driver.find_elements.side_effect = lambda by, value: [page_input] if "pager__input" in value else []

        self.assertEqual(
            ais._read_pagination_state(driver),
            {"current_page": 3, "total_pages": 3},
        )

    @patch.object(ais, "_is_ais_pager_button_disabled", return_value=False)
    @patch.object(ais, "_get_next_ais_page_button", return_value=object())
    def test_does_not_click_past_last_page(self, _button, _disabled):
        state = {
            "current_page": 3,
            "total_pages": 3,
            "total_count": 2953,
            "row_count": 953,
        }
        self.assertFalse(ais._has_next_page_for_ais(Mock(), state, page_index=3))

    @patch.object(ais, "_wait_for_optional_staleness", return_value=False)
    @patch.object(
        ais,
        "_wait_for_loaded_grid",
        return_value={
            "row_count": 0,
            "signature": (),
            "first_row": None,
            "total_count": 2953,
            "current_page": 4,
            "total_pages": 3,
        },
    )
    def test_page_four_of_three_is_pagination_end(self, _loaded, _stale):
        with self.assertRaises(ais.AISPaginationEnd):
            ais._wait_for_next_page_ready(Mock(), {"first_row": None}, timeout=0)


class AISPeriodApplicationTests(unittest.TestCase):
    def test_previous_thirty_day_window_is_cleared_before_single_day_window(self):
        inputs = {"From": "02/03/2026", "To": "31/03/2026"}
        driver = Mock()
        driver.find_element.side_effect = lambda by, xpath: FakeElement(
            value=inputs["From" if "From" in xpath else "To"]
        )

        def clear(_driver):
            inputs.update({"From": "", "To": ""})

        def set_period(_driver, period, **kwargs):
            self.assertEqual(inputs, {"From": "", "To": ""})
            self.assertFalse(kwargs["allow_from_earliest_fallback"])
            self.assertFalse(kwargs["allow_to_latest_fallback"])
            return True

        windows = ais.build_ais_period_windows({"from": "2026-03-01", "to": "2026-03-31"})
        self.assertEqual(windows, [
            {"from": "2026-03-02", "to": "2026-03-31"},
            {"from": "2026-03-01", "to": "2026-03-01"},
        ])
        with patch.object(ais.base, "clear_period_to_all", side_effect=clear), \
             patch.object(ais.base, "set_period", side_effect=set_period):
            ais._apply_ais_period(driver, windows[1])

    def test_failed_period_application_is_not_accepted(self):
        driver = Mock()
        driver.find_element.return_value = FakeElement(value="")
        with patch.object(ais.base, "clear_period_to_all"), \
             patch.object(ais.base, "set_period", return_value=False):
            with self.assertRaisesRegex(ais.TimeoutException, "period was not applied"):
                ais._apply_ais_period(driver, NetworkResponseDriver.PERIOD)

    def test_local_time_uses_the_checkbox_in_the_ais_tab(self):
        driver = Mock()
        checkbox = Mock()
        state = {"value": "false"}
        checkbox.get_attribute.side_effect = lambda name: state["value"] if name == "aria-checked" else None
        driver.find_element.return_value = checkbox
        wait = Mock()
        wait.until.side_effect = lambda condition: condition(driver)

        def click(_driver, element):
            self.assertIs(element, checkbox)
            state["value"] = "true"

        with patch.object(ais, "WebDriverWait", return_value=wait), patch.object(ais.base, "_safe_click", side_effect=click):
            ais._set_ais_local_time_checkbox(driver, True)

        self.assertIn("Local Time", driver.find_element.call_args.args[1])
        self.assertEqual(state["value"], "true")

    def test_old_empty_grid_does_not_override_nonempty_matching_response(self):
        payload = {"gridData": [{"timestamp": "2026-02-28T23:34:05Z"}], "gridResultTotalMatches": 1}
        driver = NetworkResponseDriver(payload)
        stale_state = {"row_count": 0, "total_count": 0, "current_page": 1}
        with tempfile.TemporaryDirectory() as out_dir, ExitStack() as stack:
            for name in ("_toolbar_ready", "_sleep_configured_delay", "_apply_ais_period", "_dismiss_transient_ui"):
                stack.enter_context(patch.object(ais, name))
            stack.enter_context(patch.object(ais.base, "_clear_performance_log"))
            stack.enter_context(patch.object(ais, "_capture_grid_state", return_value=stale_state))
            stack.enter_context(patch.object(ais, "_wait_for_items_per_page_apply", return_value=stale_state))
            stack.enter_context(patch.object(ais, "_return_to_first_ais_page", return_value=stale_state))
            stack.enter_context(patch.object(ais, "_items_per_page_1000_selected", return_value=True))
            stack.enter_context(patch.object(ais, "_wait_for_ais_grid_for_response", return_value={
                "row_count": 1, "total_count": 1, "current_page": 1,
            }))
            result = ais._scraping_ais_positions_single_period(
                driver, 1, page_ready=True,
                config={"out_dir": out_dir, "period": NetworkResponseDriver.PERIOD,
                        "skip_if_exists": False, "skip_if_known_no_data": False, "remember_no_data": False},
            )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["rows"], 1)
            self.assertTrue(Path(result["output_path"]).exists())


class AISNetworkTests(unittest.TestCase):
    def _read(self, driver, **kwargs):
        options = {
            "expected_rows": 1, "expected_total": 1, "llino": 1,
            "period_cfg": NetworkResponseDriver.PERIOD, "page_index": 1,
        }
        return ais._read_current_ais_grid(driver, **{**options, **kwargs})

    def _record(self, **overrides):
        record = {
            "nearestPlace": "Kish Island",
            "distanceNm": 5.1,
            "dateTime": "2026-02-28T23:34:05Z",
            "position": {"latitude": 26.5566, "longitude": 54.0695},
            "aisDestination": "I",
            "heading": 270,
            "sog": 3.8,
            "draught": 5.3,
            "cog": 288.3,
            "sourceType": "Shipborne",
            "navigationStatus": "At Anchor",
        }
        record.update(overrides)
        return record

    def test_response_matching_and_legacy_conversion(self):
        payload = {"results": [self._record()], "totalMatches": 1}
        frame = self._read(NetworkResponseDriver(payload))

        self.assertEqual(list(frame.columns), ais._AIS_OUTPUT_COLUMNS)
        self.assertEqual(frame.loc[0, "Nearest Place"], "Kish Island")
        self.assertEqual(frame.loc[0, "Date/Time"], "23:34:05 GMT 28/02/2026")
        self.assertEqual(frame.loc[0, "Lat"], "26.5566")
        self.assertEqual(frame.attrs["total_count"], 1)

    def test_response_row_count_must_match(self):
        payload = {"results": [self._record(), self._record(distanceNm=9)], "totalMatches": 2}
        with self.assertRaises(ais.TimeoutException):
            self._read(NetworkResponseDriver(payload), expected_total=2, timeout=0.01)

    def test_intermediate_date_range_is_ignored_even_when_counts_and_row_dates_match(self):
        driver = NetworkResponseDriver(
            {"results": [self._record(nearestPlace="intermediate")], "totalMatches": 1},
            period={"from": "2026-02-01", "to": "2026-03-31"},
        )
        driver.add_response({"results": [self._record(nearestPlace="correct")], "totalMatches": 1})

        frame = self._read(driver)

        self.assertEqual(frame.loc[0, "Nearest Place"], "correct")
        self.assertEqual(driver.body_request_ids, ["ais-request-2"])

    def test_other_vessel_page_or_page_size_is_not_a_matching_search(self):
        payload = {"results": [self._record()], "totalMatches": 1}
        for overrides in ({"llino": 2}, {"page_index": 2}, {"page_size": 100}):
            with self.subTest(overrides=overrides):
                driver = NetworkResponseDriver(payload, **overrides)
                with self.assertRaises(ais.TimeoutException):
                    self._read(driver, timeout=0.01)
                self.assertEqual(driver.body_request_ids, [])

    def test_late_response_without_a_request_in_the_current_capture_is_ignored(self):
        driver = NetworkResponseDriver({"results": [self._record()], "totalMatches": 1})
        driver._logs = driver._logs[1:]
        with self.assertRaises(ais.TimeoutException):
            self._read(driver, timeout=0.01)
        self.assertEqual(driver.body_request_ids, [])

    def test_table_data_and_table_total_are_used_instead_of_map_data(self):
        payload = {
            "gridData": [self._record()], "gridResultTotalMatches": 1,
            "aisPoints": [self._record(dateTime="2026-01-01T00:00:00Z")],
            "mapPointTotalMatches": 1000,
        }
        frame = self._read(NetworkResponseDriver(payload))
        self.assertEqual(frame.loc[0, "Date/Time"], "23:34:05 GMT 28/02/2026")
        self.assertIsNone(ais._ais_payload_total({"gridData": [], "totalMatches": 1000}))

    def test_server_returning_outside_or_invalid_dates_is_an_error(self):
        for value in ("2026-03-03T00:00:00Z", None, "invalid-date"):
            with self.subTest(value=value):
                payload = {"results": [self._record(dateTime=value)], "totalMatches": 1}
                with self.assertRaisesRegex(ValueError, "dates do not match period"):
                    self._read(NetworkResponseDriver(payload))

    def test_period_validation_rejects_dates_outside_the_requested_window(self):
        period = {"from": "2026-03-01", "to": "2026-03-31"}
        outside_window = pd.DataFrame({"Date/Time": [
            "23:59:59 GMT 28/02/2026",
            "00:00:00 GMT 01/04/2026",
        ]})
        with self.assertRaisesRegex(ValueError, "invalid_rows=2"):
            ais._validate_ais_frame_period(outside_window, period)

        within_window = pd.DataFrame({"Date/Time": [
            "00:00:00 GMT 01/03/2026",
            "23:59:59 GMT 31/03/2026",
        ]})
        ais._validate_ais_frame_period(within_window, period)

    def test_initial_row_count_comes_from_the_matching_response_including_zero(self):
        for records in ([], [self._record()]):
            with self.subTest(rows=len(records)):
                driver = NetworkResponseDriver({"gridData": records, "gridResultTotalMatches": len(records)})
                frame = self._read(driver, expected_rows=None, expected_total=None)
                self.assertEqual(len(frame), len(records))
                self.assertEqual(frame.attrs["total_count"], len(records))

    def test_period_endpoints_cover_the_whole_utc_day(self):
        records = [self._record(dateTime="2026-02-01T00:00:00Z"), self._record(dateTime="2026-02-28T23:59:59Z")]
        frame = self._read(
            NetworkResponseDriver({"results": records, "totalMatches": 2}),
            expected_rows=2, expected_total=2,
        )
        self.assertEqual(len(frame), 2)

    def test_all_period_rejects_a_filtered_request(self):
        driver = NetworkResponseDriver({"results": [self._record()], "totalMatches": 1})
        with self.assertRaises(ais.TimeoutException):
            self._read(driver, period_cfg="all", timeout=0.01)
        driver = NetworkResponseDriver({"results": [self._record()], "totalMatches": 1}, period="all")
        self.assertEqual(len(self._read(driver, period_cfg="all")), 1)

    def test_final_partial_page_expected_rows_use_total_count(self):
        self.assertEqual(ais._expected_ais_page_rows(2953, 1, fallback_row_count=100), 1000)
        self.assertEqual(ais._expected_ais_page_rows(2953, 2, fallback_row_count=100), 1000)
        self.assertEqual(ais._expected_ais_page_rows(2953, 3, fallback_row_count=100), 953)
        self.assertEqual(ais._expected_ais_page_rows(None, 1, fallback_row_count=17), 17)

    def test_repeated_page_and_exact_duplicate_validation(self):
        frame = ais._ais_records_to_frame([self._record()])
        frames = []
        markers = set()
        ais._append_ais_page(frame, 1, frames, markers)
        with self.assertRaises(ValueError):
            ais._append_ais_page(frame.copy(), 2, frames, markers)
        with self.assertRaises(ValueError):
            ais._merge_ais_frames([frame, frame.copy()], expected_total=2)

    def test_period_windows_are_streamed_without_pandas_bulk_read(self):
        frame_1 = ais._ais_records_to_frame([self._record()])
        frame_2 = ais._ais_records_to_frame([self._record(dateTime="2026-03-01T00:00:00Z")])
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            first_path = temp_path / "first.csv"
            second_path = temp_path / "second.csv"
            output_path = temp_path / "merged.csv"
            frame_1.to_csv(first_path, index=False, encoding="utf-8-sig")
            frame_2.loc[:, list(reversed(ais._AIS_OUTPUT_COLUMNS))].to_csv(
                second_path,
                index=False,
                encoding="utf-8-sig",
            )

            with patch.object(ais.pd, "read_csv", side_effect=AssertionError("bulk read is not allowed")):
                row_count = ais._stream_merge_ais_window_files(
                    [first_path, second_path],
                    output_path,
                    "utf-8-sig",
                )

            self.assertEqual(row_count, 2)
            merged = ais._read_csv_with_fallback(output_path)
            self.assertEqual(list(merged.columns), ais._AIS_OUTPUT_COLUMNS)
            self.assertEqual(len(merged), 2)

    @patch.object(ais, "set_items_per_page_1000")
    @patch.object(ais, "_items_per_page_1000_selected", return_value=True)
    def test_1000_selector_is_not_changed_when_already_selected(self, _selected, set_items):
        self.assertFalse(ais.ensure_items_per_page_1000(Mock()))
        set_items.assert_not_called()

    @patch.object(ais, "set_items_per_page_1000")
    @patch.object(ais, "_items_per_page_1000_selected", return_value=False)
    def test_1000_selector_changes_only_when_needed(self, _selected, set_items):
        self.assertTrue(ais.ensure_items_per_page_1000(Mock()))
        set_items.assert_called_once()


class FailureClassificationTests(unittest.TestCase):
    def test_login_page_is_session_expired(self):
        driver = FakeDriver(url="https://lli.example/SignIn", title="Sign In")
        self.assertEqual(ais._classify_driver_failure(driver)[0], "session_expired")

    def test_access_denied_is_blocked_before_login_check(self):
        driver = FakeDriver(title="Access Denied", body="Too Many Requests")
        error_type, marker = ais._classify_driver_failure(driver)
        self.assertEqual(error_type, "access_blocked")
        self.assertTrue(marker)

    @patch.object(ais, "_save_debug_artifacts", return_value={})
    @patch.object(ais, "_classify_driver_failure", return_value=("transient_timeout", None))
    @patch.object(ais.base, "_chrome_page_error_detail", return_value=None)
    @patch.object(ais.base, "open_movement", side_effect=ais.WebDriverException("Out of Memory"))
    def test_browser_flagged_webdriver_exception_remains_refreshable_timeout(
        self, _open, _detail, _classify, _debug
    ):
        with tempfile.TemporaryDirectory() as out_dir:
            result = ais._scraping_ais_positions_single_period(
                Mock(),
                1,
                config={"out_dir": out_dir, "skip_if_exists": False, "skip_if_known_no_data": False},
            )

        self.assertEqual(result["error"], "timeout")
        self.assertEqual(result["error_type"], "transient_timeout")
        self.assertTrue(result["chrome_memory_error"])
        _detail.assert_not_called()
        _classify.assert_not_called()
        _debug.assert_not_called()

    @patch.object(ais, "_save_debug_artifacts", return_value={})
    @patch.object(ais, "_classify_driver_failure", return_value=("transient_timeout", None))
    @patch.object(ais.base, "_chrome_page_error_detail", return_value=None)
    @patch.object(
        ais.base,
        "open_movement",
        side_effect=ais.WebDriverException(
            "HTTPConnectionPool(host='localhost', port=9515): Read timed out. (read timeout=45)"
        ),
    )
    def test_chromedriver_command_timeout_skips_diagnostics_and_is_refreshable(
        self, _open, _detail, _classify, _debug
    ):
        with tempfile.TemporaryDirectory() as out_dir:
            result = ais._scraping_ais_positions_single_period(
                Mock(),
                1,
                config={"out_dir": out_dir, "skip_if_exists": False, "skip_if_known_no_data": False},
            )

        self.assertEqual(result["error"], "timeout")
        self.assertEqual(result["error_type"], "transient_timeout")
        self.assertTrue(result["webdriver_command_timeout"])
        self.assertEqual(result["debug"], {})
        _detail.assert_not_called()
        _classify.assert_not_called()
        _debug.assert_not_called()


class WorkerSessionTests(unittest.TestCase):
    def _config(self):
        return {
            "out_dir": "unused-test-output",
            "show_progress": False,
            "periodic_rest_enabled": False,
            "retry_failed_llino_after_wait": True,
            "failed_llino_retry_wait_minutes": 0,
            "failed_llino_retry_random_minutes": 0,
            "failed_llino_retry_max_attempts": 2,
            "session_relogin_attempts": 1,
            "webdriver_restart_attempts": 0,
            "_stop_event": threading.Event(),
        }

    @patch.object(ais.shutil, "rmtree")
    @patch.object(ais.time, "sleep")
    @patch.object(ais, "initialize_driver")
    @patch.object(ais.base, "login_to_seasearcher")
    @patch.object(ais, "scraping_ais_positions")
    def test_generic_timeout_retries_without_relogin(
        self, scrape, login, initialize, _sleep, _rmtree
    ):
        driver = FakeDriver()
        initialize.return_value = driver
        login.return_value = driver
        scrape.side_effect = [
            {"llino": 1, "ok": False, "error": "timeout", "error_type": "transient_timeout"},
            {"llino": 1, "ok": True, "pages": 1},
        ]

        results = ais.worker_thread_ais_positions([1], config=self._config())

        self.assertTrue(results[0]["ok"])
        self.assertEqual(login.call_count, 1)
        self.assertEqual(results[0]["relogin_count"], 0)

    @patch.object(ais.shutil, "rmtree")
    @patch.object(ais, "initialize_driver")
    @patch.object(ais.base, "login_to_seasearcher")
    @patch.object(ais, "scraping_ais_positions")
    def test_confirmed_session_loss_relogs_once_in_same_browser(
        self, scrape, login, initialize, _rmtree
    ):
        driver = FakeDriver()
        initialize.return_value = driver
        login.return_value = driver
        scrape.side_effect = [
            {"llino": 1, "ok": False, "error": "session_expired", "error_type": "session_expired"},
            {"llino": 1, "ok": True, "pages": 1},
        ]

        results = ais.worker_thread_ais_positions([1], config=self._config())

        self.assertTrue(results[0]["ok"])
        self.assertEqual(login.call_count, 2)
        self.assertEqual(results[0]["relogin_count"], 1)
        self.assertIs(login.call_args_list[1].args[0], driver)

    @patch.object(ais.shutil, "rmtree")
    @patch.object(ais.time, "sleep")
    @patch.object(ais, "initialize_driver")
    @patch.object(ais.base, "login_to_seasearcher")
    @patch.object(ais, "scraping_ais_positions")
    def test_browser_issue_refreshes_same_browser_once_without_relogin(
        self, scrape, login, initialize, _sleep, _rmtree
    ):
        driver = FakeDriver()
        initialize.return_value = driver
        login.return_value = driver
        scrape.side_effect = [
            {
                "llino": 1,
                "ok": False,
                "error": "timeout",
                "error_type": "transient_timeout",
                "chrome_page_error": True,
            },
            {"llino": 1, "ok": True, "pages": 1},
        ]

        results = ais.worker_thread_ais_positions([1], config=self._config())

        self.assertTrue(results[0]["ok"])
        self.assertEqual(scrape.call_count, 2)
        self.assertTrue(scrape.call_args_list[1].kwargs["reload_current_page"])
        self.assertEqual(login.call_count, 1)

    @patch.object(ais.shutil, "rmtree")
    @patch.object(ais, "initialize_driver")
    @patch.object(ais.base, "login_to_seasearcher")
    @patch.object(ais, "scraping_ais_positions")
    def test_access_block_stops_remaining_targets_without_relogin(
        self, scrape, login, initialize, _rmtree
    ):
        driver = FakeDriver()
        initialize.return_value = driver
        login.return_value = driver
        scrape.return_value = {
            "llino": 1,
            "ok": False,
            "error": "access_blocked",
            "error_type": "access_blocked",
        }

        results = ais.worker_thread_ais_positions([1, 2], config=self._config())

        self.assertEqual(login.call_count, 1)
        self.assertEqual(scrape.call_count, 1)
        self.assertEqual(results[0]["error_type"], "access_blocked")
        self.assertEqual(results[1]["error_type"], "run_stopped")


if __name__ == "__main__":
    unittest.main()
