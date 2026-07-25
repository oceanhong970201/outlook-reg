import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config


class ConfigLoadingTests(unittest.TestCase):
    def tearDown(self):
        config._config = None

    def test_non_mapping_config_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "config.json"
            path.write_text("[]", encoding="utf-8")
            with patch.object(config, "_config_path", return_value=str(path)):
                config._config = None
                loaded = config.get_config()

        self.assertEqual(loaded["proxy_source"], config._DEFAULTS["proxy_source"])
        self.assertIsInstance(loaded["oauth2"], dict)

    def test_invalid_nested_sections_do_not_replace_defaults(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "max_tasks": 7,
                        "oauth2": "invalid",
                        "ruyipage": [],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(config, "_config_path", return_value=str(path)):
                config._config = None
                loaded = config.get_config()

        self.assertEqual(loaded["max_tasks"], 7)
        self.assertEqual(
            loaded["oauth2"]["authorization_timeout"],
            config._DEFAULTS["oauth2"]["authorization_timeout"],
        )
        self.assertEqual(
            loaded["ruyipage"]["profile_root"],
            config._DEFAULTS["ruyipage"]["profile_root"],
        )


if __name__ == "__main__":
    unittest.main()
