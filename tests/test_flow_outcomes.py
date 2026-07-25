import threading
import unittest
from unittest.mock import patch

import main
from controllers.base_controller import BaseBrowserController
from flow_types import RegistrationOutcome


class FormElement:
    def input(self, value, clear=False):
        del value, clear


class FormPage:
    def wait(self, seconds):
        del seconds


class RegistrationHarness(BaseBrowserController):
    """Exercise registration orchestration without browser or selector details."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.wait_time = 0
        self.form_step_timeout = 0.01
        self.email_suffix = "@example.test"
        self._registration_state = threading.local()
        self.element = FormElement()

    def launch_browser(self, proxy_config=None):
        del proxy_config
        return False, False

    def handle_captcha(self, page):
        del page
        return False

    def open_registration_page(self, page, locators, timeout=30):
        del page, locators, timeout
        if self.scenario == "transport_error":
            raise ConnectionResetError("connection reset")
        return True

    def ensure_email_suffix_selected(self, page):
        del page
        if self.scenario == "ui_error":
            raise TimeoutError("email input not found")
        return True

    def text_exists(self, page, text):
        del page, text
        return False

    def wait_for_any(self, owner, locators, timeout=5):
        del owner, locators, timeout
        return self.element

    def wait_for_all(self, owner, locator_groups, timeout=5):
        del owner, locator_groups, timeout
        return self.element, self.element

    def human_pause(self, page, min_seconds=None, max_seconds=None):
        del page, min_seconds, max_seconds

    def click_element(self, owner, element):
        del owner, element

    def click_primary_button(self, page, timeout=5):
        del page, timeout
        return True

    def select_with_fallback(
        self,
        page,
        select_element,
        value,
        text_candidates,
        field_name,
    ):
        del page, select_element, value, text_candidates, field_name
        return True

    def error_text_detected(self, page):
        del page
        return self.scenario == "application_risk"

    def service_unavailable_detected(self, page):
        del page
        return self.scenario == "service_unavailable"

    def challenge_active(self, page):
        del page
        return self.scenario == "captcha_failed"

    def mailbox_url_ready(self, page):
        del page
        return False

    def mailbox_shell_ready(self, page):
        del page
        return False

    def wait_for_mailbox_ready(self, page, timeout=None):
        del page, timeout
        if self.scenario == "success":
            return True, "mailbox_ready"
        return False, "timeout"

    def page_state_summary(self, page, preview_limit=500):
        del page, preview_limit
        return "test://page", "test", ""

    def persist_registered_account(self, email, password):
        del email, password


class RegistrationOutcomeTests(unittest.TestCase):
    def test_registration_outcomes_are_typed_while_return_value_stays_bool(self):
        scenarios = {
            "success": (True, "completed"),
            "application_risk": (False, "post_submit"),
            "captcha_failed": (False, "captcha"),
            "mailbox_timeout": (False, "mailbox_wait"),
            "service_unavailable": (False, "post_submit"),
            "ui_error": (False, "registration_form"),
            "transport_error": (False, "entry_page"),
        }

        for outcome, (expected_success, expected_stage) in scenarios.items():
            with self.subTest(outcome=outcome):
                controller = RegistrationHarness(outcome)
                with patch("builtins.print"):
                    result = controller.outlook_register(
                        FormPage(), "account", "Password1!"
                    )
                typed_result = controller.get_last_registration_result()

                self.assertIs(type(result), bool)
                self.assertIs(result, expected_success)
                self.assertEqual(typed_result.outcome.value, outcome)
                self.assertEqual(typed_result.stage, expected_stage)
                self.assertIs(bool(typed_result), expected_success)
                self.assertEqual(
                    controller.registration_outcome_snapshot()["failure_class"],
                    None if expected_success else outcome,
                )

    def test_registration_result_is_isolated_per_worker_thread(self):
        controller = RegistrationHarness("success")
        barrier = threading.Barrier(2)
        observed = {}

        def record(name, outcome):
            controller._set_registration_result(outcome, detail=name, stage="test")
            barrier.wait(timeout=1)
            observed[name] = controller.registration_outcome_snapshot()

        threads = [
            threading.Thread(
                target=record,
                args=("risk", RegistrationOutcome.APPLICATION_RISK),
            ),
            threading.Thread(
                target=record,
                args=("success", RegistrationOutcome.SUCCESS),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(observed["risk"]["outcome"], "application_risk")
        self.assertEqual(observed["risk"]["detail"], "risk")
        self.assertEqual(observed["success"]["outcome"], "success")
        self.assertEqual(observed["success"]["detail"], "success")

    def test_main_preserves_bool_only_custom_controller_fallback(self):
        bool_only_controller = object()

        self.assertEqual(
            main._registration_metadata(bool_only_controller, True),
            (True, "success", None, ""),
        )
        self.assertEqual(
            main._registration_metadata(bool_only_controller, False),
            (False, "application_error", None, ""),
        )

    def test_maintenance_message_is_not_classified_as_application_risk(self):
        controller = RegistrationHarness("service_unavailable")

        outcome, detail, stage = controller._classify_registration_error(
            TimeoutError("entry elements not found"), FormPage(), "entry_page"
        )

        self.assertIs(outcome, RegistrationOutcome.SERVICE_UNAVAILABLE)
        self.assertIn("service unavailable", detail)
        self.assertEqual(stage, "entry_page")

    def test_browser_transport_messages_are_not_classified_as_ui_errors(self):
        controller = RegistrationHarness("success")
        cases = {
            "WebSocket 连接未建立": "browser_disconnected",
            "[WinError 10054] connection forcibly closed": "connection_reset",
            "browser has been disconnected": "browser_disconnected",
        }

        for message, expected_class in cases.items():
            with self.subTest(message=message):
                outcome, detail, stage = controller._classify_registration_error(
                    RuntimeError(message), FormPage(), "entry_page"
                )

                self.assertIs(outcome, RegistrationOutcome.TRANSPORT_ERROR)
                self.assertIn(expected_class, detail)
                self.assertEqual(stage, "entry_page")

    def test_problem_loading_page_is_classified_as_browser_transport_error(self):
        controller = RegistrationHarness("success")

        outcome, detail, stage = controller._classify_registration_error(
            TimeoutError("entry elements not found"),
            FormPage(),
            "entry_page",
            page_title="Problem loading page",
        )

        self.assertIs(outcome, RegistrationOutcome.TRANSPORT_ERROR)
        self.assertIn("browser_load_error", detail)
        self.assertEqual(stage, "entry_page")


if __name__ == "__main__":
    unittest.main()
