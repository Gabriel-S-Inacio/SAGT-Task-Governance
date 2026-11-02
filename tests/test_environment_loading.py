import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bitrix24_task_auditor import cli
from bitrix24_task_auditor.config import ConfigurationError, load_config, webhook_from_environment
from bitrix24_task_auditor.presenter import load_result_bundle
from bitrix24_task_auditor.run_store import RunStore
from bitrix24_task_auditor.web_models import QuerySpec
from bitrix24_task_auditor.web_service import connection_status


CONFIG = """
[bitrix]
webhook_env = "BITRIX24_WEBHOOK_URL"
allowed_hosts = ["portal.bitrix24.com.br"]
[audit]
checkpoint_path = ".state/checkpoint.json"
spool_path = ".state/tasks.ndjson"
output_directory = "output"
[roles]
responsible = []
creator = []
accomplice = []
auditor = []
"""


class EnvironmentLoadingTests(unittest.TestCase):
    @staticmethod
    def project(root: Path, dotenv_value: str | None = None) -> Path:
        config_path = root / "config.toml"
        config_path.write_text(CONFIG, encoding="utf-8")
        if dotenv_value is not None:
            (root / ".env").write_text(f"BITRIX24_WEBHOOK_URL={dotenv_value}\n", encoding="utf-8")
        return config_path

    def test_webhook_is_loaded_from_project_dotenv_without_rest(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            expected = "https://portal.bitrix24.com.br/rest/1/from-file/"
            config = load_config(self.project(root, expected), project_root=root)
            with patch("bitrix24_task_auditor.client.BitrixClient.call") as rest_call:
                status = connection_status(config)
            self.assertTrue(status["configured"])
            self.assertEqual(webhook_from_environment(config.bitrix), expected)
            rest_call.assert_not_called()

    def test_existing_process_environment_has_precedence(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            from_file = "https://portal.bitrix24.com.br/rest/1/from-file/"
            from_process = "https://portal.bitrix24.com.br/rest/2/from-process/"
            with patch.dict(os.environ, {"BITRIX24_WEBHOOK_URL": from_process}, clear=True):
                config = load_config(self.project(root, from_file), project_root=root)
                self.assertEqual(webhook_from_environment(config.bitrix), from_process)

    def test_missing_webhook_keeps_clear_error(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            config = load_config(self.project(root), project_root=root)
            with self.assertRaisesRegex(ConfigurationError, "variável BITRIX24_WEBHOOK_URL não definida"):
                webhook_from_environment(config.bitrix)
            self.assertFalse(connection_status(config)["configured"])

    def test_cli_uses_the_same_dotenv_loading_without_network(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            expected = "https://portal.bitrix24.com.br/rest/3/cli-file/"
            self.project(root, expected)
            original = Path.cwd()
            try:
                os.chdir(root)
                with patch.object(cli, "BitrixClient") as client_class, \
                     patch.object(cli, "CheckpointStore"), \
                     patch.object(cli, "collect", return_value=([], [])), \
                     patch.object(cli, "write_reports", return_value=(root / "output/a.json", root / "output/a.xlsx", None)):
                    result = cli.main(["--config", "config.toml", "run", "--confirm-read-only-collection"])
            finally:
                os.chdir(original)
            self.assertEqual(result, 0)
            self.assertEqual(client_class.call_args.args[0], expected)
            client_class.return_value.call.assert_not_called()

    def test_loading_environment_preserves_existing_audit_results(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            store = RunStore(root)
            state = store.create(QuerySpec(report_name="historico"))
            run_id = state["id"]
            output = root / "output"
            output.mkdir()
            json_path = output / f"historico_{run_id}.json"
            xlsx_path = output / f"historico_{run_id}.xlsx"
            json_path.write_text(json.dumps({
                "metadata": {"run_id": run_id}, "tasks": [], "links": [],
                "users": [], "findings": [], "actions": [],
            }), encoding="utf-8")
            xlsx_path.write_bytes(b"synthetic")
            state["status"] = "completed"
            state["result"] = {"json": str(json_path.relative_to(root)), "xlsx": str(xlsx_path.relative_to(root))}
            store.write(run_id, state)
            load_config(self.project(root, "https://portal.bitrix24.com.br/rest/4/history/"), project_root=root)
            bundle = load_result_bundle(root, store.read(run_id))
            self.assertFalse(bundle["errors"])
            self.assertEqual(bundle["payload"]["metadata"]["run_id"], run_id)

    def test_dependency_and_initializers_include_dotenv(self):
        self.assertIn("python-dotenv", Path("requirements.txt").read_text(encoding="utf-8"))
        self.assertIn("dotenv", Path("iniciar_auditor.bat").read_text(encoding="utf-8"))
        self.assertIn("dotenv", Path("iniciar_auditor.ps1").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
