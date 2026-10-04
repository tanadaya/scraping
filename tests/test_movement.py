import unittest
import tempfile
from unittest.mock import MagicMock, patch

import pandas as pd

from tools.scrapers import movement


class DatePeriodSelectionTests(unittest.TestCase):
    def test_clear_period_uses_the_current_range_clear_icon(self):
        icon = MagicMock()
        icon.is_displayed.return_value = True
        driver = MagicMock()

        def find_elements(by, xpath):
            if "lucide-circle-x" in xpath:
                return [icon]
            return []

        driver.find_elements.side_effect = find_elements

        self.assertTrue(movement.clear_period_to_all(driver))
        icon.click.assert_called_once()

    @patch.object(movement.time, "sleep", return_value=None)
    @patch.object(movement, "_safe_click")
    @patch.object(movement, "_visible_by_xpath")
    def test_nonempty_old_value_is_not_accepted_after_calendar_click(self, visible, _click, _sleep):
        month = MagicMock(text="June 2026")
        visible.return_value = month
        box = MagicMock()
        box.is_displayed.return_value = True
        box.is_enabled.return_value = True
        box.get_attribute.side_effect = ["31/07/2026", "30/06/2026"]
        day = MagicMock(text="30")
        day.is_displayed.return_value = True
        driver = MagicMock()
        driver.find_element.return_value = box
        driver.find_elements.return_value = [day]

        actual = movement._set_period_input_once(
            driver, "To", "30/06/2026", search_direction="backward", boundary="01/06/2026"
        )

        self.assertEqual(actual, "30/06/2026")
        self.assertEqual(box.get_attribute.call_count, 2)

    @patch.object(movement, "_set_period_input_once")
    def test_dispatcher_honors_disabled_fallback_for_both_dates(self, select):
        for returned_dates in (["02/06/2026"], ["01/06/2026", "29/06/2026"]):
            with self.subTest(returned_dates=returned_dates):
                select.side_effect = returned_dates
                with self.assertRaisesRegex(movement.TimeoutException, "does not match requested date"):
                    movement.set_period(
                        MagicMock(), {"from": "2026-06-01", "to": "2026-06-30"},
                        allow_from_earliest_fallback=False, allow_to_latest_fallback=False,
                    )

    @patch.object(movement, "WebDriverWait")
    @patch.object(movement, "_set_period_input_once", side_effect=["01/06/2026", "30/06/2026"])
    def test_selecting_to_must_not_silently_change_from(self, _select, wait_class):
        driver = MagicMock()
        from_box = MagicMock()
        from_box.get_attribute.return_value = "02/06/2026"
        to_box = MagicMock()
        to_box.get_attribute.return_value = "30/06/2026"
        driver.find_element.side_effect = lambda by, xpath: from_box if "From" in xpath else to_box

        def wait_once(condition):
            if not condition(driver):
                raise movement.TimeoutException("Period inputs did not match")

        wait_class.return_value.until.side_effect = wait_once
        with self.assertRaisesRegex(movement.TimeoutException, "inputs did not match"):
            movement.set_period(driver, {"from": "2026-06-01", "to": "2026-06-30"})


class MovementNoDataClassificationTests(unittest.TestCase):
    def test_no_selectable_period_date_is_distinguished_from_other_timeouts(self):
        self.assertTrue(
            movement._is_no_selectable_period_date_timeout(
                "Message: No selectable From date found on or after 01/01/2025 within the requested period"
            )
        )
        self.assertTrue(
            movement._is_no_selectable_period_date_timeout(
                "No selectable To date found on or before 07/31/2026 within the requested period"
            )
        )
        self.assertFalse(
            movement._is_no_selectable_period_date_timeout(
                "Movement response was not captured: expected_rows=13"
            )
        )

    @patch.object(movement.time, "sleep", return_value=None)
    @patch.object(movement, "_clear_directory", return_value=None)
    @patch.object(movement, "_ensure_logged_in", return_value=True)
    @patch.object(movement, "login_to_seasearcher")
    @patch.object(movement, "initialize_driver")
    @patch.object(movement, "scraping_movement")
    def test_no_selectable_period_date_is_retried_once_before_no_data_result(
        self,
        scrape,
        initialize_driver,
        login,
        _ensure_logged_in,
        _clear_directory,
        _sleep,
    ):
        driver = MagicMock()
        initialize_driver.return_value = driver
        login.side_effect = lambda current_driver, **kwargs: current_driver
        scrape.side_effect = [
            {
                "llino": 1,
                "ok": False,
                "error": "timeout",
                "no_selectable_period_date": True,
            },
            {
                "llino": 1,
                "ok": True,
                "note": "no-data-in-requested-period",
            },
        ]

        results = movement.worker_thread(
            [1],
            config={
                "show_progress": False,
                "periodic_rest_enabled": False,
            },
        )

        self.assertTrue(results[0]["ok"])
        self.assertEqual(scrape.call_count, 2)
        self.assertTrue(
            scrape.call_args_list[1].kwargs["config"][
                "_no_selectable_period_date_retry"
            ]
        )

    @patch.object(
        movement,
        "open_movement",
        side_effect=movement.TimeoutException(
            "No selectable From date found on or after 01/01/2025 within the requested period"
        ),
    )
    def test_second_no_selectable_date_attempt_is_written_to_no_data_cache(
        self,
        _open_movement,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = movement.scraping_movement(
                MagicMock(),
                123,
                config={
                    "out_dir": temp_dir,
                    "period": {"from": "2025-01-01", "to": "2026-07-31"},
                    "status_list": "all",
                    "_no_selectable_period_date_retry": True,
                },
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["note"], "no-data-in-requested-period")
            cache_path = movement._movement_no_data_cache_path(
                {
                    "out_dir": temp_dir,
                    "period": {"from": "2025-01-01", "to": "2026-07-31"},
                    "status_list": "all",
                }
            )
            self.assertEqual(cache_path.read_text(encoding="utf-8").strip(), "00000123")


class MovementFrameMergeTests(unittest.TestCase):
    def test_exact_duplicate_rows_are_removed(self):
        page_one = pd.DataFrame(
            [
                {"LLI NO": 318345, "Place": "Tokyo", "Type": "Call"},
                {"LLI NO": 318345, "Place": "Yokohama", "Type": "Passing"},
            ]
        )
        page_two = pd.DataFrame(
            [
                {"LLI NO": 318345, "Place": "Tokyo", "Type": "Call"},
                {"LLI NO": 318345, "Place": "Chiba", "Type": "Sighting"},
            ]
        )

        merged, duplicate_rows = movement._merge_movement_frames(
            [page_one, page_two],
            expected_total=4,
        )

        self.assertEqual(duplicate_rows, 1)
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged["Place"].tolist(), ["Tokyo", "Yokohama", "Chiba"])

    def test_distinct_rows_are_preserved(self):
        frame = pd.DataFrame(
            [
                {"LLI NO": 1, "Place": "Tokyo", "Type": "Call"},
                {"LLI NO": 1, "Place": "Tokyo", "Type": "Passing"},
            ]
        )

        merged, duplicate_rows = movement._merge_movement_frames([frame], expected_total=2)

        self.assertEqual(duplicate_rows, 0)
        self.assertEqual(len(merged), 2)

    def test_source_row_count_mismatch_still_fails(self):
        frame = pd.DataFrame([{"LLI NO": 1, "Place": "Tokyo"}])

        with self.assertRaisesRegex(ValueError, "Movement row count mismatch"):
            movement._merge_movement_frames([frame], expected_total=2)


