import base64
import hashlib
import json
import secrets
import string
import time
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from config import get_config
from utils import build_requests_proxy, get_working_proxy


def _oauth_failure(stage, failure_class, attempt=None, status_code=None):
    payload = {
        "attempt": attempt,
        "failure_class": str(failure_class),
        "stage": str(stage),
        "status_code": status_code,
    }
    print("[Metric: OAuth] " + json.dumps(payload, sort_keys=True))
    # Record the stage so get_access_token can decide whether a retry makes
    # sense. Exchange-stage failures (token_exchange / token_response) mean
    # the authorization code was already captured but rejected by the token
    # endpoint — re-running the whole authorize → consent → captcha → callback
    # cycle just to get a fresh code re-triggers the captcha Microsoft already
    # risk-scored, burns 30-60s, and rarely succeeds. Only authorization-stage
    # failures (callback_timeout / callback_missing / navigation) are worth a
    # retry, because there is no code to exchange yet.
    _last_failure_stage[0] = str(stage)
    return False, False, False


# ponytail: single-slot mutable; get_access_token is not reentrant per page,
# and the retry loop is single-threaded by construction.
_last_failure_stage = [""]

# Failures past the authorization code capture: retrying the whole OAuth
# dance re-triggers captcha for nothing. Stop here.
_NON_RETRYABLE_STAGES = frozenset(
    {"token_exchange", "token_response", "token_timeout"}
)


def _exception_failure_class(error):
    name = type(error).__name__
    if name.endswith("Error"):
        name = name[:-5]
    normalized = "".join(
        ("_" + char.lower()) if char.isupper() else char
        for char in name
    ).lstrip("_")
    return f"{normalized or 'exception'}_error"


def get_proxy(proxy_config=None):
    if proxy_config is not None:
        return build_requests_proxy(proxy_config)
    return build_requests_proxy(get_working_proxy())


def generate_code_verifier(length=128):
    alphabet = string.ascii_letters + string.digits + "-._~"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_code_challenge(code_verifier):
    sha256_hash = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(sha256_hash).decode().rstrip("=")


def wait_for_any(page, locators, timeout=5):
    end_time = time.monotonic() + timeout
    while time.monotonic() < end_time:
        for locator in locators:
            try:
                element = page.ele(locator, timeout=0.2)
            except Exception:
                continue
            if element:
                return element
        try:
            page.wait(0.15)
        except Exception:
            time.sleep(0.15)
    return False


def handle_oauth2_form(
    page,
    email,
    allow_login=True,
    allow_consent=True,
    allow_account_select=True,
    password=None,
    allow_password=True,
    allow_confirmation=True,
):
    if allow_consent:
        consent_button = wait_for_any(
            page,
            [
                "css:[data-testid='appConsentPrimaryButton']",
                "css:button[data-testid='appConsentPrimaryButton']",
            ],
            timeout=0.5,
        )
        if consent_button:
            try:
                consent_button.click()
                return "consent"
            except Exception:
                return None

    if allow_password and password:
        password_input = wait_for_any(
            page,
            [
                "css:input[name='passwd']",
                "css:input[type='password']",
                "css:input[autocomplete='current-password']",
            ],
            timeout=0.5,
        )
        if password_input:
            try:
                password_input.input(password, clear=True)
                next_button = wait_for_any(
                    page,
                    ["css:#idSIButton9", "css:button[type='submit']"],
                    timeout=3,
                )
                if not next_button:
                    return None
                next_button.click()
                return "password"
            except Exception:
                return None

    if allow_login:
        login_input = wait_for_any(page, ["css:[name='loginfmt']"], timeout=0.5)
        if login_input:
            try:
                login_input.input(email, clear=True)
                next_button = wait_for_any(page, ["css:#idSIButton9"], timeout=3)
                if not next_button:
                    return None
                next_button.click()
                return "login"
            except Exception:
                return None

    if allow_account_select:
        account_tile = wait_for_any(
            page,
            [
                "css:[data-testid='accountTile']",
                "css:[data-test-id='accountTile']",
                f"xpath://*[@role='button'][contains(normalize-space(.), '{email}')]",
            ],
            timeout=0.5,
        )
        if account_tile:
            try:
                account_tile.click()
                return "account"
            except Exception:
                return None

    if allow_confirmation:
        confirmation_button = wait_for_any(
            page,
            [
                "css:#acceptButton",
                "xpath://button[contains(normalize-space(.), '是')]",
                "xpath://button[contains(normalize-space(.), 'Yes')]",
                "xpath://button[contains(normalize-space(.), '保持登录')]",
                "xpath://button[contains(normalize-space(.), 'Stay signed in')]",
                "xpath://button[contains(normalize-space(.), '继续')]",
            ],
            timeout=0.5,
        )
        if confirmation_button:
            try:
                confirmation_button.click()
                return "confirmation"
            except Exception:
                return None

    return None


def _safe_page_url(page):
    try:
        return str(page.url or "")
    except Exception:
        return ""


def _authorization_result(url, redirect_url):
    if not url:
        return None, None

    try:
        candidate = urlsplit(url)
        target = urlsplit(redirect_url)
    except (TypeError, ValueError):
        return None, None

    candidate_path = candidate.path.rstrip("/") or "/"
    target_path = target.path.rstrip("/") or "/"
    if (
        candidate.scheme.lower(),
        candidate.netloc.lower(),
        candidate_path,
    ) != (
        target.scheme.lower(),
        target.netloc.lower(),
        target_path,
    ):
        return None, None

    query = parse_qs(candidate.query)
    return query.get("code", [None])[0], query.get("error", [None])[0]


