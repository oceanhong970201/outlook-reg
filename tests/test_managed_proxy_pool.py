import concurrent.futures
import threading
import time
import unittest
from unittest.mock import patch

from proxy_pool import ManagedProxyPool
from utils import build_proxy_config, get_proxy_identity_key, masked_proxy_id


def wait_until(predicate, timeout=1.0, interval=0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class ManagedProxyPoolTests(unittest.TestCase):
    @staticmethod
    def proxy(username, port="9100", host="proxy-gateway.test"):
        return build_proxy_config(host, port, username, f"secret-{username}")

    @staticmethod
    def config(**overrides):
        config = {
            "proxy_health_ttl": 5,
            "proxy_failure_cooldown": 0.05,
            "proxy_failure_cooldown_max": 0.05,
            "proxy_application_cooldown": 0.15,
            "proxy_application_cooldown_max": 0.15,
            "proxy_pool_refresh_interval": 0.1,
            "proxy_probe_workers": 4,
            "proxy_gateway_max_concurrency": 0,
            "proxy_acquire_timeout": 0.2,
            "proxy_test_urls": ["https://target.invalid/health"],
            "proxy_test_timeout": 0.1,
            "proxy_metrics_enabled": False,
        }
        config.update(overrides)
        return config

    @staticmethod
    def source(proxies):
        def load(force_refresh=False):
            del force_refresh
            return list(proxies)

        return load

    def test_background_probe_populates_ready_queue_before_acquire(self):
        proxy = self.proxy("route-a")
        probe_threads = []

        def probe(_proxy, urls=None, timeout=None):
            del urls, timeout
            probe_threads.append(threading.current_thread().name)
            return {"healthy": True, "latency_ms": 12.5}

        pool = ManagedProxyPool(
            source_loader=self.source([proxy]),
            probe_loader=probe,
            config=self.config(),
            name="background-ready-test",
        )
        try:
            pool.start()
            self.assertTrue(pool.wait_for_ready(timeout=1))

            lease = pool.acquire(timeout=0.1, flow_id="flow-001")
            self.assertIsNotNone(lease)
            self.assertEqual(lease.flow_id, "flow-001")
            self.assertGreaterEqual(lease.acquire_wait_ms, 0)
            self.assertEqual(lease.proxy_id, masked_proxy_id(proxy))
            self.assertNotIn("route-a", lease.proxy_id)
            self.assertNotIn("secret-route-a", lease.proxy_id)
            self.assertTrue(
                all(name.startswith("proxy-probe") for name in probe_threads)
            )
            lease.release()
        finally:
            pool.stop()

    def test_same_route_is_leased_atomically(self):
        proxy = self.proxy("only-route")
        pool = ManagedProxyPool(
            source_loader=self.source([proxy]),
            probe_loader=lambda *_args, **_kwargs: {"healthy": True},
            config=self.config(),
        )
        try:
            pool.start()
            self.assertTrue(pool.wait_for_ready(timeout=1))
            barrier = threading.Barrier(3)

            def acquire(flow_id):
                barrier.wait(timeout=1)
                return pool.acquire(timeout=0.08, flow_id=flow_id)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(acquire, f"flow-{index}") for index in range(2)]
                barrier.wait(timeout=1)
                leases = [future.result(timeout=1) for future in futures]

            granted = [lease for lease in leases if lease is not None]
            self.assertEqual(len(granted), 1)
            self.assertEqual(pool.snapshot()["active_routes"], 1)

            granted[0].release()
            granted[0].release()
            self.assertTrue(wait_until(lambda: pool.ready_count() == 1))
        finally:
            pool.stop()

    def test_gateway_limit_caps_distinct_routes_on_same_host_and_port(self):
        route_a = self.proxy("route-a")
        route_b = self.proxy("route-b")
        pool = ManagedProxyPool(
            source_loader=self.source([route_a, route_b]),
            probe_loader=lambda *_args, **_kwargs: {"healthy": True},
            config=self.config(proxy_gateway_max_concurrency=1),
        )
        try:
            pool.start()
            self.assertTrue(pool.wait_for_ready(minimum=2, timeout=1))

            first = pool.acquire(timeout=0.1, preferred_proxy=route_a)
            self.assertIsNotNone(first)
            self.assertEqual(
                pool.snapshot()["active_gateways"],
                {"proxy-gateway.test:9100": 1},
            )
            self.assertIsNone(
                pool.acquire(timeout=0.04, preferred_proxy=route_b)
            )

            first.release()
            second = pool.acquire(timeout=0.1, preferred_proxy=route_b)
            self.assertIsNotNone(second)
            self.assertEqual(second.config, route_b)
            second.release()
        finally:
            pool.stop()

    def test_gateway_limit_also_caps_background_probes(self):
        routes = [self.proxy(f"route-{index}") for index in range(5)]
        state_lock = threading.Lock()
        active = 0
        peak = 0

        def probe(_proxy, urls=None, timeout=None):
            del urls, timeout
            nonlocal active, peak
            with state_lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.03)
                return {"healthy": True}
            finally:
                with state_lock:
                    active -= 1

        pool = ManagedProxyPool(
            source_loader=self.source(routes),
            probe_loader=probe,
            config=self.config(
                proxy_gateway_max_concurrency=2,
                proxy_probe_workers=5,
            ),
        )
        try:
            pool.start()
            self.assertTrue(pool.wait_for_ready(minimum=5, timeout=2))
            self.assertEqual(peak, 2)
        finally:
            pool.stop()

    def test_network_and_application_cooldowns_are_independent(self):
        proxy = self.proxy("flaky-route")
        calls = 0
        calls_lock = threading.Lock()

        def probe(_proxy, urls=None, timeout=None):
            del urls, timeout
            nonlocal calls
            with calls_lock:
                calls += 1
                attempt = calls
            if attempt == 1:
                return {
                    "healthy": False,
                    "latency_ms": 8.0,
                    "failure_class": "http_502",
                    "status_code": 502,
                }
            return {"healthy": True, "latency_ms": 4.0}

        pool = ManagedProxyPool(
            source_loader=self.source([proxy]),
            probe_loader=probe,
            config=self.config(),
        )
        try:
            pool.start()
            self.assertTrue(
                wait_until(
                    lambda: pool.snapshot()["states"].get("cooldown") == 1
                )
            )
            self.assertIsNone(pool.acquire(timeout=0.02))

            self.assertTrue(pool.wait_for_ready(timeout=1))
            lease = pool.acquire(timeout=0.1, flow_id="flow-risk")
            self.assertIsNotNone(lease)
            with calls_lock:
                probes_before_application_cooldown = calls
            lease.release(application_outcome="application_risk")

            snapshot = pool.snapshot()
            self.assertEqual(snapshot["states"], {"ready": 1})
            self.assertEqual(snapshot["ready"], 0)
            self.assertIsNone(pool.acquire(timeout=0.02))

            self.assertTrue(wait_until(lambda: pool.ready_count() == 1, timeout=1))
            with calls_lock:
                self.assertEqual(calls, probes_before_application_cooldown)
            recovered = pool.acquire(timeout=0.1)
            self.assertIsNotNone(recovered)
            recovered.release(application_outcome="success")
        finally:
            pool.stop()

    def test_application_risk_can_quarantine_route_for_process_lifetime(self):
        proxy = self.proxy("risk-route")
        pool = ManagedProxyPool(
            source_loader=self.source([proxy]),
            probe_loader=lambda *_args, **_kwargs: {"healthy": True},
            config=self.config(proxy_application_quarantine=True),
        )
        try:
            pool.start()
            self.assertTrue(pool.wait_for_ready(timeout=1))
            lease = pool.acquire(timeout=0.1)
            self.assertIsNotNone(lease)

            lease.release(application_outcome="application_risk")

            snapshot = pool.snapshot()
            self.assertEqual(snapshot["ready"], 0)
            self.assertTrue(snapshot["records"][0]["application_quarantined"])
            self.assertEqual(
                snapshot["records"][0]["application_cooldown_remaining_s"],
                0.0,
            )
            self.assertIsNone(pool.acquire(timeout=0.02))
        finally:
            pool.stop()

    def test_gateway_risk_downgrades_whole_gateway_not_just_identity(self):
        # Two identities on the SAME gateway. Risk on one must block the other
        # too — swapping identity on a flagged gateway IP does not help.
        proxy_a = self.proxy("route-a")
        proxy_b = self.proxy("route-b")  # same host:port as proxy_a
        pool = ManagedProxyPool(
            source_loader=self.source([proxy_a, proxy_b]),
            probe_loader=lambda *_args, **_kwargs: {"healthy": True},
            config=self.config(
                proxy_gateway_max_concurrency=0,
                proxy_gateway_risk_threshold=2,
                proxy_gateway_risk_window=300.0,
            ),
        )
        try:
            pool.start()
            self.assertTrue(pool.wait_for_ready(timeout=1))

            # Report risk on A twice (== threshold) -> gateway goes on cooldown.
            pool.report_application_outcome(proxy_a, "application_risk")
            pool.report_application_outcome(proxy_a, "application_risk")

            snapshot = pool.snapshot()
            self.assertTrue(snapshot["gateway_cooldowns"])
            self.assertEqual(snapshot["ready"], 0)
            # B was never reported but is gated by the gateway cooldown.
            self.assertIsNone(pool.acquire(timeout=0.02))

            # Success on A resets the gateway — both become acquirable again.
            pool.report_application_outcome(proxy_a, "success")
            self.assertFalse(pool.snapshot()["gateway_cooldowns"])
            self.assertTrue(wait_until(lambda: pool.ready_count() >= 1, timeout=1))
            recovered = pool.acquire(timeout=0.1)
            self.assertIsNotNone(recovered)
            recovered.release(application_outcome="success")
        finally:
            pool.stop()

    def test_probe_telemetry_and_failure_class_are_retained_in_state(self):
        proxy = self.proxy("telemetry-route")
        pool = ManagedProxyPool(
            source_loader=self.source([proxy]),
            probe_loader=lambda *_args, **_kwargs: {
                "healthy": False,
                "latency_ms": 37.25,
                "failure_class": "connect_timeout",
                "failed_url_index": 1,
            },
            config=self.config(
                proxy_failure_cooldown=5,
                proxy_failure_cooldown_max=5,
            ),
        )
        try:
            pool.start()
            self.assertTrue(
                wait_until(
                    lambda: pool.snapshot()["states"].get("cooldown") == 1
                )
            )
            snapshot = pool.snapshot()
            self.assertEqual(snapshot["states"], {"cooldown": 1})
            self.assertEqual(snapshot["ready"], 0)

            key = get_proxy_identity_key(proxy)
            with pool._condition:
                record = pool._records[key]
                self.assertEqual(record.network_state, "cooldown")
                self.assertEqual(record.probe_latency_ms, 37.25)
                self.assertEqual(record.failure_class, "connect_timeout")
                self.assertEqual(record.consecutive_failures, 1)
        finally:
            pool.stop()

    def test_stop_joins_manager_and_probe_threads(self):
        proxy = self.proxy("thread-route")
        threads_before = set(threading.enumerate())
        pool = ManagedProxyPool(
            source_loader=self.source([proxy]),
            probe_loader=lambda *_args, **_kwargs: {"healthy": True},
            config=self.config(proxy_probe_workers=2),
        )
        owned_threads = []
        try:
            pool.start()
            self.assertTrue(pool.wait_for_ready(timeout=1))
            owned_threads = [
                thread
                for thread in threading.enumerate()
                if thread not in threads_before
                and (
                    thread.name == "proxy-pool-manager"
                    or thread.name.startswith("proxy-probe")
                )
            ]
            self.assertTrue(
                any(thread.name == "proxy-pool-manager" for thread in owned_threads)
            )
            self.assertTrue(
                any(thread.name.startswith("proxy-probe") for thread in owned_threads)
            )
        finally:
            pool.stop()

        self.assertTrue(
            wait_until(
                lambda: all(not thread.is_alive() for thread in owned_threads),
                timeout=1,
            )
        )
        pool.stop()

    def test_start_failure_shuts_down_executor(self):
        pool = ManagedProxyPool(
            source_loader=self.source([self.proxy("start-failure")]),
            probe_loader=lambda *_args, **_kwargs: {"healthy": True},
            config=self.config(),
        )
        with (
            patch.object(pool, "refresh_now", side_effect=RuntimeError("startup failed")),
            self.assertRaises(RuntimeError),
        ):
            pool.start()

        self.assertFalse(pool._started)
        self.assertIsNone(pool._executor)
        self.assertIsNone(pool._manager_thread)


if __name__ == "__main__":
    unittest.main()
