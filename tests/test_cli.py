import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bitrix24_task_auditor.cli import main


class CliTests(unittest.TestCase):
    def test_offline_end_to_end_without_network(self):
        root = Path.cwd()
        with tempfile.TemporaryDirectory(dir=root) as directory:
            temp = Path(directory)
            relative = temp.relative_to(root)
            config_path = temp / "config.toml"
            config_path.write_text(f"""
[bitrix]
[audit]
checkpoint_path = "{relative.as_posix()}/c.json"
spool_path = "{relative.as_posix()}/s.ndjson"
output_directory = "{relative.as_posix()}/output"
[roles]
responsible = []
creator = []
accomplice = []
auditor = []
""", encoding="utf-8")
            input_path = temp / "input.json"
            input_path.write_text(json.dumps({
                "tasks": [{"ID": "1", "TITLE": "T", "STATUS": "3", "RESPONSIBLE_ID": "9"}],
                "users": [{"ID": "9", "NAME": "Jo", "ACTIVE": "Y"}],
            }), encoding="utf-8")
            with patch("urllib.request.urlopen", side_effect=AssertionError("network used")):
                result = main([
                    "--config", str(config_path.relative_to(root)), "audit-file",
                    str(input_path.relative_to(root)), "--output-prefix", "offline",
                ])
            self.assertEqual(result, 0)
            self.assertTrue((temp / "output/offline.json").exists())
            self.assertTrue((temp / "output/offline.xlsx").exists())

    def test_run_requires_explicit_confirmation_before_env_or_network(self):
        result = main(["--config", "config/auditor.toml", "run"])
        self.assertEqual(result, 2)


if __name__ == "__main__":
    unittest.main()