class MovementSessionRecoveryTests(unittest.TestCase):
    @staticmethod
    def _driver():
        driver = MagicMock()
        driver.current_url = "https://www.seasearcher.com/app"
        driver.title = "SeaSearcher"
        driver.find_elements.return_value = []
        return driver

    def test_login_page_is_detected_from_salesforce_signin_url(self):
        driver = self._driver()
        driver.current_url = (
            "https://lli.my.site.com/lloydslistintelligence/SignIn?startURL=example"
        )

        self.assertTrue(movement._is_seasearcher_login_page(driver))

    @patch.object(movement.time, "sleep", return_value=None)
    @patch.object(movement, "_clear_directory", return_value=None)
    @patch.object(movement, "_ensure_logged_in", return_value=True)
    @patch.object(movement, "login_to_seasearcher")
    @patch.object(movement, "initialize_driver")
    @patch.object(movement, "scraping_movement")
    def test_session_expiry_recreates_driver_and_retries_current_llino_once(
        self,
        scrape,
        initialize_driver,
        login,
        _ensure_logged_in,
        _clear_directory,
        _sleep,
    ):
        first_driver = self._driver()
        second_driver = self._driver()
        initialize_driver.side_effect = [first_driver, second_driver]
        login.side_effect = lambda driver, **kwargs: driver
        scrape.side_effect = [
            {
                "llino": 123,
                "ok": False,
                "error": "timeout",
                "session_expired": True,
            },
            {"llino": 123, "ok": True},
        ]

        results = movement.worker_thread(
            [123],
            config={
                "show_progress": False,
                "periodic_rest_enabled": False,
                "relogin_after_timeout_streak": True,
                "timeout_streak_relogin_threshold": 5,
                "retry_timeout_once_after_relogin": True,
            },
        )

        self.assertTrue(results[0]["ok"])
        self.assertTrue(results[0]["session_relogin_retry"])
        self.assertEqual(initialize_driver.call_count, 2)
        self.assertEqual(scrape.call_count, 2)

    @patch.object(movement.time, "sleep", return_value=None)
    @patch.object(movement, "_clear_directory", return_value=None)
    @patch.object(movement, "_ensure_logged_in", return_value=True)
    @patch.object(movement, "login_to_seasearcher")
    @patch.object(movement, "initialize_driver")
    @patch.object(movement, "scraping_movement")
    def test_page_load_timeouts_trigger_configured_streak_relogin(
        self,
        scrape,
        initialize_driver,
        login,
        _ensure_logged_in,
        _clear_directory,
        _sleep,
    ):
        first_driver = self._driver()
        second_driver = self._driver()
        initialize_driver.side_effect = [first_driver, second_driver]
        login.side_effect = lambda driver, **kwargs: driver

        page_timeout = {
            "ok": False,
            "error": "timeout",
            "page_load_stuck": True,
        }
        scrape.side_effect = [
            {"llino": 1, **page_timeout},
            {"llino": 1, **page_timeout},
            {"llino": 2, **page_timeout},
            {"llino": 2, **page_timeout},
            {"llino": 1, "ok": True},
            {"llino": 2, "ok": True},
        ]

        results = movement.worker_thread(
            [1, 2],
            config={
                "show_progress": False,
                "periodic_rest_enabled": False,
                "relogin_after_timeout_streak": True,
                "timeout_streak_relogin_threshold": 2,
                "retry_timeout_once_after_relogin": True,
            },
        )

        self.assertEqual([result["ok"] for result in results], [True, True])
        self.assertEqual(initialize_driver.call_count, 2)
        self.assertEqual(scrape.call_count, 6)
        self.assertEqual(
            [entry.kwargs.get("reload_current_page", False) for entry in scrape.call_args_list],
            [False, True, False, True, False, False],
        )


if __name__ == "__main__":
    unittest.main()
