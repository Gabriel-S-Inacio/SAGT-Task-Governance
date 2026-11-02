import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bitrix24_task_auditor.exporters import export_xlsx
from bitrix24_task_auditor.migration_client import AmbiguousWriteError, MigrationApiError
from bitrix24_task_auditor.migration_importer import ImportedAudit, ImportedLink, ImportedTask, import_audit_xlsx
from bitrix24_task_auditor.migration_models import (
    MigrationValidationError, OwnerRule, create_owner_plan, create_plan,
)
from bitrix24_task_auditor.migration_service import build_final_report, execute_plan, preflight_plan, report_snapshot
from bitrix24_task_auditor.migration_store import MigrationStore, MigrationStoreError
from bitrix24_task_auditor.migration_ui import _execution_blockers


SYNTHETIC_SOURCE_ID = "900001"
SYNTHETIC_TARGET_ID = "900002"
SYNTHETIC_OWNER_ALPHA_ID = "910001"
SYNTHETIC_OWNER_BETA_ID = "910002"


def task(
    task_id="1", responsible="10", accomplices=("10", "20"), auditors=("30", "10"),
    changed="2026-01-01T00:00:00+00:00", properties=None,
):
    return ImportedTask(
        task_id, f"Task {task_id}", responsible, "40", accomplices, auditors, changed,
        properties or {},
    )


def link(task_id="1", user="10", role="responsible"):
    return ImportedLink(task_id, user, role, "confirmed", role, (user,), "confirmed")


def audit(tasks=None, links=None, sha="a" * 64, property_fields=None):
    items = tasks or [task()]
    return ImportedAudit(
        "audit.xlsx", sha, 1,
        {"run_id": "run1", "schema_version": 1, "partial": False, "pagination_complete": True, "run_status": "completed", "read_only": True},
        {item.task_id: item for item in items}, tuple(links or [link()]), (), (), property_fields or {},
    )


