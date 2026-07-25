import os
import random
import requests
import secrets
import string
import threading
import time
import hashlib
from urllib.parse import quote

from config import get_config


def random_email():
    letter_count = random.randint(6, 8)
    digit_count = random.randint(5, 6)
    letters = "".join(random.choice(string.ascii_lowercase) for _ in range(letter_count))
    digits = "".join(random.choice(string.digits) for _ in range(digit_count))
    return letters + digits


def generate_strong_password(length=None):
    if length is None:
        length = random.randint(11, 15)

    chars = string.ascii_letters + string.digits + "!@#$%^&*"

    while True:
        password = "".join(secrets.choice(chars) for _ in range(length))

        if (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
            and any(c in "!@#$%^&*" for c in password)
        ):
            return password


def build_proxy_config(host, port, username="", password=""):
    server = f"http://{host}:{port}"
    if username or password:
        encoded_username = quote(username, safe="")
        encoded_password = quote(password, safe="")
        requests_url = f"http://{encoded_username}:{encoded_password}@{host}:{port}"
    else:
        requests_url = server

    return {
        "server": server,
        "username": username,
        "password": password,
        "requests_url": requests_url,
    }


def parse_http_proxy_line(line, proxy_source="file"):
    parts = line.strip().split(":")
    if proxy_source == "freefile":
        if len(parts) < 2:
            return None
        return build_proxy_config(parts[0], parts[1])

    if len(parts) != 4 or not all(parts):
        return None
    return build_proxy_config(parts[0], parts[1], parts[2], parts[3])


def parse_http_proxy_api_response(text):
    parts = text.strip().split(":", 1)
    if len(parts) != 2 or not all(parts):
        return None

    host, port = parts
    return build_proxy_config(host, port)


def parse_http_proxy_api_responses(text):
    proxies = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        proxy = parse_http_proxy_api_response(line)
        if proxy:
            proxies.append(proxy)
    return proxies


