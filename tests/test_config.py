import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bitrix24_task_auditor.config import (
    BitrixConfig,
    ConfigurationError,
    load_config,
    validate_webhook,
    webhook_from_environment,
)


BASE = """
[bitrix]
allowed_hosts = ["portal.bitrix24.com.br"]
[audit]
checkpoint_path = ".state/c.json"
spool_path = ".state/s.ndjson"
output_directory = "output"
[roles]
responsible = [10, "11"]
creator = []
accomplice = []
auditor = []
[[custom_fields]]
id = "UF_AUTO_1"
label = "Contrato"
required_for = ["responsible"]
"""


class ConfigTests(unittest.TestCase):
    def test_loads_and_normalizes(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            path = root / "config.toml"
            path.write_text(BASE, encoding="utf-8")
            config = load_config(path, project_root=root)
            self.assertEqual(config.roles["responsible"], ("10", "11"))
            self.assertEqual(config.custom_fields[0].label, "Contrato")
            self.assertTrue(config.audit.output_directory.is_relative_to(root))

    def test_rejects_path_escape(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            path = root / "config.toml"
            path.write_text(BASE.replace('output_directory = "output"', 'output_directory = "../outside"'), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path, project_root=root)

    def test_webhook_validation(self):
        clean = validate_webhook("https://portal.bitrix24.com.br/rest/1/token/", ("portal.bitrix24.com.br",))
        self.assertEqual(clean, "https://portal.bitrix24.com.br/rest/1/token/")
        for unsafe in (
            "http://portal.bitrix24.com.br/rest/1/token/",
            "https://localhost/rest/1/token/",
            "https://127.0.0.1/rest/1/token/",
            "https://portal.bitrix24.com.br/rest/1/token/?x=1",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ConfigurationError):
                validate_webhook(unsafe)

    def test_real_connection_requires_explicit_allowed_host(self):
        config = BitrixConfig(allowed_hosts=())
        with patch.dict(
            os.environ,
            {"BITRIX24_WEBHOOK_URL": "https://portal.example/rest/1/test-token/"},
            clear=True,
        ):
            with self.assertRaisesRegex(ConfigurationError, "allowed_hosts"):
                webhook_from_environment(config)

    def test_rejects_unknown_role(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            path = root / "config.toml"
            path.write_text(BASE + "\n[roles.typo]\n", encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path, project_root=root)


if __name__ == "__main__":
    unittest.main()
