import os
import random
import shutil
import tempfile
import threading
import time
from abc import ABC, abstractmethod

from faker import Faker
from ruyipage import Keys

from config import get_config
from flow_types import RegistrationOutcome, RegistrationResult
from utils import format_proxy_label, get_working_proxy, release_proxy


_result_write_lock = threading.Lock()


class _RegistrationFlowFailure(Exception):
    def __init__(self, outcome, detail, stage):
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail
        self.stage = stage


# ponytail: title strings that mean the browser failed to load the page
# (blank body / transport hiccup). Shared by _classify_registration_error and
# wait_for_mailbox_ready so the wait loop can reload instead of bleeding the
# whole mailbox_wait_timeout on a transient proxy drop after a successful
# registration.
_FAILED_LOAD_TITLES = (
    "problem loading page",
    "this site can't be reached",
    "this site can’t be reached",
    "page not found",
)


class BaseBrowserController(ABC):
    def __init__(self, proxy_pool=None):
        data = get_config()

        self.wait_time = data["bot_protection_wait"]
        self.max_captcha_retries = data["max_captcha_retries"]
        self.enable_oauth2 = data["oauth2"]["enable_oauth2"]
        self.email_suffix = data["email_suffix"]
        self.proxy_acquire_timeout = max(
            0.0, float(data.get("proxy_acquire_timeout", 60))
        )
        self.form_step_timeout = max(1.0, float(data.get("form_step_timeout", 20)))
        self.mailbox_wait_timeout = max(
            1.0, float(data.get("mailbox_wait_timeout", 45))
        )
        self.entry_page_retries = max(0, int(data.get("entry_page_retries", 1) or 0))
        self.action_pause_min = 0.5
        self.action_pause_max = 1.0
        self.proxy_pool = proxy_pool
        # The controller is shared by all scheduler workers.  Per-thread state
        # keeps one flow from overwriting another flow's outcome before main
        # has read it.
        self._registration_state = threading.local()

        controller_config = data.get("ruyipage", {})
        profile_root = controller_config.get("profile_root", "Profiles")
        if not os.path.isabs(profile_root):
            profile_root = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", profile_root
            )
        self.profile_root = os.path.abspath(profile_root)
        os.makedirs(self.profile_root, exist_ok=True)

        self.cleanup_lock = threading.Lock()
        self.active_resources = {}
        self.orphan_profile_dirs = set()
        try:
            self.orphan_profile_min_age = max(
                0.0,
                float(controller_config.get("orphan_profile_min_age_seconds", 3600)),
            )
        except (TypeError, ValueError):
            self.orphan_profile_min_age = 3600.0
        self._discover_orphan_profiles()
        # ponytail: sweep a previous crashed run's half-started profiles at
        # startup so they don't accumulate (we found 15 locked orphans). Only
        # dirs older than orphan_profile_min_age are touched, so a browser
        # mid-launch right now is never disturbed.
        try:
            self.sweep_stale_profiles(min_age_seconds=self.orphan_profile_min_age)
        except Exception:
            pass

        self.results_dir = os.path.abspath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Results")
        )
        os.makedirs(self.results_dir, exist_ok=True)

    @abstractmethod
    def launch_browser(self, proxy_config=None):
        pass

    @abstractmethod
    def handle_captcha(self, page):
        pass

    def _thread_registration_state(self):
        state = getattr(self, "_registration_state", None)
        if state is None:
            # Some tests and third-party controllers bypass __init__.
            state = threading.local()
            self._registration_state = state
        return state

    def _set_registration_result(
        self,
        outcome,
        detail="",
        stage="",
        started_at=None,
    ):
        if not isinstance(outcome, RegistrationOutcome):
            outcome = RegistrationOutcome(outcome)
        elapsed_ms = 0.0
        if started_at is not None:
            elapsed_ms = max(0.0, (time.monotonic() - started_at) * 1000)
        result = RegistrationResult(
            outcome=outcome,
            detail=str(detail or ""),
            stage=str(stage or ""),
            elapsed_ms=round(elapsed_ms, 3),
        )
        self._thread_registration_state().result = result
        return result.success

    def get_last_registration_result(self):
        """Return this worker thread's typed result, if a flow has completed."""
        return getattr(self._thread_registration_state(), "result", None)

    def _clear_registration_result(self):
        state = self._thread_registration_state()
        if hasattr(state, "result"):
            del state.result

    def reset_registration_result(self):
        """Clear this worker's previous result before starting another flow."""
        self._clear_registration_result()
        self._clear_application_outcome_hint()

    @property
    def last_registration_result(self):
        return self.get_last_registration_result()

    @property
    def last_registration_outcome(self):
        result = self.get_last_registration_result()
        return result.outcome if result is not None else None

    @property
    def last_registration_detail(self):
        result = self.get_last_registration_result()
        return result.detail if result is not None else ""

    @property
    def last_failure_class(self):
        result = self.get_last_registration_result()
        return result.failure_class if result is not None else None

    def registration_outcome_snapshot(self):
        """Return a serialization-ready snapshot for main and metrics sinks."""
        result = self.get_last_registration_result()
        return result.as_dict() if result is not None else None

    def _set_application_outcome_hint(self, outcome, detail):
        """Record a nested handler result without publishing the flow early."""
        if not isinstance(outcome, RegistrationOutcome):
            outcome = RegistrationOutcome(outcome)
        state = self._thread_registration_state()
        state.outcome_hint = (outcome, str(detail or ""))

    def _clear_application_outcome_hint(self):
        state = self._thread_registration_state()
        if hasattr(state, "outcome_hint"):
            del state.outcome_hint

    def _application_outcome_hint(self, fallback, detail):
        state = self._thread_registration_state()
        return getattr(state, "outcome_hint", (fallback, detail))

    def _transport_failure_class(self, error):
        """Return a stable transport label, or None for application/UI errors."""
        if isinstance(error, ConnectionRefusedError):
            return "connection_refused"
        if isinstance(error, ConnectionResetError):
            return "connection_reset"
        if isinstance(error, ConnectionAbortedError):
            return "connection_aborted"
        if isinstance(error, BrokenPipeError):
            return "connection_broken"
        if isinstance(error, ConnectionError):
            return "connection_error"

        message = f"{type(error).__name__}: {error}".lower()
        markers = (
            ("websocket 连接未建立", "browser_disconnected"),
            # ponytail: CJK disconnect strings surfaced by the driver on a
            # mid-session proxy/websocket drop; map to browser_disconnected so
            # all callers (captcha, mailbox wait, outer handler) funnel to
            # TRANSPORT_ERROR and main.py's relaunch loop fires.
            ("连接已断开", "browser_disconnected"),
            ("连接已中断", "browser_disconnected"),
            ("连接断开", "browser_disconnected"),
            ("websocket connection is not established", "browser_disconnected"),
            ("websocket is not connected", "browser_disconnected"),
            ("browser has been disconnected", "browser_disconnected"),
            ("browser disconnected", "browser_disconnected"),
            ("target closed", "browser_disconnected"),
            ("winerror 10054", "connection_reset"),
            ("forcibly closed by the remote host", "connection_reset"),
            ("proxy", "proxy_error"),
            ("name resolution", "dns_error"),
            ("dns", "dns_error"),
            ("net::err_", "browser_network_error"),
            ("connection refused", "connection_refused"),
            ("connection reset", "connection_reset"),
            ("connection aborted", "connection_aborted"),
            ("remote disconnected", "connection_error"),
            ("tls", "tls_error"),
            ("ssl", "tls_error"),
            ("certificate", "tls_error"),
            ("http 502", "http_502"),
            ("status 502", "http_502"),
            ("http 503", "http_503"),
            ("status 503", "http_503"),
            ("http 504", "http_504"),
            ("status 504", "http_504"),
            ("page load timeout", "page_load_timeout"),
            ("navigation timeout", "navigation_timeout"),
        )
        for marker, failure_class in markers:
            if marker in message:
                return failure_class
        return None

    def _classify_registration_error(
        self,
        error,
        page,
        default_stage,
        *,
        page_title=None,
    ):
        if isinstance(error, _RegistrationFlowFailure):
            return error.outcome, error.detail, error.stage

        try:
            service_unavailable = self.service_unavailable_detected(page)
        except Exception:
            service_unavailable = False
        if service_unavailable:
            return (
                RegistrationOutcome.SERVICE_UNAVAILABLE,
                f"{default_stage}: service unavailable message detected",
                default_stage,
            )

        try:
            risk_detected = self.error_text_detected(page)
        except Exception:
            risk_detected = False
        if risk_detected:
            return (
                RegistrationOutcome.APPLICATION_RISK,
                f"{default_stage}: risk message detected",
                default_stage,
            )

        normalized_title = str(page_title or "").strip().lower()
        if normalized_title in _FAILED_LOAD_TITLES:
            return (
                RegistrationOutcome.TRANSPORT_ERROR,
                f"browser_load_error: title={page_title}",
                default_stage,
            )

        transport_class = self._transport_failure_class(error)
        if transport_class:
            return (
                RegistrationOutcome.TRANSPORT_ERROR,
                f"{transport_class}: {error}",
                default_stage,
            )

        return (
            RegistrationOutcome.UI_ERROR,
            f"{type(error).__name__}: {error}",
            default_stage,
        )

    def get_thread_page(self, proxy_config=None, flow_id=None):
        proxy_lease = None
        proxy_pool = getattr(self, "proxy_pool", None)
        if proxy_pool is not None:
            proxy_lease = proxy_pool.acquire(
                timeout=self.proxy_acquire_timeout,
                preferred_proxy=proxy_config,
                flow_id=flow_id,
            )
            proxy_config = proxy_lease.config if proxy_lease is not None else None
        else:
            deadline = time.monotonic() + self.proxy_acquire_timeout
            retry_delay = 0.25
            while True:
                proxy_config = get_working_proxy(
                    preferred_proxy=proxy_config,
                    reserve=True,
                )
                if proxy_config:
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(retry_delay, max(0.0, deadline - time.monotonic())))
                retry_delay = min(retry_delay * 2, 2.0)

        if not proxy_config:
            print("[Error: Proxy Pool] - 没有可用的 HTTPS 代理。")
            return False

        print(f"[Info: Proxy] - current task using {format_proxy_label(proxy_config)}")

        try:
            page, resource = self.launch_browser(proxy_config)
        except Exception as error:
            if proxy_lease is not None:
                proxy_lease.release()
            else:
                release_proxy(proxy_config)
            print(f"[Error: Browser] - launch failed: {error}")
            return False
        if not page:
            if proxy_lease is not None:
                proxy_lease.release()
            else:
                release_proxy(proxy_config)
            return False

        if not isinstance(resource, dict):
            try:
                page.quit(timeout=5, force=True)
            except Exception:
                pass
            if proxy_lease is not None:
                proxy_lease.release()
            else:
                release_proxy(proxy_config)
            print("[Error: Browser] - launch returned an invalid resource")
            return False

        # Controllers may return auxiliary metadata without repeating the
        # page object.  Keep cleanup anchored to the object actually handed
        # to the worker so a successful launch cannot leak a browser.
        resource.setdefault("page", page)
        resource["proxy_config"] = proxy_config
        if proxy_lease is not None:
            resource["proxy_lease"] = proxy_lease
        with self.cleanup_lock:
            self.active_resources[id(page)] = resource
        return page

    def get_page_proxy_config(self, page):
        with self.cleanup_lock:
            resource = self.active_resources.get(id(page), {})
            proxy_lease = resource.get("proxy_lease")
            if proxy_lease is not None:
                return proxy_lease.config
            return resource.get("proxy_config")

    def report_application_outcome(self, page, outcome, detail="", flow_id=None):
        """Feed application state back without changing connectivity health."""
        proxy_pool = getattr(self, "proxy_pool", None)
        if proxy_pool is None or page is None:
            return
        with self.cleanup_lock:
            resource = self.active_resources.get(id(page), {})
            proxy_lease = resource.get("proxy_lease")
            proxy_config = (
                proxy_lease.config
                if proxy_lease is not None
                else resource.get("proxy_config")
            )
        if not proxy_config:
            return

        # main normally passes a stable failure class here.  Only forward a
        # compact token so page previews and free-form exception text cannot
        # leak into pool telemetry.
        outcome_label = str(outcome or "unknown").strip()
        outcome_is_safe = bool(outcome_label) and all(
            character.isalnum() or character in "_.-"
            for character in outcome_label
        )
        outcome_token = outcome_label[:80] if outcome_is_safe else "unknown"
        detail_label = str(detail or "").strip()
        token_is_safe = bool(detail_label) and all(
            character.isalnum() or character in "_.-"
            for character in detail_label
        )
        if outcome_token == RegistrationOutcome.SUCCESS.value:
            safe_detail = "outcome=success"
        elif token_is_safe:
            safe_detail = f"failure_class={detail_label[:80]}"
        else:
            safe_detail = f"outcome={outcome_token}"
        reporter = getattr(proxy_pool, "report_application_outcome", None)
        if reporter is None:
            return
        try:
            reporter(proxy_config, outcome_token, detail=safe_detail, flow_id=flow_id)
        except TypeError:
            try:
                reporter(proxy_config, outcome_token, detail=safe_detail)
            except TypeError:
                try:
                    reporter(proxy_config, outcome_token)
                except Exception as error:
                    print(f"[Warn: Proxy Pool] - application outcome report failed: {error}")
            except Exception as error:
                print(f"[Warn: Proxy Pool] - application outcome report failed: {error}")
        except Exception as error:
            print(f"[Warn: Proxy Pool] - application outcome report failed: {error}")

    def clean_up(self, page=None, type="all_browser"):
        if type == "done_browser" and page is not None:
            self._close_resource(id(page))
            return

        if type == "all_browser":
            with self.cleanup_lock:
                page_ids = list(self.active_resources.keys())
            for page_id in page_ids:
                self._close_resource(page_id)
            self._retry_orphan_profiles()

    def _close_resource(self, page_id):
        with self.cleanup_lock:
            resource = self.active_resources.get(page_id)

        if resource is None:
            return True

        page = resource.get("page")
        browser_closed = page is None or self._quit_page(page)

        profile_dir = resource.get("profile_dir")
        profile_removed = bool(profile_dir) and self._remove_profile_dir(profile_dir)
        profile_ok = not profile_dir or profile_removed

        if not browser_closed:
            print(
                "[Warn: Cleanup] - browser/profile cleanup incomplete; "
                f"resource retained: {profile_dir or page_id}"
            )
            return False

        if not profile_ok and profile_dir:
            with self.cleanup_lock:
                self.orphan_profile_dirs.add(profile_dir)

        should_release = False
        with self.cleanup_lock:
            if self.active_resources.get(page_id) is resource:
                self.active_resources.pop(page_id, None)
                should_release = True

        if should_release:
            proxy_lease = resource.get("proxy_lease")
            if proxy_lease is not None:
                proxy_lease.release()
            else:
                release_proxy(resource.get("proxy_config"))
        return True

    def _retry_orphan_profiles(self):
        with self.cleanup_lock:
            profile_dirs = list(self.orphan_profile_dirs)

        for profile_dir in profile_dirs:
            # A live browser owns parent.lock.  Leave that directory alone;
            # a later cleanup pass can reclaim it after the process exits.
            if os.path.exists(os.path.join(profile_dir, "parent.lock")):
                continue
            if self._remove_profile_dir(profile_dir, retries=3, delay=0.25):
                with self.cleanup_lock:
                    self.orphan_profile_dirs.discard(profile_dir)

    def sweep_stale_profiles(self, min_age_seconds=0):
        """Remove leftover ruyipage-* profile dirs that no live Firefox owns.

        Called at startup so a previous crashed run's half-started profiles
        (we found 15) don't pile up. A dir with a parent.lock younger than
        min_age is left alone in case a browser is mid-launch. Returns the
        number of dirs removed."""
        root = os.path.realpath(self.profile_root)
        now = time.time()
        removed = 0
        try:
            entries = list(os.scandir(root))
        except OSError:
            return 0
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False) or not entry.name.startswith(
                "ruyipage-"
            ):
                continue
            profile_dir = os.path.realpath(entry.path)
            try:
                if os.path.commonpath((root, profile_dir)) != root:
                    continue
                age = max(0.0, now - entry.stat(follow_symlinks=False).st_mtime)
            except (OSError, ValueError):
                continue
            if age < min_age_seconds:
                continue
            # Locked + a live process -> skip; locked + dead process -> the lock
            # file is stale and safe to delete with the dir.
            if self._remove_profile_dir(profile_dir, retries=2, delay=0.2):
                removed += 1
        if removed:
            print(f"[Info: Cleanup] - swept {removed} stale profile dir(s) at startup")
        return removed

    def _discover_orphan_profiles(self):
        """Remember old, process-independent profiles for a later cleanup.

        Discovery is intentionally non-destructive.  The cleanup pass only
        removes directories that no longer contain the browser lock file.
        """
        root = os.path.realpath(self.profile_root)
        now = time.time()
        try:
            entries = list(os.scandir(root))
        except OSError:
            return

        discovered = []
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False) or not entry.name.startswith(
                "ruyipage-"
            ):
                continue
            profile_dir = os.path.realpath(entry.path)
            try:
                if os.path.commonpath((root, profile_dir)) != root:
                    continue
                age = max(0.0, now - entry.stat(follow_symlinks=False).st_mtime)
            except (OSError, ValueError):
                continue
            if age >= self.orphan_profile_min_age:
                discovered.append(profile_dir)
        if discovered:
            with self.cleanup_lock:
                self.orphan_profile_dirs.update(discovered)

    def _quit_page(self, page, retries=2, delay=0.25):
        for attempt in range(retries):
            try:
                page.quit(timeout=5, force=True)
                return True
            except TypeError:
                try:
                    page.quit()
                    return True
                except Exception:
                    pass
            except Exception:
                pass

            if attempt + 1 < retries:
                time.sleep(delay)
        return False

    def _remove_profile_dir(self, profile_dir, retries=8, delay=0.5):
        for _ in range(retries):
            if not os.path.exists(profile_dir):
                return True
            try:
                shutil.rmtree(profile_dir)
                return True
            except Exception:
                time.sleep(delay)

        if os.path.exists(profile_dir):
            print(f"[Warn: Cleanup] - profile dir not removed: {profile_dir}")
            return False
        return True

    def build_profile_dir(self):
        return tempfile.mkdtemp(prefix="ruyipage-", dir=self.profile_root)

    def _account_record(self, email, password):
        return f"{email}: {password}\n"

    def _append_account_record_unlocked(self, path, record):
        with open(path, "a", encoding="utf-8") as result_file:
            result_file.write(record)
            result_file.flush()
            os.fsync(result_file.fileno())

    def persist_registered_account(self, email, password):
        """Persist a newly registered account before optional OAuth work."""
        path = os.path.join(self.results_dir, "unlogged_email.txt")
        record = self._account_record(email, password)
        with _result_write_lock:
            self._append_account_record_unlocked(path, record)

    def promote_registered_account(self, email, password):
        """Promote a token-authenticated account and remove its pending record.

        The logged record is written first so a failure while rewriting the
        pending file cannot lose the account.  The rewrite uses replace() to
        avoid exposing a partially written result file to readers.
        """
        logged_path = os.path.join(self.results_dir, "logged_email.txt")
        pending_path = os.path.join(self.results_dir, "unlogged_email.txt")
        record = self._account_record(email, password)
        record_without_newline = record.rstrip("\r\n")

        with _result_write_lock:
            self._append_account_record_unlocked(logged_path, record)

            try:
                with open(pending_path, "r", encoding="utf-8") as pending_file:
                    lines = pending_file.readlines()
            except FileNotFoundError:
                return

            remaining = [
                line
                for line in lines
                if line.rstrip("\r\n") != record_without_newline
            ]
            if len(remaining) == len(lines):
                return

            fd, temporary_path = tempfile.mkstemp(
                prefix=".unlogged-", suffix=".tmp", dir=self.results_dir
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as pending_file:
                    pending_file.writelines(remaining)
                    pending_file.flush()
                    os.fsync(pending_file.fileno())
                os.replace(temporary_path, pending_path)
            finally:
                if os.path.exists(temporary_path):
                    try:
                        os.remove(temporary_path)
                    except OSError:
                        pass

    def human_pause(self, page, min_seconds=None, max_seconds=None):
        pause_min = self.action_pause_min if min_seconds is None else min_seconds
        pause_max = self.action_pause_max if max_seconds is None else max_seconds
        page.wait(random.uniform(pause_min, pause_max))

    def wait_for_any(self, owner, locators, timeout=5):
        end_time = time.monotonic() + timeout
        while time.monotonic() < end_time:
            for locator in locators:
                try:
                    element = owner.ele(locator, timeout=0.2)
                except Exception:
                    continue
                if element:
                    return element
            try:
                owner.wait(0.15)
            except Exception:
                time.sleep(0.15)
        return False

    def wait_for_all(self, owner, locator_groups, timeout=5):
        end_time = time.monotonic() + timeout
        while time.monotonic() < end_time:
            found = []
            complete = True
            for locators in locator_groups:
                element = self.wait_for_any(owner, locators, timeout=0.25)
                if not element:
                    complete = False
                    break
                found.append(element)
            if complete:
                return tuple(found)
            try:
                owner.wait(0.15)
            except Exception:
                time.sleep(0.15)
        return tuple(False for _ in locator_groups)

    def visible_locator_exists(self, owner, locators, timeout=1.5, min_size=8):
        end_time = time.monotonic() + timeout
        while time.monotonic() < end_time:
            for locator in locators:
                try:
                    elements = owner.eles(locator, timeout=0.2)
                except Exception:
                    elements = []

                for element in elements:
                    try:
                        if not element or not element.is_displayed:
                            continue
                        size = element.size or {}
                        width = int(size.get("width", 0) or 0)
                        height = int(size.get("height", 0) or 0)
                        if width < min_size or height < min_size:
                            continue
                        return element
                    except Exception:
                        continue
            try:
                owner.wait(0.2)
            except Exception:
                time.sleep(0.2)
        return False

    def click_element(self, owner, element):
        try:
            owner.actions.move_to(
                element,
                offset_x=random.randint(-3, 3),
                offset_y=random.randint(-2, 2),
                duration=random.randint(80, 180),
            ).click().perform()
            return
        except Exception as action_error:
            # ponytail: ruyipage's element.click() -> click_self() scrolls to
            # center then reads getBoundingClientRect; an off-viewport or
            # zero-size target raises '无法获取元素可点击坐标'. Try a plain JS
            # el.click() first (no geometry needed), and only then fall back
            # to the coordinate path, so off-screen controls don't die.
            try:
                self._el_run_js(element, "this.click();")
                return
            except Exception:
                pass
            try:
                element.click()
                return
            except Exception:
                raise action_error

    def click_locator(self, owner, locators, timeout=5):
        element = self.wait_for_any(owner, locators, timeout=timeout)
        if not element:
            return False
        self.click_element(owner, element)
        return element

    def text_exists(self, page, text):
        try:
            return bool(
                page.run_js(
                    "return !!document.body && document.body.innerText.includes(arguments[0]);",
                    text,
                    as_expr=False,
                )
            )
        except Exception:
            return False

    def page_preview(self, page, limit=300):
        try:
            preview = page.run_js(
                "return document.body ? document.body.innerText.slice(0, arguments[0]) : '';",
                limit,
                as_expr=False,
            )
            return str(preview).replace("\r", " ").replace("\n", " ").strip()
        except Exception:
            return ""

    def page_state_summary(self, page, preview_limit=500):
        try:
            current_url = page.url or "unknown"
        except Exception:
            current_url = "unknown"

        try:
            page_title = page.title or "unknown"
        except Exception:
            page_title = "unknown"

        return current_url, page_title, self.page_preview(page, limit=preview_limit)

    def _button_is_disabled(self, button):
        if not button:
            return True
        try:
            if button.disabled:
                return True
        except Exception:
            pass
        try:
            if str(button.attr("aria-disabled") or "").lower() == "true":
                return True
        except Exception:
            pass
        try:
            if "disabled" in str(button.attr("class") or "").lower():
                return True
        except Exception:
            pass
        return False

    def primary_button(self, page, timeout=5):
        # ponytail: the zh Outlook Next button is type=submit and present even
        # when client-side validation has disabled it. Skipping disabled
        # buttons here protects every click_primary_button caller (email,
        # password, birthday) — otherwise the click is a no-op and we move on
        # assuming the page advanced.
        locators = [
            "css:[data-testid='primaryButton']",
            "css:button[type='submit']",
            "xpath://button[@data-testid='primaryButton']",
        ]
        end_time = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < end_time:
            for locator in locators:
                try:
                    element = page.ele(locator, timeout=0.2)
                except Exception:
                    element = False
                if element and not self._button_is_disabled(element):
                    return element
            try:
                page.wait(0.2)
            except Exception:
                time.sleep(0.2)
        return False

    def click_primary_button(self, page, timeout=5):
        button = self.primary_button(page, timeout=timeout)
        if not button:
            return False
        self.click_element(page, button)
        return True

    def application_risk_detected(self, page):
        return self.text_exists(page, "一些异常活动")

    def service_unavailable_detected(self, page):
        return self.text_exists(page, "此站点正在维护，暂时无法使用，请稍后重试。")

    def error_text_detected(self, page):
        """Compatibility detector for callers that only need an error boolean."""
        return self.application_risk_detected(page) or self.service_unavailable_detected(
            page
        )

    def challenge_active(self, page):
        if self.mailbox_url_ready(page):
            return False

        iframe_locators = [
            "css:iframe#enforcementFrame",
            "css:iframe[title*='验证']",
            "css:iframe[src*='arkoselabs']",
            "css:iframe[src*='funcaptcha']",
        ]
        if self.visible_locator_exists(page, iframe_locators, timeout=0.5, min_size=20):
            return True

        challenge_texts = [
            "验证你不是机器人",
            "可访问性挑战",
            "再次按下",
            "请再试一次",
            "长按该按钮",
            "Click to verify",
            "Press and hold",
        ]
        return any(self.text_exists(page, text) for text in challenge_texts)

    def _consent_page_active(self, page):
        """True when Microsoft shows the '个人数据导出许可' consent interstitial
        (zh-CN market). Has '同意并继续' / '拒绝并退出' buttons."""
        return self.text_exists(page, "同意并继续") or self.text_exists(
            page, "拒绝并退出"
        )

    def _dismiss_consent_page(self, page, email_input_locators, timeout=20):
        """Click '同意并继续' and wait for the registration form to appear.

        The consent button lives in a Fluent UI portal/overlay where the
        coordinate-based click_element (actions.move_to) sometimes misses — same
        root cause as the combobox option fix — so use the trusted element.click()
        path. Verify the consent actually dismissed (text gone OR email input
        appeared) and retry a few times; never assume one click succeeded.
        Returns the email input element, or False if consent could not be cleared.
        """
        consent_locators = [
            "xpath://button[contains(normalize-space(.), '同意并继续')]",
            "xpath://*[@role='button'][contains(normalize-space(.), '同意并继续')]",
            "xpath://span[contains(normalize-space(.), '同意并继续')]/ancestor::*[@role='button' or self::button][1]",
            "css:button[type='submit']",
        ]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._consent_page_active(page):
                # consent gone — registration form should be present now
                return self.wait_for_any(page, email_input_locators, timeout=5)
            element = self.wait_for_any(page, consent_locators, timeout=3)
            if element:
                self.human_pause(page)
                try:
                    # trusted click — works on portaled overlays regardless of geometry
                    element.click()
                except Exception:
                    self.click_element(page, element)
                try:
                    page.wait(1.0)
                except Exception:
                    time.sleep(1.0)
            else:
                try:
                    page.wait(0.8)
                except Exception:
                    time.sleep(0.8)
        # final check: consent cleared even if email input not yet visible
        if not self._consent_page_active(page):
            return self.wait_for_any(page, email_input_locators, timeout=3)
        return False

    def mailbox_shell_ready(self, page):
        mailbox_locators = [
            "css:[aria-label='新邮件']",
            "xpath://*[contains(normalize-space(.), '新邮件')]",
            "xpath://*[contains(normalize-space(.), '收件箱')]",
            "xpath://*[contains(normalize-space(.), '创建新的电子邮件')]",
            "xpath://*[contains(normalize-space(.), '重点')]",
            "xpath://*[contains(normalize-space(.), '其他')]",
        ]
        if self.visible_locator_exists(page, mailbox_locators, timeout=0.3):
            return True

        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""

        try:
            page_title = page.title or ""
        except Exception:
            page_title = ""

        mailbox_url = (
            "outlook.live.com/mail" in current_url
            and "prompt=create_account" not in current_url
        )
        mailbox_title = "Outlook" in page_title or "邮件 -" in page_title
        mailbox_text = any(
            self.text_exists(page, text)
            for text in ("收件箱", "新邮件", "创建新的电子邮件", "重点", "其他")
        )
        return mailbox_url and (mailbox_title or mailbox_text)

    def mailbox_url_ready(self, page):
        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""

        return "https://outlook.live.com/mail/0/" in current_url

    def proofs_page_active(self, page):
        # ponytail: proofs/Add = "让我们来保护你的帐户" appears AFTER the account
        # is created. Hitting it means registration succeeded; we just need to
        # dismiss it (click 暂时跳过) to land in the mailbox.
        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""
        if "account.live.com/proofs/Add" not in current_url:
            return False
        return self.text_exists(page, "保护你的帐户") or self.text_exists(page, "保護你的帳戶")

    def dismiss_proofs_page(self, page):
        # Click the Skip control until the URL leaves proofs/Add. The page is
        # an ASP.NET/React form that re-renders on the async skip POST, and the
        # skip element lives in a portaled container that detaches on re-render
        # — so re-query each iteration and verify the URL actually advanced.
        # Returns True once we've left the page (or it's no longer shown).
        skip_locators = [
            # stable Microsoft id across locales/markets; prefer over text.
            "css:#iShowSkip",
            "xpath://*[@id='iShowSkip']",
            "xpath://a[@id='iShowSkip']",
            "xpath://a[normalize-space(.)='Skip for now']",
            "xpath://a[contains(normalize-space(.), '暂时跳过')]",
            "xpath://a[contains(normalize-space(.), '暫時跳過')]",
            "xpath://button[contains(normalize-space(.), 'Skip for now')]",
            "xpath://button[contains(normalize-space(.), '暂时跳过')]",
            "xpath://button[contains(normalize-space(.), '暫時跳過')]",
        ]
        for _ in range(3):
            element = self.wait_for_any(page, skip_locators, timeout=2)
            if not element:
                # No skip control (some account states show only frmAddProof).
                return False
            try:
                self.click_element(page, element)
            except Exception:
                pass
            try:
                page.wait(1.2)
            except Exception:
                time.sleep(1.2)
            if not self.proofs_page_active(page):
                return True
        return not self.proofs_page_active(page)

    def _el_run_js(self, element, script_body):
        """Run JS on an element. FirefoxElement.run_js expects a FUNCTION
        declaration string (body executed with `this` = element), NOT a bare
        script — passing 'return this.value;' raises 'SyntaxError: expected
        expression, got keyword return'. Wrap the body in an IIFE."""
        return element.run_js(f"function(){{ {script_body} }}")

    def get_select_state(self, element):
        try:
            value = self._el_run_js(element, "return this.value;")
        except Exception:
            value = ""

        try:
            text = self._el_run_js(
                element,
                "return this.options && this.selectedIndex >= 0 ? this.options[this.selectedIndex].text : '';",
            )
        except Exception:
            text = ""

        return str(value or ""), str(text or "")

    def _element_is_native_select(self, element):
        """True only if `element` is a real <select> — false for React
        comboboxes (div role=combobox) where native .select API / .options
        don't apply. ponytail: get_select_state reads this.options; calling it
        on a div returns empty and the old empty-means-success guard then
        falsely reported success."""
        try:
            tag = self._el_run_js(element, "return (this.tagName || '').toUpperCase();")
        except Exception:
            tag = ""
        return tag == "SELECT"

    def _combobox_selected_text(self, element):
        """Read the displayed text of a non-<select> combobox control (the
        visible selected value). Falls back through text/value/aria-label."""
        if not element:
            return ""
        try:
            return str(
                self._collect_control_text(element)
            ).strip()
        except Exception:
            return ""

    def _collect_control_text(self, element):
        merged = []
        for attr_name in ("text", "value"):
            try:
                merged.append(str(getattr(element, attr_name) or ""))
            except Exception:
                pass
        for attr_name in ("aria-label", "title", "value"):
            try:
                merged.append(str(element.attr(attr_name) or ""))
            except Exception:
                pass
        return " ".join(merged).strip()

    def select_with_fallback(self, page, select_element, value, text_candidates, field_name):
        # ponytail: React combobox widgets (Outlook birth month/day) are NOT
        # <select> — native .select.by_value/.by_text silently no-op on a div
        # and get_select_state (.options/.selectedIndex) reads empty. The old
        # `empty value and empty text => True` guard then misreported success,
        # so the birthday 'next' click did nothing and name inputs never
        # appeared. Route non-<select> straight to the click-control + click
        # role=option + DOM-text-verify path used by ensure_email_suffix_selected.
        if not self._element_is_native_select(select_element):
            return self._select_combobox(
                page, select_element, value, text_candidates, field_name
            )

        try:
            if select_element.select.by_value(value):
                current_value, current_text = self.get_select_state(select_element)
                if current_value.lstrip("0") == str(value).lstrip("0") or any(
                    candidate and candidate in current_text for candidate in text_candidates
                ):
                    return True
        except Exception:
            pass

        for text in text_candidates:
            try:
                if select_element.select.by_text(text):
                    current_value, current_text = self.get_select_state(select_element)
                    if (
                        current_value.lstrip("0") == str(value).lstrip("0")
                        or text in current_text
                    ):
                        return True
            except Exception:
                pass

        self.human_pause(page, 0.2, 0.5)
        self.click_element(page, select_element)

        option_locators = []
        for text in text_candidates:
            option_locators.extend(
                [
                    f"xpath://*[@role='option'][contains(normalize-space(.), '{text}')]",
                    f"xpath://option[contains(normalize-space(.), '{text}')]",
                    f"xpath://*[contains(normalize-space(.), '{text}')]",
                ]
            )

        option = self.wait_for_any(page, option_locators, timeout=3)
        if option:
            self.click_element(page, option)
            current_value, current_text = self.get_select_state(select_element)
            if (
                current_value.lstrip("0") == str(value).lstrip("0")
                or any(candidate and candidate in current_text for candidate in text_candidates)
            ):
                return True

        current_value, current_text = self.get_select_state(select_element)
        raise TimeoutError(
            f"{field_name} select failed; expected={value}; actual_value={current_value}; actual_text={current_text}"
        )

    def _dump_combobox_diagnostics(self, page, control, value, text_candidates, field_name):
        """Dump real DOM structure to the log so the operator can see the
        actual widget shape instead of guessing locators. Called only on
        combobox-select failure."""
        try:
            tag = self._el_run_js(control, "return (this.tagName||'')+' | role='+(this.getAttribute('role')||'')+' | aria-label='+(this.getAttribute('aria-label')||'')+' | value='+(this.value||'')+' | name='+(this.getAttribute('name')||'')+' | activedesc='+(this.getAttribute('aria-activedescendant')||'')+' | childCount='+(this.children||[]).length;")
        except Exception as e:
            tag = f"<probe failed: {e}>"
        try:
            outer = self._el_run_js(control, "return this.outerHTML || '';")
        except Exception as e:
            outer = f"<outerHTML probe failed: {e}>"
        try:
            # page.run_js DOES accept as_expr; keep it. Probe the control's
            # selected child so we can see where the displayed value lives.
            opts = page.run_js(
                "var c=arguments[0]; var out=[]; if(c){ var sel=c.querySelector('[aria-selected=true],[aria-selected=\"true\"],.selected,[data-selected]'); if(sel) out.push('SELECTED: '+(sel.getAttribute('role')||sel.tagName)+'/' +(sel.innerText||sel.textContent||'').trim().slice(0,30)); } document.querySelectorAll('[role=option], li, option, [role=menuitemradio], [role=listitem]').forEach(function(n){ var t=(n.innerText||n.textContent||'').trim(); if(t) out.push(n.tagName+'/'+(n.getAttribute('role')||'')+':'+t.slice(0,20)); }); return out.slice(0,65).join('\\n');",
                control,
                as_expr=False,
            )
        except Exception as e:
            opts = f"<opts probe failed: {e}>"
        print(
            f"[Debug: Combobox:{field_name}] - DOM dump; value={value}; "
            f"candidates={text_candidates}; probe={tag}; "
            f"outerHTML={str(outer)[:400]}; options:\n{opts}"
        )

    def _select_combobox(self, page, control, value, text_candidates, field_name):
        """Select an option in a React/Fluent combobox (role=combobox +
        role=option listbox).

        ponytail: Fluent UI's day/month dropdowns are VIRTUALIZED — the
        role='option' <div> ruyipage finds has unstable ids (fluent-option45
        one render, fluent-option77 the next) and clicking the detached node
        dispatches a trusted click but the React model re-resolves the value
        from its own state, so day=8 lands on 9 every time. The DOM-option
        click path is therefore unreliable on these widgets. Keyboard input
        is the Fluent-blessed interaction: focus the control, open the
        listbox, type the numeric value (Fluent filters/jumps to the match),
        Enter to commit. Fall back to the DOM-option click only if keyboard
        input is not available or fails to verify.

        Verify uses only candidates that carry a unit (日/月/号) so a lone
        digit can't match inside a larger value like '5' in '15日'."""
        self.human_pause(page, 0.2, 0.5)
        self.click_element(page, control)  # focus + open listbox
        self.human_pause(page, 0.25, 0.5)

        unit_candidates = [
            c for c in text_candidates
            if c and any(u in c for u in ("日", "月", "号"))
        ]
        verify_candidates = unit_candidates or text_candidates

        matched = self._combobox_keyboard_select(page, control, value, verify_candidates)
        if matched:
            return True

        # Primary structured path: Fluent's own keyboard contract (open popup,
        # ArrowDown to the matching option, Enter). Immune to virtualizer
        # off-by-one and sticky-group-header because each ArrowDown mounts the
        # next row as it advances — works for day=31 / month=12 where the
        # scrollIntoView+click path breaks.
        matched = self._combobox_arrow_select(
            page, control, value, text_candidates, verify_candidates
        )
        if matched:
            return True

        # Middle path: Fluent UI's month listbox fits in the viewport, so the
        # wanted role=option node IS in the render tree (the debug dump showed
        # it) — keyboard type-ahead just isn't moving selection on this widget
        # build. Drive the option's own trusted .click() with a scrollIntoView
        # first; that dispatches the real DOM event regardless of coordinate
        # math. Only if no option node exists do we fall through to the
        # wait+click path below.
        js_clicked = self._combobox_js_option_click(page, control, text_candidates)
        if js_clicked:
            self.human_pause(page, 0.3, 0.5)
            current_text = self._combobox_selected_text(control)
            if any(c and c in current_text for c in verify_candidates):
                return True

        # Fallback: DOM-option click path (unreliable on virtualized Fluent
        # lists, but correct for plain React/non-virtualized listboxes).
        click_candidates = text_candidates or [value]
        option = None
        for text in click_candidates:
            exact_locators = [
                f"xpath://*[@role='option'][normalize-space(.)='{text}']",
                f"xpath://li[normalize-space(.)='{text}']",
                f"xpath://*[@role='option' and normalize-space(.)='{text}']",
                f"xpath://option[normalize-space(.)='{text}']",
            ]
            option = self.wait_for_any(page, exact_locators, timeout=2)
            if option:
                break

        if not option:
            self._dump_combobox_diagnostics(page, control, value, text_candidates, field_name)
            current_text = self._combobox_selected_text(control)
            raise TimeoutError(
                f"{field_name} combobox option not found; expected={text_candidates}; "
                f"control_text={current_text}"
            )
        self._click_combobox_option(page, option, control)
        self.human_pause(page, 0.25, 0.5)

        current_text = self._combobox_selected_text(control)
        matched = any(c and c in current_text for c in verify_candidates)
        if not matched:
            # Re-open, refocus the exact option with a plain trusted click
            # (no scroll), and re-verify once before giving up.
            try:
                self.click_element(page, control)
                self.human_pause(page, 0.2, 0.4)
                option.click()
                self.human_pause(page, 0.3, 0.5)
            except Exception:
                pass
            current_text = self._combobox_selected_text(control)
            matched = any(c and c in current_text for c in verify_candidates)
        if not matched:
            # ponytail: the signup page also renders the chosen value as
            # "<n>月" / "<n>日" with a space or full-width space, and the
            # button's accessible name sometimes collapses to just the unit
            # suffix. Widen verification to accept the numeric value with a
            # unit anywhere in the control's merged text before giving up.
            unit_chars = ("月", "日", "号")
            if str(value) in current_text and any(u in current_text for u in unit_chars):
                return True
            self._dump_combobox_diagnostics(page, control, value, text_candidates, field_name)
            raise TimeoutError(
                f"{field_name} combobox select did not stick; "
                f"expected={text_candidates}; control_text={current_text}"
            )
        return True

    def _combobox_js_option_click(self, page, control, text_candidates):
        """Click a role=option inside the page's open listbox via trusted DOM
        .click(), choosing the node by exact/normalized text in JS. Returns
        True if a matching option was found and clicked.

        Runs in page context and searches every visible listbox, so a
        virtualized Fluent list that never mounted the target row simply
        returns False (caller falls back to the locator path). Exact
        normalized match first so '1' can't hit '10月'; startswith second so
        '1' still matches a '1月' row when no bare-digit candidate exists."""
        try:
            control.focus()
        except Exception:
            pass
        self.human_pause(page, 0.15, 0.3)
        try:
            return bool(
                page.run_js(
                    "var cands=arguments[0];"
                    "var lists=document.querySelectorAll('[role=listbox]');"
                    "function norm(s){return (s||'').replace(/\\s+/g,' ').trim();}"
                    "for(var i=0;i<lists.length;i++){"
                    "  var opts=lists[i].querySelectorAll('[role=option]');"
                    "  var best=null;"
                    "  for(var j=0;j<opts.length;j++){"
                    "    var t=norm(opts[j].innerText||opts[j].textContent);"
                    "    if(!t) continue;"
                    "    for(var k=0;k<cands.length;k++){"
                    "      var c=norm(cands[k]);"
                    "      if(!c) continue;"
                    "      if(t===c){ best=opts[j]; break; }"
                    "      if(!best && t.indexOf(c)===0){ best=opts[j]; }"
                    "    }"
                    "    if(best) break;"
                    "  }"
                    "  if(best){"
                    "    best.scrollIntoView({block:'nearest'});"
                    "    best.click();"
                    "    return true;"
                    "  }"
                    "}"
                    "return false;",
                    list(text_candidates or []),
                    as_expr=False,
                )
            )
        except Exception:
            return False

    def _combobox_arrow_select(self, page, control, value, text_candidates, verify_candidates):
        """Drive Fluent UI's own keyboard contract: open the popup, ArrowDown
        until the active option matches the target, Enter to commit.

        This is the virtualization-immune path. Fluent keeps DOM focus on the
        combobox button and moves the *active* option via aria-activedescendant;
        each ArrowDown both advances selection AND forces the virtualizer to
        mount the next row, so even day=31 comes into existence as we step.
        No scrollIntoView on a not-yet-mounted node, no sticky-group-header
        coordinate math, no React-fiber mutation. We read the active option's
        own text (not the detached handle) each step so re-renders don't
        break the loop. Returns True if Enter was pressed on a matching option.
        """
        try:
            control.focus()
        except Exception:
            pass
        # Open the popup (click). Enter only commits when popup is open.
        try:
            self.click_element(page, control)
        except Exception:
            pass
        self.human_pause(page, 0.25, 0.45)

        def _active_text():
            try:
                return str(
                    page.run_js(
                        "var c=arguments[0];"
                        "var out='';"
                        "var id=c && c.getAttribute && c.getAttribute('aria-activedescendant');"
                        "if(id){ var n=document.getElementById(id); if(n) out=(n.innerText||n.textContent||''); }"
                        "if(!out){"
                        "  var sel=document.querySelector('[role=listbox] [role=option][aria-selected=\"true\"]');"
                        "  if(sel) out=(sel.innerText||sel.textContent||'');"
                        "}"
                        "return (out||'').replace(/\\s+/g,' ').trim();",
                        control,
                        as_expr=False,
                    )
                    or ""
                ).strip()
            except Exception:
                return ""

        def _matches(text):
            if not text:
                return False
            t = text.replace(" ", "")
            for c in text_candidates:
                if not c:
                    continue
                cand = str(c).replace(" ", "")
                if t == cand or t.startswith(cand):
                    return True
            return False

        # Jump to the first option, then step down. Month<=12, Day<=31 steps.
        max_steps = 34
        try:
            actions = page.actions
            if actions is None:
                return False
            actions.press(Keys.HOME)
            actions.perform()
        except Exception:
            pass
        self.human_pause(page, 0.1, 0.2)

        # Home already lands on option 1; if that's the target, commit now.
        if _matches(_active_text()):
            try:
                page.actions.press(Keys.ENTER).perform()
            except Exception:
                return False
            self.human_pause(page, 0.25, 0.45)
            current = self._combobox_selected_text(control)
            return any(c and c in current for c in verify_candidates) or (
                str(value) in current and any(u in current for u in ("月", "日", "号"))
            )

        for _ in range(max_steps):
            try:
                page.actions.press(Keys.ARROW_DOWN).perform()
            except Exception:
                return False
            self.human_pause(page, 0.06, 0.12)
            if _matches(_active_text()):
                try:
                    page.actions.press(Keys.ENTER).perform()
                except Exception:
                    return False
                self.human_pause(page, 0.25, 0.45)
                current = self._combobox_selected_text(control)
                return any(c and c in current for c in verify_candidates) or (
                    str(value) in current and any(u in current for u in ("月", "日", "号"))
                )
        return False

    def _combobox_keyboard_select(self, page, control, value, verify_candidates):
        """Type a value into a Fluent/React combobox via BiDi keyboard input
        and Enter, then verify the control's displayed text matches. Returns
        True on success. No-op (returns False) if the control has no usable
        focus/keyboard path, so the caller falls back to DOM-option click."""
        try:
            control.focus()
        except Exception:
            pass
        self.human_pause(page, 0.15, 0.3)
        try:
            actions = page.actions
            if actions is None:
                return False
            actions.type(str(value), interval=random.randint(40, 90))
            actions.press(Keys.ENTER)
            actions.perform()
        except Exception:
            return False
        self.human_pause(page, 0.3, 0.5)
        current_text = self._combobox_selected_text(control)
        return any(c and c in current_text for c in verify_candidates)

    def _click_combobox_option(self, page, option, control=None):
        """Click a listbox option via the trusted element.click() path, after
        scrollIntoView. The global click_element uses coordinate-based
        actions.move_to — on Fluent UI portaled listboxes that lands +2 rows
        low (control_text lands on want+2), and on off-screen options
        (day>~24, listbox taller than viewport) the coordinate is outside the
        viewport so nothing selects and the control stays on its placeholder.
        Trusted element.click() dispatches the real DOM event regardless of
        geometry, and scrollIntoView makes sure the option exists in the
        render tree first. Falls back to coordinate move_to only if trusted
        click raises.

        ponytail: block:'nearest' not 'center'. Fluent UI day/month listboxes
        have a sticky group header row pinned at the viewport top; 'center'
        scrolls the target option under that header, so the trusted click lands
        one row low (day=8 -> 9). 'nearest' only scrolls as far as needed to
        clear the viewport edge, leaving the header above the option. Verified
        empirically: day<=7 never scrolls (no bug); day=8 is the first row
        that triggers a center-scroll and consistently lands on day+1."""
        try:
            self._el_run_js(
                option,
                "this.scrollIntoView({block:'nearest', inline:'nearest'});",
            )
        except Exception:
            pass
        try:
            option.click()
            return
        except Exception as click_error:
            try:
                page.actions.move_to(option, duration=random.randint(80, 180)).click().perform()
                return
            except Exception:
                raise click_error

    def _combobox_option_is_selected(self, option):
        """Read the option's selection state. Fluent UI marks the active row
        with aria-selected='true' on a role='option' node; the control's
        own value/aria-activedescendant then reflects it."""
        try:
            return bool(
                self._el_run_js(
                    option,
                    "return (this.getAttribute('aria-selected')==='true')||this.classList.contains('selected')||(this.getAttribute('data-selected')!==null);",
                )
            )
        except Exception:
            return False

    def _suffix_control_locators(self):
        return [
            "xpath://*[@role='combobox' and (contains(normalize-space(.), '@outlook.com') or contains(normalize-space(.), '@hotmail.com'))]",
            "xpath://button[contains(normalize-space(.), '@outlook.com') or contains(normalize-space(.), '@hotmail.com')]",
            "xpath://*[@aria-haspopup='listbox' and (contains(@aria-label, '@outlook.com') or contains(@aria-label, '@hotmail.com'))]",
            "css:[role='combobox']",
        ]

    def _extract_suffix_from_element(self, element):
        if not element:
            return None

        candidates = []
        for attr_name in ("text", "value"):
            try:
                candidates.append(str(getattr(element, attr_name) or ""))
            except Exception:
                pass
        for attr_name in ("aria-label", "title", "value"):
            try:
                candidates.append(str(element.attr(attr_name) or ""))
            except Exception:
                pass

        merged = " ".join(candidates)
        if "@hotmail.com" in merged:
            return "@hotmail.com"
        if "@outlook.com" in merged:
            return "@outlook.com"
        return None

    def read_current_email_suffix(self, page):
        control = self.wait_for_any(page, self._suffix_control_locators(), timeout=1)
        return self._extract_suffix_from_element(control)

    def ensure_email_suffix_selected(self, page):
        desired_suffix = self.email_suffix
        current_suffix = self.read_current_email_suffix(page)

        if desired_suffix == "@outlook.com" and current_suffix in (None, "@outlook.com"):
            return True
        if current_suffix == desired_suffix:
            return True

        control = self.wait_for_any(page, self._suffix_control_locators(), timeout=5)
        if not control:
            raise TimeoutError("email suffix control not found")

        self.human_pause(page, 0.2, 0.5)
        self.click_element(page, control)
        self.human_pause(page, 0.2, 0.5)

        option = self.click_locator(
            page,
            [
                f"xpath://*[@role='option'][contains(normalize-space(.), '{desired_suffix}')]",
                f"xpath://*[contains(normalize-space(.), '{desired_suffix}')]",
            ],
            timeout=5,
        )
        if not option:
            raise TimeoutError(f"email suffix option not found: {desired_suffix}")

        self.human_pause(page, 0.3, 0.6)
        current_suffix = self.read_current_email_suffix(page)
        if current_suffix != desired_suffix:
            raise TimeoutError(
                f"email suffix selection did not stick; expected={desired_suffix}; actual={current_suffix}"
            )
        return True

    def mailbox_ready(self, page):
        if self.mailbox_url_ready(page):
            return True
        if self.mailbox_shell_ready(page):
            return True

        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""

        if (
            "outlook.live.com/mail" in current_url
            and "prompt=create_account" not in current_url
            and not self.challenge_active(page)
        ):
            return True

        return False

    def _failed_load_detected(self, page):
        """True when the page failed to render (blank body / transport error
        title). Reuses _FAILED_LOAD_TITLES so this and the post-loop classifier
        agree on what counts as a failed load."""
        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""
        # Only treat proofs/mail destinations as reload-recoverable; a blank
        # page elsewhere (e.g. mid-form) is the caller's concern.
        recoverable = (
            "account.live.com/proofs" in current_url
            or "outlook.live.com/mail" in current_url
        )
        if not recoverable:
            return False
        try:
            title = page.title or ""
        except Exception:
            title = ""
        if str(title).strip().lower() in _FAILED_LOAD_TITLES:
            return True
        # Empty body after a short settle: failed to load rather than slow parse.
        return self.page_preview(page, limit=1) == ""

    def _reload_current(self, page):
        """Re-navigate to the current url. Returns False if navigation itself
        blew up (dead transport) so the caller can classify transport_error."""
        try:
            current_url = page.url or ""
        except Exception:
            return False
        if not current_url:
            return False
        try:
            page.get(current_url, wait="interactive", timeout=20)
            time.sleep(0.4)
            return True
        except Exception as reload_error:
            print(f"[Warn: MailboxWait] - reload failed: {reload_error}")
            return False

    def wait_for_mailbox_ready(self, page, timeout=None):
        if timeout is None:
            timeout = self.mailbox_wait_timeout
        end_time = time.monotonic() + timeout
        reload_count = 0
        mailbox_reload_limit = 3
        while time.monotonic() < end_time:
            if self.service_unavailable_detected(page):
                return False, "service_unavailable"
            if self.error_text_detected(page):
                return False, "risk"
            if self.mailbox_url_ready(page):
                return True, "mailbox_url_ready"
            if self.mailbox_ready(page):
                return True, "mailbox_ready"
            if self.mailbox_shell_ready(page):
                return True, "mailbox_shell_ready"
            if self.challenge_active(page):
                if not self.handle_captcha(page):
                    if self.service_unavailable_detected(page):
                        return False, "service_unavailable"
                    if self.error_text_detected(page):
                        return False, "risk"
                    hinted_outcome, _hinted_detail = self._application_outcome_hint(
                        RegistrationOutcome.CAPTCHA_FAILED,
                        "captcha handler returned false",
                    )
                    if hinted_outcome is RegistrationOutcome.APPLICATION_RISK:
                        return False, "risk"
                    if hinted_outcome is RegistrationOutcome.TRANSPORT_ERROR:
                        return False, "transport_error"
                    if hinted_outcome is RegistrationOutcome.SERVICE_UNAVAILABLE:
                        return False, "service_unavailable"
                    if hinted_outcome is RegistrationOutcome.UI_ERROR:
                        return False, "ui_error"
                    return False, "captcha_still_active"
                self._clear_application_outcome_hint()
                continue
            if self.proofs_page_active(page):
                if self.dismiss_proofs_page(page):
                    print("[Info: Proofs] - 帐号已创建，跳过安全验证页（暂时跳过）")
                    try:
                        page.wait(1.5)
                    except Exception:
                        time.sleep(1.5)
                else:
                    # proofs page present but skip control missing or not
                    # advancing — some account states only offer frmAddProof.
                    print("[Warn: Proofs] - 安全验证页无法跳过（无 Skip 控件或未前进）")
                continue
            # ponytail: a blank/failed-load page (title "Problem loading page" /
            # empty body) means the proofs or mailbox page never rendered —
            # almost always a transient proxy hiccup AFTER the account was
            # already created. Reload the current url a bounded number of
            # times instead of bleeding the whole timeout and losing the
            # account to mailbox_timeout. Bounded by end_time, so worst case
            # is unchanged.
            if self._failed_load_detected(page):
                if reload_count < mailbox_reload_limit:
                    reload_count += 1
                    print(
                        f"[Info: MailboxWait] - blank/failed-load detected, "
                        f"reloading page ({reload_count}/{mailbox_reload_limit})"
                    )
                    if not self._reload_current(page):
                        return False, "transport_error"
                    continue
            try:
                page.wait(0.25)
            except Exception:
                time.sleep(0.25)
        current_url, page_title, preview = self.page_state_summary(page, preview_limit=220)
        shell_ready = self.mailbox_shell_ready(page)
        challenge_still_active = self.challenge_active(page)
        print(
            "[Debug: MailboxWait] - "
            f"shell_ready={shell_ready}; challenge_active={challenge_still_active}; "
            f"url={current_url}; title={page_title}; preview={preview}"
        )
        return False, "timeout"

    def open_registration_page(self, page, locators, timeout=30):
        last_error = None
        for attempt in range(self.entry_page_retries + 1):
            try:
                page.get(
                    "https://outlook.live.com/mail/0/?prompt=create_account",
                    wait="interactive",
                    timeout=timeout,
                )
                if self.wait_for_any(page, locators, timeout=timeout):
                    return True
                last_error = TimeoutError("entry elements not found")
            except Exception as error:
                last_error = error

            if attempt < self.entry_page_retries:
                try:
                    page.wait(1.0)
                except Exception:
                    time.sleep(1.0)

        if last_error:
            raise last_error
        return False

    def outlook_register(self, page, email, password):
        start_time = time.monotonic()
        self._clear_registration_result()
        self._clear_application_outcome_hint()

        try:
            fake = Faker()
            lastname = fake.last_name()
            firstname = fake.first_name()
            year = str(random.randint(1960, 2005))
            month = str(random.randint(1, 12))
            day = str(random.randint(1, 28))
        except Exception as error:
            return self._set_registration_result(
                RegistrationOutcome.UI_ERROR,
                detail=f"identity generation failed: {type(error).__name__}: {error}",
                stage="identity_generation",
                started_at=start_time,
            )

        email_input_locators = [
            "css:[aria-label='新建电子邮件']",
            "css:input[aria-label='新建电子邮件']",
            "css:input[name='MemberName']",
            "css:input[autocomplete='username']",
            "css:input[aria-label*='email' i]",
        ]
        consent_locators = [
            "xpath://button[contains(normalize-space(.), '同意并继续')]",
            "xpath://*[@role='button'][contains(normalize-space(.), '同意并继续')]",
            "xpath://span[contains(normalize-space(.), '同意并继续')]",
            "css:button[type='submit']",
        ]

        try:
            self.open_registration_page(
                page,
                consent_locators + email_input_locators,
                timeout=max(30, self.form_step_timeout),
            )

            # '个人数据导出许可' consent interstitial (zh-CN market). Must be
            # dismissed before the email input can appear — and the click must
            # be verified, because the portaled button is missed by coordinate
            # clicks. Keep this inside the try so a stuck consent page surfaces as
            # a diagnosable entry_page failure instead of falling through to an
            # uncaught 'email input not found' ui_error.
            if self._consent_page_active(page):
                self.human_pause(page)
                if not self._dismiss_consent_page(
                    page, email_input_locators, timeout=20
                ):
                    raise TimeoutError("consent page could not be dismissed")
        except Exception as e:
            current_url, page_title, preview = self.page_state_summary(page)
            diagnostics = []

            if self.service_unavailable_detected(page):
                diagnostics.append("命中服务维护提示")
            elif self.error_text_detected(page):
                diagnostics.append("命中风控提示")
            if self._consent_page_active(page):
                diagnostics.append("consent 页面卡住（同意并继续点击未生效）")
            if not self.wait_for_any(page, email_input_locators, timeout=1):
                diagnostics.append("未找到邮箱输入框")

            diagnostics_text = "；".join(diagnostics) if diagnostics else "无明确诊断"
            print(
                "[Error: Entry Page] - 无法进入注册界面: "
                f"{e}; url={current_url}; title={page_title}; "
                f"diagnostics={diagnostics_text}; preview={preview}"
            )
            outcome, detail, stage = self._classify_registration_error(
                e,
                page,
                "entry_page",
                page_title=page_title,
            )
            detail = f"{detail}; diagnostics={diagnostics_text}"
            return self._set_registration_result(
                outcome,
                detail=detail,
                stage=stage,
                started_at=start_time,
            )

        try:
            self.ensure_email_suffix_selected(page)

            email_input = self.wait_for_any(
                page, email_input_locators, timeout=self.form_step_timeout
            )
            if not email_input:
                raise TimeoutError("email input not found")
            self.human_pause(page)
            self.click_element(page, email_input)
            self.human_pause(page, 0.2, 0.5)
            email_input.input(email, clear=True)

            self.human_pause(page)
            if not self.click_primary_button(page):
                raise TimeoutError("email next button not found")

            # ponytail: same present-but-disabled Next race the password step
            # already guards against. Clicking Next on the email page can
            # no-op (client validation hasn't re-enabled the button), leaving
            # us on the email form while the code blindly waits for a password
            # field that never appears — flow-28776-000004's exact death.
            # Re-find the input, retype, resubmit once, then classify.
            EMAIL_ERROR_TEXTS = (
                "电子邮件地址无效", "电子邮件地址已", "此电子邮件", "Someone already has this email",
                "该电子邮件地址已被使用", "Enter a valid email", "isn't available",
            )
            # ponytail: signup.live.com A/B-tests the password step. The
            # common layout is a real input[type=password], but flow-21120-000009
            # hit a combobox-style 创建密码 variant whose preview shows no
            # password <input> — widen the locator set so we detect the step
            # even when the control is a button/fluent field, then click into
            # it to reveal the real input before typing.
            PASSWORD_INPUT_LOCATORS = [
                "css:input[type='password']",
                "css:input[name='Password']",
                "css:input[autocomplete='new-password']",
            ]
            PASSWORD_STEP_MARKERS = PASSWORD_INPUT_LOCATORS + [
                "css:[name='Password']",
                "xpath://*[@role='combobox' and (contains(@aria-label,'密码') or contains(@aria-label,'Password'))]",
                "xpath://*[contains(normalize-space(.),'创建密码') or contains(normalize-space(.),'Create a password')]",
            ]
            email_retried = False
            deadline = time.monotonic() + self.form_step_timeout
            password_input = False
            while time.monotonic() < deadline:
                password_input = self.wait_for_any(
                    page, PASSWORD_INPUT_LOCATORS, timeout=0.4
                )
                if password_input:
                    break
                still_on_email = self.wait_for_any(page, email_input_locators, timeout=0.3)
                if still_on_email and not email_retried:
                    email_retried = True
                    print("[Info: Email] - still on email page after submit; re-focusing and retrying once")
                    try:
                        self.click_element(page, still_on_email)
                        still_on_email.input(email, clear=True)
                        self.human_pause(page, 0.3, 0.5)
                    except Exception:
                        pass
                    if not self.click_primary_button(page):
                        raise TimeoutError("email next button not found on retry")
                    continue
                try:
                    page.wait(0.3)
                except Exception:
                    time.sleep(0.3)

            if not password_input:
                # Maybe the password step is present but as a combobox/fluent
                # field with no <input> yet. Click the step control to expand
                # it, then re-look for the real input once.
                step_marker = self.wait_for_any(page, PASSWORD_STEP_MARKERS[3:], timeout=2)
                if step_marker:
                    try:
                        self.click_element(page, step_marker)
                        self.human_pause(page, 0.3, 0.6)
                    except Exception:
                        pass
                    password_input = self.wait_for_any(
                        page, PASSWORD_INPUT_LOCATORS, timeout=3
                    )
            if not password_input:
                stuck = next(
                    (t for t in EMAIL_ERROR_TEXTS if self.text_exists(page, t)), None
                )
                if stuck:
                    raise _RegistrationFlowFailure(
                        RegistrationOutcome.UI_ERROR,
                        f"email submit rejected by page (inline error): {stuck}",
                        "email_step",
                    )
                raise TimeoutError("password input not found")
            self.human_pause(page)
            self.click_element(page, password_input)
            self.human_pause(page, 0.2, 0.5)
            password_input.input(password, clear=True)

            self.human_pause(page)
            if not self.click_primary_button(page):
                raise TimeoutError("password next button not found")

            # ponytail: the zh Next button is present-but-disabled until the
            # client re-validates; a no-op submit left us on the password page
            # and the old code blindly waited for BirthYear, then blamed birth
            # year for the password step's failure. Accept advancing to the
            # birthday page OR detect the inline password error and retry the
            # submit once; if still stuck, raise a clear password-step error.
            PASSWORD_ERROR_TEXTS = ("密码必须至少包含", "密码必须包含", "Password must")
            password_retried = False
            deadline = time.monotonic() + self.form_step_timeout
            birth_year_input = False
            while time.monotonic() < deadline:
                birth_year_input = self.wait_for_any(
                    page,
                    [
                        "css:[name='BirthYear']",
                        "css:input[name='BirthYear']",
                        "css:input[autocomplete='bday-year']",
                    ],
                    timeout=0.4,
                )
                if birth_year_input:
                    break
                stuck_on_password = any(
                    self.text_exists(page, t) for t in PASSWORD_ERROR_TEXTS
                )
                if stuck_on_password and not password_retried:
                    password_retried = True
                    print("[Info: Password] - still on password page after submit; re-focusing and retrying once")
                    pw = self.wait_for_any(
                        page,
                        [
                            "css:input[type='password']",
                            "css:input[name='Password']",
                            "css:input[autocomplete='new-password']",
                        ],
                        timeout=1.0,
                    )
                    if pw:
                        self.click_element(page, pw)
                        pw.input(password, clear=True)
                        self.human_pause(page, 0.3, 0.5)
                        if not self.click_primary_button(page):
                            raise _RegistrationFlowFailure(
                                RegistrationOutcome.UI_ERROR,
                                "password next button not found on retry",
                                "password_step",
                            )
                    continue
                try:
                    page.wait(0.3)
                except Exception:
                    time.sleep(0.3)

            if not birth_year_input:
                stuck = next(
                    (t for t in PASSWORD_ERROR_TEXTS if self.text_exists(page, t)), None
                )
                if stuck:
                    raise _RegistrationFlowFailure(
                        RegistrationOutcome.UI_ERROR,
                        f"password submit rejected by page (inline error): {stuck}",
                        "password_step",
                    )
                raise TimeoutError("birth year input not found")
            self.human_pause(page)
            birth_year_input.input(year, clear=True)

            birth_month = self.wait_for_any(
                page,
                [
                    "css:[name='BirthMonth']",
                    "css:[aria-label='月']",
                    "css:[aria-label='月份']",
                    "xpath://*[@role='combobox' and (@aria-label='月' or @aria-label='月份' or contains(@aria-label, 'BirthMonth'))]",
                ],
                timeout=self.form_step_timeout,
            )
            birth_day = self.wait_for_any(
                page,
                [
                    "css:[name='BirthDay']",
                    "css:[aria-label='日']",
                    "css:[aria-label='日期']",
                    "xpath://*[@role='combobox' and (@aria-label='日' or @aria-label='日期' or contains(@aria-label, 'BirthDay'))]",
                ],
                timeout=self.form_step_timeout,
            )
            if not birth_month or not birth_day:
                raise TimeoutError("birthday selects not found")

            self.human_pause(page, 0.2, 0.5)
            self.select_with_fallback(
                page,
                birth_month,
                month,
                [month, f"{month}月", f"{month} 月"],
                "BirthMonth",
            )
            self.human_pause(page, 0.2, 0.5)
            self.select_with_fallback(
                page,
                birth_day,
                day,
                [day, f"{day}日", f"{day} 号", f"{day} 日"],
                "BirthDay",
            )

            self.human_pause(page)
            if not self.click_primary_button(page):
                raise TimeoutError("birthday next button not found")

            last_name_input, first_name_input = self.wait_for_all(
                page,
                [
                    [
                        "css:#lastNameInput",
                        "css:input[name='LastName']",
                        "css:input[autocomplete='family-name']",
                    ],
                    [
                        "css:#firstNameInput",
                        "css:input[name='FirstName']",
                        "css:input[autocomplete='given-name']",
                    ],
                ],
                timeout=self.form_step_timeout,
            )
            if not last_name_input or not first_name_input:
                month_value, month_text = self.get_select_state(birth_month)
                day_value, day_text = self.get_select_state(birth_day)
                raise TimeoutError(
                    "name inputs not found after birthday submit; "
                    f"month_value={month_value}; month_text={month_text}; "
                    f"day_value={day_value}; day_text={day_text}"
                )

            self.human_pause(page)
            self.click_element(page, last_name_input)
            self.human_pause(page, 0.2, 0.5)
            last_name_input.input(lastname, clear=True)

            self.human_pause(page, 0.2, 0.5)
            first_name_input.input(firstname, clear=True)

            remaining_wait = self.wait_time - (time.monotonic() - start_time)
            if remaining_wait > 0:
                page.wait(remaining_wait)

            self.human_pause(page)
            if not self.click_primary_button(page):
                raise TimeoutError("profile submit button not found")

            page.wait(0.8)

            mailbox_ready = None
            reason = None

            if self.service_unavailable_detected(page):
                raise _RegistrationFlowFailure(
                    RegistrationOutcome.SERVICE_UNAVAILABLE,
                    "service unavailable after profile submit",
                    "post_submit",
                )

            if self.error_text_detected(page):
                print("[Error: IP or browser] - 当前 IP 注册频率过快，或浏览器环境已被风控。")
                raise _RegistrationFlowFailure(
                    RegistrationOutcome.APPLICATION_RISK,
                    "risk message detected after profile submit",
                    "post_submit",
                )

            if self.challenge_active(page):
                self._clear_application_outcome_hint()
                try:
                    if not self.handle_captcha(page):
                        if self.service_unavailable_detected(page):
                            outcome, detail = (
                                RegistrationOutcome.SERVICE_UNAVAILABLE,
                                "service unavailable while handling captcha",
                            )
                        elif self.error_text_detected(page):
                            outcome, detail = (
                                RegistrationOutcome.APPLICATION_RISK,
                                "risk message detected while handling captcha",
                            )
                        else:
                            outcome, detail = self._application_outcome_hint(
                                RegistrationOutcome.CAPTCHA_FAILED,
                                "captcha handler returned false",
                            )
                        raise _RegistrationFlowFailure(
                            outcome,
                            detail,
                            "captcha",
                        )
                    self._clear_application_outcome_hint()
                except Exception as e:
                    if self.mailbox_url_ready(page):
                        mailbox_ready, reason = True, "mailbox_url_ready_after_captcha"
                    else:
                        if isinstance(e, _RegistrationFlowFailure):
                            raise
                        outcome, detail, _stage = self._classify_registration_error(
                            e, page, "captcha"
                        )
                        hinted_outcome, hinted_detail = self._application_outcome_hint(
                            outcome,
                            detail,
                        )
                        raise _RegistrationFlowFailure(
                            hinted_outcome,
                            hinted_detail,
                            "captcha",
                        ) from e

            if mailbox_ready is True:
                pass
            elif self.mailbox_url_ready(page):
                mailbox_ready, reason = True, "mailbox_url_ready"
            elif self.mailbox_shell_ready(page):
                mailbox_ready, reason = True, "mailbox_shell_ready"
            else:
                mailbox_ready, reason = self.wait_for_mailbox_ready(page)
            if not mailbox_ready:
                mailbox_outcomes = {
                    "risk": RegistrationOutcome.APPLICATION_RISK,
                    "captcha_still_active": RegistrationOutcome.CAPTCHA_FAILED,
                    "timeout": RegistrationOutcome.MAILBOX_TIMEOUT,
                    "service_unavailable": RegistrationOutcome.SERVICE_UNAVAILABLE,
                    "transport_error": RegistrationOutcome.TRANSPORT_ERROR,
                    "ui_error": RegistrationOutcome.UI_ERROR,
                }
                hinted_outcome, hinted_detail = self._application_outcome_hint(
                    mailbox_outcomes.get(reason, RegistrationOutcome.MAILBOX_TIMEOUT),
                    f"mailbox not ready; state={reason or 'unknown'}",
                )
                raise _RegistrationFlowFailure(
                    hinted_outcome,
                    hinted_detail,
                    "mailbox_wait",
                )
        except Exception as e:
            current_url, page_title, preview = self.page_state_summary(page)
            print(
                "[Error: Register Flow] - "
                f"{e}; url={current_url}; title={page_title}; preview={preview}"
            )
            outcome, detail, stage = self._classify_registration_error(
                e,
                page,
                "registration_form",
                page_title=page_title,
            )
            return self._set_registration_result(
                outcome,
                detail=detail,
                stage=stage,
                started_at=start_time,
            )

        full_email = f"{email}{self.email_suffix}"
        persistence_warning = ""
        try:
            self.persist_registered_account(full_email, password)
        except Exception as error:
            # Registration has already completed; persistence errors must not
            # turn a valid account into a scheduler failure.
            print(f"[Warn: Results] - registered account persistence failed: {error}")
            persistence_warning = f"; persistence_warning={type(error).__name__}: {error}"
        print(f"[Success: Email Registration] - {full_email}")

        return self._set_registration_result(
            RegistrationOutcome.SUCCESS,
            detail=f"{reason or 'registration_completed'}{persistence_warning}",
            stage="completed",
            started_at=start_time,
        )