def load_http_proxies(file_path=None, proxy_source="file"):
    if file_path is None:
        data = get_config()
        file_path = data.get("proxy_file", "proxies.txt")

    if not os.path.isabs(file_path):
        file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), file_path)

    if not os.path.exists(file_path):
        return []

    proxies = []
    with open(file_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            proxy = parse_http_proxy_line(line, proxy_source=proxy_source)
            if proxy:
                proxies.append(proxy)
    return proxies


def load_proxies_from_api(api_url, timeout=8):
    if not api_url:
        return []

    try:
        response = requests.get(api_url, timeout=(5, timeout))
        response.raise_for_status()
        return parse_http_proxy_api_responses(response.text)
    except Exception:
        return None


def build_requests_proxy(proxy_config):
    if not proxy_config:
        return {"http": None, "https": None}

    return {
        "http": proxy_config["requests_url"],
        "https": proxy_config["requests_url"],
    }


def get_proxy_identity_key(proxy_config):
    """Return the full route identity without exposing credentials in logs."""
    if not proxy_config:
        return None
    return (
        proxy_config.get("server", ""),
        proxy_config.get("username", ""),
        proxy_config.get("password", ""),
    )


def get_proxy_gateway_key(proxy_config):
    """Return the host:port resource shared by route identities."""
    if not proxy_config:
        return None
    server = str(proxy_config.get("server", ""))
    if "://" in server:
        server = server.split("://", 1)[1]
    return server.rstrip("/")


def masked_proxy_id(proxy_config):
    """Return a stable, non-secret identifier suitable for operational logs."""
    identity = get_proxy_identity_key(proxy_config)
    if identity is None:
        return "proxy-none"
    digest = hashlib.sha256("\x1f".join(identity).encode("utf-8")).hexdigest()
    return f"proxy-{digest[:12]}"


_proxy_health_lock = threading.Lock()
_bad_proxy_keys = set()
_proxy_health_cache = {}
_proxy_assignment_lock = threading.Lock()
_proxy_refresh_lock = threading.Lock()
_proxy_assignment_index = 0
_cached_api_proxy_batch = None
_cached_api_proxy_signature = None
# ``_cached_api_proxy_checked_at`` records the most recent fetch attempt.  A
# failed request must not make an old successful batch appear fresh, so cache
# freshness is tracked independently in ``_cached_api_proxy_last_success_at``.
_cached_api_proxy_checked_at = 0.0
_cached_api_proxy_last_success_at = None
_cached_api_proxy_last_attempt_succeeded = None
_api_proxy_generation = 0
_active_proxy_keys = set()


def _get_proxy_key(proxy_config):
    return get_proxy_identity_key(proxy_config)


def _merge_proxy_batches(existing, incoming):
    merged = []
    seen = set()
    for proxy in list(existing or []) + list(incoming or []):
        try:
            key = _get_proxy_key(proxy)
        except (KeyError, TypeError):
            continue
        if key in seen:
            continue
        seen.add(key)
        merged.append(proxy)
    return merged


def _limit_api_proxy_batch(proxies, source_config):
    try:
        max_size = max(1, int(source_config.get("proxy_api_pool_max_size", 100)))
    except (TypeError, ValueError):
        max_size = 100

    if len(proxies) <= max_size:
        return proxies

    proxy_keys = {_get_proxy_key(proxy) for proxy in proxies}
    active_keys = _active_proxy_keys.intersection(proxy_keys)
    inactive = [
        proxy
        for proxy in proxies
        if _get_proxy_key(proxy) not in active_keys
    ]
    inactive_slots = max(0, max_size - len(active_keys))
    newest_inactive = inactive[-inactive_slots:] if inactive_slots else []
    keep_keys = active_keys | {_get_proxy_key(proxy) for proxy in newest_inactive}
    return [proxy for proxy in proxies if _get_proxy_key(proxy) in keep_keys]


def _proxy_health_limits():
    data = get_config()

    def as_float(value, fallback):
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return fallback

    return (
        as_float(data.get("proxy_health_ttl", 120), 120.0),
        as_float(data.get("proxy_failure_cooldown", 30), 30.0),
    )


def _proxy_health_cache_max_size():
    data = get_config()
    try:
        return max(1, int(data.get("proxy_health_cache_max_size", 1000)))
    except (TypeError, ValueError):
        return 1000


def _prune_proxy_health_cache(
    candidate_keys=None, preserve_keys=None, known_candidate_keys=None
):
    """Bound health state and discard entries evicted from an API pool.

    API lease sources can rotate indefinitely.  Keeping health records for
    every historical lease would make a long-running batch grow without
    bound, even though those proxies can no longer be selected.
    """
    candidate_keys = None if candidate_keys is None else set(candidate_keys)
    known_candidate_keys = set(known_candidate_keys or ())
    preserve_keys = set(preserve_keys or ())
    max_size = _proxy_health_cache_max_size()
    with _proxy_health_lock:
        # Re-read active assignments while holding both locks so eviction
        # cannot race a reserve operation and discard a live lease's health.
        with _proxy_assignment_lock:
            preserve_keys.update(_active_proxy_keys)
        if candidate_keys is not None:
            stale_keys = [
                key
                for key in known_candidate_keys
                if key not in candidate_keys and key not in preserve_keys
            ]
            for key in stale_keys:
                _proxy_health_cache.pop(key, None)
                _bad_proxy_keys.discard(key)

        if len(_proxy_health_cache) <= max_size:
            return

        removable = [
            (state[1], key)
            for key, state in _proxy_health_cache.items()
            if key not in preserve_keys
        ]
        removable.sort()
        for _checked_at, key in removable[: max(0, len(_proxy_health_cache) - max_size)]:
            _proxy_health_cache.pop(key, None)
            _bad_proxy_keys.discard(key)


def _get_cached_proxy_health(proxy_config):
    if not proxy_config:
        return None

    key = _get_proxy_key(proxy_config)
    with _proxy_health_lock:
        state = _proxy_health_cache.get(key)

    if state is None:
        return None

    healthy, checked_at = state
    healthy_ttl, failure_ttl = _proxy_health_limits()
    ttl = healthy_ttl if healthy else failure_ttl
    if ttl <= 0 or time.monotonic() - checked_at >= ttl:
        with _proxy_health_lock:
            if _proxy_health_cache.get(key) == state:
                _proxy_health_cache.pop(key, None)
            if not healthy:
                _bad_proxy_keys.discard(key)
        return None

    return healthy


def _record_proxy_health(proxy_config, healthy):
    if not proxy_config:
        return

    key = _get_proxy_key(proxy_config)
    with _proxy_health_lock:
        with _proxy_assignment_lock:
            active_keys = set(_active_proxy_keys)
        _proxy_health_cache[key] = (bool(healthy), time.monotonic())
        if healthy:
            _bad_proxy_keys.discard(key)
        else:
            _bad_proxy_keys.add(key)
        max_size = _proxy_health_cache_max_size()
        if len(_proxy_health_cache) > max_size:
            removable = sorted(
                (state[1], cached_key)
                for cached_key, state in _proxy_health_cache.items()
                if cached_key != key and cached_key not in active_keys
            )
            for _checked_at, cached_key in removable[: len(_proxy_health_cache) - max_size]:
                _proxy_health_cache.pop(cached_key, None)
                _bad_proxy_keys.discard(cached_key)


def format_proxy_label(proxy_config):
    if not proxy_config:
        return "none"
    gateway = get_proxy_gateway_key(proxy_config) or "unknown-gateway"
    return f"{masked_proxy_id(proxy_config)}@{gateway}"


def reserve_proxy(proxy_config):
    if not proxy_config:
        return False

    key = _get_proxy_key(proxy_config)
    with _proxy_assignment_lock:
        if key in _active_proxy_keys:
            return False
        _active_proxy_keys.add(key)
        return True


def release_proxy(proxy_config):
    if not proxy_config:
        return

    key = _get_proxy_key(proxy_config)
    with _proxy_assignment_lock:
        _active_proxy_keys.discard(key)


def _is_proxy_active(proxy_config):
    if not proxy_config:
        return False

    key = _get_proxy_key(proxy_config)
    with _proxy_assignment_lock:
        return key in _active_proxy_keys


def _get_api_proxy_signature(source_config):
    # Timeout is a fetch policy, not the identity of the proxy source.  Keeping
    # it out of the signature lets a temporary timeout change retain a stale
    # batch while the next refresh uses the new value.
    return str(source_config.get("proxy_api_url", "")).strip()


def _get_api_cache_ttl(source_config):
    try:
        return max(0.0, float(source_config.get("proxy_api_cache_ttl", 60)))
    except (TypeError, ValueError):
        return 60.0


def _get_api_retry_backoff(source_config):
    try:
        return max(0.0, float(source_config.get("proxy_api_retry_backoff", 1)))
    except (TypeError, ValueError):
        return 1.0


def _api_retry_allowed_locked(source_config, now=None):
    if _cached_api_proxy_last_attempt_succeeded is not False:
        return True
    if now is None:
        now = time.monotonic()
    return now - _cached_api_proxy_checked_at >= _get_api_retry_backoff(
        source_config
    )


def _api_cache_expired_locked(signature, cache_ttl, now=None):
    """Return whether the API batch needs a fetch.

    TTL ``0`` explicitly disables reuse.  A failed/empty fetch leaves the
    last successful timestamp untouched, so the next call can retry instead
    of extending a stale batch's lifetime.
    """
    if _cached_api_proxy_batch is None or _cached_api_proxy_signature != signature:
        return True
    if cache_ttl <= 0:
        return True
    if _cached_api_proxy_last_success_at is None:
        return True
    if now is None:
        now = time.monotonic()
    return now - _cached_api_proxy_last_success_at >= cache_ttl


def get_proxy_source_config():
    data = get_config()

    return {
        "proxy_source": data.get("proxy_source", "file"),
        "proxy_file": data.get("proxy_file", "proxies.txt"),
        "proxy_api_url": data.get("proxy_api_url", ""),
        "proxy_api_timeout": data.get("proxy_api_timeout", 8),
        "proxy_api_cache_ttl": data.get("proxy_api_cache_ttl", 60),
        "proxy_api_retry_backoff": data.get("proxy_api_retry_backoff", 1),
        "proxy_api_lease_mode": bool(data.get("proxy_api_lease_mode", True)),
        "proxy_api_pool_max_size": data.get("proxy_api_pool_max_size", 100),
        "proxy_residential_host": data.get("proxy_residential_host", ""),
        "proxy_residential_port": data.get("proxy_residential_port", 10000),
        "proxy_residential_username": data.get("proxy_residential_username", ""),
        "proxy_residential_password": data.get("proxy_residential_password", ""),
        "proxy_residential_pool_size": data.get("proxy_residential_pool_size", 0),
        "proxy_residential_mode": data.get("proxy_residential_mode", "sticky"),
        "proxy_gateway_max_concurrency": data.get("proxy_gateway_max_concurrency", 3),
        "proxy_gateway_risk_threshold": data.get("proxy_gateway_risk_threshold", 3),
        "proxy_gateway_risk_window": data.get("proxy_gateway_risk_window", 300),
    }


def build_residential_proxy_candidates(source_config):
    """Generate 711proxy residential configs.

    A residential rotating endpoint is ONE host:port:user:pass, which would
    collapse into a single pool record (one concurrent lease). To get N
    concurrently leaseable flows with per-flow IP stability, materialize N
    configs each with a synthetic ``-session-<8-digit index>`` suffix on the
    username — 711's sticky model: a fixed session holds one IP, a different
    suffix forces a new IP. Deterministic per-index suffix keeps identity keys
    stable across refreshes (random suffixes would churn the pool each refresh).
    """
    host = str(source_config.get("proxy_residential_host", "")).strip()
    port = source_config.get("proxy_residential_port", 10000)
    base_user = str(source_config.get("proxy_residential_username", ""))
    password = str(source_config.get("proxy_residential_password", ""))
    mode = str(source_config.get("proxy_residential_mode", "sticky")).lower()
    pool_size = int(source_config.get("proxy_residential_pool_size", 0) or 0)
    if pool_size <= 0:
        # ponytail: reuse gateway concurrency as the pool-size knob, no 2nd dial
        pool_size = int(source_config.get("proxy_gateway_max_concurrency", 3) or 3)
    pool_size = max(1, pool_size)
    if not host or not base_user:
        return []
    if mode == "rotating":
        # WARNING: per-request IP — captcha-solve IP != register IP within a flow,
        # which Microsoft risk heuristics flag. Sticky-per-flow is the correct mode.
        return [build_proxy_config(host, port, base_user, password)]
    candidates = []
    # 711 sticky: ``-session-<id>`` must follow the main username and PRECEDE any
    # other ``-param`` (zone/region/country...). Appending it to the tail makes 711
    # reject the whole credential (proxy_error / connection 000), so insert it
    # right after the first segment: ``USER-session-00000000-zone-custom-...``.
    head, sep, tail = base_user.partition("-")
    for index in range(pool_size):
        # ponytail: deterministic per-index suffix = stable identity across refreshes
        session_id = f"session-{index:08d}"
        if sep:
            username = f"{head}-{session_id}-{tail}"
        else:
            username = f"{head}-{session_id}"
        candidates.append(build_proxy_config(host, port, username, password))
    return candidates


def get_proxy_candidates(
    file_path=None,
    proxy_source=None,
    force_refresh=False,
    _consistency_retries=0,
):
    source_config = get_proxy_source_config()
    selected_source = proxy_source or source_config["proxy_source"]

    if selected_source == "api":
        global _api_proxy_generation
        global _cached_api_proxy_batch, _cached_api_proxy_checked_at
        global _cached_api_proxy_last_attempt_succeeded
        global _cached_api_proxy_last_success_at
        global _cached_api_proxy_signature

        signature = _get_api_proxy_signature(source_config)
        with _proxy_assignment_lock:
            observed_generation = _api_proxy_generation
            cache_ttl = _get_api_cache_ttl(source_config)
            cache_invalid = (
                _cached_api_proxy_batch is None
                or _cached_api_proxy_signature != signature
            )
            cache_expired = _api_cache_expired_locked(signature, cache_ttl)
            should_refresh = (
                (force_refresh or cache_invalid or cache_expired)
                and (cache_invalid or _api_retry_allowed_locked(source_config))
            )

        if should_refresh:
            with _proxy_refresh_lock:
                latest_source_config = get_proxy_source_config()
                latest_signature = _get_api_proxy_signature(latest_source_config)
                if latest_signature != signature:
                    source_config = latest_source_config
                    signature = latest_signature
                with _proxy_assignment_lock:
                    cache_ttl = _get_api_cache_ttl(source_config)
                    cache_invalid = (
                        _cached_api_proxy_batch is None
                        or _cached_api_proxy_signature != signature
                    )
                    cache_expired = _api_cache_expired_locked(
                        signature, cache_ttl
                    )
                    retry_allowed = _api_retry_allowed_locked(source_config)
                    # A forced refresh is coalesced with a refresh that began
                    # after this caller observed the cache generation.  This
                    # prevents a burst of workers from serially hitting the
                    # proxy API while retaining an explicit refresh for the
                    # first caller.
                    refresh_requested = (
                        cache_invalid
                        or cache_expired
                        or (
                            force_refresh
                            and _api_proxy_generation == observed_generation
                        )
                    )
                    should_refresh = refresh_requested and (
                        cache_invalid or retry_allowed
                    )
                if should_refresh:
                    # The source can be edited while a network request is in
                    # flight.  Re-check the URL before publishing the batch;
                    # otherwise a response from the old endpoint can be
                    # assigned to workers after the switch.
                    refresh_attempts = 0
                    batch = None
                    while True:
                        batch = load_proxies_from_api(
                            source_config["proxy_api_url"],
                            source_config["proxy_api_timeout"],
                        )
                        try:
                            latest_source_config = get_proxy_source_config()
                        except Exception:
                            latest_source_config = source_config
                        latest_signature = _get_api_proxy_signature(
                            latest_source_config
                        )
                        source_config = latest_source_config
                        if latest_signature == signature:
                            break
                        signature = latest_signature
                        refresh_attempts += 1
                        if refresh_attempts >= 3:
                            # The source keeps changing.  Publish an empty
                            # state for the latest identity, never the stale
                            # response that just came back.
                            batch = None
                            break
                    now = time.monotonic()
                    with _proxy_assignment_lock:
                        previous_batch = list(_cached_api_proxy_batch or [])
                        previous_api_keys = {
                            _get_proxy_key(proxy) for proxy in previous_batch
                        }
                        if batch is not None:
                            # Normalize duplicate/malformed API lines before
                            # applying the pool bound or publishing health
                            # state.
                            batch = _merge_proxy_batches([], batch)
                            same_source = _cached_api_proxy_signature == signature
                            lease_mode = bool(
                                source_config.get("proxy_api_lease_mode", True)
                            )
                            if not batch and previous_batch and same_source:
                                # An empty 200 response is usually a
                                # temporarily exhausted endpoint.  Retain a
                                # known-good pool and leave its success time
                                # untouched so the next read retries promptly.
                                _cached_api_proxy_batch = previous_batch
                            elif force_refresh and same_source and lease_mode:
                                _cached_api_proxy_batch = _merge_proxy_batches(
                                    previous_batch, batch
                                )
                            else:
                                _cached_api_proxy_batch = batch
                            if batch:
                                _cached_api_proxy_batch = _limit_api_proxy_batch(
                                    _cached_api_proxy_batch, source_config
                                )
                            _cached_api_proxy_signature = signature
                            if batch:
                                _cached_api_proxy_last_success_at = now
                                _cached_api_proxy_last_attempt_succeeded = True
                            else:
                                _cached_api_proxy_last_attempt_succeeded = False
                                if not same_source:
                                    _cached_api_proxy_last_success_at = None
                        elif _cached_api_proxy_signature != signature:
                            # Never reuse a stale batch from a different API.
                            _cached_api_proxy_batch = []
                            _cached_api_proxy_signature = signature
                            _cached_api_proxy_last_success_at = None
                            _cached_api_proxy_last_attempt_succeeded = False
                        else:
                            _cached_api_proxy_last_attempt_succeeded = False
                        _cached_api_proxy_checked_at = now
                        _api_proxy_generation += 1

                    with _proxy_assignment_lock:
                        current_keys = {
                            _get_proxy_key(proxy)
                            for proxy in (_cached_api_proxy_batch or [])
                        }
                        active_keys = set(_active_proxy_keys)
                    _prune_proxy_health_cache(
                        current_keys, active_keys, previous_api_keys
                    )

        with _proxy_assignment_lock:
            candidates = list(_cached_api_proxy_batch or [])
            active_keys = set(_active_proxy_keys)
        _prune_proxy_health_cache(preserve_keys=active_keys)

        try:
            final_source_config = get_proxy_source_config()
            final_signature = _get_api_proxy_signature(final_source_config)
        except Exception:
            final_signature = signature
        if final_signature != signature:
            if _consistency_retries >= 2:
                return []

            stale_keys = set()
            with _proxy_assignment_lock:
                if _cached_api_proxy_signature == signature:
                    stale_keys = {
                        _get_proxy_key(proxy)
                        for proxy in (_cached_api_proxy_batch or [])
                    }
                    _cached_api_proxy_batch = None
                    _cached_api_proxy_signature = None
                    _cached_api_proxy_checked_at = 0.0
                    _cached_api_proxy_last_success_at = None
                    _cached_api_proxy_last_attempt_succeeded = None
                active_keys = set(_active_proxy_keys)
            _prune_proxy_health_cache(set(), active_keys, stale_keys)
            return get_proxy_candidates(
                file_path=file_path,
                proxy_source="api",
                force_refresh=True,
                _consistency_retries=_consistency_retries + 1,
            )
        return candidates

    if selected_source == "residential":
        # Pure generation, no I/O — deterministic suffixes keep identity keys
        # stable across refresh, so force_refresh is a no-op here.
        return build_residential_proxy_candidates(source_config)

    if file_path is not None:
        return load_http_proxies(file_path, proxy_source=selected_source)

    return load_http_proxies(source_config["proxy_file"], proxy_source=selected_source)


def fetch_api_proxy_assignment(reserve=True):
    """Return an API proxy, reserving it by default.

    Pass ``reserve=False`` only for a read-only pool peek; assignment callers
    must reserve atomically to avoid handing the same proxy to two workers.
    """
    source_config = get_proxy_source_config()
    proxies = get_proxy_candidates(proxy_source="api")
    candidate_pool = [
        proxy for proxy in proxies if _get_cached_proxy_health(proxy) is not False
    ]
    random.shuffle(candidate_pool)

    for proxy in candidate_pool:
        if reserve:
            if reserve_proxy(proxy):
                return proxy
        elif not _is_proxy_active(proxy):
            return proxy

    if not source_config.get("proxy_api_lease_mode", True):
        return None

    # Lease-style APIs often return one fresh proxy per request.  Refresh once
    # when the cached batch is fully occupied so callers can continue without
    # requiring a process restart.
    refreshed = get_proxy_candidates(proxy_source="api", force_refresh=True)
    for proxy in refreshed:
        if _get_cached_proxy_health(proxy) is False:
            continue
        if reserve:
            if reserve_proxy(proxy):
                return proxy
        elif not _is_proxy_active(proxy):
            return proxy
    return None


def get_next_proxy_assignment(file_path=None, proxy_source=None):
    global _proxy_assignment_index

    source_config = get_proxy_source_config()
    selected_source = proxy_source or source_config["proxy_source"]
    if selected_source != "api":
        return None

    # Reserve while selecting so two callers cannot receive the same lease.
    proxy = fetch_api_proxy_assignment(reserve=True)
    if proxy:
        with _proxy_assignment_lock:
            _proxy_assignment_index += 1
    return proxy


def get_proxy_capacity(file_path=None, proxy_source=None):
    proxies = get_proxy_candidates(file_path, proxy_source=proxy_source)
    return len({_get_proxy_key(proxy) for proxy in proxies})


def test_proxy_connectivity(proxy_config, url, timeout):
    if not proxy_config:
        return False

    try:
        response = requests.get(
            url,
            proxies=build_requests_proxy(proxy_config),
            timeout=(timeout, timeout),
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                )
            },
        )
        return response.ok
    except Exception:
        return False


