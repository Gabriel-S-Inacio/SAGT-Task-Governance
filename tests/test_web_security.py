import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bitrix24_task_auditor.client import READ_ONLY_METHODS, BitrixClient, BitrixError
from bitrix24_task_auditor.config import BitrixConfig
from bitrix24_task_auditor.run_store import RunStore
from bitrix24_task_auditor.web_models import QuerySpec
from bitrix24_task_auditor.web_service import reconcile_worker_state, start_worker
from bitrix24_task_auditor.web_service import create_and_start
from bitrix24_task_auditor.run_store import RunStoreError


class WebSecurityTests(unittest.TestCase):
    def test_server_is_local_with_security_protections_enabled(self):
        config = tomllib.loads(Path(".streamlit/config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["server"]["address"], "127.0.0.1")
        self.assertTrue(config["server"]["enableCORS"])
        self.assertTrue(config["server"]["enableXsrfProtection"])

    def test_interface_and_worker_have_no_normal_collection_limits(self):
        app_source = Path("app.py").read_text(encoding="utf-8")
        worker_source = Path("src/bitrix24_task_auditor/worker.py").read_text(encoding="utf-8")
        self.assertNotIn("Limite de páginas", app_source)
        self.assertNotIn("max_pages", worker_source)
        self.assertNotIn("max_records", worker_source)
        self.assertNotIn("st.progress(", app_source)

    def test_rest_allowlist_contains_no_mutations(self):
        self.assertTrue(READ_ONLY_METHODS)
        self.assertFalse(any(token in method for method in READ_ONLY_METHODS for token in (".add", ".update", ".delete")))
        client = BitrixClient("https://portal/rest/1/token/", BitrixConfig())
        with self.assertRaises(BitrixError):
            client.call("tasks.task.update", {})

    def test_worker_command_does_not_contain_credentials(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text("", encoding="utf-8")
            python_path = root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
            python_path.parent.mkdir(parents=True)
            python_path.write_bytes(b"")
            store = RunStore(root)
            run_id = store.create(QuerySpec())["id"]
            process = MagicMock(pid=321)
            process.poll.return_value = None
            with patch("bitrix24_task_auditor.web_service.subprocess.Popen", return_value=process) as popen:
                pid = start_worker(root, config_path, store, run_id, startup_timeout=0)
            self.assertEqual(pid, 321)
            command = " ".join(popen.call_args.args[0])
            self.assertEqual(Path(popen.call_args.args[0][0]).resolve(), python_path.resolve())
            self.assertNotIn("token", command.casefold())
            self.assertNotIn("webhook", command.casefold())
            self.assertEqual(Path(popen.call_args.kwargs["cwd"]), root)
            self.assertEqual(popen.call_args.kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0], str(root / "src"))
            if os.name == "nt":
                self.assertEqual(popen.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
                self.assertFalse(popen.call_args.kwargs["creationflags"] & subprocess.DETACHED_PROCESS)

    def test_immediate_worker_exit_is_persisted_and_reported(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text("", encoding="utf-8")
            python_path = root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
            python_path.parent.mkdir(parents=True)
            python_path.write_bytes(b"")
            store = RunStore(root)
            run_id = store.create(QuerySpec())["id"]
            process = MagicMock(pid=654)
            process.poll.return_value = 7
            with patch("bitrix24_task_auditor.web_service.subprocess.Popen", return_value=process):
                with self.assertRaises(RunStoreError):
                    start_worker(root, config_path, store, run_id, startup_timeout=0)
            failed = store.read(run_id)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["worker_exit_code"], 7)
            self.assertIn("código 7", failed["last_error"])

    def test_popen_failure_is_persisted_without_credentials(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text("", encoding="utf-8")
            python_path = root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
            python_path.parent.mkdir(parents=True)
            python_path.write_bytes(b"")
            store = RunStore(root)
            run_id = store.create(QuerySpec())["id"]
            with patch.dict(os.environ, {"BITRIX24_WEBHOOK_URL": "https://host/rest/1/very-secret-token/"}):
                with patch("bitrix24_task_auditor.web_service.subprocess.Popen", side_effect=OSError("falha sem segredo")):
                    with self.assertRaises(RunStoreError):
                        start_worker(root, config_path, store, run_id, startup_timeout=0)
            failed = store.read(run_id)
            self.assertEqual(failed["status"], "failed")
            self.assertNotIn("very-secret-token", failed["last_error"])

    def test_dead_running_pid_is_reconciled(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = RunStore(Path(directory))
            state = store.create(QuerySpec())
            state["status"] = "running"
            state["pid"] = 99999
            store.write(state["id"], state)
            reconciled = reconcile_worker_state(store, state["id"], process_probe=lambda pid: (False, 3))
            self.assertEqual(reconciled["status"], "failed")
            self.assertEqual(reconciled["worker_exit_code"], 3)

    def test_second_concurrent_run_is_refused(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text("", encoding="utf-8")
            store = RunStore(root)
            first = store.create(QuerySpec())
            first["status"] = "running"
            store.write(first["id"], first)
            with self.assertRaises(RunStoreError):
                create_and_start(root, config_path, store, QuerySpec(report_name="segunda"), confirmed_fields=[])


if __name__ == "__main__":
    unittest.main()