class FakeClient:
    responsible_reassignment_without_retention_verified = True

    def __init__(self, tasks=None):
        self.tasks = tasks or {"1": self.raw()}
        self.updates = []
        self.timeout_after_write = False

    @staticmethod
    def raw(task_id="1", responsible="10", accomplices=None, auditors=None, changed="2026-01-01T00:00:00+00:00", edit=True):
        return {
            "id": task_id, "title": f"Task {task_id}", "responsibleId": responsible, "createdBy": "40",
            "accomplices": accomplices if accomplices is not None else ["10", "20"],
            "auditors": auditors if auditors is not None else ["30", "10"],
            "changedDate": changed, "action": {"edit": edit},
        }

    def get_user(self, user_id):
        return {"ID": user_id, "NAME": "Target", "ACTIVE": "Y"}

    def get_task(self, task_id, extra_select=()):
        if task_id not in self.tasks:
            raise MigrationApiError("not found", code="NOT_ACCESSIBLE", status=404)
        return dict(self.tasks[task_id])

    def update_task(self, task_id, fields):
        self.updates.append((task_id, dict(fields)))
        raw = self.tasks[task_id]
        aliases = {"RESPONSIBLE_ID": "responsibleId", "ACCOMPLICES": "accomplices", "AUDITORS": "auditors"}
        for field, value in fields.items():
            raw[aliases[field]] = value
        if self.timeout_after_write:
            raise AmbiguousWriteError("timeout")
        return {"result": {"task": {"id": task_id}}}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.temp.name)
        self.store = MigrationStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def prepared(self, roles, source_audit=None, client=None):
        plan = create_plan(source_audit or audit(), "10", "99", roles)
        plan = preflight_plan(plan, client or FakeClient())
        self.store.create(plan)
        return plan

    def prepared_add(self, source_audit, client):
        plan = create_plan(source_audit, "10", "99", ["accomplice"], operation_type="ADD_USER")
        plan = preflight_plan(plan, client)
        self.store.create(plan)
        return plan

    def approve(self, plan):
        return self.store.approve(plan.plan_id, self.store.confirmation_phrase(plan))

    def test_01_responsible_replacement(self):
        plan = create_plan(audit(), "10", "99", ["responsible"])
        self.assertEqual(plan.operations[0].patch, {"RESPONSIBLE_ID": "99"})

    def test_02_accomplice_replacement(self):
        plan = create_plan(audit(links=[link(role="accomplice")]), "10", "99", ["accomplice"])
        self.assertEqual(plan.operations[0].patch["ACCOMPLICES"], ["99", "20"])

    def test_03_auditor_replacement(self):
        plan = create_plan(audit(links=[link(role="auditor")]), "10", "99", ["auditor"])
        self.assertEqual(plan.operations[0].patch["AUDITORS"], ["30", "99"])

    def test_04_creator_not_editable(self):
        source = audit(links=[link(user="40", role="creator")])
        plan = create_plan(source, "40", "99", ["creator"])
        self.assertFalse(plan.operations)
        self.assertEqual(plan.preview[0].eligibility, "NAO_SUPORTADO")

    def test_05_multiple_roles_one_patch(self):
        source = audit(links=[link(role="responsible"), link(role="accomplice"), link(role="auditor")])
        plan = create_plan(source, "10", "99", ["responsible", "accomplice", "auditor"])
        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(set(plan.operations[0].patch), {"RESPONSIBLE_ID", "ACCOMPLICES", "AUDITORS"})

    def test_06_target_already_in_list(self):
        source = audit(tasks=[task(accomplices=("10", "99", "20"))], links=[link(role="accomplice")])
        plan = preflight_plan(create_plan(source, "10", "99", ["accomplice"]), FakeClient({"1": FakeClient.raw(accomplices=["10", "99", "20"])}))
        self.assertEqual(plan.preview[0].eligibility, "ELEGIVEL")
        self.assertEqual(plan.preview[0].proposed_value, ["99", "20"])
        self.assertEqual(plan.task_ids, ["1"])

    def test_07_preserves_other_users_and_order(self):
        source = audit(tasks=[task(accomplices=("20", "10", "30"))], links=[link(role="accomplice")])
        plan = create_plan(source, "10", "99", ["accomplice"])
        self.assertEqual(plan.operations[0].patch["ACCOMPLICES"], ["20", "99", "30"])

    def test_08_task_outside_spreadsheet_is_never_planned(self):
        plan = create_plan(audit(), "10", "99", ["responsible"])
        self.assertEqual(plan.task_ids, ["1"])
        self.assertNotIn("2", plan.task_ids)

    def test_09_unregistered_role_is_not_eligible(self):
        with self.assertRaises(MigrationValidationError):
            create_plan(audit(), "10", "99", ["auditor"])

    def test_10_missing_source_is_divergent(self):
        client = FakeClient({"1": FakeClient.raw(responsible="20")})
        plan = preflight_plan(create_plan(audit(), "10", "99", ["responsible"]), client)
        self.assertEqual(plan.preview[0].eligibility, "DIVERGENTE")

    def test_11_unrelated_changed_date_does_not_block_field_operation(self):
        raw = FakeClient.raw(changed="2026-02-01T00:00:00+00:00")
        raw.update(title="Título alterado", description="Descrição e comentários alheios ao vínculo")
        client = FakeClient({"1": raw})
        plan = preflight_plan(create_plan(audit(), "10", "99", ["responsible"]), client)
        self.assertEqual(plan.preview[0].eligibility, "ELEGIVEL")

    def test_12_permission_absent(self):
        client = FakeClient({"1": FakeClient.raw(edit=False)})
        plan = preflight_plan(create_plan(audit(), "10", "99", ["responsible"]), client)
        self.assertEqual(plan.preview[0].eligibility, "SEM_PERMISSAO")

    def test_13_timeout_after_possible_update_is_verified_not_retried(self):
        client = FakeClient(); client.timeout_after_write = True
        plan = self.prepared(["responsible"], client=client); self.approve(plan)
        with patch.dict(os.environ, {"BITRIX24_MIGRATION_WRITES_ENABLED": "YES"}):
            report = execute_plan(self.store, plan.plan_id, client, dry_run=False, allow_live_writes=True)
        self.assertEqual(report["summary"]["updated_verified"], 1)
        self.assertEqual(len(client.updates), 1)

    def test_14_interruption_and_resume_in_batches(self):
        tasks = [task("1"), task("2")]
        source = audit(tasks=tasks, links=[link("1"), link("2")])
        client = FakeClient({"1": FakeClient.raw("1"), "2": FakeClient.raw("2")})
        plan = self.prepared(["responsible"], source, client)
        first = execute_plan(self.store, plan.plan_id, client, dry_run=True, max_tasks=1)
        self.assertEqual(first["state"]["status"], "PAUSADA_LOTE")
        second = execute_plan(self.store, plan.plan_id, client, dry_run=True, max_tasks=1)
        self.assertEqual(second["summary"]["dry_run"], 2)
        self.assertFalse(client.updates)

    def test_15_approval_invalidated_when_plan_changes(self):
        plan = self.prepared(["responsible"]); self.approve(plan)
        plan.operations[0].patch["RESPONSIBLE_ID"] = "100"
        self.store.save_plan(plan)
        with self.assertRaises(MigrationStoreError):
            self.store.valid_approval(plan.plan_id)

    def test_16_distinct_audits_same_user_have_distinct_binding(self):
        first = create_plan(audit(sha="a" * 64), "10", "99", ["responsible"])
        second = create_plan(audit(sha="b" * 64), "10", "99", ["responsible"])
        self.assertNotEqual(first.digest, second.digest)

    def test_17_report_export(self):
        plan = self.prepared(["responsible"])
        execute_plan(self.store, plan.plan_id, FakeClient(), dry_run=True)
        build_final_report(self.store, plan.plan_id)
        directory = self.store.plan_dir(plan.plan_id)
        self.assertTrue((directory / "relatorio_migracao.xlsx").is_file())
        self.assertTrue((directory / "relatorio_migracao.json").is_file())

    def test_18_dry_run_never_writes(self):
        client = FakeClient(); plan = self.prepared(["responsible"], client=client)
        execute_plan(self.store, plan.plan_id, client, dry_run=True)
        self.assertEqual(client.updates, [])

    def test_19_real_run_requires_explicit_approval(self):
        client = FakeClient(); plan = self.prepared(["responsible"], client=client)
        execute_plan(self.store, plan.plan_id, client, dry_run=True)
        with patch.dict(os.environ, {"BITRIX24_MIGRATION_WRITES_ENABLED": "YES"}):
            with self.assertRaises(MigrationStoreError):
                execute_plan(self.store, plan.plan_id, client, dry_run=False, allow_live_writes=True)
        self.assertFalse(client.updates)

    def test_20_verified_change_is_not_repeated(self):
        client = FakeClient(); plan = self.prepared(["responsible"], client=client); self.approve(plan)
        with patch.dict(os.environ, {"BITRIX24_MIGRATION_WRITES_ENABLED": "YES"}):
            execute_plan(self.store, plan.plan_id, client, dry_run=False, allow_live_writes=True)
            execute_plan(self.store, plan.plan_id, client, dry_run=False, allow_live_writes=True)
        self.assertEqual(len(client.updates), 1)

    def test_importer_hash_and_integrity(self):
        path = self.root / "synthetic.xlsx"
        export_xlsx(path, [
            ("Tarefas", ["ID", "Título", "Responsável", "Criador", "Participantes", "Observadores", "Alterada em"],
             [["1", "Task", "10", "40", "10,20", "30", "2026-01-01T00:00:00+00:00"]]),
            ("Vínculos", ["task_id", "user_id", "role", "verification", "evidence_field", "observed_user_ids", "reason"],
             [["1", "10", "responsible", "confirmed", "responsibleId", '["10"]', "ok"]]),
            ("Metadados", ["Propriedade", "Valor"], [
                ["schema_version", "1"], ["read_only", "true"], ["partial", "false"],
                ["pagination_complete", "true"], ["run_status", "completed"], ["run_id", "run1"],
            ]),
        ])
        imported = import_audit_xlsx(path)
        self.assertTrue(imported.executable)
        self.assertEqual(len(imported.sha256), 64)

    def test_excel_serial_timestamp_is_normalized_to_utc(self):
        path = self.root / "serial.xlsx"
        export_xlsx(path, [
            ("Tarefas", ["ID", "Título", "Responsável", "Criador", "Participantes", "Observadores", "Alterada em"],
             [["1", "Task", "10", "40", "10,20", "30", 46223.57670138889]]),
            ("Vínculos", ["task_id", "user_id", "role", "verification", "evidence_field", "observed_user_ids", "reason"],
             [["1", "10", "responsible", "confirmed", "responsibleId", '["10"]', "ok"]]),
            ("Metadados", ["Propriedade", "Valor"], [
                ["schema_version", "1"], ["read_only", "true"], ["partial", "false"],
                ["pagination_complete", "true"], ["run_status", "completed"], ["run_id", "run1"],
            ]),
        ])
        imported = import_audit_xlsx(path)
        self.assertEqual(imported.tasks["1"].changed_at, "2026-07-20T13:50:27+00:00")

    def test_excel_serial_and_bitrix_offset_timestamp_are_equal(self):
        source = audit(tasks=[task(changed="46223.57670138889")])
        client = FakeClient({"1": FakeClient.raw(changed="2026-07-20T10:50:27-03:00")})
        plan = preflight_plan(create_plan(source, "10", "99", ["responsible"]), client)
        self.assertEqual(plan.task_ids, ["1"])
        self.assertEqual(plan.preview[0].eligibility, "ELEGIVEL")

    def test_revalidation_can_clear_a_previous_false_divergence(self):
        source = audit(tasks=[task(changed="46223.57670138889")])
        plan = create_plan(source, "10", "99", ["responsible"])
        plan = preflight_plan(plan, FakeClient({"1": FakeClient.raw(responsible="20")}))
        self.assertFalse(plan.task_ids)
        self.assertEqual(plan.preview[0].eligibility, "DIVERGENTE")
        plan = preflight_plan(plan, FakeClient({"1": FakeClient.raw(responsible="10")}))
        self.assertEqual(plan.task_ids, ["1"])
        self.assertEqual(plan.preview[0].eligibility, "ELEGIVEL")

    def test_execution_rejects_plan_without_eligible_tasks(self):
        client = FakeClient({"1": FakeClient.raw(responsible="20")})
        plan = preflight_plan(create_plan(audit(), "10", "99", ["responsible"]), client)
        self.store.create(plan)
        with self.assertRaisesRegex(MigrationStoreError, "não possui tarefas elegíveis"):
            execute_plan(self.store, plan.plan_id, client, dry_run=True)
        self.assertFalse(client.updates)

    def test_approval_rejects_divergent_operations(self):
        client = FakeClient({"1": FakeClient.raw(responsible="20")})
        plan = preflight_plan(create_plan(audit(), "10", "99", ["responsible"]), client)
        self.store.create(plan)
        with self.assertRaisesRegex(MigrationStoreError, "sem operações elegíveis"):
            self.store.approve(plan.plan_id, self.store.confirmation_phrase(plan))

    def test_ui_explains_every_execution_blocker(self):
        client = FakeClient({"1": FakeClient.raw(responsible="20")})
        plan = preflight_plan(create_plan(audit(), "10", "99", ["responsible"]), client)
        blockers = _execution_blockers(
            plan, {"cancel_requested": False}, dry_run=True, writes_enabled=False, approval_error="ausente",
        )
        self.assertTrue(any("Nenhuma tarefa" in item for item in blockers))
        self.assertFalse(any("escrita real" in item.lower() for item in blockers))

    def test_plan_records_import_scope_counts(self):
        items = [task(str(index)) for index in range(1, 6)]
        source = audit(tasks=items, links=[link(str(index)) for index in range(1, 5)])
        plan = create_plan(source, "10", "99", ["responsible"])
        self.assertEqual(plan.audit_task_count, 5)
        self.assertEqual(plan.audit_link_count, 4)
        self.assertEqual(plan.matched_link_count, 4)

    def test_target_field_composition_change_is_incorporated_when_source_remains(self):
        source = audit(tasks=[task(accomplices=("10", "20"))], links=[link(role="accomplice")])
        client = FakeClient({"1": FakeClient.raw(accomplices=["10", "20", "30"], changed="2026-02-01T00:00:00+00:00")})
        plan = preflight_plan(create_plan(source, "10", "99", ["accomplice"]), client)
        self.assertEqual(plan.task_ids, ["1"])
        self.assertEqual(plan.preview[0].eligibility, "ELEGIVEL")
        self.assertEqual(plan.preview[0].audited_value, ["10", "20"])
        self.assertEqual(plan.preview[0].previous_value, ["10", "20", "30"])
        self.assertEqual(plan.preview[0].proposed_value, ["99", "20", "30"])

    def test_add_user_preserves_source_and_all_members(self):
        source = audit(tasks=[task(accomplices=("20", "10", "30"))], links=[link(role="accomplice")])
        plan = create_plan(source, "10", "99", ["accomplice"], operation_type="ADD_USER")
        self.assertEqual(plan.operations[0].patch["ACCOMPLICES"], ["20", "10", "30", "99"])
        self.assertEqual(plan.preview[0].source_disposition, "MANTIDO")
        self.assertIn("10", plan.preview[0].proposed_value)

    def test_add_user_requires_source_to_remain_in_current_role(self):
        source = audit(tasks=[task(accomplices=("10", "20"))], links=[link(role="accomplice")])
        client = FakeClient({"1": FakeClient.raw(accomplices=["20"])})
        plan = preflight_plan(
            create_plan(source, "10", "99", ["accomplice"], operation_type="ADD_USER"), client,
        )
        self.assertEqual(plan.preview[0].eligibility, "DIVERGENTE")

    def test_add_user_is_already_satisfied_when_target_present(self):
        source = audit(tasks=[task(accomplices=("10", "20"))], links=[link(role="accomplice")])
        client = FakeClient({"1": FakeClient.raw(accomplices=["10", "20", "99"])})
        plan = preflight_plan(
            create_plan(source, "10", "99", ["accomplice"], operation_type="ADD_USER"), client,
        )
        self.assertEqual(plan.preview[0].eligibility, "JA_ATENDIDO")
        self.assertFalse(plan.task_ids)

    def test_add_user_source_absent_is_divergent_even_when_target_is_present(self):
        source = audit(tasks=[task(accomplices=("10", "20"))], links=[link(role="accomplice")])
        client = FakeClient({"1": FakeClient.raw(accomplices=["20", "99"])})
        plan = preflight_plan(
            create_plan(source, "10", "99", ["accomplice"], operation_type="ADD_USER"), client,
        )
        self.assertEqual(plan.preview[0].eligibility, "DIVERGENTE")
        self.assertFalse(plan.task_ids)

    def test_add_user_mock_write_persists_without_removing_source(self):
        source = audit(tasks=[task(accomplices=("10", "20"))], links=[link(role="accomplice")])
        client = FakeClient({"1": FakeClient.raw(accomplices=["10", "20"])})
        plan = self.prepared_add(source, client)
        self.approve(plan)
        with patch.dict(os.environ, {"BITRIX24_MIGRATION_WRITES_ENABLED": "YES"}):
            report = execute_plan(
                self.store, plan.plan_id, client, dry_run=False, allow_live_writes=True,
                expected_operation_type="ADD_USER",
            )
        self.assertEqual(report["summary"]["updated_verified"], 1)
        self.assertEqual(client.tasks["1"]["accomplices"], ["10", "20", "99"])

    def test_operation_intent_mismatch_blocks_execution(self):
        plan = self.prepared(["responsible"])
        with self.assertRaisesRegex(MigrationStoreError, "Objetivo incompatível"):
            execute_plan(
                self.store, plan.plan_id, FakeClient(), dry_run=True,
                expected_operation_type="ADD_USER",
            )

    def test_add_user_preview_for_four_links_marks_origin_preserved(self):
        items = [
            task("1", accomplices=("10", "20"), auditors=("30",)),
            task("2", accomplices=("10",), auditors=("30",)),
            task("3", accomplices=("20",), auditors=("30", "10")),
            task("4", accomplices=("20",), auditors=("10",)),
        ]
        links = [link("1", role="accomplice"), link("2", role="accomplice"), link("3", role="auditor"), link("4", role="auditor")]
        source = audit(tasks=items, links=links)
        plan = create_plan(source, "10", "99", ["accomplice", "auditor"], operation_type="ADD_USER")
        self.assertEqual(len(plan.preview), 4)
        self.assertTrue(all(row.source_disposition == "MANTIDO" for row in plan.preview))
        self.assertTrue(all("10" in row.proposed_value and "99" in row.proposed_value for row in plan.preview))

    def test_pilot_add_preview_accepts_all_four_links_despite_changed_date(self):
        source_id, target_id = SYNTHETIC_SOURCE_ID, SYNTHETIC_TARGET_ID
        items = [
            task("1", responsible=source_id, accomplices=(source_id, "20"), auditors=("30",)),
            task("2", responsible=source_id, accomplices=(source_id,), auditors=("30",)),
            task("3", responsible=source_id, accomplices=("20",), auditors=("30", source_id)),
            task("4", responsible=source_id, accomplices=("20",), auditors=(source_id,)),
        ]
        links = [
            link("1", source_id, "accomplice"), link("2", source_id, "accomplice"),
            link("3", source_id, "auditor"), link("4", source_id, "auditor"),
        ]
        source = audit(tasks=items, links=links)
        client = FakeClient({
            "1": FakeClient.raw("1", responsible=source_id, accomplices=[source_id, "20", "21"], auditors=["30"], changed="2026-10-01T00:00:00+00:00"),
            "2": FakeClient.raw("2", responsible=source_id, accomplices=["22", source_id], auditors=["30"], changed="2026-10-02T00:00:00+00:00"),
            "3": FakeClient.raw("3", responsible=source_id, accomplices=["20"], auditors=["30", source_id, "31"], changed="2026-10-03T00:00:00+00:00"),
            "4": FakeClient.raw("4", responsible=source_id, accomplices=["20"], auditors=["32", source_id], changed="2026-10-04T00:00:00+00:00"),
        })
        plan = preflight_plan(
            create_plan(source, source_id, target_id, ["accomplice", "auditor"], operation_type="ADD_USER"),
            client,
        )
        self.assertEqual(plan.task_ids, ["1", "2", "3", "4"])
        self.assertTrue(all(row.eligibility == "ELEGIVEL" for row in plan.preview))
        self.assertTrue(all(source_id in row.proposed_value for row in plan.preview))
        self.assertTrue(all(target_id in row.proposed_value for row in plan.preview))

    def test_concurrent_target_field_change_after_preview_blocks_only_affected_task(self):
        items = [task("1", accomplices=("10", "20")), task("2", accomplices=("10", "21"))]
        source = audit(tasks=items, links=[link("1", role="accomplice"), link("2", role="accomplice")])
        client = FakeClient({
            "1": FakeClient.raw("1", accomplices=["10", "20", "30"]),
            "2": FakeClient.raw("2", accomplices=["10", "21"]),
        })
        plan = create_plan(source, "10", "99", ["accomplice"], operation_type="ADD_USER")
        plan = preflight_plan(plan, client)
        self.store.create(plan)
        client.tasks["1"]["accomplices"] = ["10", "20", "30", "40"]
        report = execute_plan(
            self.store, plan.plan_id, client, dry_run=True, expected_operation_type="ADD_USER",
        )
        results = report["state"]["results"]
        self.assertEqual(results["1"]["result"], "DIVERGENTE")
        self.assertEqual(results["2"]["result"], "PREVISTO_DRY_RUN")
        self.assertFalse(client.updates)

    def test_legacy_plan_cannot_be_executed_or_reused(self):
        plan = self.prepared(["responsible"])
        plan.logic_version = 1
        plan.refresh_digest()
        self.store.save_plan(plan)
        with self.assertRaisesRegex(MigrationStoreError, "lógica anterior"):
            execute_plan(self.store, plan.plan_id, FakeClient(), dry_run=True)

    def test_linear_ui_does_not_offer_persisted_plan_selection(self):
        source = (Path(__file__).parents[1] / "src" / "bitrix24_task_auditor" / "migration_ui.py").read_text(encoding="utf-8")
        self.assertNotIn("store.list_plans", source)
        self.assertNotIn("Plano persistido", source)
        self.assertNotIn("Revalidar prévia", source)
        self.assertIn("Confirmar esta prévia", source)
        self.assertIn("Simulação concluída", source)
        self.assertIn("Execução real bloqueada", source)

    def test_add_user_rejects_scalar_roles(self):
        with self.assertRaisesRegex(MigrationValidationError, "somente para participante e observador"):
            create_plan(audit(), "10", "99", ["responsible"], operation_type="ADD_USER")

    def test_render_snapshot_does_not_overwrite_report_files(self):
        plan = self.prepared(["responsible"])
        directory = self.store.plan_dir(plan.plan_id)
        self.assertFalse((directory / "final.json").exists())
        report = report_snapshot(self.store, plan.plan_id)
        self.assertEqual(report["plan"]["plan_id"], plan.plan_id)
        self.assertFalse((directory / "final.json").exists())
        self.assertFalse((directory / "relatorio_migracao.xlsx").exists())

    def test_dry_run_and_live_execution_have_independent_checkpoints(self):
        items = [task(str(index), accomplices=("10", str(20 + index))) for index in range(1, 5)]
        source = audit(tasks=items, links=[link(str(index), role="accomplice") for index in range(1, 5)])
        client = FakeClient({
            str(index): FakeClient.raw(str(index), accomplices=["10", str(20 + index)])
            for index in range(1, 5)
        })
        plan = create_plan(source, "10", "99", ["accomplice"], operation_type="ADD_USER")
        plan = preflight_plan(plan, client)
        self.store.create(plan)

        simulated = execute_plan(
            self.store, plan.plan_id, client, dry_run=True, expected_operation_type="ADD_USER",
        )
        self.assertEqual(simulated["summary"]["dry_run"], 4)
        self.assertEqual(simulated["summary"]["updated_verified"], 0)
        self.assertEqual(client.updates, [])
        state = self.store.read_state(plan.plan_id)
        self.assertEqual(len(state["dry_run_results"]), 4)
        self.assertEqual(state["live_results"], {})

        self.approve(plan)
        with patch.dict(os.environ, {"BITRIX24_MIGRATION_WRITES_ENABLED": "YES"}):
            executed = execute_plan(
                self.store, plan.plan_id, client, dry_run=False, allow_live_writes=True,
                expected_operation_type="ADD_USER",
            )
            repeated = execute_plan(
                self.store, plan.plan_id, client, dry_run=False, allow_live_writes=True,
                expected_operation_type="ADD_USER",
            )

        self.assertEqual(executed["summary"]["updated_verified"], 4)
        self.assertEqual(repeated["summary"]["updated_verified"], 4)
        self.assertEqual(len(client.updates), 4)
        self.assertTrue(all("10" in raw["accomplices"] and "99" in raw["accomplices"] for raw in client.tasks.values()))
        state = self.store.read_state(plan.plan_id)
        self.assertEqual(len(state["dry_run_results"]), 4)
        self.assertEqual(len(state["live_results"]), 4)
        self.assertTrue(all(item["result"] == "ATUALIZADA_E_VERIFICADA" for item in state["live_results"].values()))

    def test_approved_live_execution_is_blocked_when_write_flag_is_disabled(self):
        client = FakeClient()
        plan = self.prepared(["responsible"], client=client)
        execute_plan(self.store, plan.plan_id, client, dry_run=True)
        self.approve(plan)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(MigrationStoreError, "bloqueadas por configuração segura"):
                execute_plan(
                    self.store, plan.plan_id, client, dry_run=False, allow_live_writes=True,
                )
        self.assertFalse(client.updates)

    def test_dry_run_checkpoint_does_not_block_first_live_execution_in_ui(self):
        source = audit(tasks=[task(auditors=("30",))])
        client = FakeClient({"1": FakeClient.raw(auditors=["30"])})
        plan = self.prepared(["responsible"], source, client)
        self.approve(plan)
        dry_result = {
            "1": {
                "result": "PREVISTO_DRY_RUN", "plan_digest": plan.digest,
                "dry_run": True, "reason": "simulado",
            }
        }
        state = {
            "cancel_requested": False,
            "dry_run_results": dry_result,
            "live_results": {},
            "results": dry_result,
        }
        blockers = _execution_blockers(
            plan, state, dry_run=False, writes_enabled=True, approval_error="",
            requested_operation_type=plan.operation_type,
        )
        self.assertFalse(any("operações reais" in item for item in blockers))
        self.assertFalse(any("ainda não homologada" in item for item in blockers))

    def test_concurrent_change_between_dry_run_and_live_blocks_only_affected_task(self):
        items = [task("1", accomplices=("10", "20")), task("2", accomplices=("10", "21"))]
        source = audit(tasks=items, links=[link("1", role="accomplice"), link("2", role="accomplice")])
        client = FakeClient({
            "1": FakeClient.raw("1", accomplices=["10", "20"]),
            "2": FakeClient.raw("2", accomplices=["10", "21"]),
        })
        plan = create_plan(source, "10", "99", ["accomplice"], operation_type="ADD_USER")
        plan = preflight_plan(plan, client)
        self.store.create(plan)
        execute_plan(self.store, plan.plan_id, client, dry_run=True, expected_operation_type="ADD_USER")
        self.approve(plan)
        client.tasks["1"]["accomplices"] = ["10", "20", "30"]

        with patch.dict(os.environ, {"BITRIX24_MIGRATION_WRITES_ENABLED": "YES"}):
            report = execute_plan(
                self.store, plan.plan_id, client, dry_run=False, allow_live_writes=True,
                expected_operation_type="ADD_USER",
            )

        live_results = report["checkpoints"]["live"]
        self.assertEqual(live_results["1"]["result"], "DIVERGENTE")
        self.assertEqual(live_results["2"]["result"], "ATUALIZADA_E_VERIFICADA")
        self.assertEqual(len(client.updates), 1)
        self.assertEqual(report["summary"]["dry_run"], 2)
        self.assertEqual(report["summary"]["updated_verified"], 1)

    def test_legacy_dry_run_results_are_not_treated_as_live_results(self):
        legacy_state = {
            "results": {
                "1": {"result": "PREVISTO_DRY_RUN", "dry_run": True, "plan_digest": "abc"},
            }
        }
        self.assertEqual(len(MigrationStore.results_for_mode(legacy_state, True)), 1)
        self.assertEqual(MigrationStore.results_for_mode(legacy_state, False), {})

    def test_owner_rules_route_synthetic_units_to_distinct_responsibles(self):
        source_id = SYNTHETIC_SOURCE_ID
        tasks = [
            task("1", responsible=source_id, properties={"GROUP_ID": ("101",)}),
            task("2", responsible=source_id, properties={"GROUP_ID": ("202",)}),
        ]
        source = audit(
            tasks=tasks,
            links=[link("1", source_id, "responsible"), link("2", source_id, "responsible")],
            property_fields={"GROUP_ID": "Grupo"},
        )
        raw1 = FakeClient.raw("1", responsible=source_id); raw1["groupId"] = "101"
        raw2 = FakeClient.raw("2", responsible=source_id); raw2["groupId"] = "202"
        plan = create_owner_plan(source, source_id, "GROUP_ID", [
            OwnerRule("UNIT_ALPHA", "Unidade Sintética Alfa", "101", SYNTHETIC_OWNER_ALPHA_ID),
            OwnerRule("UNIT_BETA", "Unidade Sintética Beta", "202", SYNTHETIC_OWNER_BETA_ID),
        ])
        plan = preflight_plan(plan, FakeClient({"1": raw1, "2": raw2}))

        self.assertEqual(plan.task_ids, ["1", "2"])
        self.assertEqual([item.patch for item in plan.operations], [
            {"RESPONSIBLE_ID": SYNTHETIC_OWNER_ALPHA_ID, "AUDITORS": ["30", "10"]},
            {"RESPONSIBLE_ID": SYNTHETIC_OWNER_BETA_ID, "AUDITORS": ["30", "10"]},
        ])
        self.assertEqual(
            [row.owner_label for row in plan.preview],
            ["Unidade Sintética Alfa", "Unidade Sintética Beta"],
        )
        self.assertEqual([row.current_owner_value for row in plan.preview], [["101"], ["202"]])
        self.assertTrue(all(set(item.patch) == {"RESPONSIBLE_ID", "AUDITORS"} for item in plan.operations))

    def test_owner_rule_unknown_and_ambiguous_properties_are_not_planned(self):
        source_id = SYNTHETIC_SOURCE_ID
        source = audit(
            tasks=[
                task("1", responsible=source_id, properties={"UF_OWNER": ("303",)}),
                task("2", responsible=source_id, properties={"UF_OWNER": ("101", "202")}),
            ],
            links=[link("1", source_id, "responsible"), link("2", source_id, "responsible")],
            property_fields={"UF_OWNER": "Proprietário Operacional"},
        )
        plan = create_owner_plan(source, source_id, "UF_OWNER", [
            OwnerRule("UNIT_ALPHA", "Unidade Sintética Alfa", "101", SYNTHETIC_OWNER_ALPHA_ID),
        ])
        self.assertEqual(plan.task_ids, [])
        self.assertEqual(plan.preview[0].eligibility, "IGNORADO")
        self.assertEqual(plan.preview[1].eligibility, "DIVERGENTE")
        self.assertIn("mais de um", plan.preview[1].blocking_reason)

    def test_owner_rule_conflict_is_rejected_before_preview(self):
        source_id = SYNTHETIC_SOURCE_ID
        source = audit(
            tasks=[task("1", responsible=source_id, properties={"GROUP_ID": ("101",)})],
            links=[link("1", source_id, "responsible")],
            property_fields={"GROUP_ID": "Grupo"},
        )
        with self.assertRaisesRegex(MigrationValidationError, "Conflito"):
            create_owner_plan(source, source_id, "GROUP_ID", [
                OwnerRule("UNIT_ALPHA", "Unidade Sintética Alfa", "101", SYNTHETIC_OWNER_ALPHA_ID),
                OwnerRule("UNIT_BETA", "Unidade Sintética Beta", "101", SYNTHETIC_OWNER_BETA_ID),
            ])

    def test_owner_rule_live_property_mismatch_is_divergent(self):
        source_id = SYNTHETIC_SOURCE_ID
        source = audit(
            tasks=[task("1", responsible=source_id, properties={"GROUP_ID": ("101",)})],
            links=[link("1", source_id, "responsible")],
            property_fields={"GROUP_ID": "Grupo"},
        )
        raw = FakeClient.raw("1", responsible=source_id); raw["groupId"] = "202"
        plan = create_owner_plan(source, source_id, "GROUP_ID", [
            OwnerRule("UNIT_ALPHA", "Unidade Sintética Alfa", "101", SYNTHETIC_OWNER_ALPHA_ID),
        ])
        plan = preflight_plan(plan, FakeClient({"1": raw}))
        self.assertEqual(plan.task_ids, [])
        self.assertEqual(plan.preview[0].eligibility, "DIVERGENTE")
        self.assertIn("não confirma exclusivamente", plan.preview[0].blocking_reason)

    def test_owner_rule_mock_write_changes_only_responsible_and_verifies(self):
        source_id = SYNTHETIC_SOURCE_ID
        source = audit(
            tasks=[task("1", responsible=source_id, properties={"UF_OWNER": ("101",)})],
            links=[link("1", source_id, "responsible")],
            property_fields={"UF_OWNER": "Proprietário Operacional"},
        )
        raw = FakeClient.raw(
            "1", responsible=source_id, accomplices=[source_id, "20"], auditors=["30", source_id],
        )
        raw["ufOwner"] = "101"
        raw["description"] = "preservar"
        client = FakeClient({"1": raw})
        plan = create_owner_plan(source, source_id, "UF_OWNER", [
            OwnerRule("UNIT_ALPHA", "Unidade Sintética Alfa", "101", SYNTHETIC_OWNER_ALPHA_ID),
        ])
        plan = preflight_plan(plan, client)
        self.store.create(plan)
        execute_plan(self.store, plan.plan_id, client, dry_run=True)
        self.approve(plan)
        before = dict(client.tasks["1"])
        with patch.dict(os.environ, {"BITRIX24_MIGRATION_WRITES_ENABLED": "YES"}):
            report = execute_plan(
                self.store, plan.plan_id, client, dry_run=False, allow_live_writes=True,
            )
        self.assertEqual(report["summary"]["updated_verified"], 1)
        self.assertEqual(client.updates, [(
            "1", {"RESPONSIBLE_ID": SYNTHETIC_OWNER_ALPHA_ID, "AUDITORS": ["30", SYNTHETIC_SOURCE_ID]},
        )])
        self.assertEqual(client.tasks["1"]["responsibleId"], SYNTHETIC_OWNER_ALPHA_ID)
        for field in ("accomplices", "auditors", "createdBy", "title", "description", "ufOwner"):
            self.assertEqual(client.tasks["1"][field], before[field])

    def test_owner_rule_homologation_limit_is_five_tasks_per_rule(self):
        source_id = SYNTHETIC_SOURCE_ID
        tasks = [
            task(str(index), responsible=source_id, properties={"GROUP_ID": ("101",)})
            for index in range(1, 7)
        ]
        source = audit(
            tasks=tasks,
            links=[link(str(index), source_id, "responsible") for index in range(1, 7)],
            property_fields={"GROUP_ID": "Grupo"},
        )
        plan = create_owner_plan(source, source_id, "GROUP_ID", [
            OwnerRule("UNIT_ALPHA", "Unidade Sintética Alfa", "101", SYNTHETIC_OWNER_ALPHA_ID),
        ], max_tasks_per_rule=5)
        self.assertEqual(plan.task_ids, ["1", "2", "3", "4", "5"])
        self.assertEqual(plan.preview[5].eligibility, "IGNORADO")
        self.assertIn("Limite de homologação", plan.preview[5].blocking_reason)

    def test_importer_maps_group_and_explicit_custom_owner_field(self):
        path = self.root / "owner.xlsx"
        export_xlsx(path, [
            ("Tarefas", [
                "ID", "Título", "Responsável", "Criador", "Participantes", "Observadores",
                "Alterada em", "Grupo", "Proprietário Operacional",
            ], [["1", "Task", SYNTHETIC_SOURCE_ID, "40", "", "", "2026-01-01T00:00:00+00:00", "101", "202"]]),
            ("Vínculos", [
                "task_id", "user_id", "role", "verification", "evidence_field", "observed_user_ids", "reason",
            ], [["1", SYNTHETIC_SOURCE_ID, "responsible", "confirmed", "responsibleId", f'["{SYNTHETIC_SOURCE_ID}"]', "ok"]]),
            ("Metadados", ["Propriedade", "Valor"], [
                ["schema_version", "1"], ["read_only", "true"], ["partial", "false"],
                ["pagination_complete", "true"], ["run_status", "completed"], ["run_id", "run1"],
                ["custom_fields", '[{"id":"UF_OWNER","label":"Proprietário Operacional","export":true}]'],
            ]),
        ])
        imported = import_audit_xlsx(path)
        self.assertEqual(imported.property_fields, {
            "GROUP_ID": "Grupo", "UF_OWNER": "Proprietário Operacional",
        })
        self.assertEqual(imported.tasks["1"].properties, {
            "GROUP_ID": ("101",), "UF_OWNER": ("202",),
        })


if __name__ == "__main__":
    unittest.main()