def _proxy_failure_class(error):
    """Map transport exceptions to stable metric labels."""
    if isinstance(error, requests.exceptions.ConnectTimeout):
        return "connect_timeout"
    if isinstance(error, requests.exceptions.ReadTimeout):
        return "read_timeout"
    if isinstance(error, requests.exceptions.ProxyError):
        return "proxy_error"
    if isinstance(error, requests.exceptions.SSLError):
        return "tls_error"
    if isinstance(error, requests.exceptions.ConnectionError):
        return "connection_error"
    if isinstance(error, requests.exceptions.Timeout):
        return "timeout"
    return "probe_exception"


def probe_proxy_connectivity(proxy_config, urls=None, timeout=None):
    """Probe all configured target endpoints and return detailed telemetry.

    ``test_proxy_connectivity`` remains as the boolean compatibility helper.
    The pool uses this richer result so a transport failure is not confused
    with an application-level registration outcome.
    """
    if not proxy_config:
        return {
            "healthy": False,
            "latency_ms": 0.0,
            "failure_class": "missing_proxy",
            "failed_url_index": None,
            "status_code": None,
        }

    if urls is None:
        urls, timeout = get_proxy_test_targets()
    urls = list(urls or [])
    try:
        timeout = max(0.1, float(timeout if timeout is not None else 8))
    except (TypeError, ValueError):
        timeout = 8.0

    if not urls:
        return {
            "healthy": True,
            "latency_ms": 0.0,
            "failure_class": None,
            "failed_url_index": None,
            "status_code": None,
        }

    started = time.perf_counter()
    try:
        with requests.Session() as session:
            session.trust_env = False
            for index, url in enumerate(urls):
                try:
                    response = session.get(
                        url,
                        proxies=build_requests_proxy(proxy_config),
                        timeout=(timeout, timeout),
                        allow_redirects=True,
                        headers={
                            "User-Agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/136.0.0.0 Safari/537.36"
                            )
                        },
                    )
                except Exception as error:
                    return {
                        "healthy": False,
                        "latency_ms": (time.perf_counter() - started) * 1000,
                        "failure_class": _proxy_failure_class(error),
                        "failed_url_index": index,
                        "status_code": None,
                    }

                if not response.ok:
                    return {
                        "healthy": False,
                        "latency_ms": (time.perf_counter() - started) * 1000,
                        "failure_class": f"http_{response.status_code}",
                        "failed_url_index": index,
                        "status_code": response.status_code,
                    }
    except Exception as error:
        return {
            "healthy": False,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "failure_class": _proxy_failure_class(error),
            "failed_url_index": None,
            "status_code": None,
        }

    reputation = check_proxy_ip_reputation(proxy_config, timeout=timeout)
    if reputation is not None and reputation.get("failure_class"):
        return {
            "healthy": False,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "failure_class": reputation["failure_class"],
            "failed_url_index": None,
            "status_code": None,
            "ip_reputation": reputation,
        }

    return {
        "healthy": True,
        "latency_ms": (time.perf_counter() - started) * 1000,
        "failure_class": None,
        "failed_url_index": None,
        "status_code": None,
        "ip_reputation": reputation,
    }


