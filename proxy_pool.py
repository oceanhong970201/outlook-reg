"""Background-probed proxy pool with separate network and application state."""

from __future__ import annotations

import concurrent.futures
import json
import random
import threading
import time
from dataclasses import dataclass, field

from config import get_config
from utils import (
    get_proxy_candidates,
    get_proxy_gateway_key,
    get_proxy_identity_key,
    get_proxy_test_targets,
    masked_proxy_id,
    probe_proxy_connectivity,
)


def _float_setting(data, name, fallback, minimum=0.0):
    try:
        return max(minimum, float(data.get(name, fallback)))
    except (TypeError, ValueError):
        return max(minimum, float(fallback))


def _int_setting(data, name, fallback, minimum=0):
    try:
        return max(minimum, int(data.get(name, fallback)))
    except (TypeError, ValueError):
        return max(minimum, int(fallback))


@dataclass(frozen=True)
class ProxyProbeResult:
    healthy: bool
    latency_ms: float = 0.0
    failure_class: str | None = None
    failed_url_index: int | None = None
    status_code: int | None = None
    ip_reputation: dict | None = None

    @classmethod
    def from_mapping(cls, value):
        if isinstance(value, cls):
            return value
        if isinstance(value, bool):
            return cls(healthy=value)
        value = value or {}
        return cls(
            healthy=bool(value.get("healthy", False)),
            latency_ms=float(value.get("latency_ms", 0.0) or 0.0),
            failure_class=value.get("failure_class"),
            failed_url_index=value.get("failed_url_index"),
            status_code=value.get("status_code"),
            ip_reputation=value.get("ip_reputation"),
        )


@dataclass
class _ProxyRecord:
    config: dict = field(repr=False)
    key: tuple
    gateway_key: str
    network_state: str = "unknown"
    last_probe_at: float | None = None
    next_probe_at: float = 0.0
    probe_latency_ms: float = 0.0
    probe_count: int = 0
    failure_class: str | None = None
    consecutive_failures: int = 0
    application_state: str | None = None
    application_cooldown_until: float = 0.0
    application_failures: int = 0
    application_quarantined: bool = False
    last_leased_at: float | None = None
    active: bool = False
    probing: bool = False
    is_datacenter: bool = False


