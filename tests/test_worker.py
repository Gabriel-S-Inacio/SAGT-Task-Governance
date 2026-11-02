import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from bitrix24_task_auditor.run_store import RunStore
from bitrix24_task_auditor.web_models import QuerySpec
from bitrix24_task_auditor.worker import execute_run


CONFIG = """
[bitrix]
max_retries = 0
allowed_hosts = []
[audit]
checkpoint_path = ".state/cli-checkpoint.json"
spool_path = ".state/cli-tasks.ndjson"
output_directory = "output"
[roles]
responsible = []
creator = []
accomplice = []
auditor = []
"""


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params.get("start")))
        if method == "tasks.task.list":
            return {"result": {"tasks": [{
                "id": "101", "title": "Sintética", "status": "3", "responsibleId": "7",
                "createdBy": "8", "deadline": "2026-12-01T12:00:00+00:00", "groupId": "2",
            }]}}
        if method == "user.get":
            return {"result": [{"ID": "7", "NAME": "Ana", "ACTIVE": "Y"}]}
        raise AssertionError(method)


class WorkerTests(unittest.TestCase):
    def test_transient_run_json_permission_error_does_not_fail_worker(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            (root / "config.toml").write_text(CONFIG, encoding="utf-8")
            store = RunStore(root)
            run_id = store.create(QuerySpec(user_ids=("7",), roles=("responsible",), report_name="retry"))["id"]
            run_path = store.run_dir(run_id) / "run.json"
            original_read_text = Path.read_text
            first = {"pending": True}

            def flaky_once(path, *args, **kwargs):
                if path == run_path and first["pending"]:
                    first["pending"] = False
                    raise PermissionError(13, "synthetic sharing violation", str(path))
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", new=flaky_once), \
                 patch("bitrix24_task_auditor.run_store.time.sleep"):
                self.assertEqual(execute_run(root, root / "config.toml", run_id, client=FakeClient()), 0)
            state = store.read(run_id)
            self.assertEqual(state["status"], "completed")
            self.assertFalse(state["partial"])
            self.assertNotIn("last_error", state)

    def test_synthetic_run_cancel_resume_and_six_sheet_export(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            (root / "config.toml").write_text(CONFIG, encoding="utf-8")
            store = RunStore(root)
            state = store.create(QuerySpec(user_ids=("7",), roles=("responsible",), report_name="teste"))
            run_id = state["id"]
            store.request_cancel(run_id)
            self.assertEqual(execute_run(root, root / "config.toml", run_id, client=FakeClient()), 0)
            self.assertEqual(store.read(run_id)["status"], "cancelled")
            self.assertTrue(store.read(run_id)["partial"])

            store.clear_cancel(run_id)
            client = FakeClient()
            self.assertEqual(execute_run(root, root / "config.toml", run_id, client=client), 0)
            completed = store.read(run_id)
            self.assertEqual(completed["status"], "completed")
            self.assertFalse(completed["partial"])
            self.assertIn(("tasks.task.list", 0), client.calls)
            result_json = root / completed["result"]["json"]
            payload = json.loads(result_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["metadata"]["run_id"], run_id)
            self.assertEqual(len(payload["tasks"]), 1)
            self.assertEqual(len(payload["links"]), 1)
            with zipfile.ZipFile(root / completed["result"]["xlsx"]) as archive:
                workbook = archive.read("xl/workbook.xml").decode("utf-8")
                self.assertEqual(workbook.count("<sheet "), 6)

    def test_initialization_failure_updates_state_without_network(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            (root / "config.toml").write_text(CONFIG, encoding="utf-8")
            store = RunStore(root)
            run_id = store.create(QuerySpec(report_name="falha_inicio"))["id"]
            with patch.dict(os.environ, {"BITRIX24_WEBHOOK_URL": ""}):
                result = execute_run(root, root / "config.toml", run_id)
            self.assertEqual(result, 2)
            failed = store.read(run_id)
            self.assertEqual(failed["status"], "failed")
            self.assertIn("Falha de inicialização", failed["last_error"])
            self.assertEqual(failed["worker_exit_code"], 2)


if __name__ == "__main__":
    unittest.main()
