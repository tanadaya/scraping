import unittest
import tempfile
from unittest.mock import MagicMock, patch

import pandas as pd

from tools.scrapers import movement


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