# ponytail: per-gateway reputation cache. ip-api.com free tier is rate-limited
# (~45/min) and the exit IP for a given sticky gateway is stable, so cache the
# verdict per gateway_key for a TTL instead of probing every route every time.
_ip_reputation_cache = {}
_ip_reputation_lock = threading.Lock()


def check_proxy_ip_reputation(proxy_config, timeout=8):
    """Return a reputation verdict for the proxy's exit IP, or None if unknown.

    Uses ip-api.com's hosting/proxy/mobile flags. A verdict of ``clean=False``
    means the exit is a datacenter/hosting ASN — exactly the class Microsoft
    risk-scores into ``application_risk``. Result is cached per gateway_key for
    ``proxy_ip_reputation_ttl`` seconds. Returns None (don't gate) on lookup
    failure so a reputation-API outage never blocks the whole pool.
    """
    data = get_config()
    if not data.get("proxy_ip_reputation_enabled", True):
        return None

    gateway_key = get_proxy_gateway_key(proxy_config)
    ttl = float(data.get("proxy_ip_reputation_ttl", 3600) or 3600)
    # ponytail: datacenter IPs are NOT an automatic fail — they still succeed
    # sometimes (the log shows ~45% success on an AWS exit). "block_datacenter"
    # gates hard only when the operator explicitly opts in; default is to tag
    # the verdict and let the pool prefer residential while keeping DC as a
    # fallback when nothing residential is ready.
    block_dc = bool(data.get("proxy_ip_reputation_block_datacenter", False))
    now = time.monotonic()
    with _ip_reputation_lock:
        cached = _ip_reputation_cache.get(gateway_key)
        if cached and now - cached[0] < ttl:
            return cached[1]

    verdict = None
    endpoints = [
        # HTTPS first: many proxy gateways block plain-HTTP CONNECT (we got a
        # 502 on http://ip-api.com through the local gateway). ipinfo HTTPS works.
        (
            "https://ipinfo.io/json",
            lambda info: {
                "country": info.get("country", ""),
                "org": info.get("org", ""),
                "as": info.get("org", ""),
                "ip": info.get("ip", ""),
                "hosting": "org" in info and any(
                    k in info.get("org", "").lower()
                    for k in ("amazon", "aws", "google", "microsoft", "azure",
                              "digitalocean", "ovh", "hetzner", "oracle",
                              "alibaba", "tencent", "linode", "vultr", "choopa")
                ),
                "proxy": False,
                "mobile": False,
            },
        ),
        (
            "http://ip-api.com/json/?fields=status,country,org,as,hosting,proxy,mobile,query",
            lambda info: {
                "country": info.get("country", ""),
                "org": info.get("org", ""),
                "as": info.get("as", ""),
                "ip": info.get("query", ""),
                "hosting": bool(info.get("hosting")),
                "proxy": bool(info.get("proxy")),
                "mobile": bool(info.get("mobile")),
            },
        ),
    ]
    for url, normalize in endpoints:
        try:
            resp = requests.get(
                url,
                proxies=build_requests_proxy(proxy_config),
                timeout=(timeout, timeout),
            )
            if not resp.ok:
                continue
            info = resp.json()
            if info.get("status") not in (None, "success"):
                continue
            norm = normalize(info)
            hosting = bool(norm.get("hosting"))
            mobile = bool(norm.get("mobile"))
            # clean = residential or mobile; datacenter/hosting ASN is the
            # class Microsoft risk-scores hardest. block_dc decides whether
            # that's fatal here or just a recorded attribute.
            is_dc = hosting and not mobile
            verdict = {
                "clean": not is_dc,
                "is_datacenter": is_dc,
                "hosting": hosting,
                "proxy": bool(norm.get("proxy")),
                "mobile": mobile,
                "country": norm.get("country", ""),
                "org": norm.get("org", ""),
                "as": norm.get("as", ""),
                "ip": norm.get("ip", ""),
                "failure_class": "datacenter_ip" if (is_dc and block_dc) else None,
            }
            break
        except Exception:
            continue

    with _ip_reputation_lock:
        _ip_reputation_cache[gateway_key] = (now, verdict)
    return verdict


