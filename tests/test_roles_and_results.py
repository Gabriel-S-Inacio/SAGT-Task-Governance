import json
import tempfile
import unittest
from pathlib import Path

from bitrix24_task_auditor.presenter import load_result_bundle, result_indicators, result_rows, select_result_run_id
from bitrix24_task_auditor.roles import merge_links, verify_role
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


class RoleClient:
    def __init__(self, *, omit_role_field=False, empty_role=None):
        self.calls = []
        self.omit_role_field = omit_role_field
        self.empty_role = empty_role

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "user.get":
            return {"result": []}
        if method == "tasks.task.get":
            return {"result": {"task": {
                "id": str(params["taskId"]), "responsibleId": "7", "createdBy": "7",
                "accomplices": ["7"], "auditors": ["7"],
            }}}
        if method != "tasks.task.list":
            raise AssertionError(method)
        filters = params["filter"]
        role_by_filter = {
            "RESPONSIBLE_ID": ("responsibleId", "responsible"),
            "CREATED_BY": ("createdBy", "creator"),
            "ACCOMPLICE": ("accomplices", "accomplice"),
            "AUDITOR": ("auditors", "auditor"),
        }
        filter_name = next(key for key in role_by_filter if key in filters)
        field, role = role_by_filter[filter_name]
        if role == self.empty_role:
            return {"result": {"tasks": []}, "total": 0}
        user = str(filters[filter_name])
        task = {
            "id": f"{user}-{role}", "title": role, "status": "3", "responsibleId": "70",
            "createdBy": "80", "accomplices": ["90"], "auditors": ["100"],
        }
        task[field] = [user] if field in {"accomplices", "auditors"} else user
        if self.omit_role_field:
            task.pop(field)
        return {"result": {"tasks": [task]}, "total": 1}


