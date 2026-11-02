import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from bitrix24_task_auditor.client import BitrixError
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


def task(task_id: str, *, deadline: str | None = "2026-12-01T12:00:00+00:00", responsible: str = "7"):
    return {
        "id": task_id, "title": f"Tarefa {task_id}", "status": "3", "responsibleId": responsible,
        "createdBy": "8", "deadline": deadline, "groupId": "2",
    }


class SyntheticClient:
    def __init__(self, task_handler):
        self.task_handler = task_handler
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params.copy()))
        if method == "tasks.task.list":
            return self.task_handler(params)
        if method == "user.get":
            return {"result": []}
        raise AssertionError(method)


class CompletePaginationTests(unittest.TestCase):
    def make_run(self, root: Path, spec: QuerySpec | None = None):
        (root / "config.toml").write_text(CONFIG, encoding="utf-8")
        store = RunStore(root)
        state = store.create(spec or QuerySpec(report_name="paginacao"))
        return store, state["id"]

    def execute(self, root: Path, run_id: str, client: SyntheticClient) -> int:
        with redirect_stdout(io.StringIO()):
            return execute_run(root, root / "config.toml", run_id, client=client)

    @staticmethod
    def result_payload(root: Path, state):
        return json.loads((root / state["result"]["json"]).read_text(encoding="utf-8"))

    def test_collects_more_than_one_hundred_pages_without_limit(self):
        def handler(params):
            cursor = params["start"]
            page_number = cursor // 50
            payload = {"result": {"tasks": [task(str(page_number + 1))]}}
            if page_number < 104:
                payload["next"] = cursor + 50
            return payload

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory); store, run_id = self.make_run(root)
            self.assertEqual(self.execute(root, run_id, SyntheticClient(handler)), 0)
            state = store.read(run_id)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["progress"]["pages"], 105)
            self.assertEqual(state["progress"]["unique_tasks"], 105)
            self.assertEqual(state["progress"]["pagination_proofs"][0]["evidence"], "next_absent")

    def test_unknown_total_uses_next_until_absent(self):
        def handler(params):
            if params["start"] == 0:
                return {"result": {"tasks": [task("1")]}, "next": 50}
            return {"result": {"tasks": [task("2")]}}

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory); store, run_id = self.make_run(root)
            self.execute(root, run_id, SyntheticClient(handler))
            state = store.read(run_id)
            self.assertEqual(state["status"], "completed")
            self.assertIsNone(state["progress"]["current_api_total"])
            self.assertTrue(self.result_payload(root, state)["metadata"]["pagination_complete"])

    def test_empty_first_page_without_next_is_complete(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory); store, run_id = self.make_run(root)
            self.execute(root, run_id, SyntheticClient(lambda params: {"result": {"tasks": []}, "total": 0}))
            state = store.read(run_id)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["progress"]["unique_tasks"], 0)

    def test_empty_page_with_next_does_not_end_collection(self):
        def handler(params):
            return {"result": {"tasks": []}, "next": 50} if params["start"] == 0 else {"result": {"tasks": [task("2")]}}

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory); store, run_id = self.make_run(root)
            self.execute(root, run_id, SyntheticClient(handler))
            state = store.read(run_id)
            self.assertEqual(state["status"], "completed")
            self.assertEqual((state["progress"]["pages"], state["progress"]["unique_tasks"]), (2, 1))

    def test_absent_next_marks_short_final_page_as_terminal(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory); store, run_id = self.make_run(root)
            self.execute(root, run_id, SyntheticClient(lambda params: {"result": {"tasks": [task("1")]}}))
            state = store.read(run_id)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["progress"]["pagination_proofs"][0]["evidence"], "next_absent")

    def test_repeated_cursor_fails_and_exports_partial_result(self):
        def handler(params):
            if params["start"] == 0:
                return {"result": {"tasks": [task("1")]}, "next": 50}
            return {"result": {"tasks": [task("2")]}, "next": 50}

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory); store, run_id = self.make_run(root)
            self.assertEqual(self.execute(root, run_id, SyntheticClient(handler)), 2)
            state = store.read(run_id)
            self.assertEqual(state["status"], "failed")
            self.assertTrue(state["partial"])
            payload = self.result_payload(root, state)
            self.assertFalse(payload["metadata"]["pagination_complete"])
            self.assertGreater(payload["metadata"]["pending_scopes"], 0)
            self.assertTrue((root / state["result"]["xlsx"]).exists())
            self.assertIn("não avançou", state["last_error"])

    def test_intermediate_failure_preserves_cursor_and_partial_export(self):
        def handler(params):
            if params["start"] == 0:
                return {"result": {"tasks": [task("1")]}, "next": 50}
            raise BitrixError("falha sintética intermediária")

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory); store, run_id = self.make_run(root)
            self.assertEqual(self.execute(root, run_id, SyntheticClient(handler)), 2)
            state = store.read(run_id)
            self.assertEqual(state["progress"]["next_cursor"], 50)
            self.assertEqual(len(self.result_payload(root, state)["tasks"]), 1)

    def test_resume_after_intermediate_failure_continues_from_checkpoint(self):
        calls = {"failed": False}

        def unstable(params):
            if params["start"] == 0:
                return {"result": {"tasks": [task("1")]}, "next": 50}
            calls["failed"] = True
            raise BitrixError("interrupção sintética")

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory); store, run_id = self.make_run(root)
            self.execute(root, run_id, SyntheticClient(unstable))
            resumed = SyntheticClient(lambda params: {"result": {"tasks": [task("2")]}})
            self.assertEqual(self.execute(root, run_id, resumed), 0)
            state = store.read(run_id)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(resumed.calls[0][1]["start"], 50)
            self.assertEqual(len(self.result_payload(root, state)["tasks"]), 2)

    def test_duplicate_tasks_between_pages_are_deduplicated(self):
        def handler(params):
            if params["start"] == 0:
                return {"result": {"tasks": [task("1")]}, "next": 50}
            return {"result": {"tasks": [task("1"), task("2")]}}

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory); store, run_id = self.make_run(root)
            self.execute(root, run_id, SyntheticClient(handler))
            state = store.read(run_id)
            self.assertEqual(state["progress"]["records"], 3)
            self.assertEqual(state["progress"]["unique_tasks"], 2)
            self.assertEqual(len(self.result_payload(root, state)["tasks"]), 2)

    def test_task_without_deadline_is_collected_and_audited(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory); store, run_id = self.make_run(root)
            self.execute(root, run_id, SyntheticClient(lambda params: {"result": {"tasks": [task("1", deadline=None)]}}))
            payload = self.result_payload(root, store.read(run_id))
            self.assertIn("MISSING_DEADLINE", {item["code"] for item in payload["findings"]})

    def test_multiple_users_and_roles_complete_every_scope(self):
        spec = QuerySpec(user_ids=("7", "9"), roles=("responsible", "creator"), report_name="multi")

        def handler(params):
            filters = params["filter"]
            user = str(filters.get("RESPONSIBLE_ID", filters.get("CREATED_BY")))
            role = "r" if "RESPONSIBLE_ID" in filters else "c"
            item = task(f"{user}{role}", responsible=user)
            if "CREATED_BY" in filters:
                item["createdBy"] = user
            return {"result": {"tasks": [item]}, "total": 1}

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory); store, run_id = self.make_run(root, spec)
            self.execute(root, run_id, SyntheticClient(handler))
            state = store.read(run_id)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["progress"]["unit_total"], 4)
            self.assertEqual(len(state["progress"]["completed_units"]), 4)
            self.assertEqual(len(state["progress"]["pagination_proofs"]), 4)


if __name__ == "__main__":
    unittest.main()
