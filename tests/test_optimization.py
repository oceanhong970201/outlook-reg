import builtins
import concurrent.futures
import io
import threading
import unittest
from unittest.mock import Mock, patch

import main
import get_token
import utils
from controllers.base_controller import BaseBrowserController
from controllers.ruyipage_controller import RuyiPageController


class DummyController(BaseBrowserController):
    def launch_browser(self, proxy_config=None):
        return False, False

    def handle_captcha(self, page):
        return False


class FlakyOwner:
    def __init__(self, result):
        self.calls = 0
        self.result = result

    def ele(self, locator, timeout=0.2):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient navigation")
        return self.result

    def wait(self, seconds):
        return None


class FailingActions:
    def move_to(self, *args, **kwargs):
        raise RuntimeError("coordinate action failed")


class ClickOwner:
    actions = FailingActions()


class ClickElement:
    def __init__(self):
        self.clicked = False

    def click(self):
        self.clicked = True


class NavigationPage:
    def __init__(self):
        self.get_calls = 0

    def get(self, *args, **kwargs):
        self.get_calls += 1
        if self.get_calls == 1:
            raise RuntimeError("temporary load failure")

    def ele(self, locator, timeout=0.2):
        return object()

    def wait(self, seconds):
        return None


class OptimizationTests(unittest.TestCase):
    def setUp(self):
        utils.reset_bad_proxies(clear_active=True)
        main.stop_event.clear()
        main.status_bar.submitted = 0
        main.status_bar.succeeded = 0
        main.status_bar.failed = 0
        main.status_bar.oauth_succeeded = 0
        main.status_bar.oauth_failed = 0

    def tearDown(self):
        utils.reset_bad_proxies(clear_active=True)

    def test_password_length_is_chosen_per_call(self):
        with patch.object(utils.random, "randint", side_effect=[11, 15]) as randint:
            passwords = [utils.generate_strong_password(), utils.generate_strong_password()]

        self.assertEqual([len(password) for password in passwords], [11, 15])
        self.assertEqual(randint.call_count, 2)

    def test_recently_healthy_proxy_skips_repeated_connectivity_tests(self):
        proxy = utils.build_proxy_config("127.0.0.1", "8080", "user", "pass")
        source = {
            "proxy_source": "file",
            "proxy_file": "unused.txt",
            "proxy_api_url": "",
            "proxy_api_timeout": 1,
        }
        config = {
            "proxy_health_ttl": 120,
            "proxy_failure_cooldown": 30,
        }

        with (
            patch.object(utils, "get_proxy_candidates", return_value=[proxy]),
            patch.object(utils, "get_proxy_test_targets", return_value=(["https://example.invalid"], 1)),
            patch.object(utils, "get_proxy_source_config", return_value=source),
            patch.object(utils, "get_config", return_value=config),
            patch.object(utils, "test_proxy_connectivity", return_value=True) as connectivity,
        ):
            first = utils.get_working_proxy(reserve=True)
            utils.release_proxy(first)
            second = utils.get_working_proxy(reserve=True)
            utils.release_proxy(second)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        connectivity.assert_called_once()

    def test_api_refresh_failure_keeps_last_successful_batch(self):
        proxy = utils.build_proxy_config("127.0.0.1", "8080")
        source = {
            "proxy_source": "api",
            "proxy_file": "unused.txt",
            "proxy_api_url": "https://proxy.example/list",
            "proxy_api_timeout": 1,
        }
        with (
            patch.object(utils, "get_proxy_source_config", return_value=source),
            patch.object(utils, "load_proxies_from_api", side_effect=[[proxy], None]),
        ):
            self.assertEqual(utils.get_proxy_candidates(proxy_source="api"), [proxy])
            self.assertEqual(
                utils.get_proxy_candidates(proxy_source="api", force_refresh=True),
                [proxy],
            )

    def test_concurrent_forced_api_refreshes_are_coalesced(self):
        proxy = utils.build_proxy_config("127.0.0.1", "8080")
        source = {
            "proxy_source": "api",
            "proxy_file": "unused.txt",
            "proxy_api_url": "https://proxy.example/list",
            "proxy_api_timeout": 1,
        }
        calls = []

        class BarrierLock:
            def __init__(self, parties):
                self._barrier = threading.Barrier(parties)
                self._lock = threading.Lock()

            def __enter__(self):
                self._barrier.wait(timeout=2)
                self._lock.acquire()
                return self

            def __exit__(self, *_args):
                self._lock.release()

        def load_batch(*_args):
            calls.append(1)
            return [proxy]

        with (
            patch.object(utils, "get_proxy_source_config", return_value=source),
            patch.object(utils, "load_proxies_from_api", side_effect=load_batch),
            patch.object(utils, "_proxy_refresh_lock", BarrierLock(4)),
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = [
                    executor.submit(
                        utils.get_proxy_candidates,
                        proxy_source="api",
                        force_refresh=True,
                    )
                    for _ in range(4)
                ]
                batches = [future.result(timeout=2) for future in futures]

        self.assertEqual(len(calls), 1)
        self.assertEqual(batches, [[proxy]] * 4)

    def test_api_lease_refreshes_when_cached_proxy_is_busy(self):
        first_proxy = utils.build_proxy_config("127.0.0.1", "8080", "a", "a")
        second_proxy = utils.build_proxy_config("127.0.0.1", "8080", "b", "b")
        source = {
            "proxy_source": "api",
            "proxy_file": "unused.txt",
            "proxy_api_url": "https://proxy.example/lease",
            "proxy_api_timeout": 1,
            "proxy_api_lease_mode": True,
        }
        config = {
            "proxy_health_ttl": 120,
            "proxy_failure_cooldown": 30,
        }
        with (
            patch.object(utils, "get_proxy_source_config", return_value=source),
            patch.object(
                utils,
                "load_proxies_from_api",
                side_effect=[[first_proxy], [second_proxy]],
            ) as load,
            patch.object(utils, "get_proxy_test_targets", return_value=([], 1)),
            patch.object(utils, "test_proxy_connectivity", return_value=True),
            patch.object(utils, "get_config", return_value=config),
        ):
            assigned = utils.get_working_proxy(reserve=True)
            next_assigned = utils.get_working_proxy(reserve=True)
            utils.release_proxy(assigned)
            utils.release_proxy(next_assigned)

        self.assertEqual(assigned, first_proxy)
        self.assertEqual(next_assigned, second_proxy)
        self.assertEqual(load.call_count, 2)

    def test_oauth_retry_converts_listener_exception_to_failure_tuple(self):
        with patch.object(get_token, "_try_get_access_token", side_effect=RuntimeError):
            self.assertEqual(
                get_token.get_access_token(object(), "name", max_retries=1),
                (False, False, False),
            )

    def test_oauth_redirect_parser_requires_matching_origin_and_path(self):
        self.assertEqual(
            get_token._authorization_result(
                "http://localhost:8000/?code=abc",
                "http://localhost:8000",
            ),
            ("abc", None),
        )
        self.assertEqual(
            get_token._authorization_result(
                "http://localhost.evil:8000/?code=abc",
                "http://localhost:8000",
            ),
            (None, None),
        )

    def test_cleanup_retains_live_browser_and_tracks_orphan_profiles(self):
        class Page:
            pass

        cases = (
            (False, False, True, False),
            (False, True, True, False),
            (True, False, False, True),
            (True, True, False, True),
        )
        for browser_closed, profile_removed, retained, released in cases:
            controller = object.__new__(DummyController)
            controller.cleanup_lock = threading.Lock()
            controller.active_resources = {}
            controller.orphan_profile_dirs = set()
            page = Page()
            controller.active_resources[id(page)] = {
                "page": page,
                "profile_dir": "profile-dir",
                "proxy_config": {"server": "proxy", "username": "", "password": ""},
            }
            with (
                patch.object(controller, "_quit_page", return_value=browser_closed),
                patch.object(
                    controller, "_remove_profile_dir", return_value=profile_removed
                ),
                patch("controllers.base_controller.release_proxy") as release,
                patch("builtins.print"),
            ):
                controller.clean_up(page, "done_browser")

            self.assertEqual(id(page) in controller.active_resources, retained)
            self.assertEqual(bool(release.called), released)
            if browser_closed and not profile_removed:
                self.assertIn("profile-dir", controller.orphan_profile_dirs)

    def test_failed_proxy_is_not_retested_immediately(self):
        proxy = utils.build_proxy_config("127.0.0.1", "8080", "user", "pass")
        source = {
            "proxy_source": "file",
            "proxy_file": "unused.txt",
            "proxy_api_url": "",
            "proxy_api_timeout": 1,
        }
        config = {
            "proxy_health_ttl": 120,
            "proxy_failure_cooldown": 30,
        }

        with (
            patch.object(utils, "get_proxy_candidates", return_value=[proxy]),
            patch.object(utils, "get_proxy_test_targets", return_value=(["https://example.invalid"], 1)),
            patch.object(utils, "get_proxy_source_config", return_value=source),
            patch.object(utils, "get_config", return_value=config),
            patch.object(utils, "test_proxy_connectivity", return_value=False) as connectivity,
            patch("builtins.print"),
        ):
            self.assertIsNone(utils.get_working_proxy(reserve=True))
            self.assertIsNone(utils.get_working_proxy(reserve=True))

        connectivity.assert_called_once()

    def test_wait_for_any_ignores_transient_navigation_errors(self):
        expected = object()
        owner = FlakyOwner(expected)
        controller = object.__new__(DummyController)

        result = controller.wait_for_any(owner, ["css:#field"], timeout=0.05)

        self.assertIs(result, expected)

    def test_entry_page_retries_before_form_submission(self):
        controller = object.__new__(DummyController)
        controller.entry_page_retries = 1
        page = NavigationPage()

        self.assertTrue(controller.open_registration_page(page, ["css:#email"], timeout=0.05))
        self.assertEqual(page.get_calls, 2)

    def test_click_element_falls_back_to_direct_click(self):
        controller = object.__new__(DummyController)
        element = ClickElement()

        controller.click_element(ClickOwner(), element)

        self.assertTrue(element.clicked)

    def test_non_tty_status_bar_does_not_start_animation(self):
        bar = main.StatusBar()
        stdout = io.StringIO()
        stderr = io.StringIO()
        original_print = builtins.print

        with patch.object(main.sys, "stdout", stdout), patch.object(main.sys, "stderr", stderr):
            bar.start()
            refresh_thread = bar._refresh_thread
            print_was_patched = builtins.print is not original_print
            bar.stop()

        self.assertIsNone(refresh_thread)
        self.assertFalse(print_was_patched)

    def test_worker_count_is_limited_by_proxy_capacity(self):
        self.assertEqual(main.effective_worker_count(10, 3), 3)
        self.assertEqual(main.effective_worker_count(3, 10), 3)
        self.assertEqual(main.effective_worker_count(4, 0), 4)

    def test_scheduler_completes_all_bounded_tasks(self):
        controller = type("Controller", (), {"enable_oauth2": False})()
        result = {"success": True, "oauth_success": False}
        scheduler_config = {
            "scheduler_poll_interval": 0.05,
            "launch_stagger": 0,
        }

        with (
            patch.object(main, "get_proxy_source_config", return_value={"proxy_source": "file"}),
            patch.object(main, "get_proxy_capacity", return_value=2),
            patch.object(main, "get_config", return_value=scheduler_config),
            patch.object(main, "process_single_flow", return_value=result) as process,
        ):
            main.run_concurrent_flows(controller, concurrent_flows=4, max_tasks=5)

        self.assertEqual(process.call_count, 5)
        self.assertEqual(main.status_bar.submitted, 5)
        self.assertEqual(main.status_bar.succeeded, 5)
        self.assertEqual(main.status_bar.failed, 0)

    def test_oauth_exception_does_not_erase_registration_success(self):
        page = object()

        class Controller:
            enable_oauth2 = True
            email_suffix = "@example.com"

            def get_thread_page(self, _proxy):
                return page

            def outlook_register(self, _page, _email, _password):
                return True

            def get_page_proxy_config(self, _page):
                return None

            def clean_up(self, _page, _type):
                return None

        with (
            patch.object(main, "random_email", return_value="account"),
            patch.object(main, "generate_strong_password", return_value="Password1!"),
            patch.object(main, "get_access_token", side_effect=RuntimeError("listener")),
            patch("builtins.print"),
        ):
            result = main.process_single_flow(Controller())

        self.assertEqual(result, {"success": True, "oauth_success": False})

    def test_transport_failure_relaunches_once_without_retrying_ui_failure(self):
        first_page = object()
        second_page = object()

        class TypedResult:
            def __init__(self, outcome, detail):
                self.outcome = outcome
                self.failure_class = outcome if outcome != "success" else None
                self.detail = detail

        class Controller:
            enable_oauth2 = False
            email_suffix = "@example.com"

            def __init__(self, final_outcome):
                self.pages = [first_page, second_page]
                self.final_outcome = final_outcome
                self.calls = 0
                self.result = None
                self.clean_up = Mock()
                self.report_application_outcome = Mock()

            def get_thread_page(self, _proxy, flow_id=None):
                del flow_id
                return self.pages.pop(0)

            def outlook_register(self, _page, _email, _password):
                self.calls += 1
                if self.calls == 1:
                    self.result = TypedResult(
                        "transport_error", "browser_disconnected"
                    )
                    return False
                self.result = TypedResult(self.final_outcome, self.final_outcome)
                return self.final_outcome == "success"

            def get_last_registration_result(self):
                return self.result

            def reset_registration_result(self):
                self.result = None

            def get_page_proxy_config(self, _page):
                return None

        with (
            patch.object(main, "random_email", return_value="account"),
            patch.object(main, "generate_strong_password", return_value="Password1!"),
            patch("builtins.print"),
        ):
            transport_controller = Controller("success")
            result = main.process_single_flow(transport_controller)

        self.assertEqual(result, {"success": True, "oauth_success": False})
        self.assertEqual(transport_controller.calls, 2)
        transport_controller.clean_up.assert_any_call(first_page, "done_browser")

        with (
            patch.object(main, "random_email", return_value="account"),
            patch.object(main, "generate_strong_password", return_value="Password1!"),
            patch("builtins.print"),
        ):
            ui_controller = Controller("ui_error")
            ui_controller.result = TypedResult("ui_error", "selector missing")

            def ui_failure(_page, _email, _password):
                ui_controller.calls += 1
                ui_controller.result = TypedResult("ui_error", "selector missing")
                return False

            ui_controller.outlook_register = ui_failure
            result = main.process_single_flow(ui_controller)

        self.assertEqual(result, {"success": False, "oauth_success": False})
        self.assertEqual(ui_controller.calls, 1)

    def test_completed_registration_state_clears_challenge(self):
        controller = object.__new__(RuyiPageController)
        with (
            patch.object(controller, "mailbox_url_ready", return_value=False),
            patch.object(controller, "proofs_page_active", return_value=True),
            patch.object(controller, "_challenge_active", return_value=True),
        ):
            self.assertTrue(controller._challenge_cleared(object()))

    def test_public_challenge_detector_uses_ruyipage_detector(self):
        controller = object.__new__(RuyiPageController)
        page = object()
        with patch.object(controller, "_challenge_active", return_value=True) as detector:
            self.assertTrue(controller.challenge_active(page))
        detector.assert_called_once_with(page)

    def test_combobox_option_click_uses_nearest_scroll_not_center(self):
        # ponytail: regression for day=8 -> 9. The sticky listbox header made
        # block:'center' land one row low. Assert the scroll script is now
        # 'nearest' so the trusted click hits the exact option.
        controller = object.__new__(DummyController)
        scroll_scripts = []

        class Option:
            def click(self):
                pass

        def fake_el_run_js(_element, script_body):
            if "scrollIntoView" in script_body:
                scroll_scripts.append(script_body)
            return None

        with patch.object(controller, "_el_run_js", side_effect=fake_el_run_js):
            controller._click_combobox_option(object(), Option())

        self.assertTrue(scroll_scripts, "scrollIntoView was never called")
        self.assertTrue(
            all("nearest" in s and "center" not in s for s in scroll_scripts),
            f"expected nearest scroll, got {scroll_scripts}",
        )

    def test_combobox_select_uses_keyboard_path_first_on_virtualized_fluent(self):
        # ponytail: the real failure (run_20260724_202918) was Fluent UI's
        # virtualized day/month listbox — clicking the DOM role='option' node
        # dispatched a trusted click but React re-resolved the value, so day=8
        # landed on 9. The fix routes through keyboard input (type value + Enter)
        # BEFORE the DOM-option click fallback. Assert the keyboard path is
        # tried first and, on success, the DOM fallback is never reached.
        controller = object.__new__(DummyController)
        control = Mock()
        keyboard_called = {"count": 0}

        def fake_keyboard_select(_page, _control, value, _verify):
            keyboard_called["count"] += 1
            return True  # keyboard path verifies OK

        controller.click_element = Mock()
        controller.human_pause = Mock(return_value=None)
        controller._combobox_selected_text = Mock()
        controller._combobox_keyboard_select = Mock(side_effect=fake_keyboard_select)
        controller._click_combobox_option = Mock()
        controller.wait_for_any = Mock()

        result = controller._select_combobox(
            object(), control, "8", ["8", "8日", "8 号"], "BirthDay"
        )

        self.assertTrue(result)
        self.assertEqual(keyboard_called["count"], 1)
        controller._click_combobox_option.assert_not_called()  # DOM fallback skipped

    def test_combobox_select_falls_back_to_dom_click_when_keyboard_unavailable(self):
        # keyboard path returns False (no BiDi actions on the page mock) ->
        # fall through to the DOM-option click + verify, which succeeds.
        controller = object.__new__(DummyController)
        control = Mock()
        option = Mock()
        control_text_values = iter(["9日", "8日"])
        controller.click_element = Mock()
        controller.human_pause = Mock(return_value=None)
        controller._combobox_selected_text = Mock(side_effect=lambda _el: next(control_text_values))
        controller._combobox_keyboard_select = Mock(return_value=False)
        controller._click_combobox_option = Mock()
        controller.wait_for_any = Mock(return_value=option)
        with patch.object(controller, "_dump_combobox_diagnostics") as dump:
            result = controller._select_combobox(
                object(), control, "8", ["8", "8日", "8 号"], "BirthDay"
            )
        self.assertTrue(result)
        # option clicked twice: initial + retry re-click
        self.assertEqual(option.click.call_count, 1)
        self.assertEqual(controller._click_combobox_option.call_count, 1)
        dump.assert_not_called()


if __name__ == "__main__":
    unittest.main()
