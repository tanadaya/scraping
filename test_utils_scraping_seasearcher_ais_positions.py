import threading
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import utils_scraping_seasearcher_ais_positions as ais


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
    def __init__(self, payload, url="https://www.seasearcher.com/api/vessel/ais-positions"):
        message = {
            "message": json.dumps(
                {
                    "message": {
                        "method": "Network.responseReceived",
                        "params": {
                            "requestId": "ais-request-1",
                            "response": {"url": url},
                        },
                    }
                }
            )
        }
        self._logs = [message]
        self._body = {"body": json.dumps(payload), "base64Encoded": False}

    def get_log(self, name):
        logs, self._logs = self._logs, []
        return logs

    def execute_cdp_cmd(self, name, params):
        self._last_cdp_call = (name, params)
        return self._body


class PaginationTests(unittest.TestCase):
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


class AISNetworkTests(unittest.TestCase):
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
        frame = ais._read_current_ais_grid(NetworkResponseDriver(payload), expected_rows=1, expected_total=1)

        self.assertEqual(list(frame.columns), ais._AIS_OUTPUT_COLUMNS)
        self.assertEqual(frame.loc[0, "Nearest Place"], "Kish Island")
        self.assertEqual(frame.loc[0, "Date/Time"], "23:34:05 GMT 28/02/2026")
        self.assertEqual(frame.loc[0, "Lat"], "26.5566")
        self.assertEqual(frame.attrs["total_count"], 1)

    def test_response_row_count_must_match(self):
        payload = {"results": [self._record(), self._record(distanceNm=9)], "totalMatches": 2}
        with self.assertRaises(ais.TimeoutException):
            ais._read_current_ais_grid(NetworkResponseDriver(payload), expected_rows=1, expected_total=2, timeout=0.01)

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