class RoleAndResultRegressionTests(unittest.TestCase):
    def test_each_classic_role_and_multiple_roles_are_verified_from_fields(self):
        task = {
            "id": "1", "responsibleId": "7", "createdBy": "7",
            "accomplices": ["7", "8"], "auditors": [{"id": "7"}, {"ID": "9"}],
        }
        for role in ("responsible", "creator", "accomplice", "auditor"):
            with self.subTest(role=role):
                self.assertEqual(verify_role(task, "7", role).verification, "confirmed")
        links = merge_links([verify_role(task, "7", role).to_link(source_unit=0, query_filter=role)
                             for role in ("responsible", "creator", "accomplice", "auditor")])
        self.assertEqual(len(links), 4)

    def test_multiple_users_same_task_and_missing_or_contradictory_fields(self):
        task = {"id": "2", "accomplices": ["7", "8"]}
        self.assertEqual(verify_role(task, "7", "accomplice").verification, "confirmed")
        self.assertEqual(verify_role(task, "8", "accomplice").verification, "confirmed")
        self.assertEqual(verify_role(task, "7", "auditor").verification, "unverifiable")
        self.assertEqual(verify_role({"id": "2", "auditors": ["9"]}, "7", "auditor").verification, "contradictory")

    def test_worker_uses_all_four_filters_enriches_missing_fields_and_records_zero_result(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            (root / "config.toml").write_text(CONFIG, encoding="utf-8")
            store = RunStore(root)
            spec = QuerySpec(user_ids=("7",), roles=("responsible", "creator", "accomplice", "auditor"), report_name="papeis")
            run_id = store.create(spec)["id"]
            client = RoleClient(omit_role_field=True, empty_role="auditor")
            self.assertEqual(execute_run(root, root / "config.toml", run_id, client=client), 0)
            state = store.read(run_id)
            payload = json.loads((root / state["result"]["json"]).read_text(encoding="utf-8"))
            filters = [next(iter(params["filter"])) for method, params in client.calls if method == "tasks.task.list"]
            self.assertEqual(filters, ["RESPONSIBLE_ID", "CREATED_BY", "ACCOMPLICE", "AUDITOR"])
            self.assertTrue(any(method == "tasks.task.get" for method, _ in client.calls))
            self.assertEqual(len(payload["metadata"]["query_stats"]), 4)
            self.assertEqual(payload["metadata"]["query_stats"][3]["result"], "zero_results")
            self.assertEqual({link["role"] for link in payload["links"]}, {"responsible", "creator", "accomplice", "auditor"})

    def test_missing_field_after_enrichment_marks_partial_and_creates_manual_action(self):
        class MissingAuditorClient:
            def call(self, method, params):
                if method == "tasks.task.list":
                    return {"result": {"tasks": [{"id": "77", "title": "Sem observadores", "status": "3"}]}}
                if method == "tasks.task.get":
                    return {"result": {"task": {"id": "77", "title": "Sem observadores", "status": "3"}}}
                if method == "user.get":
                    return {"result": []}
                raise AssertionError(method)

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            (root / "config.toml").write_text(CONFIG, encoding="utf-8")
            store = RunStore(root)
            run_id = store.create(QuerySpec(user_ids=("7",), roles=("auditor",), report_name="parcial"))["id"]
            self.assertEqual(execute_run(root, root / "config.toml", run_id, client=MissingAuditorClient()), 0)
            state = store.read(run_id)
            self.assertEqual(state["status"], "partial")
            self.assertTrue(state["partial"])
            payload = json.loads((root / state["result"]["json"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["links"][0]["verification"], "unverifiable")
            self.assertEqual(payload["actions"][0]["recommended_action"],
                             "Avaliar manualmente o vínculo; nenhuma reatribuição foi definida.")
            self.assertFalse(payload["metadata"]["role_coverage_complete"])

    def test_run_a_then_b_selection_and_artifacts_survive_refresh(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            store = RunStore(root)
            run_a = store.create(QuerySpec(report_name="A"))
            run_b = store.create(QuerySpec(report_name="B"))
            for state, title in ((run_a, "Auditoria A"), (run_b, "Auditoria B")):
                run_id = state["id"]
                output = root / "output"
                output.mkdir(exist_ok=True)
                json_path = output / f"relatorio_{run_id}.json"
                xlsx_path = output / f"relatorio_{run_id}.xlsx"
                json_path.write_text(json.dumps({
                    "metadata": {"run_id": run_id}, "tasks": [{"id": run_id, "title": title}],
                    "links": [], "users": [], "findings": [], "actions": [],
                }), encoding="utf-8")
                xlsx_path.write_bytes(("xlsx-" + run_id).encode())
                state["status"] = "completed"
                state["result"] = {"json": str(json_path.relative_to(root)), "xlsx": str(xlsx_path.relative_to(root))}
                store.write(run_id, state)
            runs = store.list_runs()
            selected = select_result_run_id(runs, requested_run_id=run_b["id"], active_run_id=run_a["id"])
            self.assertEqual(selected, run_b["id"])
            self.assertEqual(select_result_run_id(runs, requested_run_id=selected), run_b["id"])
            bundle = load_result_bundle(root, store.read(selected))
            self.assertFalse(bundle["errors"])
            self.assertEqual(bundle["payload"]["tasks"][0]["title"], "Auditoria B")
            self.assertIn(run_b["id"], bundle["json_path"].name)
            self.assertIn(run_b["id"], bundle["xlsx_path"].name)
            self.assertEqual(bundle["xlsx_path"].read_bytes(), ("xlsx-" + run_b["id"]).encode())

    def test_result_inventory_maps_roles_per_user_and_indicators(self):
        payload = {
            "tasks": [{"id": "1", "title": "T", "status": "3"}],
            "users": [{"user_id": "7", "name": "Ana"}, {"user_id": "8", "name": "Beto"}],
            "links": [
                {"task_id": "1", "user_id": "7", "role": "responsible", "verification": "confirmed"},
                {"task_id": "1", "user_id": "7", "role": "creator", "verification": "confirmed"},
                {"task_id": "1", "user_id": "8", "role": "auditor", "verification": "confirmed"},
                {"task_id": "1", "user_id": "8", "role": "accomplice", "verification": "unverifiable"},
            ], "findings": [],
        }
        row = result_rows(payload)[0]
        self.assertIn("Ana: Criador, Responsável", row["Papéis por usuário"])
        self.assertIn("Beto: Observador, Participante [unverifiable]", row["Papéis por usuário"])
        indicators = result_indicators(payload)
        self.assertEqual((indicators["responsible"], indicators["creator"], indicators["accomplice"], indicators["auditor"]), (1, 1, 0, 1))

    def test_bundle_rejects_artifact_from_previous_run(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            store = RunStore(root)
            a = store.create(QuerySpec(report_name="A"))
            b = store.create(QuerySpec(report_name="B"))
            output = root / "output"; output.mkdir()
            foreign = output / f"relatorio_{a['id']}.json"
            foreign.write_text(json.dumps({"metadata": {"run_id": a["id"]}}), encoding="utf-8")
            b["result"] = {"json": str(foreign.relative_to(root))}
            store.write(b["id"], b)
            bundle = load_result_bundle(root, store.read(b["id"]))
            self.assertIsNone(bundle["payload"])
            self.assertTrue(bundle["errors"])


if __name__ == "__main__":
    unittest.main()
