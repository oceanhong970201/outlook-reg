import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
from controllers.base_controller import BaseBrowserController


class PersistenceController(BaseBrowserController):
    def launch_browser(self, proxy_config=None):
        return False, False

    def handle_captcha(self, page):
        return False


class FormElement:
    def input(self, value, clear=False):
        return None


class FormPage:
    def wait(self, seconds):
        return None


class FlowController:
    enable_oauth2 = True
    email_suffix = "@outlook.com"

    def __init__(self, promote_side_effect=None):
        self.page = object()
        self.promote_registered_account = Mock(side_effect=promote_side_effect)
        self.clean_up = Mock()

    def get_thread_page(self, assigned_proxy=None):
        return self.page

    def outlook_register(self, page, email, password):
        return True

    def get_page_proxy_config(self, page):
        return {"requests_url": "http://proxy.invalid:8080"}


class OAuthPersistenceTests(unittest.TestCase):
    def make_persistence_controller(self, results_dir):
        controller = object.__new__(PersistenceController)
        controller.results_dir = str(results_dir)
        return controller

    def test_registered_account_stays_unlogged_until_promoted(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            results_dir = Path(temporary_dir)
            controller = self.make_persistence_controller(results_dir)
            record = "user@outlook.com: Secret123!\n"

            controller.persist_registered_account(
                "user@outlook.com", "Secret123!"
            )

            self.assertEqual(
                (results_dir / "unlogged_email.txt").read_text(encoding="utf-8"),
                record,
            )
            self.assertFalse((results_dir / "logged_email.txt").exists())

            controller.promote_registered_account(
                "user@outlook.com", "Secret123!"
            )

            self.assertEqual(
                (results_dir / "logged_email.txt").read_text(encoding="utf-8"),
                record,
            )
            self.assertEqual(
                (results_dir / "unlogged_email.txt").read_text(encoding="utf-8"),
                "",
            )

    def test_pending_record_survives_failed_atomic_rewrite(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            results_dir = Path(temporary_dir)
            controller = self.make_persistence_controller(results_dir)
            record = "user@outlook.com: Secret123!\n"
            controller.persist_registered_account(
                "user@outlook.com", "Secret123!"
            )

            with patch(
                "controllers.base_controller.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaises(OSError):
                    controller.promote_registered_account(
                        "user@outlook.com", "Secret123!"
                    )

            self.assertEqual(
                (results_dir / "unlogged_email.txt").read_text(encoding="utf-8"),
                record,
            )
            self.assertEqual(
                (results_dir / "logged_email.txt").read_text(encoding="utf-8"),
                record,
            )
            self.assertEqual(list(results_dir.glob(".unlogged-*.tmp")), [])

    def test_registration_persistence_error_does_not_change_success(self):
        controller = object.__new__(PersistenceController)
        controller.wait_time = 0
        controller.form_step_timeout = 0.01
        controller.email_suffix = "@outlook.com"
        element = FormElement()

        with (
            patch.object(controller, "open_registration_page", return_value=True),
            patch.object(controller, "text_exists", return_value=False),
            patch.object(controller, "ensure_email_suffix_selected", return_value=True),
            patch.object(controller, "wait_for_any", return_value=element),
            patch.object(controller, "wait_for_all", return_value=(element, element)),
            patch.object(controller, "human_pause"),
            patch.object(controller, "click_element"),
            patch.object(controller, "click_primary_button", return_value=True),
            patch.object(controller, "select_with_fallback", return_value=True),
            patch.object(controller, "error_text_detected", return_value=False),
            patch.object(controller, "challenge_active", return_value=False),
            patch.object(controller, "mailbox_url_ready", return_value=True),
            patch.object(
                controller,
                "persist_registered_account",
                side_effect=OSError("disk full"),
            ),
            patch("builtins.print") as print_mock,
        ):
            result = controller.outlook_register(
                FormPage(), "user123", "Secret123!"
            )

        self.assertTrue(result)
        self.assertTrue(
            any(
                "registered account persistence failed" in str(call)
                for call in print_mock.call_args_list
            )
        )

    def test_token_is_persisted_before_account_is_promoted(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            results_dir = Path(temporary_dir) / "Results"
            results_dir.mkdir()
            token_path = results_dir / "outlook_token.txt"

            def assert_token_exists(email, password):
                self.assertEqual(email, "user123@outlook.com")
                self.assertEqual(password, "Secret123!")
                self.assertEqual(
                    token_path.read_text(encoding="utf-8"),
                    "user123@outlook.com----Secret123!----client-id----refresh-token\n",
                )

            controller = FlowController(promote_side_effect=assert_token_exists)
            with (
                patch.object(main, "PROJECT_ROOT", temporary_dir),
                patch.object(main, "random_email", return_value="user123"),
                patch.object(
                    main, "generate_strong_password", return_value="Secret123!"
                ),
                patch.object(
                    main,
                    "get_config",
                    return_value={"oauth2": {"client_id": "client-id"}},
                ),
                patch.object(
                    main,
                    "get_access_token",
                    return_value=("refresh-token", "access-token", 123.0),
                ),
                patch("builtins.print"),
            ):
                result = main.process_single_flow(controller)

            self.assertEqual(result, {"success": True, "oauth_success": True})
            controller.promote_registered_account.assert_called_once_with(
                "user123@outlook.com", "Secret123!"
            )

    def test_token_persistence_error_keeps_registration_success(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            (Path(temporary_dir) / "Results").mkdir()
            controller = FlowController()

            with (
                patch.object(main, "PROJECT_ROOT", temporary_dir),
                patch.object(main, "random_email", return_value="user123"),
                patch.object(
                    main, "generate_strong_password", return_value="Secret123!"
                ),
                patch.object(
                    main,
                    "get_config",
                    return_value={"oauth2": {"client_id": "client-id"}},
                ),
                patch.object(
                    main,
                    "get_access_token",
                    return_value=("refresh-token", "access-token", 123.0),
                ),
                patch.object(main.os, "fsync", side_effect=OSError("disk error")),
                patch("builtins.print"),
            ):
                result = main.process_single_flow(controller)

            self.assertEqual(result, {"success": True, "oauth_success": False})
            controller.promote_registered_account.assert_not_called()


if __name__ == "__main__":
    unittest.main()
