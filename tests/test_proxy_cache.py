import concurrent.futures
import unittest
from unittest.mock import patch

import utils


class FakeClock:
    def __init__(self, value=0.0):
        self.value = value

    def monotonic(self):
        return self.value


class ProxyCacheTests(unittest.TestCase):
    def setUp(self):
        utils.reset_bad_proxies(clear_active=True)

    def tearDown(self):
        utils.reset_bad_proxies(clear_active=True)

    def test_residential_candidates_are_distinct_sticky_sessions_one_gateway(self):
        source = {
            "proxy_residential_host": "global.xxx.711proxy.com",
            "proxy_residential_port": 10000,
            "proxy_residential_username": "OutlookReg.user",
            "proxy_residential_password": "secret",
            "proxy_residential_pool_size": 3,
            "proxy_residential_mode": "sticky",
            "proxy_gateway_max_concurrency": 3,
        }
        candidates = utils.build_residential_proxy_candidates(source)
        self.assertEqual(len(candidates), 3)

        keys = {utils.get_proxy_identity_key(c) for c in candidates}
        self.assertEqual(len(keys), 3)  # distinct identity keys -> concurrent leases
        for c in candidates:
            self.assertIn("-session-", c["username"])
        # All share ONE gateway host:port.
        gateways = {utils.get_proxy_gateway_key(c) for c in candidates}
        self.assertEqual(gateways, {"global.xxx.711proxy.com:10000"})
        # Deterministic: regenerating yields identical identity keys.
        self.assertEqual(
            keys,
            {utils.get_proxy_identity_key(c) for c in utils.build_residential_proxy_candidates(source)},
        )

    def test_residential_pool_size_falls_back_to_gateway_concurrency(self):
        source = {
            "proxy_residential_host": "gw.711proxy.com",
            "proxy_residential_port": 20000,
            "proxy_residential_username": "u",
            "proxy_residential_password": "p",
            "proxy_residential_pool_size": 0,  # -> follow proxy_gateway_max_concurrency
            "proxy_residential_mode": "sticky",
            "proxy_gateway_max_concurrency": 2,
        }
        candidates = utils.build_residential_proxy_candidates(source)
        self.assertEqual(len(candidates), 2)

    def test_residential_rotating_mode_returns_single_bare_config(self):
        source = {
            "proxy_residential_host": "gw.711proxy.com",
            "proxy_residential_port": 10000,
            "proxy_residential_username": "u",
            "proxy_residential_password": "p",
            "proxy_residential_pool_size": 5,
            "proxy_residential_mode": "rotating",
            "proxy_gateway_max_concurrency": 3,
        }
        candidates = utils.build_residential_proxy_candidates(source)
        self.assertEqual(len(candidates), 1)
        self.assertNotIn("-session-", candidates[0]["username"])

    @staticmethod
    def source(**overrides):
        source = {
            "proxy_source": "api",
            "proxy_file": "unused.txt",
            "proxy_api_url": "https://proxy.example/list",
            "proxy_api_timeout": 1,
            "proxy_api_cache_ttl": 60,
            "proxy_api_retry_backoff": 1,
            "proxy_api_lease_mode": True,
            "proxy_api_pool_max_size": 100,
        }
        source.update(overrides)
        return source

    def test_zero_cache_ttl_refreshes_each_normal_read(self):
        proxy_a = utils.build_proxy_config("127.0.0.1", "8001")
        proxy_b = utils.build_proxy_config("127.0.0.1", "8002")

        with (
            patch.object(
                utils,
                "get_proxy_source_config",
                return_value=self.source(proxy_api_cache_ttl=0),
            ),
            patch.object(
                utils,
                "load_proxies_from_api",
                side_effect=[[proxy_a], [proxy_b]],
            ) as load,
        ):
            self.assertEqual(
                utils.get_proxy_candidates(proxy_source="api"), [proxy_a]
            )
            self.assertEqual(
                utils.get_proxy_candidates(proxy_source="api"), [proxy_b]
            )

        self.assertEqual(load.call_count, 2)

    def test_failed_refresh_does_not_reset_stale_age(self):
        proxy_a = utils.build_proxy_config("127.0.0.1", "8001")
        proxy_b = utils.build_proxy_config("127.0.0.1", "8002")
        clock = FakeClock()

        with (
            patch.object(
                utils,
                "get_proxy_source_config",
                return_value=self.source(proxy_api_cache_ttl=10),
            ),
            patch.object(utils.time, "monotonic", side_effect=clock.monotonic),
            patch.object(
                utils,
                "load_proxies_from_api",
                side_effect=[[proxy_a], None, [proxy_b]],
            ) as load,
        ):
            self.assertEqual(
                utils.get_proxy_candidates(proxy_source="api"), [proxy_a]
            )

            clock.value = 10
            self.assertEqual(
                utils.get_proxy_candidates(proxy_source="api"), [proxy_a]
            )

            # Coalesce callers during the short failure backoff.
            clock.value = 10.1
            self.assertEqual(
                utils.get_proxy_candidates(proxy_source="api"), [proxy_a]
            )

            # A failed attempt must not make an expired batch look fresh for
            # the full cache TTL.  Retry as soon as the short backoff expires.
            clock.value = 11.1
            self.assertEqual(
                utils.get_proxy_candidates(proxy_source="api"), [proxy_b]
            )

        self.assertEqual(load.call_count, 3)

    def test_lease_pool_is_bounded_after_repeated_refreshes(self):
        proxies = [
            utils.build_proxy_config("127.0.0.1", str(8000 + index))
            for index in range(4)
        ]

        with (
            patch.object(
                utils,
                "get_proxy_source_config",
                return_value=self.source(proxy_api_pool_max_size=2),
            ),
            patch.object(
                utils,
                "load_proxies_from_api",
                side_effect=[[proxy] for proxy in proxies],
            ),
        ):
            utils.get_proxy_candidates(proxy_source="api")
            for _ in proxies[1:]:
                utils.get_proxy_candidates(
                    proxy_source="api", force_refresh=True
                )

            candidates = utils.get_proxy_candidates(proxy_source="api")

        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            {proxy["server"] for proxy in candidates},
            {proxies[-2]["server"], proxies[-1]["server"]},
        )

    def test_empty_successful_refresh_keeps_stale_batch(self):
        proxy = utils.build_proxy_config("127.0.0.1", "8001")
        clock = FakeClock()

        with (
            patch.object(
                utils,
                "get_proxy_source_config",
                return_value=self.source(
                    proxy_api_cache_ttl=10,
                    proxy_api_lease_mode=False,
                ),
            ),
            patch.object(utils.time, "monotonic", side_effect=clock.monotonic),
            patch.object(
                utils,
                "load_proxies_from_api",
                side_effect=[[proxy], []],
            ) as load,
        ):
            self.assertEqual(
                utils.get_proxy_candidates(proxy_source="api"), [proxy]
            )
            clock.value = 10
            self.assertEqual(
                utils.get_proxy_candidates(proxy_source="api"), [proxy]
            )

        self.assertEqual(load.call_count, 2)

    def test_mixed_unavailable_pool_triggers_lease_refresh(self):
        bad_proxy = utils.build_proxy_config("127.0.0.1", "8101")
        active_proxy = utils.build_proxy_config("127.0.0.1", "8102")
        fresh_proxy = utils.build_proxy_config("127.0.0.1", "8103")
        source = self.source(proxy_api_cache_ttl=60, proxy_api_lease_mode=True)

        with (
            patch.object(utils, "get_proxy_source_config", return_value=source),
            patch.object(
                utils,
                "load_proxies_from_api",
                side_effect=[[bad_proxy, active_proxy], [fresh_proxy]],
            ) as load,
            patch.object(utils, "get_proxy_test_targets", return_value=([], 1)),
        ):
            utils.get_proxy_candidates(proxy_source="api")
            utils._record_proxy_health(bad_proxy, False)
            utils.reserve_proxy(active_proxy)
            assigned = utils.get_working_proxy(reserve=True)

        self.assertEqual(assigned, fresh_proxy)
        self.assertEqual(load.call_count, 2)
        utils.release_proxy(active_proxy)
        utils.release_proxy(assigned)

    def test_api_pruning_preserves_file_proxy_health(self):
        file_proxy = utils.build_proxy_config("127.0.0.1", "8201")
        api_proxy = utils.build_proxy_config("127.0.0.1", "8202")
        utils._record_proxy_health(file_proxy, False)

        with (
            patch.object(
                utils, "get_proxy_source_config", return_value=self.source()
            ),
            patch.object(utils, "load_proxies_from_api", return_value=[api_proxy]),
        ):
            utils.get_proxy_candidates(proxy_source="api")

        self.assertIs(utils._get_cached_proxy_health(file_proxy), False)

    def test_health_bound_keeps_active_lease_state(self):
        active_proxy = utils.build_proxy_config("127.0.0.1", "8301")
        other_proxy = utils.build_proxy_config("127.0.0.1", "8302")
        config = {"proxy_health_cache_max_size": 1}

        with patch.object(utils, "get_config", return_value=config):
            self.assertTrue(utils.reserve_proxy(active_proxy))
            utils._record_proxy_health(active_proxy, True)
            utils._record_proxy_health(other_proxy, False)

        self.assertIs(utils._get_cached_proxy_health(active_proxy), True)
        utils.release_proxy(active_proxy)

    def test_source_switch_during_fetch_does_not_publish_old_batch(self):
        proxy_a = utils.build_proxy_config("127.0.0.1", "8501")
        proxy_b = utils.build_proxy_config("127.0.0.1", "8502")
        source = self.source(proxy_api_url="https://proxy.example/a")

        def load_batch(url, _timeout):
            if url.endswith("/a"):
                source["proxy_api_url"] = "https://proxy.example/b"
                return [proxy_a]
            return [proxy_b]

        with (
            patch.object(utils, "get_proxy_source_config", return_value=source),
            patch.object(utils, "load_proxies_from_api", side_effect=load_batch),
        ):
            candidates = utils.get_proxy_candidates(proxy_source="api")

        self.assertEqual(candidates, [proxy_b])

    def test_source_switch_after_fetch_validation_retries_before_return(self):
        proxy_a = utils.build_proxy_config("127.0.0.1", "8601")
        proxy_b = utils.build_proxy_config("127.0.0.1", "8602")
        source_a = self.source(proxy_api_url="https://proxy.example/a")
        source_b = self.source(proxy_api_url="https://proxy.example/b")
        config_calls = 0

        def current_source():
            nonlocal config_calls
            config_calls += 1
            # First fetch and its in-flight validation see A.  The final
            # pre-return validation observes the switch to B.
            return source_a if config_calls <= 3 else source_b

        def load_batch(url, _timeout):
            return [proxy_a] if url.endswith("/a") else [proxy_b]

        with (
            patch.object(
                utils, "get_proxy_source_config", side_effect=current_source
            ),
            patch.object(utils, "load_proxies_from_api", side_effect=load_batch),
        ):
            candidates = utils.get_proxy_candidates(proxy_source="api")

        self.assertEqual(candidates, [proxy_b])

    def test_default_api_assignment_is_atomic(self):
        proxy = utils.build_proxy_config("127.0.0.1", "8701")
        source = self.source(proxy_api_lease_mode=False)

        with (
            patch.object(utils, "get_proxy_source_config", return_value=source),
            patch.object(utils, "get_proxy_candidates", return_value=[proxy]),
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        lambda _index: utils.fetch_api_proxy_assignment(),
                        range(2),
                    )
                )

        self.assertEqual(results.count(proxy), 1)
        self.assertEqual(results.count(None), 1)
        utils.release_proxy(proxy)

    def test_health_reset_does_not_release_live_lease(self):
        proxy = utils.build_proxy_config("127.0.0.1", "8801")

        self.assertTrue(utils.reserve_proxy(proxy))
        utils.reset_bad_proxies()
        self.assertFalse(utils.reserve_proxy(proxy))
        utils.release_proxy(proxy)


if __name__ == "__main__":
    unittest.main()
