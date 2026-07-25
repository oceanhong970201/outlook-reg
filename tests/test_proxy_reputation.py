"""Self-check for the proxy IP-reputation probe and datacenter preference.
Run: python tests/test_proxy_reputation.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils
from proxy_pool import ProxyProbeResult


def test_result_carries_reputation():
    r = ProxyProbeResult.from_mapping({
        "healthy": True, "latency_ms": 12.5,
        "ip_reputation": {"clean": True, "is_datacenter": False},
    })
    assert r.healthy and r.ip_reputation["clean"] is True
    r2 = ProxyProbeResult.from_mapping({"healthy": False, "failure_class": "datacenter_ip"})
    assert not r2.healthy and r2.failure_class == "datacenter_ip" and r2.ip_reputation is None


def test_probe_flags_dc_only_when_block_enabled():
    # DC + block on -> unhealthy; DC + block off -> healthy but tagged
    orig_get = utils.get_config
    orig_check = utils.check_proxy_ip_reputation
    orig_targets = utils.get_proxy_test_targets
    orig_session = utils.requests.Session

    class _Resp:
        ok = True
        status_code = 200
        def json(self): return {}
    class _Sess:
        def __init__(self, *a, **k): self.trust_env = False
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): return _Resp()

    try:
        # one fake url so the probe runs the loop (not the empty-url early return)
        utils.get_proxy_test_targets = lambda: (["http://x"], 1)
        utils.requests.Session = _Sess
        utils.get_config = lambda: {
            "proxy_ip_reputation_enabled": True,
            "proxy_ip_reputation_block_datacenter": True,
            "proxy_ip_reputation_ttl": 60,
        }
        dc = {"clean": False, "is_datacenter": True, "failure_class": "datacenter_ip"}
        utils.check_proxy_ip_reputation = lambda *a, **k: dc
        cfg = {"server": "h:1", "requests_url": "http://u:p@h:1"}
        out = utils.probe_proxy_connectivity(cfg, timeout=1)
        assert out["healthy"] is False and out["failure_class"] == "datacenter_ip"

        utils.get_config = lambda: {
            "proxy_ip_reputation_enabled": True,
            "proxy_ip_reputation_block_datacenter": False,
            "proxy_ip_reputation_ttl": 60,
        }
        dc2 = {"clean": False, "is_datacenter": True, "failure_class": None}
        utils.check_proxy_ip_reputation = lambda *a, **k: dc2
        out2 = utils.probe_proxy_connectivity(cfg, timeout=1)
        assert out2["healthy"] is True and out2["ip_reputation"]["is_datacenter"] is True
    finally:
        utils.get_config = orig_get
        utils.check_proxy_ip_reputation = orig_check
        utils.get_proxy_test_targets = orig_targets
        utils.requests.Session = orig_session


def test_select_prefers_residential():
    import time
    from proxy_pool import ManagedProxyPool, _ProxyRecord
    pool = ManagedProxyPool.__new__(ManagedProxyPool)
    pool.gateway_limit = 0
    pool.health_ttl = 999999
    pool._gateway_cooldown_until = {}
    pool._gateway_active = {}
    pool._gateway_probing = {}
    now = time.monotonic()

    def mk(dc, lat):
        r = _ProxyRecord(config={"server": "h:1"}, key=("h", "u", "p"), gateway_key="h:1")
        r.network_state = "ready"; r.is_datacenter = dc
        r.probe_latency_ms = lat
        r.last_probe_at = now
        return r

    # opt-in off (default): faster DC wins over slower residential
    pool.prefer_residential = False
    res = mk(False, 50); dc = mk(True, 10)
    pool._records = {"a": res, "b": dc}
    picked = pool._select_record(now)
    assert picked is dc

    # opt-in on + residential present: residential wins despite higher latency
    pool.prefer_residential = True
    res2 = mk(False, 50); dc2 = mk(True, 10)
    pool._records = {"a": res2, "b": dc2}
    picked = pool._select_record(now)
    assert picked is res2

    # opt-in on but NO residential ready: DC still eligible (never starved)
    pool.prefer_residential = True
    dc_only = mk(True, 10)
    pool._records = {"b": dc_only}
    picked = pool._select_record(now)
    assert picked is dc_only


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print("PASS", fn.__name__)
    print(f"{len(fns)} checks passed")
