import threading
import unittest
from unittest.mock import Mock, patch

from controllers.base_controller import BaseBrowserController
from proxy_pool import ManagedProxyPool
from utils import build_proxy_config, get_proxy_identity_key


class Page:
    def __init__(self):
        self.quit_calls = 0

    def quit(self, timeout=5, force=True):
        del timeout, force
        self.quit_calls += 1


class Lease:
    def __init__(self, config):
        self.config = config
        self.release_calls = 0

    def release(self):
        self.release_calls += 1


class Pool:
    def __init__(self, lease):
        self.lease = lease
        self.acquire_kwargs = None

    def acquire(self, **kwargs):
        self.acquire_kwargs = kwargs
        return self.lease


class PoolController(BaseBrowserController):
    def __init__(self, proxy_pool):
        self.proxy_pool = proxy_pool
        self.proxy_acquire_timeout = 0.1
        self.cleanup_lock = threading.Lock()
        self.active_resources = {}
        self.orphan_profile_dirs = set()
        self.page = None
        self.launched_with = None

    def launch_browser(self, proxy_config=None):
        self.launched_with = proxy_config
        self.page = Page()
        return self.page, {"page": self.page, "profile_dir": None}

    def handle_captcha(self, page):
        del page
        return False


class ControllerProxyPoolTests(unittest.TestCase):
    @staticmethod
    def proxy():
        return build_proxy_config(
            "proxy-gateway.test",
            "9200",
            "route-a",
            "route-secret",
        )

    @staticmethod
    def pool_config():
        return {
            "proxy_health_ttl": 5,
            "proxy_failure_cooldown": 0.05,
            "proxy_failure_cooldown_max": 0.05,
            "proxy_application_cooldown": 0.2,
            "proxy_application_cooldown_max": 0.2,
            "proxy_pool_refresh_interval": 0.1,
            "proxy_probe_workers": 1,
            "proxy_gateway_max_concurrency": 1,
            "proxy_acquire_timeout": 0.2,
            "proxy_test_urls": ["https://target.invalid/health"],
            "proxy_test_timeout": 0.1,
            "proxy_metrics_enabled": False,
        }

    def test_managed_lease_receives_flow_id_and_cleanup_releases_once(self):
        proxy = self.proxy()
        lease = Lease(proxy)
        pool = Pool(lease)
        controller = PoolController(pool)

        with (
            patch("controllers.base_controller.release_proxy") as legacy_release,
            patch("builtins.print"),
        ):
            page = controller.get_thread_page(flow_id="flow-lease-001")
            controller.clean_up(page, "done_browser")
            controller.clean_up(page, "done_browser")

        self.assertIs(page, controller.page)
        self.assertIs(controller.launched_with, proxy)
        self.assertEqual(
            pool.acquire_kwargs,
            {
                "timeout": 0.1,
                "preferred_proxy": None,
                "flow_id": "flow-lease-001",
            },
        )
        self.assertEqual(page.quit_calls, 1)
        self.assertEqual(lease.release_calls, 1)
        self.assertEqual(controller.active_resources, {})
        legacy_release.assert_not_called()

    def test_application_outcome_is_tokenized_without_changing_network_state(self):
        proxy = self.proxy()

        def source(force_refresh=False):
            del force_refresh
            return [proxy]

        pool = ManagedProxyPool(
            source_loader=source,
            probe_loader=lambda *_args, **_kwargs: {
                "healthy": True,
                "latency_ms": 4.5,
            },
            config=self.pool_config(),
        )
        controller = PoolController(pool)
        lease = None
        try:
            pool.start()
            self.assertTrue(pool.wait_for_ready(timeout=1))
            lease = pool.acquire(timeout=0.1, flow_id="flow-risk-001")
            self.assertIsNotNone(lease)

            page = Page()
            controller.active_resources[id(page)] = {
                "page": page,
                "profile_dir": None,
                "proxy_config": proxy,
                "proxy_lease": lease,
            }
            reporter = Mock(wraps=pool.report_application_outcome)
            pool.report_application_outcome = reporter

            key = get_proxy_identity_key(proxy)
            with pool._condition:
                before = pool._records[key]
                before_network_state = before.network_state
                before_failure_class = before.failure_class

            controller.report_application_outcome(
                page,
                "application_risk",
                detail="application_risk",
                flow_id="flow-risk-001",
            )
            controller.report_application_outcome(
                page,
                "application_risk",
                detail="application_risk\nPAGE BODY MUST NOT LEAK",
                flow_id="flow-risk-001",
            )

            self.assertEqual(reporter.call_count, 2)
            reporter.assert_any_call(
                proxy,
                "application_risk",
                detail="failure_class=application_risk",
                flow_id="flow-risk-001",
            )
            reporter.assert_any_call(
                proxy,
                "application_risk",
                detail="outcome=application_risk",
                flow_id="flow-risk-001",
            )
            with pool._condition:
                after = pool._records[key]
                self.assertEqual(after.network_state, before_network_state)
                self.assertEqual(after.failure_class, before_failure_class)
                self.assertEqual(after.application_state, "application_risk")
                self.assertGreater(after.application_cooldown_until, 0)

            controller.clean_up(page, "done_browser")
            lease = None
        finally:
            if lease is not None:
                lease.release()
            pool.stop()


if __name__ == "__main__":
    unittest.main()
