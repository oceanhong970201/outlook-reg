"""Controlled proxy-pool readiness and registration measurements.

The default command performs only a low-volume preflight and emits a JSON
report. Registration trials require ``--execute`` and are blocked unless the
configured readiness threshold is met. Static and dynamic identity modes are
separate runs so their effects are not mixed.
"""

from __future__ import annotations

import argparse
import copy
import json
import random
import re
import secrets
import sys
import threading
import time
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from urllib.parse import urlsplit

from config import get_config, update_config
from controllers.ruyipage_controller import RuyiPageController
from main import run_concurrent_flows, status_bar, stop_event
from proxy_pool import ManagedProxyPool
from utils import (
    build_proxy_config,
    get_proxy_candidates,
    get_proxy_identity_key,
)


DEFAULT_CONCURRENCIES = (3, 5, 10)
DEFAULT_TASKS = 20
DEFAULT_REPEATS = 3
DEFAULT_READINESS_THRESHOLD = 0.90


def percentile(values, fraction):
    """Return a nearest-rank percentile without requiring a statistics package."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999999) - 1))
    return round(ordered[index], 2)


def summarize_readiness(snapshot, threshold=DEFAULT_READINESS_THRESHOLD):
    records = list(snapshot.get("records", []))
    total = int(snapshot.get("total", len(records)) or 0)
    ready = sum(1 for record in records if record.get("network_state") == "ready")
    latencies = [
        record.get("probe_latency_ms", 0)
        for record in records
        if float(record.get("probe_latency_ms", 0) or 0) > 0
    ]
    failures = Counter(record.get("failure_class") or "none" for record in records)
    probe_counts = [int(record.get("probe_count", 0) or 0) for record in records]
    return {
        "total": total,
        "ready": ready,
        "ready_rate": round(ready / total, 4) if total else 0.0,
        "threshold": float(threshold),
        "gate_passed": bool(total and ready / total >= threshold),
        "states": dict(sorted(snapshot.get("states", {}).items())),
        "failure_classes": dict(sorted(failures.items())),
        "probe_attempts": {
            "total": sum(probe_counts),
            "min_per_route": min(probe_counts) if probe_counts else 0,
            "max_per_route": max(probe_counts) if probe_counts else 0,
        },
        "probe_latency_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "max": round(max(latencies), 2) if latencies else None,
        },
    }


def _route_loader(routes):
    routes = tuple(routes)

    def load_routes(force_refresh=False):
        del force_refresh
        return list(routes)

    return load_routes


def run_preflight(routes, base_config, timeout, threshold, name="measurement"):
    """Probe a route set in the background and return masked aggregate data."""
    config = copy.deepcopy(base_config)
    config.update(
        {
            "proxy_metrics_enabled": False,
            "proxy_pool_refresh_interval": max(
                0.2, float(config.get("proxy_pool_refresh_interval", 5))
            ),
        }
    )
    pool = ManagedProxyPool(
        source_loader=_route_loader(routes),
        config=config,
        name=name,
    )
    started = time.perf_counter()
    snapshot = {"total": 0, "ready": 0, "states": {}, "records": []}
    try:
        pool.start()
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            snapshot = pool.snapshot()
            summary = summarize_readiness(snapshot, threshold=threshold)
            initial_cycle_finished = snapshot.get("total", 0) and all(
                int(record.get("probe_count", 0) or 0) >= 1
                and not record.get("probing")
                for record in snapshot.get("records", [])
            )
            if initial_cycle_finished and summary["gate_passed"]:
                break
            time.sleep(0.2)
        snapshot = pool.snapshot()
    finally:
        pool.stop()

    result = summarize_readiness(snapshot, threshold=threshold)
    result["elapsed_s"] = round(time.perf_counter() - started, 2)
    result["route_ids"] = sorted(
        record.get("proxy_id")
        for record in snapshot.get("records", [])
        if record.get("network_state") == "ready" and record.get("proxy_id")
    )
    return result


def build_trial_plan(
    concurrencies=DEFAULT_CONCURRENCIES,
    tasks=DEFAULT_TASKS,
    repeats=DEFAULT_REPEATS,
    gateway_limit=0,
    seed=None,
):
    """Create a reproducibly randomized, flat trial order."""
    rng = random.Random(seed)
    entries = [
        {"requested_concurrency": int(concurrency), "tasks": int(tasks), "repeat": repeat}
        for repeat in range(1, int(repeats) + 1)
        for concurrency in concurrencies
    ]
    rng.shuffle(entries)
    for index, entry in enumerate(entries, start=1):
        entry["trial_id"] = f"trial-{index:02d}"
        # Keep the gateway resource limit fixed while varying scheduler demand.
        # A zero value means the configured gateway is unlimited.
        entry["gateway_limit"] = int(gateway_limit)
    return entries


def _split_server(server):
    parsed = urlsplit(str(server))
    if not parsed.hostname or parsed.port is None:
        raise ValueError("proxy server must contain a host and port")
    return parsed.hostname, str(parsed.port)


def build_dynamic_routes(static_routes, count, run_id=None, username_template=None):
    """Build a separate identity set from a static route template.

    The default suffix is intentionally only a route-identity experiment. It
    does not claim that a provider maps every suffix to a distinct exit.
    """
    if not static_routes:
        return []
    run_id = run_id or time.strftime("%Y%m%d%H%M%S", time.gmtime())
    nonce = secrets.token_hex(4)
    routes = []
    for index in range(1, int(count) + 1):
        base = static_routes[(index - 1) % len(static_routes)]
        base_username = str(base.get("username", ""))
        if username_template:
            username = username_template.format(
                base=base_username,
                index=index,
                run=run_id,
                nonce=nonce,
            )
        else:
            match = re.match(r"^(.*?\.s)\d+$", base_username)
            if match:
                username = f"{match.group(1)}{run_id}{index:03d}"
            else:
                username = f"{base_username}.measurement_{run_id}_{index:03d}"
        host, port = _split_server(base["server"])
        routes.append(build_proxy_config(host, port, username, base.get("password", "")))

    keys = [get_proxy_identity_key(route) for route in routes]
    if len(set(keys)) != len(keys):
        raise ValueError("dynamic identity template produced duplicate routes")
    return routes


class _MetricSink(StringIO):
    """Capture only structured metrics; discard page/log text in memory."""

    def __init__(self):
        super().__init__()
        self.events = []
        self._partial = ""
        self._lock = threading.Lock()

    def write(self, data):
        text = str(data)
        with self._lock:
            self._partial += text
            lines = self._partial.splitlines(keepends=True)
            self._partial = ""
            if lines and not lines[-1].endswith(("\n", "\r")):
                self._partial = lines.pop()
            for line in lines:
                self._consume(line.strip())
        return len(text)

    def flush(self):
        with self._lock:
            if self._partial:
                self._consume(self._partial.strip())
                self._partial = ""

    def _consume(self, line):
        for prefix in ("[Metric: Flow] ", "[Metric: Proxy] "):
            if not line.startswith(prefix):
                continue
            try:
                payload = json.loads(line[len(prefix) :])
            except (TypeError, ValueError):
                return
            if isinstance(payload, dict):
                if prefix == "[Metric: Flow] ":
                    payload.setdefault("event", "flow_finished")
                self.events.append(payload)
            return


def _reset_scheduler_state():
    stop_event.clear()
    status_bar.total_tasks = 0
    status_bar.submitted = 0
    status_bar.active_threads = 0
    status_bar.succeeded = 0
    status_bar.failed = 0
    status_bar.oauth_succeeded = 0
    status_bar.oauth_failed = 0
    status_bar.stopping = False
    status_bar._started_at = time.monotonic()
    status_bar._finished_at = None


def _summarize_trial(events, elapsed_s):
    # Flow metrics have no event field; proxy-pool metrics do. The flow_id and
    # outcome pair is the stable discriminator between the two schemas.
    flow_events = [
        event
        for event in events
        if event.get("flow_id") and "outcome" in event and "elapsed_ms" in event
    ]
    lease_events = [event for event in events if event.get("event") == "lease_acquired"]
    outcomes = Counter(event.get("outcome") or "unknown" for event in flow_events)
    failures = Counter(event.get("failure_class") or "none" for event in flow_events)
    waits = [event.get("acquire_wait_ms", 0) for event in lease_events]
    infrastructure_failures = outcomes.get("proxy_unavailable", 0)
    return {
        "elapsed_s": round(elapsed_s, 2),
        "submitted": int(status_bar.submitted),
        "success": int(status_bar.succeeded),
        "failed": int(status_bar.failed),
        "success_rate": round(
            status_bar.succeeded / (status_bar.succeeded + status_bar.failed), 4
        )
        if status_bar.succeeded + status_bar.failed
        else 0.0,
        "throughput_per_min": round(status_bar.submitted / elapsed_s * 60, 3)
        if elapsed_s > 0
        else 0.0,
        "outcomes": dict(sorted(outcomes.items())),
        "failure_classes": dict(sorted(failures.items())),
        "valid_for_comparison": bool(
            status_bar.submitted > 0 and infrastructure_failures == 0
        ),
        "acquire_wait_ms": {
            "p50": percentile(waits, 0.50),
            "p95": percentile(waits, 0.95),
            "max": round(max(waits), 2) if waits else None,
        },
    }


def run_registration_trial(routes, base_config, trial, readiness_timeout, threshold):
    """Run one isolated trial after a fresh readiness gate."""
    trial_config = copy.deepcopy(base_config)
    trial_config.update(
        {
            "proxy_gateway_max_concurrency": trial["gateway_limit"],
            "proxy_metrics_enabled": True,
            "concurrent_flows": trial["requested_concurrency"],
            "max_tasks": trial["tasks"],
        }
    )
    oauth_config = dict(trial_config.get("oauth2", {}))
    oauth_config["enable_oauth2"] = False
    trial_config["oauth2"] = oauth_config
    preflight = run_preflight(
        routes,
        trial_config,
        timeout=readiness_timeout,
        threshold=threshold,
        name=f"{trial['trial_id']}-preflight",
    )
    result = {"trial": dict(trial), "preflight": preflight, "status": "skipped"}
    if not preflight["gate_passed"]:
        result["reason"] = "readiness_gate_failed"
        return result

    original_config = copy.deepcopy(get_config())
    update_config(trial_config)
    pool = ManagedProxyPool(
        source_loader=_route_loader(routes),
        config=trial_config,
        name=trial["trial_id"],
    )
    sink = _MetricSink()
    started = time.perf_counter()
    try:
        _reset_scheduler_state()
        with redirect_stdout(sink), redirect_stderr(sink):
            pool.start()
            controller = RuyiPageController(proxy_pool=pool)
            scheduler = run_concurrent_flows(
                controller,
                concurrent_flows=trial["requested_concurrency"],
                max_tasks=trial["tasks"],
            )
            controller.clean_up(type="all_browser")
        result["scheduler"] = scheduler
        result["metrics"] = _summarize_trial(
            sink.events, time.perf_counter() - started
        )
        if result["metrics"]["valid_for_comparison"]:
            result["status"] = "completed"
        else:
            result["status"] = "invalid"
            result["reason"] = "proxy_acquire_timeout"
    except Exception as error:
        result["status"] = "error"
        result["error_class"] = type(error).__name__
    finally:
        try:
            if "controller" in locals():
                with redirect_stdout(sink), redirect_stderr(sink):
                    controller.clean_up(type="all_browser")
        except Exception:
            pass
        with redirect_stdout(sink), redirect_stderr(sink):
            pool.stop()
        update_config(original_config)
    return result


def _trial_is_complete(result):
    if result.get("status") != "completed":
        return False
    metrics = result.get("metrics", {})
    if metrics.get("valid_for_comparison") is False:
        return False
    return not metrics.get("outcomes", {}).get("proxy_unavailable", 0)


def _parse_int_list(value):
    values = [int(part.strip()) for part in str(value).split(",") if part.strip()]
    if not values or any(value < 1 for value in values):
        raise argparse.ArgumentTypeError("expected positive comma-separated integers")
    return tuple(values)


def _load_routes(mode, config, identities, run_id, username_template):
    static_routes = list(
        get_proxy_candidates(
            proxy_source=config.get("proxy_source", "file"),
            force_refresh=True,
        )
    )
    if mode == "static":
        return static_routes, static_routes
    return static_routes, build_dynamic_routes(
        static_routes,
        identities,
        run_id=run_id,
        username_template=username_template,
    )


def _write_json(path, payload):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    temporary.replace(destination)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("static", "dynamic"), default="static")
    parser.add_argument("--identities", type=int, default=20)
    parser.add_argument("--identity-template", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--concurrency", type=_parse_int_list, default=DEFAULT_CONCURRENCIES)
    parser.add_argument("--tasks", type=int, default=DEFAULT_TASKS)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--seed", type=int, default=20260723)
    parser.add_argument("--readiness-threshold", type=float, default=DEFAULT_READINESS_THRESHOLD)
    parser.add_argument("--readiness-timeout", type=float, default=180.0)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="run registration trials after the gate",
    )
    parser.add_argument("--max-trials", type=int, default=1)
    parser.add_argument("--resume-report", default=None)
    parser.add_argument("--baseline-report", default=None)
    parser.add_argument("--output", default=None)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.identities < 1 or args.tasks < 1 or args.repeats < 1:
        raise SystemExit("identities, tasks and repeats must be positive")
    if args.max_trials < 1:
        raise SystemExit("max-trials must be positive")
    if not 0.0 <= args.readiness_threshold <= 1.0:
        raise SystemExit("readiness threshold must be between 0 and 1")

    config = copy.deepcopy(get_config())
    static_routes, measurement_routes = _load_routes(
        args.mode,
        config,
        args.identities,
        args.run_id,
        args.identity_template,
    )
    if not measurement_routes:
        raise SystemExit("no proxy routes were loaded")

    preflight = run_preflight(
        measurement_routes,
        config,
        timeout=args.readiness_timeout,
        threshold=args.readiness_threshold,
        name=f"{args.mode}-readiness",
    )
    configured_gateway_limit = int(
        config.get("proxy_gateway_max_concurrency", 0) or 0
    )
    plan = build_trial_plan(
        args.concurrency,
        args.tasks,
        args.repeats,
        gateway_limit=configured_gateway_limit,
        seed=args.seed,
    )
    output = args.output or args.resume_report or str(
        Path("Results")
        / f"proxy_measurement_{args.mode}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    )
    if args.resume_report:
        existing_path = Path(args.resume_report)
        if not existing_path.exists():
            raise SystemExit("resume report does not exist")
        report = json.loads(existing_path.read_text(encoding="utf-8"))
        if report.get("mode") != args.mode:
            raise SystemExit("resume report mode does not match --mode")
        plan = list(report.get("plan", []))
        if not plan:
            raise SystemExit("resume report has no trial plan")
        for trial in plan:
            trial["gateway_limit"] = configured_gateway_limit
        normalized_trials = []
        for result in report.get("trials", []):
            trial = result.get("trial", {})
            if trial.get("gateway_limit") == configured_gateway_limit:
                normalized_trials.append(result)
        report["trials"] = normalized_trials
        history = list(report.get("preflight_history", []))
        if not history and report.get("preflight"):
            history.append(report["preflight"])
        report["preflight_history"] = history
        report["preflight_history"].append(preflight)
        report["preflight"] = preflight
        report["identity_count"] = len(measurement_routes)
    else:
        report = {
            "schema_version": 1,
            "mode": args.mode,
            "seed": args.seed,
            "identity_count": len(measurement_routes),
            "preflight": preflight,
            "preflight_history": [preflight],
            "plan": plan,
            "trials_executed": False,
            "trials": [],
        }

    if args.mode == "dynamic" and args.execute:
        if not args.baseline_report:
            raise SystemExit("dynamic execution requires --baseline-report from a static run")
        baseline = json.loads(Path(args.baseline_report).read_text(encoding="utf-8"))
        if baseline.get("mode") != "static" or not baseline.get("trials_executed"):
            raise SystemExit("baseline report must be a completed static measurement")

    if args.execute:
        if not preflight["gate_passed"]:
            report["blocked_reason"] = "readiness_gate_failed"
        else:
            trial_results = []
            completed_ids = {
                result.get("trial", {}).get("trial_id")
                for result in report.get("trials", [])
                if _trial_is_complete(result)
            }
            pending = [
                trial for trial in plan if trial.get("trial_id") not in completed_ids
            ]
            dynamic_run_base = args.run_id or time.strftime(
                "%Y%m%d%H%M%S", time.gmtime()
            )
            for trial_index, trial in enumerate(pending[: args.max_trials], start=1):
                routes = measurement_routes
                if args.mode == "dynamic":
                    routes = build_dynamic_routes(
                        static_routes,
                        args.identities,
                        run_id=f"{dynamic_run_base}{trial_index:02d}",
                        username_template=args.identity_template,
                    )
                trial_results.append(
                    run_registration_trial(
                        routes,
                        config,
                        trial,
                        readiness_timeout=args.readiness_timeout,
                        threshold=args.readiness_threshold,
                    )
                )
            report["trials"] = list(report.get("trials", [])) + trial_results
            completed_ids = {
                result.get("trial", {}).get("trial_id")
                for result in report["trials"]
                if _trial_is_complete(result)
            }
            report["pending_trials"] = [
                trial.get("trial_id")
                for trial in plan
                if trial.get("trial_id") not in completed_ids
            ]
            report["completed_trials"] = len(completed_ids)
            report["trials_executed"] = not report["pending_trials"]
            if any(result.get("status") != "completed" for result in trial_results):
                report["blocked_reason"] = "one_or_more_trials_incomplete"
            elif report["pending_trials"]:
                report["blocked_reason"] = "trials_remaining"
            else:
                report.pop("blocked_reason", None)
    _write_json(output, report)
    print(json.dumps({
        "mode": report["mode"],
        "output": output,
        "preflight": report["preflight"],
        "completed_trials": report.get("completed_trials", 0),
        "pending_trials": len(report.get("pending_trials", report.get("plan", []))),
        "trials_executed": report["trials_executed"],
        "blocked_reason": report.get("blocked_reason"),
    }, sort_keys=True))
    hard_failure = report.get("blocked_reason") in {
        "readiness_gate_failed",
        "one_or_more_trials_incomplete",
    }
    return 2 if args.execute and hard_failure else 0


if __name__ == "__main__":
    sys.exit(main())
