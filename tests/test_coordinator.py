import os
import unittest
from unittest import mock

import scraping_coordinator as coordinator


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.poll_count = 0

    def post(self, action, **payload):
        self.calls.append((action, payload))
        job_id = payload.get("job_id", "job")
        if action == "reserve":
            return {
                "ok": True,
                "job": {"job_id": job_id, "status": "QUEUED"},
                "current_job": {
                    "operator": "other-user",
                    "job_type": "movement",
                    "completed": 10,
                    "total": 100,
                    "eta_at": None,
                },
                "queue_position": 1,
            }
        if action == "poll":
            self.poll_count += 1
            return {
                "ok": True,
                "job": {"job_id": job_id, "status": "RUNNING"},
                "current_job": {"job_id": job_id, "status": "RUNNING"},
                "queue_position": None,
            }
        if action in {"heartbeat", "release"}:
            return {"ok": True, "job": {"job_id": job_id, "status": "RUNNING"}}
        if action == "status":
            return {"ok": True, "accounts": [], "queued_jobs": []}
        raise AssertionError(action)


class CoordinatorTests(unittest.TestCase):
    def test_job_waits_then_acquires_and_releases(self):
        transport = FakeTransport()
        client = coordinator.ScrapeCoordinatorClient(
            transport,
            coordinator.CoordinatorIdentity("tester", "host", 123),
            poll_interval_seconds=2,
            heartbeat_interval_seconds=999,
            progress_push_interval_seconds=2,
        )
        job = client.job(account_id="account_1", job_type="movement", total=10)
        with mock.patch("scraping_coordinator.time.sleep", return_value=None):
            with job:
                job.progress_callback(completed=0, total=10, stage="local_skip")
                job.progress_callback(completed=2, total=10, stage="scrape")
        actions = [action for action, _ in transport.calls]
        self.assertEqual(actions[0], "reserve")
        self.assertIn("poll", actions)
        self.assertIn("heartbeat", actions)
        self.assertEqual(actions[-1], "release")

    def test_missing_config_fails_closed(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(coordinator.CoordinatorError):
                coordinator.coordinated_job(account_id="account_1", job_type="movement")

    def test_explicit_bypass_is_available(self):
        with mock.patch.dict(os.environ, {"SCRAPE_COORDINATOR_BYPASS": "1"}, clear=True):
            job = coordinator.coordinated_job(account_id="account_1", job_type="movement")
            self.assertTrue(job.bypassed)

    def test_track_unique_function_calls_counts_unique_keys_and_restores(self):
        class Module:
            pass

        module = Module()
        calls = []

        def scrape(driver, llino):
            calls.append(llino)
            return {"llino": llino, "ok": True}

        module.scrape = scrape

        class Job:
            def __init__(self):
                self.progress = []

            def progress_callback(self, **kwargs):
                self.progress.append(kwargs)

        job = Job()
        original = module.scrape
        with coordinator.track_unique_function_calls(
            module, "scrape", job, total=4, initial_completed=1, key_arg_index=1
        ):
            module.scrape(None, "A")
            module.scrape(None, "A")
            module.scrape(None, "B")

        self.assertIs(module.scrape, original)
        completed = [entry["completed"] for entry in job.progress]
        self.assertEqual(completed, [1, 2, 3, 4])

    def test_account_inference_prefers_matching_numbered_login(self):
        env = {
            "SEASEARCHER_LOGIN_USER_1": "one@example.com",
            "SEASEARCHER_LOGIN_USER_2": "two@example.com",
            "SEASEARCHER_ACCOUNT_ID": "account_1",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                coordinator.infer_seasearcher_account_id({"login_user": "two@example.com"}),
                "account_2",
            )

    def test_account_inference_uses_explicit_env_for_generic_login(self):
        env = {"SEASEARCHER_ACCOUNT_ID": "account_2", "SEASEARCHER_LOGIN_USER": "shared@example.com"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(coordinator.infer_seasearcher_account_id({}), "account_2")


if __name__ == "__main__":
    unittest.main()
