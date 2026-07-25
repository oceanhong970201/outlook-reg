import threading
import unittest
import os
import tempfile
from unittest.mock import patch

from controllers.base_controller import BaseBrowserController
from utils import build_proxy_config


class Page:
    def __init__(self):
        self.quit_calls = 0

    def quit(self, timeout=5, force=True):
        self.quit_calls += 1


class MetadataOnlyController(BaseBrowserController):
    def launch_browser(self, proxy_config=None):
        return self.test_page, {"profile_dir": None}

    def handle_captcha(self, page):
        return False


class ResourceCleanupTests(unittest.TestCase):
    def test_base_controller_anchors_cleanup_to_returned_page(self):
        controller = object.__new__(MetadataOnlyController)
        controller.proxy_acquire_timeout = 0
        controller.cleanup_lock = threading.Lock()
        controller.active_resources = {}
        controller.orphan_profile_dirs = set()
        controller.test_page = Page()
        proxy = build_proxy_config("127.0.0.1", "8401")

        with (
            patch(
                "controllers.base_controller.get_working_proxy",
                return_value=proxy,
            ),
            patch("controllers.base_controller.release_proxy") as release,
            patch("builtins.print"),
        ):
            page = controller.get_thread_page()
            controller.clean_up(page, "done_browser")

        self.assertIs(page, controller.test_page)
        self.assertEqual(page.quit_calls, 1)
        self.assertEqual(controller.active_resources, {})
        release.assert_called_once_with(proxy)

    def test_orphan_discovery_reclaims_unlocked_profiles_only(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = os.path.realpath(temporary_dir)
            unlocked = os.path.join(root, "ruyipage-old")
            locked = os.path.join(root, "ruyipage-live")
            os.makedirs(unlocked)
            os.makedirs(locked)
            with open(os.path.join(locked, "parent.lock"), "w", encoding="ascii"):
                pass

            controller = object.__new__(MetadataOnlyController)
            controller.profile_root = root
            controller.cleanup_lock = threading.Lock()
            controller.orphan_profile_dirs = set()
            controller.orphan_profile_min_age = 0

            controller._discover_orphan_profiles()
            controller._retry_orphan_profiles()

            self.assertFalse(os.path.exists(unlocked))
            self.assertTrue(os.path.exists(locked))
            self.assertIn(os.path.realpath(locked), controller.orphan_profile_dirs)


if __name__ == "__main__":
    unittest.main()
