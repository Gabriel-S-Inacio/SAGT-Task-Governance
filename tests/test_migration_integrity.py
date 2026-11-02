import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from bitrix24_task_auditor.config import BitrixConfig
from bitrix24_task_auditor.migration_client import AmbiguousWriteError, MigrationApiError, MigrationBitrixClient
from bitrix24_task_auditor.migration_models import create_plan
from bitrix24_task_auditor.migration_rule_engine import create_rules_plan
from bitrix24_task_auditor.migration_rules import MigrationRuleStore
from bitrix24_task_auditor.migration_service import execute_plan, preflight_plan, report_snapshot
from bitrix24_task_auditor.migration_store import MigrationStore, MigrationStoreError
from bitrix24_task_auditor.migration_ui import _execution_blockers, _preview_rows

from tests.test_migration import FakeClient, audit, link, task


SYNTHETIC_SOURCE_ID = "900001"
SYNTHETIC_CREATOR_ALPHA_ID = "920001"
SYNTHETIC_CREATOR_BETA_ID = "920002"
SYNTHETIC_TARGET_ALPHA_ID = "930001"


class IntegrityClient(FakeClient):
    def __init__(self, tasks=None, *, auto_old_responsible_auditor=False, fail_cleanup=False):
        super().__init__(tasks)
        self.auto_old_responsible_auditor = auto_old_responsible_auditor
        self.fail_cleanup = fail_cleanup
        self.get_calls = 0

    def get_task(self, task_id, extra_select=()):
        self.get_calls += 1
        return super().get_task(task_id, extra_select)

    def update_task(self, task_id, fields):
        self.updates.append((task_id, dict(fields)))
        if self.fail_cleanup and set(fields) == {"AUDITORS"}:
            raise MigrationApiError("cleanup denied", code="ACCESS_DENIED", status=403)
        raw = self.tasks[task_id]
        old_responsible = str(raw["responsibleId"])
        aliases = {"RESPONSIBLE_ID": "responsibleId", "ACCOMPLICES": "accomplices", "AUDITORS": "auditors"}
        for field, value in fields.items():
            raw[aliases[field]] = list(value) if isinstance(value, list) else value
        if "RESPONSIBLE_ID" in fields and self.auto_old_responsible_auditor:
            auditors = list(raw.get("auditors") or [])
            if old_responsible not in auditors:
                auditors.append(old_responsible)
            raw["auditors"] = auditors
        if self.timeout_after_write:
            raise AmbiguousWriteError("timeout")
        return {"result": {"task": {"id": task_id}}}


class MigrationIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.temp.name)
        self.store = MigrationStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def prepared(self, client, *, cleanup=False, tasks=None, links=None):
        imported = audit(
            tasks=tasks or [task("1", responsible="10", accomplices=("20",), auditors=("30",))],
            links=links or [link("1", "10", "responsible")],
        )
        plan = create_plan(
            imported,
            "10",
            "99",
            ["responsible"],
            allow_responsible_auditor_cleanup=cleanup,
        )
        plan = preflight_plan(plan, client)
        self.store.create(plan)
        self.store.approve(plan.plan_id, self.store.confirmation_phrase(plan))
        return plan

    def live(self, plan, client, *, max_tasks=None):
        with patch.dict(os.environ, {"BITRIX24_MIGRATION_WRITES_ENABLED": "YES"}):
            return execute_plan(
                self.store,
                plan.plan_id,
                client,
                dry_run=False,
                allow_live_writes=True,
                max_tasks=max_tasks,
            )

    def test_01_responsible_replacement_without_side_effect(self):
        client = IntegrityClient({"1": FakeClient.raw("1", responsible="10", accomplices=["20"], auditors=["30"])})
        report = self.live(self.prepared(client), client)
        self.assertEqual(report["state"]["live_results"]["1"]["result"], "ATUALIZADA_E_VERIFICADA")
        self.assertEqual(client.updates, [("1", {"RESPONSIBLE_ID": "99", "AUDITORS": ["30"]})])

    def test_02_automatic_old_responsible_auditor_is_observed(self):
        client = IntegrityClient({"1": FakeClient.raw("1", responsible="10", auditors=["30"])}, auto_old_responsible_auditor=True)
        report = self.live(self.prepared(client), client)
        attempt = report["attempts"][-1]
        self.assertEqual(attempt["result"], "ATUALIZADA_E_VERIFICADA")
        self.assertEqual(attempt["corrections"][0]["before"], ["30", "10"])
        self.assertEqual(client.tasks["1"]["auditors"], ["30"])
        self.assertEqual(client.updates, [
            ("1", {"RESPONSIBLE_ID": "99", "AUDITORS": ["30"]}),
            ("1", {"AUDITORS": ["30"]}),
        ])

    def test_03_approved_finalization_reaches_verified_state(self):
        client = IntegrityClient({"1": FakeClient.raw("1", responsible="10", auditors=["30"])}, auto_old_responsible_auditor=True)
        report = self.live(self.prepared(client), client)
        self.assertEqual(report["state"]["live_results"]["1"]["result"], "ATUALIZADA_E_VERIFICADA")
        self.assertEqual(report["attempts"][-1]["postcheck"]["AUDITORS"], ["30"])

    def test_04_finalization_preserves_all_previous_observers(self):
        client = IntegrityClient({"1": FakeClient.raw("1", responsible="10", auditors=["30", "31"])}, auto_old_responsible_auditor=True)
        report = self.live(self.prepared(client, cleanup=True), client)
        self.assertEqual(report["summary"]["updated_verified"], 1)
        self.assertEqual(client.tasks["1"]["auditors"], ["30", "31"])
        self.assertEqual(len(client.updates), 2)

    def test_05_unapproved_cleanup_is_never_sent(self):
        client = IntegrityClient({"1": FakeClient.raw("1", responsible="10", auditors=["30"])}, auto_old_responsible_auditor=True)
        plan = self.prepared(client)
        plan.operations[0].authorized_side_effect_corrections = {}
        plan.refresh_digest()
        self.store.save_plan(plan)
        self.store.approve(plan.plan_id, self.store.confirmation_phrase(plan))
        report = self.live(plan, client)
        self.assertEqual(report["state"]["live_results"]["1"]["result"], "OPERACAO_NAO_COMPROVADA")
        self.assertEqual(client.updates, [])

    def test_06_failed_finalization_is_not_reported_as_success(self):
        client = IntegrityClient(
            {"1": FakeClient.raw("1", responsible="10", auditors=["30"])},
            auto_old_responsible_auditor=True,
            fail_cleanup=True,
        )
        report = self.live(self.prepared(client, cleanup=True), client)
        self.assertEqual(report["summary"]["correction_pending"], 1)
        self.assertEqual(report["attempts"][-1]["corrections"][0]["result"], "FALHA_FINALIZACAO")
        self.assertEqual(len(client.updates), 2)

    def test_07_existing_observers_are_preserved(self):
        client = IntegrityClient({"1": FakeClient.raw("1", responsible="10", auditors=["30", "31"])})
        self.live(self.prepared(client), client)
        self.assertEqual(client.tasks["1"]["auditors"], ["30", "31"])

    def test_08_existing_participants_are_preserved(self):
        client = IntegrityClient({"1": FakeClient.raw("1", responsible="10", accomplices=["20", "21"], auditors=["30"])})
        self.live(self.prepared(client), client)
        self.assertEqual(client.tasks["1"]["accomplices"], ["20", "21"])

    def test_09_preexisting_source_auditor_is_not_removed(self):
        client = IntegrityClient(
            {"1": FakeClient.raw("1", responsible="10", auditors=["30", "10"])},
            auto_old_responsible_auditor=True,
        )
        report = self.live(self.prepared(client, cleanup=True), client)
        self.assertEqual(report["summary"]["updated_verified"], 1)
        self.assertIn("10", client.tasks["1"]["auditors"])
        self.assertEqual(len(client.updates), 1)

    def test_10_preexisting_source_participant_is_preserved(self):
        client = IntegrityClient({"1": FakeClient.raw("1", responsible="10", accomplices=["10", "20"], auditors=["30"])})
        report = self.live(self.prepared(client), client)
        self.assertEqual(report["summary"]["updated_verified"], 1)
        self.assertIn("10", client.tasks["1"]["accomplices"])

    def test_11_created_by_is_preserved(self):
        raw = FakeClient.raw("1", responsible="10", auditors=["30"])
        raw["createdBy"] = "40"
        client = IntegrityClient({"1": raw})
        report = self.live(self.prepared(client), client)
        self.assertEqual(report["attempts"][-1]["postcheck"]["CREATED_BY"], "40")

    def test_12_preview_distinguishes_role_from_total_removal(self):
        client = IntegrityClient({"1": FakeClient.raw("1", responsible="10", auditors=["10", "30"])})
        plan = self.prepared(client)
        row = _preview_rows(plan, None)[0]
        self.assertEqual(row["Efeito no papel"], "REMOVIDO_DO_PAPEL")
        self.assertEqual(row["Remoção total da tarefa"], "NÃO COMPROVADA NA PRÉVIA")
        self.assertIn("auditor", row["Outros vínculos da origem"])

    def test_13_postcheck_reports_effect_details(self):
        client = IntegrityClient({"1": FakeClient.raw("1", responsible="10", auditors=["30"])}, auto_old_responsible_auditor=True)
        report = self.live(self.prepared(client), client)
        attempt = report["attempts"][-1]
        self.assertEqual(attempt["side_effects"][0]["added"], ["10"])
        self.assertEqual(attempt["corrections"][0]["field"], "AUDITORS")
        self.assertEqual(attempt["postcheck"]["AUDITORS"], ["30"])

    def test_14_pending_correction_stops_next_task_and_blocks_batch(self):
        imported_tasks = [
            task("1", responsible="10", accomplices=("20",), auditors=("30",)),
            task("2", responsible="10", accomplices=("20",), auditors=("31",)),
        ]
        client = IntegrityClient(
            {
                "1": FakeClient.raw("1", responsible="10", accomplices=["20"], auditors=["30"]),
                "2": FakeClient.raw("2", responsible="10", accomplices=["20"], auditors=["31"]),
            },
            auto_old_responsible_auditor=True,
            fail_cleanup=True,
        )
        plan = self.prepared(client, tasks=imported_tasks, links=[link("1"), link("2")])
        report = self.live(plan, client)
        self.assertEqual([item[0] for item in client.updates], ["1", "1"])
        blockers = _execution_blockers(
            plan,
            report["state"],
            dry_run=False,
            writes_enabled=True,
            approval_error="",
            requested_operation_type="REPLACE_USER",
        )
        self.assertTrue(any("efeito colateral pendente" in item for item in blockers))
        with self.assertRaisesRegex(MigrationStoreError, "correção pendente"):
            self.live(plan, client)
        self.assertEqual([item[0] for item in client.updates], ["1", "1"])

    def test_15_timeout_after_write_is_verified_from_fresh_read(self):
        client = IntegrityClient({"1": FakeClient.raw("1", responsible="10", auditors=["30"])})
        client.timeout_after_write = True
        report = self.live(self.prepared(client), client)
        self.assertEqual(report["summary"]["updated_verified"], 1)

    def test_16_timeout_does_not_repeat_confirmed_update(self):
        client = IntegrityClient({"1": FakeClient.raw("1", responsible="10", auditors=["30"])})
        client.timeout_after_write = True
        plan = self.prepared(client)
        self.live(plan, client)
        self.live(plan, client)
        self.assertEqual(len(client.updates), 1)
        self.assertGreaterEqual(client.get_calls, 3)

    def test_17_task_outside_spreadsheet_is_not_touched(self):
        client = IntegrityClient({
            "1": FakeClient.raw("1", responsible="10", auditors=["30"]),
            "2": FakeClient.raw("2", responsible="10", auditors=["31"]),
        })
        self.live(self.prepared(client), client)
        self.assertEqual(client.tasks["2"]["responsibleId"], "10")
        self.assertNotIn("2", [item[0] for item in client.updates])

    def _conditional_plan(self, condition_type):
        catalog = MigrationRuleStore(self.root)
        rule = catalog.create(
            name=f"Regra {condition_type}",
            description="",
            source_user_id="10",
            operation="REPLACE_RESPONSIBLE",
            target_user_id="99",
            condition_type=condition_type,
            condition_field_id=None,
            condition_value="40" if condition_type == "CREATED_BY" else "101",
            cleanup_previous_responsible_auditor=True,
        )
        properties = {"GROUP_ID": ("101",)} if condition_type == "GROUP_ID" else {}
        source = audit(
            tasks=[task("1", responsible="10", auditors=("30",), properties=properties)],
            links=[link("1", "10", "responsible")],
            property_fields={"GROUP_ID": "Grupo"} if condition_type == "GROUP_ID" else {},
        )
        raw = FakeClient.raw("1", responsible="10", auditors=["30"])
        if condition_type == "GROUP_ID":
            raw["groupId"] = "101"
        client = IntegrityClient({"1": raw})
        return preflight_plan(create_rules_plan(source, [rule]), client), client

    def test_18_created_by_conditional_rule_remains_supported(self):
        plan, _ = self._conditional_plan("CREATED_BY")
        self.assertEqual(plan.task_ids, ["1"])
        self.assertEqual(plan.rule_snapshots[0]["condition_value"], "40")

    def test_19_group_id_conditional_rule_remains_supported(self):
        plan, _ = self._conditional_plan("GROUP_ID")
        self.assertEqual(plan.task_ids, ["1"])
        self.assertEqual(plan.operations[0].patch, {"RESPONSIBLE_ID": "99", "AUDITORS": ["30"]})

    def test_20_report_contains_before_after_effects_and_corrections(self):
        client = IntegrityClient({"1": FakeClient.raw("1", responsible="10", auditors=["30"])}, auto_old_responsible_auditor=True)
        plan = self.prepared(client, cleanup=True)
        self.live(plan, client)
        report = report_snapshot(self.store, plan.plan_id)
        attempt = report["attempts"][-1]
        for field in ("integrity_before", "patch", "postcheck", "side_effects", "corrections", "result"):
            self.assertIn(field, attempt)

    def test_21_dynamic_rule_binds_final_state_to_single_approval(self):
        plan, client = self._conditional_plan("CREATED_BY")
        client.auto_old_responsible_auditor = True
        self.store.create(plan)
        approval = self.store.approve(plan.plan_id, self.store.confirmation_phrase(plan))
        self.assertEqual(
            approval["authorized_side_effect_corrections"],
            {"1": {"AUDITORS_REMOVE_IF_REINTRODUCED": ["10"]}},
        )
        report = self.live(plan, client)
        self.assertEqual(report["summary"]["updated_verified"], 1)
        self.assertEqual(client.tasks["1"]["auditors"], ["30"])
        self.assertEqual(len(client.updates), 2)

    def test_22_new_preview_authorizes_final_auditor_state(self):
        client = IntegrityClient({"1": FakeClient.raw("1", responsible="10", auditors=["30"])})
        plan = self.prepared(client)
        report = self.live(plan, client)
        self.assertEqual(report["state"]["live_results"]["1"]["result"], "ATUALIZADA_E_VERIFICADA")
        self.assertEqual(plan.operations[0].patch["AUDITORS"], ["30"])
        self.assertEqual(
            plan.operations[0].authorized_side_effect_corrections,
            {"AUDITORS_REMOVE_IF_REINTRODUCED": ["10"]},
        )

    def test_23_unproven_responsible_does_not_block_safe_accomplice_task(self):
        imported = audit(
            tasks=[
                task("1", responsible="10", accomplices=("20",), auditors=("30",)),
                task("2", responsible="20", accomplices=("10", "21"), auditors=("31",)),
            ],
            links=[link("1", "10", "responsible"), link("2", "10", "accomplice")],
        )
        client = IntegrityClient({
            "1": FakeClient.raw("1", responsible="10", accomplices=["20"], auditors=["30"]),
            "2": FakeClient.raw("2", responsible="20", accomplices=["10", "21"], auditors=["31"]),
        })
        plan = preflight_plan(create_plan(imported, "10", "99", ["responsible", "accomplice"]), client)
        plan.operations[0].authorized_side_effect_corrections = {}
        plan.refresh_digest()
        self.store.create(plan)
        self.store.approve(plan.plan_id, self.store.confirmation_phrase(plan))
        blockers = _execution_blockers(
            plan,
            self.store.read_state(plan.plan_id),
            dry_run=False,
            writes_enabled=True,
            approval_error="",
            requested_operation_type="REPLACE_USER",
        )
        self.assertFalse(any("ainda não homologada" in item for item in blockers))
        report = self.live(plan, client)
        self.assertEqual(report["state"]["live_results"]["1"]["result"], "OPERACAO_NAO_COMPROVADA")
        self.assertEqual(report["state"]["live_results"]["2"]["result"], "ATUALIZADA_E_VERIFICADA")
        self.assertEqual(client.updates, [("2", {"ACCOMPLICES": ["99", "21"]})])

    def test_24_combined_update_payload_is_serialized_but_not_proven_safe(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"result":{"task":{"id":"1"}}}'

        def opener(request, timeout):
            captured.update(json.loads(request.data.decode("utf-8")))
            return Response()

        client = MigrationBitrixClient(
            "https://portal.example/rest/1/token",
            BitrixConfig(timeout_seconds=1, max_retries=0),
            opener=opener,
            sleeper=lambda _: None,
            min_interval_seconds=0,
        )
        client.update_task("1", {"RESPONSIBLE_ID": "99", "AUDITORS": ["30"]})
        self.assertEqual(captured, {
            "taskId": 1,
            "fields": {"RESPONSIBLE_ID": "99", "AUDITORS": ["30"]},
        })
        self.assertEqual(set(captured["fields"]), {"RESPONSIBLE_ID", "AUDITORS"})

    def test_25_requested_created_by_rules_match_only_four_scoped_tasks(self):
        catalog = MigrationRuleStore(self.root)
        first = catalog.create(
            name="CREATED_BY sintético alfa",
            description="",
            source_user_id=SYNTHETIC_SOURCE_ID,
            operation="REPLACE_RESPONSIBLE",
            target_user_id=SYNTHETIC_TARGET_ALPHA_ID,
            condition_type="CREATED_BY",
            condition_value=SYNTHETIC_CREATOR_ALPHA_ID,
        )
        second = catalog.create(
            name="CREATED_BY sintético beta",
            description="",
            source_user_id=SYNTHETIC_SOURCE_ID,
            operation="REPLACE_RESPONSIBLE",
            target_user_id=SYNTHETIC_CREATOR_BETA_ID,
            condition_type="CREATED_BY",
            condition_value=SYNTHETIC_CREATOR_BETA_ID,
        )
        imported_tasks = [
            replace(task(str(task_id), responsible=SYNTHETIC_SOURCE_ID, auditors=("40",)), created_by=creator)
            for task_id, creator in (
                ("1", SYNTHETIC_CREATOR_ALPHA_ID), ("2", SYNTHETIC_CREATOR_ALPHA_ID),
                ("3", SYNTHETIC_CREATOR_BETA_ID), ("4", SYNTHETIC_CREATOR_BETA_ID),
            )
        ]
        source = audit(
            tasks=imported_tasks,
            links=[link(str(task_id), SYNTHETIC_SOURCE_ID, "responsible") for task_id in range(1, 5)],
        )
        live = {}
        for task_id, creator in (
            ("1", SYNTHETIC_CREATOR_ALPHA_ID), ("2", SYNTHETIC_CREATOR_ALPHA_ID),
            ("3", SYNTHETIC_CREATOR_BETA_ID), ("4", SYNTHETIC_CREATOR_BETA_ID),
        ):
            raw = FakeClient.raw(task_id, responsible=SYNTHETIC_SOURCE_ID, accomplices=["20"], auditors=["40"])
            raw["createdBy"] = creator
            live[task_id] = raw
        plan = preflight_plan(create_rules_plan(source, [first, second]), IntegrityClient(live))
        self.assertEqual(plan.task_ids, ["1", "2", "3", "4"])
        self.assertEqual(
            [operation.patch["RESPONSIBLE_ID"] for operation in plan.operations],
            [SYNTHETIC_TARGET_ALPHA_ID, SYNTHETIC_TARGET_ALPHA_ID, SYNTHETIC_CREATOR_BETA_ID, SYNTHETIC_CREATOR_BETA_ID],
        )
        self.assertTrue(all(operation.patch["AUDITORS"] == ["40"] for operation in plan.operations))
        self.assertTrue(all(operation.authorized_side_effect_corrections for operation in plan.operations))

    def test_26_write_http_5xx_is_ambiguous_and_never_retried(self):
        calls = 0

        def opener(request, timeout):
            nonlocal calls
            calls += 1
            raise HTTPError(request.full_url, 503, "Service Unavailable", {}, None)

        client = MigrationBitrixClient(
            "https://portal.example/rest/1/test-token",
            BitrixConfig(timeout_seconds=1, max_retries=4),
            opener=opener,
            sleeper=lambda _: None,
            min_interval_seconds=0,
        )

        with self.assertRaises(AmbiguousWriteError) as raised:
            client.update_task("1", {"RESPONSIBLE_ID": "99"})

        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
