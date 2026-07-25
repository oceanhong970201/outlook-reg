import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class SaveLogTests(unittest.TestCase):
    def _run_bar(self, directory, show_logs):
        bar = main.StatusBar()
        bar.show_logs = show_logs
        bar.configure_log_file(True)
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch.object(main, "PROJECT_ROOT", directory),
            patch.object(main.sys, "stdout", stdout),
            patch.object(main.sys, "stderr", stderr),
        ):
            bar.start()
            print("\033[31m[Info] complete output\033[0m")
            print("[Success] saved")
            path = bar.log_path
            bar.stop()

        return Path(path), stdout.getvalue()

    def test_full_mode_saves_utf8_log_without_ansi(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            path, terminal = self._run_bar(temporary_dir, show_logs=True)
            content = path.read_text(encoding="utf-8")

        self.assertIn("[Info] complete output", content)
        self.assertIn("[Success] saved", content)
        self.assertNotIn("\x1b[", content)
        self.assertIn("[Info] complete output", terminal)

    def test_quiet_mode_still_saves_full_log(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            path, terminal = self._run_bar(temporary_dir, show_logs=False)
            content = path.read_text(encoding="utf-8")

        self.assertIn("[Info] complete output", content)
        self.assertIn("[Success] saved", content)
        self.assertNotIn("[Info] complete output", terminal)
        self.assertIn("[Success] saved", terminal)

    def test_disabled_toggle_does_not_create_a_log(self):
        bar = main.StatusBar()
        bar.configure_log_file(False)
        stdout = io.StringIO()

        with patch.object(main.sys, "stdout", stdout), patch.object(
            main.sys, "stderr", io.StringIO()
        ):
            bar.start()
            path = bar.log_path
            bar.stop()

        self.assertIsNone(path)

    def test_configure_disabled_clears_previous_path(self):
        bar = main.StatusBar()
        bar.log_path = "old.log"

        bar.configure_log_file(False)

        self.assertIsNone(bar.log_path)


if __name__ == "__main__":
    unittest.main()