def get_access_token(
    page,
    email,
    max_retries=3,
    proxy_config=None,
    password=None,
):
    for attempt in range(1, max_retries + 1):
        _last_failure_stage[0] = ""
        try:
            result = _try_get_access_token(
                page,
                email,
                proxy_config=proxy_config,
                password=password,
            )
        except Exception as error:
            result = _oauth_failure(
                "oauth_attempt",
                _exception_failure_class(error),
                attempt=attempt,
            )
        if result[0] is not False:
            return result
        # Stop the retry loop once we are past the authorization-code capture:
        # an exchange-stage failure means the code was already obtained and the
        # token endpoint rejected it. Re-navigating to the authorize URL forces
        # a fresh consent + captcha on a session Microsoft just risk-scored,
        # which is the wasted behavior we want to avoid.
        if _last_failure_stage[0] in _NON_RETRYABLE_STAGES:
            return result
    return False, False, False


def _try_get_access_token(page, email, proxy_config=None, password=None):
    data = get_config()

    oauth_config = data["oauth2"]
    scopes = oauth_config["Scopes"]
    client_id = oauth_config["client_id"]
    redirect_url = oauth_config["redirect_url"]
    email_suffix = data["email_suffix"]

    if not client_id or not redirect_url:
        return _oauth_failure("configuration", "missing_configuration")

    try:
        authorization_timeout = max(
            5.0, float(oauth_config.get("authorization_timeout", 45))
        )
    except (TypeError, ValueError):
        authorization_timeout = 45.0

    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_url,
        "scope": " ".join(scopes),
        "response_mode": "query",
        "prompt": oauth_config.get("prompt", "consent"),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    authorize_url = (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?"
        + urlencode(params)
    )

    captured_url = None
    listener_started = False
    deadline = time.monotonic() + authorization_timeout

    try:
        try:
            page.listen.start(redirect_url)
            listener_started = True
            listening = getattr(page.listen, "listening", None)
            if listening is False:
                return _oauth_failure("callback_listener", "listener_not_started")
            page.wait(0.25)
            page.get(
                authorize_url,
                wait="interactive",
                timeout=min(30, authorization_timeout),
            )
        except Exception as error:
            return _oauth_failure(
                "authorization_navigation",
                _exception_failure_class(error),
            )

        account_email = f"{email}{email_suffix}"
        last_login_action = 0.0
        last_password_action = 0.0
        last_consent_action = 0.0
        last_account_action = 0.0
        last_confirmation_action = 0.0
        next_form_check = 0.0

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                packet = page.listen.wait(timeout=min(0.2, max(0.01, remaining)))
                packet_url = str(getattr(packet, "url", "") or "") if packet else ""
            except Exception:
                packet_url = ""
                time.sleep(0.1)

            current_url = _safe_page_url(page)
            for candidate_url in (packet_url, current_url):
                auth_code, auth_error = _authorization_result(
                    candidate_url, redirect_url
                )
                if auth_code:
                    captured_url = candidate_url
                    break
                if auth_error:
                    return _oauth_failure(
                        "authorization_callback",
                        f"authorization_{auth_error}",
                    )
            if captured_url:
                break

            now = time.monotonic()
            if now >= next_form_check:
                action = handle_oauth2_form(
                    page,
                    account_email,
                    password=password,
                    allow_login=now - last_login_action >= 5.0,
                    allow_password=now - last_password_action >= 5.0,
                    allow_consent=now - last_consent_action >= 5.0,
                    allow_account_select=now - last_account_action >= 5.0,
                    allow_confirmation=now - last_confirmation_action >= 5.0,
                )
                if action == "login":
                    last_login_action = now
                elif action == "password":
                    last_password_action = now
                elif action == "consent":
                    last_consent_action = now
                elif action == "account":
                    last_account_action = now
                elif action == "confirmation":
                    last_confirmation_action = now
                next_form_check = now + 0.5
        else:
            return _oauth_failure("authorization_callback", "callback_timeout")
    finally:
        if listener_started:
            try:
                page.listen.stop()
            except Exception:
                pass

    if not captured_url:
        return _oauth_failure("authorization_callback", "callback_missing")

    auth_code, auth_error = _authorization_result(captured_url, redirect_url)
    if auth_error or not auth_code:
        return _oauth_failure(
            "authorization_callback",
            f"authorization_{auth_error}" if auth_error else "code_missing",
        )

    try:
        token_timeout = max(
            5, float(oauth_config.get("token_timeout", 30))
        )
        response = requests.post(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "code": auth_code,
                "redirect_uri": redirect_url,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
                "scope": " ".join(scopes),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies=get_proxy(proxy_config),
            timeout=(5, token_timeout),
        )

        response.raise_for_status()
        tokens = response.json()
        refresh_token = tokens.get("refresh_token")
        if refresh_token:
            expires_in = float(tokens.get("expires_in", 0) or 0)
            return (
                refresh_token,
                tokens.get("access_token", ""),
                datetime.now().timestamp() + expires_in,
            )
    except requests.HTTPError as error:
        response = getattr(error, "response", None)
        return _oauth_failure(
            "token_exchange",
            "token_http_error",
            status_code=getattr(response, "status_code", None),
        )
    except requests.Timeout:
        return _oauth_failure("token_exchange", "token_timeout")
    except requests.RequestException as error:
        return _oauth_failure(
            "token_exchange",
            _exception_failure_class(error),
        )
    except (TypeError, ValueError, KeyError):
        return _oauth_failure("token_response", "invalid_token_response")

    return _oauth_failure("token_response", "refresh_token_missing")