def get_proxy_test_targets():
    data = get_config()

    urls = data.get("proxy_test_urls")
    if not urls:
        urls = [
            data.get("proxy_test_url", "https://outlook.live.com/mail/0/?prompt=create_account"),
            "https://login.live.com",
        ]

    return urls, data.get("proxy_test_timeout", 8)


def get_working_proxy(file_path=None, preferred_proxy=None, reserve=False, _retry_depth=0):
    proxies = get_proxy_candidates(file_path)
    if not proxies and preferred_proxy:
        proxies = [preferred_proxy]

    if preferred_proxy:
        preferred_key = _get_proxy_key(preferred_proxy)
        ordered_proxies = [preferred_proxy]
        ordered_proxies.extend(
            proxy_config
            for proxy_config in proxies
            if _get_proxy_key(proxy_config) != preferred_key
        )
        proxies = ordered_proxies

    if not proxies:
        return None

    test_urls, timeout = get_proxy_test_targets()
    if not preferred_proxy:
        random.shuffle(proxies)

    for proxy_config in proxies:
        cached_health = _get_cached_proxy_health(proxy_config)
        if cached_health is False:
            continue

        claimed = False
        if reserve:
            if not reserve_proxy(proxy_config):
                continue
            claimed = True
        elif _is_proxy_active(proxy_config):
            continue

        if cached_health is not True:
            failed_url = None
            for url in test_urls:
                if not test_proxy_connectivity(proxy_config, url, timeout):
                    failed_url = url
                    break

            if failed_url is not None:
                _record_proxy_health(proxy_config, False)
                if claimed:
                    release_proxy(proxy_config)

                proxy_label = format_proxy_label(proxy_config)
                print(
                    f"[Skip: Proxy] - 代理阶段检测失败，已冷却 {proxy_label} "
                    f"| failed_url={failed_url}"
                )
                continue

            _record_proxy_health(proxy_config, True)

        return proxy_config

    source_config = get_proxy_source_config()
    all_unavailable = bool(proxies) and all(
        _get_cached_proxy_health(proxy) is False or _is_proxy_active(proxy)
        for proxy in proxies
    )
    refresh_needed = not proxies or all_unavailable
    if (
        source_config["proxy_source"] == "api"
        and not preferred_proxy
        and refresh_needed
        and _retry_depth < 1
    ):
        refreshed_proxies = get_proxy_candidates(proxy_source="api", force_refresh=True)
        if refreshed_proxies:
            return get_working_proxy(
                file_path=file_path,
                reserve=reserve,
                _retry_depth=_retry_depth + 1,
            )

    return None


