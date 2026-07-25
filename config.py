import copy
import json
import os
import threading

_config = None
_config_lock = threading.Lock()

_DEFAULTS = {
    "choose_browser": "ruyipage",
    "email_suffix": "@outlook.com",
    "proxy_source": "file",
    "proxy_file": "proxies.txt",
    "proxy_api_url": "",
    "proxy_api_timeout": 8,
    "proxy_api_cache_ttl": 60,
    "proxy_api_retry_backoff": 1,
    "proxy_api_pool_max_size": 100,
    "proxy_api_lease_mode": True,
    "proxy_test_urls": [
        "https://outlook.live.com/mail/0/?prompt=create_account",
        "https://login.live.com",
    ],
    "proxy_test_timeout": 8,
    "proxy_health_ttl": 120,
    "proxy_failure_cooldown": 30,
    "proxy_failure_cooldown_max": 300,
    "proxy_health_cache_max_size": 1000,
    "proxy_acquire_timeout": 60,
    "proxy_application_cooldown": 60,
    "proxy_application_cooldown_max": 300,
    "proxy_application_quarantine": True,
    "proxy_probe_workers": 4,
    "proxy_pool_refresh_interval": 5,
    "proxy_startup_ready_timeout": 60,
    "proxy_gateway_max_concurrency": 3,
    "proxy_metrics_enabled": True,
    "proxy_residential_host": "",
    "proxy_residential_port": 10000,
    "proxy_residential_username": "",
    "proxy_residential_password": "",
    "proxy_residential_pool_size": 0,
    "proxy_residential_mode": "sticky",
    "proxy_gateway_risk_threshold": 3,
    "proxy_gateway_risk_window": 300,
    "bot_protection_wait": 11,
    "max_captcha_retries": 2,
    "challenge_wait_timeout": 15,
    "entry_page_retries": 1,
    "transport_retry_limit": 1,
    "form_step_timeout": 20,
    "mailbox_wait_timeout": 45,
    "concurrent_flows": 10,
    "max_tasks": 20,
    "launch_stagger": 0.15,
    "scheduler_poll_interval": 0.15,
    "status_refresh_interval": 0.2,
    "save_logs": False,
    "oauth2": {
        "enable_oauth2": False,
        "client_id": "",
        "redirect_url": "http://localhost:8000",
        "prompt": "consent",
        "authorization_timeout": 45,
        "token_timeout": 30,
        "Scopes": [
            "offline_access",
            "https://graph.microsoft.com/Mail.ReadWrite",
            "https://graph.microsoft.com/Mail.Send",
            "https://graph.microsoft.com/User.Read",
        ],
    },
    "ruyipage": {
        "browser_path": "",
        "profile_root": "Profiles",
        "headless": False,
        "xpath_picker": False,
        "action_visual": False,
        "block_images": False,
        "javascript_memory_limit_mb": 0,
        "orphan_profile_min_age_seconds": 3600,
    },
}


def _config_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _load_base_config():
    path = _config_path()
    if not os.path.exists(path):
        return copy.deepcopy(_DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return copy.deepcopy(_DEFAULTS)
        merged = copy.deepcopy(_DEFAULTS)
        for key, value in data.items():
            if key not in ("oauth2", "ruyipage"):
                merged[key] = value
        if isinstance(data.get("oauth2"), dict):
            merged_oauth = dict(_DEFAULTS["oauth2"])
            merged_oauth.update(data["oauth2"])
            merged["oauth2"] = merged_oauth
        if isinstance(data.get("ruyipage"), dict):
            merged_rp = dict(_DEFAULTS["ruyipage"])
            merged_rp.update(data["ruyipage"])
            merged["ruyipage"] = merged_rp
        return merged
    except (json.JSONDecodeError, OSError):
        return copy.deepcopy(_DEFAULTS)


def get_config():
    global _config
    if _config is None:
        with _config_lock:
            if _config is None:
                _config = _load_base_config()
    return _config


def update_config(overrides):
    global _config
    with _config_lock:
        if _config is None:
            _config = _load_base_config()
        _config.update(overrides)


def save_config():
    with _config_lock:
        if _config is None:
            return
        path = _config_path()
        temp_path = f"{path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(_config, f, ensure_ascii=False, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
