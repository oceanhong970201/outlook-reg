import json
import threading
import unittest
from unittest.mock import patch

import measure_proxy_pool as measurement
from proxy_pool import ManagedProxyPool
from utils import build_proxy_config, get_proxy_identity_key


def proxy(index=1):
    return build_proxy_config("127.0.0.1", "2260", f"route.s{index}", "secret")


class MeasurementTests(unittest.TestCase):
    def test_trial_plan_is_randomized_but_reproducible_and_balanced(self):
        first = measurement.build_trial_plan((3, 5, 10), tasks=20, repeats=3, seed=19)
        second = measurement.build_trial_plan((3, 5, 10), tasks=20, repeats=3, seed=19)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 9)
        self.assertEqual(
            sorted(entry["requested_concurrency"] for entry in first),
            [3, 3, 3, 5, 5, 5, 10, 10, 10],
        )
        self.assertTrue(all(entry["gateway_limit"] == 0 for entry in first))

    def test_trial_plan_keeps_configured_gateway_limit_fixed(self):
        plan = measurement.build_trial_plan(
            (3, 5, 10),
            tasks=20,
            repeats=1,
            gateway_limit=3,
            seed=19,
        )

        self.assertTrue(all(entry["gateway_limit"] == 3 for entry in plan))

    def test_dynamic_routes_are_unique_without_claiming_exit_uniqueness(self):
        routes = measurement.build_dynamic_routes([proxy()], 20, run_id="test")

        self.assertEqual(len(routes), 20)
        self.assertEqual(len({get_proxy_identity_key(route) for route in routes}), 20)
        self.assertTrue(all(route["server"] == "http://127.0.0.1:2260" for route in routes))
        self.assertTrue(all(route["password"] == "secret" for route in routes))

    def test_readiness_summary_uses_route_count_and_masks_records(self):
        snapshot = {
            "total": 4,
            "states": {"ready": 3, "cooldown": 1},
            "records": [
                {
                    "proxy_id": f"proxy-{index}",
                    "network_state": "ready" if index < 3 else "cooldown",
                    "probe_latency_ms": index + 1,
                    "failure_class": None if index < 3 else "proxy_error",
                }
                for index in range(4)
            ],
        }

        result = measurement.summarize_readiness(snapshot, threshold=0.90)

        self.assertEqual(result["ready"], 3)
        self.assertEqual(result["ready_rate"], 0.75)
        self.assertFalse(result["gate_passed"])
        self.assertNotIn("records", result)

    def test_metric_sink_and_summary_include_flow_metrics(self):
        sink = measurement._MetricSink()
        sink.write(
            '[Metric: Flow] {"elapsed_ms": 10, "failure_class": null, '
            '"flow_id": "flow-1", "outcome": "success", "proxy_id": "proxy-a"}\n'
        )
        sink.write(
            '[Metric: Proxy] {"event":"lease_acquired","flow_id":"flow-1",'
            '"proxy_id":"proxy-a","acquire_wait_ms":2.5}\n'
        )
        sink.flush()
        with (
            patch.object(measurement.status_bar, "submitted", 1),
            patch.object(measurement.status_bar, "succeeded", 1),
            patch.object(measurement.status_bar, "failed", 0),
        ):
            summary = measurement._summarize_trial(sink.events, elapsed_s=2)

        self.assertEqual(summary["outcomes"], {"success": 1})
        self.assertEqual(summary["acquire_wait_ms"]["p50"], 2.5)

    def test_dynamic_execution_requires_completed_static_baseline(self):
        dynamic_route = proxy()
        preflight = {
            "total": 1,
            "ready": 1,
            "ready_rate": 1.0,
            "threshold": 0.9,
            "gate_passed": True,
            "states": {"ready": 1},
            "failure_classes": {"none": 1},
            "probe_latency_ms": {"p50": 1, "p95": 1, "max": 1},
            "elapsed_s": 0.01,
            "route_ids": ["proxy-test"],
        }
        with (
            patch.object(measurement, "get_config", return_value={"proxy_source": "file"}),
            patch.object(
                measurement,
                "_load_routes",
                return_value=([dynamic_route], [dynamic_route]),
            ),
            patch.object(measurement, "run_preflight", return_value=preflight),
        ):
            with self.assertRaises(SystemExit):
                measurement.main(["--mode", "dynamic", "--execute"])

    def test_resume_drops_trials_from_a_different_gateway_limit(self):
        report = {
            "schema_version": 1,
            "mode": "static",
            "seed": 19,
            "identity_count": 1,
            "preflight": {},
            "preflight_history": [],
            "plan": [
                {
                    "requested_concurrency": 3,
                    "tasks": 1,
                    "repeat": 1,
                    "trial_id": "trial-01",
                    "gateway_limit": 10,
                }
            ],
            "trials_executed": False,
            "trials": [
                {
                    "trial": {"trial_id": "trial-01", "gateway_limit": 10},
                    "status": "completed",
                    "metrics": {"valid_for_comparison": True},
                }
            ],
        }
        preflight = {
            "total": 1,
            "ready": 1,
            "ready_rate": 1.0,
            "threshold": 0.9,
            "gate_passed": True,
            "states": {"ready": 1},
            "failure_classes": {"none": 1},
            "probe_latency_ms": {"p50": 1, "p95": 1, "max": 1},
            "elapsed_s": 0.01,
            "route_ids": ["proxy-test"],
        }
        writes = []
        with (
            patch.object(
                measurement,
                "get_config",
                return_value={
                    "proxy_source": "file",
                    "proxy_gateway_max_concurrency": 3,
                },
            ),
            patch.object(measurement, "_load_routes", return_value=([proxy()], [proxy()])),
            patch.object(measurement, "run_preflight", return_value=preflight),
            patch.object(measurement.Path, "exists", return_value=True),
            patch.object(
                measurement.Path,
                "read_text",
                return_value=json.dumps(report),
            ),
            patch.object(
                measurement,
                "_write_json",
                side_effect=lambda _path, payload: writes.append(payload),
            ),
            patch("builtins.print"),
        ):
            result = measurement.main(
                ["--resume-report", "report.json", "--output", "out.json"]
            )

        self.assertEqual(result, 0)
        self.assertEqual(writes[0]["trials"], [])
        self.assertEqual(writes[0]["plan"][0]["gateway_limit"], 3)


class ProxyPoolStartFailureTests(unittest.TestCase):
    def test_start_failure_closes_executor_and_marks_pool_stopped(self):
        config = {
            "proxy_probe_workers": 1,
            "proxy_pool_refresh_interval": 0.01,
            "proxy_metrics_enabled": False,
        }

        def fail_loader(force_refresh=False):
            del force_refresh
            raise KeyboardInterrupt("source interrupted")

        pool = ManagedProxyPool(source_loader=fail_loader, config=config)
        with self.assertRaises(KeyboardInterrupt):
            pool.start()

        self.assertFalse(pool._started)
        self.assertIsNone(pool._executor)
        self.assertFalse(
            any(thread.name.startswith("proxy-probe") for thread in threading.enumerate())
        )


if __name__ == "__main__":
    unittest.main()
