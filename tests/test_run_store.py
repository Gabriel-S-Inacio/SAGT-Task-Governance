import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bitrix24_task_auditor.run_store import IO_RETRY_ATTEMPTS, RunStore, RunStoreError
from bitrix24_task_auditor.web_models import QuerySpec
from bitrix24_task_auditor.web_service import reconcile_worker_state


class RunStoreTests(unittest.TestCase):
    def test_history_template_and_cancel_are_persisted_without_credentials(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            store = RunStore(root)
            spec = QuerySpec(user_ids=("8",), roles=("responsible",), report_name="semanal")
            state = store.create(spec)
            run_id = state["id"]
            store.request_cancel(run_id)
            self.assertTrue(store.cancel_requested(run_id))
            self.assertEqual(store.read(run_id)["status"], "cancelling")
            persisted = (store.run_dir(run_id) / "run.json").read_text(encoding="utf-8")
            self.assertNotIn("webhook", persisted.casefold())
            self.assertNotIn("token", persisted.casefold())
            store.clear_cancel(run_id)
            self.assertFalse(store.cancel_requested(run_id))
            store.save_template("semanal", spec)
            self.assertEqual(store.load_template("semanal"), spec)
            self.assertEqual(store.list_runs()[0]["id"], run_id)

    def test_concurrent_spool_reads_never_observe_partial_json_lines(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = RunStore(Path(directory))
            run_id = store.create(QuerySpec())["id"]
            issues = []

            def writer():
                for index in range(50):
                    store.append_records(run_id, "tasks.ndjson", [{"id": str(index), "title": "x" * 200}])

            thread = threading.Thread(target=writer)
            thread.start()
            while thread.is_alive():
                _, current_issues = store.read_records_report(run_id, "tasks.ndjson")
                issues.extend(current_issues)
            thread.join()
            records, final_issues = store.read_records_report(run_id, "tasks.ndjson")
            self.assertEqual(issues + final_issues, [])
            self.assertEqual(len(records), 50)

    def test_transient_permission_error_while_reading_run_json_is_retried(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = RunStore(Path(directory))
            run_id = store.create(QuerySpec())["id"]
            run_path = store.run_dir(run_id) / "run.json"
            original_read_text = Path.read_text
            attempts = {"count": 0}

            def flaky_read(path, *args, **kwargs):
                if path == run_path and attempts["count"] < 2:
                    attempts["count"] += 1
                    raise PermissionError(13, "synthetic sharing violation", str(path))
                attempts["count"] += path == run_path
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", new=flaky_read), \
                 patch("bitrix24_task_auditor.run_store.time.sleep"):
                state = store.read(run_id)
            self.assertEqual(state["status"], "queued")
            self.assertEqual(attempts["count"], 3)

    def test_transient_replace_error_is_retried_after_temp_file_is_closed(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = RunStore(Path(directory))
            run_id = store.create(QuerySpec())["id"]
            run_path = store.run_dir(run_id) / "run.json"
            real_replace = os.replace
            attempts = {"count": 0}

            def flaky_replace(source, destination):
                if Path(destination) == run_path and attempts["count"] < 2:
                    attempts["count"] += 1
                    raise PermissionError(13, "synthetic sharing violation", str(destination))
                attempts["count"] += Path(destination) == run_path
                return real_replace(source, destination)

            with patch("bitrix24_task_auditor.run_store.os.replace", new=flaky_replace), \
                 patch("bitrix24_task_auditor.run_store.time.sleep"):
                store.update(run_id, status="running")
            self.assertEqual(attempts["count"], 3)
            self.assertEqual(store.read(run_id)["status"], "running")
            self.assertFalse(list(store.run_dir(run_id).glob("run.json.*.tmp")))

    def test_permanent_permission_error_is_reported_after_limited_retries(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = RunStore(Path(directory))
            run_id = store.create(QuerySpec())["id"]
            run_path = store.run_dir(run_id) / "run.json"
            original_read_text = Path.read_text
            attempts = {"count": 0}

            def denied_read(path, *args, **kwargs):
                if path == run_path:
                    attempts["count"] += 1
                    raise PermissionError(13, "synthetic permanent denial", str(path))
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", new=denied_read), \
                 patch("bitrix24_task_auditor.run_store.time.sleep"), \
                 self.assertRaisesRegex(RunStoreError, "não foi possível ler"):
                store.list_runs()
            self.assertEqual(attempts["count"], IO_RETRY_ATTEMPTS)

    def test_concurrent_run_json_readers_and_writers_keep_valid_state(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            writer_store, reader_store = RunStore(root), RunStore(root)
            run_id = writer_store.create(QuerySpec())["id"]
            errors = []
            seen = []

            def writer():
                try:
                    for index in range(75):
                        writer_store.update(run_id, status="running", synthetic_sequence=index)
                except Exception as exc:  # captured for assertion in the main thread
                    errors.append(exc)

            thread = threading.Thread(target=writer)
            thread.start()
            while thread.is_alive():
                try:
                    seen.append(reader_store.read(run_id).get("synthetic_sequence", -1))
                except Exception as exc:
                    errors.append(exc)
            thread.join()
            final = reader_store.read(run_id)
            persisted = json.loads((reader_store.run_dir(run_id) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(errors, [])
            self.assertTrue(seen)
            self.assertEqual(final["synthetic_sequence"], 74)
            self.assertEqual(persisted["id"], run_id)

    def test_transient_read_during_reconciliation_does_not_mark_failed(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = RunStore(Path(directory))
            state = store.create(QuerySpec())
            run_id = state["id"]
            store.update(run_id, status="running", pid=12345)
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
                reconciled = reconcile_worker_state(store, run_id, process_probe=lambda pid: (True, None))
            self.assertEqual(reconciled["status"], "running")
            self.assertNotIn("last_error", reconciled)


if __name__ == "__main__":
    unittest.main()
