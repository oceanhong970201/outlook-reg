import threading
import unittest
from unittest.mock import patch

import main


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        main.stop_event.clear()
        main.status_bar.submitted = 0
        main.status_bar.succeeded = 0
        main.status_bar.failed = 0
        main.status_bar.oauth_succeeded = 0
        main.status_bar.oauth_failed = 0

    def test_lease_api_initial_capacity_does_not_clamp_workers(self):
        controller = type("Controller", (), {"enable_oauth2": False})()
        barrier = threading.Barrier(3)
        state_lock = threading.Lock()
        active = 0
        peak_active = 0

        def process_flow(_controller, _assigned_proxy):
            nonlocal active, peak_active
            with state_lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                barrier.wait(timeout=2)
                return {"success": True, "oauth_success": False}
            finally:
                with state_lock:
                    active -= 1

        with (
            patch.object(
                main,
                "get_proxy_source_config",
                return_value={
                    "proxy_source": "api",
                    "proxy_api_lease_mode": True,
                },
            ),
            patch.object(main, "get_proxy_capacity", return_value=1),
            patch.object(
                main,
                "get_config",
                return_value={
                    "scheduler_poll_interval": 0.05,
                    "launch_stagger": 0,
                },
            ),
            patch.object(main, "process_single_flow", side_effect=process_flow),
            patch("builtins.print"),
        ):
            main.run_concurrent_flows(
                controller,
                concurrent_flows=3,
                max_tasks=3,
            )

        self.assertEqual(peak_active, 3)
        self.assertEqual(main.status_bar.succeeded, 3)

    def test_managed_gateway_capacity_backpressures_scheduler_workers(self):
        controller = type("Controller", (), {"enable_oauth2": False})()

        class Pool:
            gateway_limit = 2

            @staticmethod
            def snapshot():
                return {
                    "total": 5,
                    "records": [
                        {"gateway_key": "gateway.test:1"}
                        for _ in range(5)
                    ],
                }

            @staticmethod
            def wait_for_ready(*_args, **_kwargs):
                return True

            @staticmethod
            def ready_count():
                return 5

        controller.proxy_pool = Pool()
        state_lock = threading.Lock()
        active = 0
        peak_active = 0

        def process_flow(_controller, _assigned_proxy):
            nonlocal active, peak_active
            with state_lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                threading.Event().wait(0.03)
                return {"success": True, "oauth_success": False}
            finally:
                with state_lock:
                    active -= 1

        with (
            patch.object(
                main,
                "get_proxy_source_config",
                return_value={"proxy_source": "file"},
            ),
            patch.object(
                main,
                "get_config",
                return_value={
                    "scheduler_poll_interval": 0.01,
                    "launch_stagger": 0,
                    "proxy_startup_ready_timeout": 1,
                },
            ),
            patch.object(main, "process_single_flow", side_effect=process_flow),
            patch("builtins.print"),
        ):
            summary = main.run_concurrent_flows(
                controller,
                concurrent_flows=5,
                max_tasks=5,
            )

        self.assertEqual(peak_active, 2)
        self.assertEqual(summary["requested_workers"], 5)
        self.assertEqual(summary["worker_count"], 2)
        self.assertEqual(summary["gateway_capacity"], 2)


if __name__ == "__main__":
    unittest.main()