def reset_bad_proxies(clear_active=False):
    """Reset health and API cache state without stealing live leases.

    ``clear_active=True`` is reserved for isolated test/process teardown;
    normal recovery calls must leave worker reservations intact.
    """
    global _api_proxy_generation, _proxy_assignment_index
    global _cached_api_proxy_batch, _cached_api_proxy_checked_at
    global _cached_api_proxy_last_success_at
    global _cached_api_proxy_last_attempt_succeeded
    global _cached_api_proxy_signature

    with _proxy_refresh_lock:
        with _proxy_health_lock:
            _bad_proxy_keys.clear()
            _proxy_health_cache.clear()
        with _proxy_assignment_lock:
            _proxy_assignment_index = 0
            _cached_api_proxy_batch = None
            _cached_api_proxy_signature = None
            _cached_api_proxy_checked_at = 0.0
            _cached_api_proxy_last_success_at = None
            _cached_api_proxy_last_attempt_succeeded = None
            _api_proxy_generation = 0
            if clear_active:
                _active_proxy_keys.clear()


def get_browser_proxy_settings(proxy_config):
    if not proxy_config:
        return None

    return {
        "server": proxy_config["server"],
        "username": proxy_config["username"],
        "password": proxy_config["password"],
        "bypass": "localhost",
    }