class ProxyLease:
    """An idempotent route lease owned by one flow."""

    def __init__(self, pool, record, flow_id, acquire_wait_ms):
        self._pool = pool
        self._record_key = record.key
        self.config = record.config
        self.proxy_config = record.config
        self.flow_id = flow_id
        self.acquire_wait_ms = acquire_wait_ms
        self.acquired_at = time.monotonic()
        self.released = False

    @property
    def proxy_id(self):
        return masked_proxy_id(self.config)

    @property
    def gateway_key(self):
        return get_proxy_gateway_key(self.config)

    def release(self, application_outcome=None, detail=""):
        if self.released:
            return
        self.released = True
        self._pool.release(
            self,
            application_outcome=application_outcome,
            detail=detail,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.release()


class ManagedProxyPool:
    """Maintain a ready queue while workers perform registration work."""

    def __init__(
        self,
        source_loader=None,
        probe_loader=None,
        config=None,
        name="default",
    ):
        self.config = dict(config or get_config())
        self.source_loader = source_loader or self._load_source
        self.probe_loader = probe_loader or probe_proxy_connectivity
        self.name = name

        self.health_ttl = _float_setting(self.config, "proxy_health_ttl", 120.0)
        self.failure_cooldown = _float_setting(
            self.config, "proxy_failure_cooldown", 30.0
        )
        self.failure_cooldown_max = _float_setting(
            self.config, "proxy_failure_cooldown_max", 300.0
        )
        self.application_cooldown = _float_setting(
            self.config, "proxy_application_cooldown", 60.0
        )
        self.application_cooldown_max = _float_setting(
            self.config, "proxy_application_cooldown_max", 300.0
        )
        self.application_quarantine = bool(
            self.config.get("proxy_application_quarantine", False)
        )
        self.refresh_interval = _float_setting(
            self.config, "proxy_pool_refresh_interval", 5.0, minimum=0.1
        )
        self.probe_workers = _int_setting(
            self.config, "proxy_probe_workers", 4, minimum=1
        )
        self.gateway_limit = _int_setting(
            self.config, "proxy_gateway_max_concurrency", 0, minimum=0
        )
        self.gateway_risk_threshold = _int_setting(
            self.config, "proxy_gateway_risk_threshold", 3, minimum=1
        )
        self.gateway_risk_window = _float_setting(
            self.config, "proxy_gateway_risk_window", 300.0, minimum=0.0
        )
        self.probe_timeout = _float_setting(
            self.config,
            "proxy_test_timeout",
            8.0,
            minimum=0.1,
        )
        self.metrics_enabled = bool(self.config.get("proxy_metrics_enabled", True))
        # ponytail: with scarce proxy resources the operator accepts a lower
        # success rate but will NOT drop any route that might succeed. Default
        # to no residential preference so every ready route competes equally;
        # opt in only when there's a real residential surplus to prefer.
        self.prefer_residential = bool(
            self.config.get("proxy_ip_reputation_prefer_residential", False)
        )

        self._condition = threading.Condition()
        self._records = {}
        self._gateway_active = {}
        self._gateway_probing = {}
        self._gateway_application_failures = {}  # gateway_key -> int
        self._gateway_cooldown_until = {}        # gateway_key -> float (monotonic)
        self._manager_thread = None
        self._executor = None
        self._stop_event = threading.Event()
        self._started = False
        self._source_loaded_at = 0.0
        self._last_source_error = None

    def _load_source(self, force_refresh=False):
        source = self.config.get("proxy_source", "file")
        return get_proxy_candidates(
            proxy_source=source,
            force_refresh=force_refresh,
        )

    def _emit(self, event, **fields):
        if not self.metrics_enabled:
            return
        payload = {"event": event, "pool": self.name, **fields}
        try:
            print("[Metric: Proxy] " + json.dumps(payload, sort_keys=True))
        except Exception:
            pass

    def start(self):
        with self._condition:
            if self._started:
                return self
            self._started = True
            self._stop_event.clear()
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self.probe_workers,
                thread_name_prefix="proxy-probe",
            )

        try:
            self.refresh_now()
            self._manager_thread = threading.Thread(
                target=self._manager_loop,
                name="proxy-pool-manager",
                daemon=True,
            )
            self._manager_thread.start()
        except BaseException:
            # Initial source or scheduling failures must not leave the probe
            # executor alive after start() reports failure.
            self.stop()
            raise
        return self

    def stop(self):
        with self._condition:
            if not self._started:
                return
            self._started = False
            self._stop_event.set()
            self._condition.notify_all()

        if self._manager_thread and self._manager_thread is not threading.current_thread():
            self._manager_thread.join(timeout=max(1.0, self.refresh_interval * 2))
        self._manager_thread = None

        executor = self._executor
        self._executor = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_args):
        self.stop()

    def _manager_loop(self):
        next_refresh = 0.0
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= next_refresh:
                self._refresh_candidates()
                next_refresh = now + self.refresh_interval
            self._schedule_due_probes(now)
            with self._condition:
                self._condition.wait(timeout=self.refresh_interval)

    def refresh_now(self):
        self._refresh_candidates(force_refresh=True)
        self._schedule_due_probes(time.monotonic())

    def _refresh_candidates(self, force_refresh=False):
        try:
            try:
                candidates = list(self.source_loader(force_refresh=force_refresh))
            except TypeError:
                candidates = list(self.source_loader())
        except Exception as error:
            self._last_source_error = type(error).__name__
            self._emit("source_refresh_failed", failure_class=self._last_source_error)
            return

        current = {}
        for config in candidates:
            key = get_proxy_identity_key(config)
            if key is None or key in current:
                continue
            current[key] = config

        now = time.monotonic()
        with self._condition:
            for key, config in current.items():
                record = self._records.get(key)
                if record is None:
                    record = _ProxyRecord(
                        config=config,
                        key=key,
                        gateway_key=get_proxy_gateway_key(config),
                        next_probe_at=now,
                    )
                    self._records[key] = record
                else:
                    record.config = config
                    record.gateway_key = get_proxy_gateway_key(config)

            for key, record in list(self._records.items()):
                if key not in current and not record.active and not record.probing:
                    self._records.pop(key, None)

            self._source_loaded_at = now
            self._last_source_error = None
            self._condition.notify_all()

    def register_proxy(self, proxy_config):
        """Register a dynamically supplied route for a separate experiment."""
        key = get_proxy_identity_key(proxy_config)
        if key is None:
            return None
        with self._condition:
            record = self._records.get(key)
            if record is None:
                record = _ProxyRecord(
                    config=proxy_config,
                    key=key,
                    gateway_key=get_proxy_gateway_key(proxy_config),
                    next_probe_at=time.monotonic(),
                )
                self._records[key] = record
            self._condition.notify_all()
        self._schedule_due_probes(time.monotonic())
        return record

    def _schedule_due_probes(self, now):
        submissions = []
        executor = self._executor
        if executor is None:
            return
        with self._condition:
            for record in self._records.values():
                if record.active or record.probing:
                    continue
                if now < record.next_probe_at:
                    continue
                if not self._gateway_probe_available(record.gateway_key):
                    continue
                record.probing = True
                self._gateway_probing[record.gateway_key] = (
                    self._gateway_probing.get(record.gateway_key, 0) + 1
                )
                submissions.append((record.key, record.config))

        for key, config in submissions:
            try:
                future = executor.submit(self._run_probe, key, config)
                future.proxy_key = key
                future.add_done_callback(self._probe_finished)
            except Exception:
                with self._condition:
                    record = self._records.get(key)
                    if record:
                        self._finish_probe_locked(record)

    def _run_probe(self, key, config):
        urls, timeout = get_proxy_test_targets()
        try:
            result = self.probe_loader(
                config,
                urls=urls,
                timeout=timeout or self.probe_timeout,
            )
        except TypeError:
            result = self.probe_loader(config, urls, timeout or self.probe_timeout)
        return key, ProxyProbeResult.from_mapping(result)

    def _probe_finished(self, future):
        key = getattr(future, "proxy_key", None)
        try:
            key, result = future.result()
        except Exception as error:
            if key is not None:
                with self._condition:
                    record = self._records.get(key)
                    if record:
                        self._finish_probe_locked(record)
                        record.probe_count += 1
                        record.failure_class = type(error).__name__
                        record.next_probe_at = time.monotonic() + self.failure_cooldown
                        record.network_state = "cooldown"
                        self._condition.notify_all()
            self._emit("probe_crashed", failure_class=type(error).__name__)
            return

        now = time.monotonic()
        with self._condition:
            record = self._records.get(key)
            if record is None:
                return
            self._finish_probe_locked(record)
            record.probe_count += 1
            record.last_probe_at = now
            record.probe_latency_ms = max(0.0, result.latency_ms)
            record.failure_class = result.failure_class
            reputation = result.ip_reputation or {}
            record.is_datacenter = bool(reputation.get("is_datacenter"))
            if result.healthy:
                record.network_state = "ready"
                record.consecutive_failures = 0
                record.next_probe_at = now + self.health_ttl
            else:
                record.network_state = "cooldown"
                record.consecutive_failures += 1
                cooldown = min(
                    self.failure_cooldown
                    * (2 ** max(0, record.consecutive_failures - 1)),
                    self.failure_cooldown_max,
                )
                record.next_probe_at = now + cooldown + random.uniform(0, cooldown * 0.15)
            self._condition.notify_all()

        self._emit(
            "probe_finished",
            proxy_id=masked_proxy_id(record.config),
            gateway_key=record.gateway_key,
            healthy=result.healthy,
            latency_ms=round(result.latency_ms, 2),
            failure_class=result.failure_class,
            failed_url_index=result.failed_url_index,
            status_code=result.status_code,
        )

    def _gateway_available(self, gateway_key):
        return self.gateway_limit <= 0 or (
            self._gateway_active.get(gateway_key, 0)
            + self._gateway_probing.get(gateway_key, 0)
            < self.gateway_limit
        )

    def _gateway_probe_available(self, gateway_key):
        return self._gateway_available(gateway_key)

    def _finish_probe_locked(self, record):
        if not record.probing:
            return
        record.probing = False
        active = self._gateway_probing.get(record.gateway_key, 0) - 1
        if active > 0:
            self._gateway_probing[record.gateway_key] = active
        else:
            self._gateway_probing.pop(record.gateway_key, None)

    def _select_record(self, now, preferred_key=None):
        candidates = []
        for record in self._records.values():
            if preferred_key is not None and record.key != preferred_key:
                continue
            if record.active or record.probing or record.network_state != "ready":
                continue
            if record.application_quarantined:
                continue
            if record.application_cooldown_until > now:
                continue
            if self._gateway_cooldown_until.get(record.gateway_key, 0.0) > now:
                continue
            if record.last_probe_at is None or now - record.last_probe_at >= self.health_ttl:
                record.network_state = "unknown"
                record.next_probe_at = now
                continue
            if not self._gateway_available(record.gateway_key):
                continue
            candidates.append(record)

        if not candidates:
            return None

        # ponytail: only prefer residential when the operator opted in AND
        # there's a residential surplus. With scarce resources we must not
        # starve datacenter routes that might still succeed.
        pool = candidates
        if self.prefer_residential:
            residential = [r for r in candidates if not r.is_datacenter]
            if residential:
                pool = residential

        random.shuffle(pool)
        return min(
            pool,
            key=lambda item: (
                item.last_leased_at is not None,
                item.last_leased_at or 0.0,
                item.probe_latency_ms,
            ),
        )

    def acquire(self, timeout=None, flow_id=None, preferred_proxy=None):
        if not self._started:
            self.start()
        timeout = _float_setting(
            {"timeout": timeout}, "timeout", 20.0, minimum=0.0
        ) if timeout is not None else _float_setting(
            self.config, "proxy_acquire_timeout", 60.0, minimum=0.0
        )
        started = time.perf_counter()
        preferred_key = get_proxy_identity_key(preferred_proxy)
        if preferred_proxy is not None:
            self.register_proxy(preferred_proxy)
        deadline = time.monotonic() + timeout

        with self._condition:
            while True:
                now = time.monotonic()
                record = self._select_record(now, preferred_key=preferred_key)
                if record is not None:
                    record.active = True
                    record.last_leased_at = now
                    self._gateway_active[record.gateway_key] = (
                        self._gateway_active.get(record.gateway_key, 0) + 1
                    )
                    wait_ms = (time.perf_counter() - started) * 1000
                    lease = ProxyLease(self, record, flow_id, wait_ms)
                    self._emit(
                        "lease_acquired",
                        flow_id=flow_id,
                        proxy_id=lease.proxy_id,
                        gateway_key=lease.gateway_key,
                        acquire_wait_ms=round(wait_ms, 2),
                    )
                    return lease

                remaining = deadline - now
                if remaining <= 0:
                    wait_ms = (time.perf_counter() - started) * 1000
                    self._emit(
                        "lease_unavailable",
                        flow_id=flow_id,
                        acquire_wait_ms=round(wait_ms, 2),
                    )
                    return None
                self._condition.wait(timeout=min(remaining, self.refresh_interval))

    def release(self, lease, application_outcome=None, detail=""):
        with self._condition:
            record = self._records.get(lease._record_key)
            if record is None or not record.active:
                return
            record.active = False
            active = self._gateway_active.get(record.gateway_key, 0) - 1
            if active > 0:
                self._gateway_active[record.gateway_key] = active
            else:
                self._gateway_active.pop(record.gateway_key, None)
            if (
                record.last_probe_at is None
                or time.monotonic() - record.last_probe_at >= self.health_ttl
            ):
                record.network_state = "unknown"
                record.next_probe_at = time.monotonic()
            self._condition.notify_all()

        if application_outcome:
            self.report_application_outcome(
                lease.config,
                application_outcome,
                detail=detail,
                flow_id=lease.flow_id,
            )

        self._emit(
            "lease_released",
            flow_id=lease.flow_id,
            proxy_id=lease.proxy_id,
            gateway_key=lease.gateway_key,
            lease_ms=round((time.monotonic() - lease.acquired_at) * 1000, 2),
        )

    def report_application_outcome(self, proxy_config, outcome, detail="", flow_id=None):
        key = get_proxy_identity_key(proxy_config)
        if key is None:
            return
        outcome = str(outcome or "unknown")
        now = time.monotonic()
        with self._condition:
            record = self._records.get(key)
            if record is None:
                return
            record.application_state = outcome
            if outcome in {"application_risk", "rate_limited"}:
                record.application_failures += 1
                if outcome == "application_risk" and self.application_quarantine:
                    record.application_quarantined = True
                    record.application_cooldown_until = 0.0
                else:
                    cooldown = min(
                        self.application_cooldown
                        * (2 ** max(0, record.application_failures - 1)),
                        self.application_cooldown_max,
                    )
                    record.application_cooldown_until = now + cooldown
            elif outcome == "success":
                record.application_failures = 0
                record.application_quarantined = False
                record.application_cooldown_until = 0.0
            # Gateway-level risk: consecutive risk on one gateway host:port
            # downgrades the WHOLE gateway, not just the identity record —
            # swapping identity on a flagged gateway IP does not help.
            gateway_key = record.gateway_key
            if outcome in {"application_risk", "rate_limited"}:
                count = self._gateway_application_failures.get(gateway_key, 0) + 1
                self._gateway_application_failures[gateway_key] = count
                if count >= self.gateway_risk_threshold:
                    self._gateway_cooldown_until[gateway_key] = (
                        now + self.gateway_risk_window
                    )
            elif outcome == "success":
                self._gateway_application_failures.pop(gateway_key, None)
                self._gateway_cooldown_until.pop(gateway_key, None)
            self._condition.notify_all()

        self._emit(
            "application_outcome",
            flow_id=flow_id,
            proxy_id=masked_proxy_id(proxy_config),
            gateway_key=get_proxy_gateway_key(proxy_config),
            outcome=outcome,
            detail=str(detail or "")[:160],
        )

    def ready_count(self):
        with self._condition:
            return self._ready_count_locked(time.monotonic())

    def _ready_count_locked(self, now):
        return sum(
            1
            for record in self._records.values()
            if record.network_state == "ready"
            and not record.active
            and not record.probing
            and not record.application_quarantined
            and record.application_cooldown_until <= now
            and self._gateway_cooldown_until.get(record.gateway_key, 0.0) <= now
        )

    def wait_for_ready(self, minimum=1, timeout=20.0):
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while self._ready_count_locked(time.monotonic()) < minimum:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=min(remaining, self.refresh_interval))
            return True

    def snapshot(self):
        now = time.monotonic()
        with self._condition:
            states = {}
            for record in self._records.values():
                states[record.network_state] = states.get(record.network_state, 0) + 1
            records = [
                {
                    "proxy_id": masked_proxy_id(record.config),
                    "gateway_key": record.gateway_key,
                    "network_state": record.network_state,
                    "is_datacenter": record.is_datacenter,
                    "probe_latency_ms": round(record.probe_latency_ms, 2),
                    "probe_count": record.probe_count,
                    "failure_class": record.failure_class,
                    "application_state": record.application_state,
                    "application_quarantined": record.application_quarantined,
                    "application_cooldown_remaining_s": round(
                        max(0.0, record.application_cooldown_until - now),
                        3,
                    ),
                    "active": record.active,
                    "probing": record.probing,
                }
                for record in self._records.values()
            ]
            return {
                "total": len(self._records),
                "states": states,
                "ready": self._ready_count_locked(now),
                "active_routes": sum(
                    1 for record in self._records.values() if record.active
                ),
                "active_gateways": dict(self._gateway_active),
                "gateway_application_failures": dict(
                    self._gateway_application_failures
                ),
                "gateway_cooldowns": dict(self._gateway_cooldown_until),
                "source_loaded_age_s": max(0.0, now - self._source_loaded_at)
                if self._source_loaded_at
                else None,
                "last_source_error": self._last_source_error,
                "records": records,
            }
