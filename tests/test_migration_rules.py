import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bitrix24_task_auditor.migration_models import MigrationValidationError
from bitrix24_task_auditor.migration_rule_engine import create_rules_plan
from bitrix24_task_auditor.migration_rules import MigrationRuleError, MigrationRuleStore
from bitrix24_task_auditor.migration_service import execute_plan, preflight_plan
from bitrix24_task_auditor.migration_store import MigrationStore, MigrationStoreError

from tests.test_migration import FakeClient, audit, link, task


class MissingUserClient(FakeClient):
    def get_user(self, user_id):
        return None


class MigrationRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.temp.name)
        self.rules = MigrationRuleStore(self.root)
        self.migrations = MigrationStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def create_rule(self, **overrides):
        values = {
            "name": "Regra principal",
            "description": "",
            "source_user_id": "10",
            "operation": "REPLACE_RESPONSIBLE",
            "target_user_id": "50",
            "condition_type": "NONE",
            "condition_field_id": None,
            "condition_value": None,
            "active": True,
        }
        values.update(overrides)
        return self.rules.create(**values)

    def test_create_edit_duplicate_delete_and_persist_after_restart(self):
        original = self.create_rule(name="Original")
        edited = self.rules.edit(original.rule_id, name="Editada", target_user_id="51")
        duplicate = self.rules.duplicate(original.rule_id)

        self.assertEqual(edited.version, 2)
        self.assertEqual(edited.target_user_id, "51")
        self.assertNotEqual(duplicate.rule_id, original.rule_id)
        self.assertIn("cópia", duplicate.name)
        self.rules.delete(duplicate.rule_id)

        restarted = MigrationRuleStore(self.root)
        persisted = restarted.get(original.rule_id)
        self.assertEqual((persisted.name, persisted.version, persisted.target_user_id), ("Editada", 2, "51"))
        self.assertEqual(len(restarted.list_rules()), 1)

    def test_activate_deactivate_and_template_persistence(self):
        first = self.create_rule(name="Primeira")
        second = self.create_rule(name="Segunda", operation="ADD_AUDITOR", source_user_id="30", target_user_id="99")
        disabled = self.rules.set_active(first.rule_id, False)
        self.assertFalse(disabled.active)
        self.assertEqual(self.rules.list_rules(active_only=True), [second])
        template = self.rules.save_template("Modelo duplo", [first.rule_id, second.rule_id])

        restarted = MigrationRuleStore(self.root)
        self.assertEqual(restarted.list_templates()[0]["template_id"], template["template_id"])
        with self.assertRaisesRegex(MigrationRuleError, "modelo"):
            restarted.delete(first.rule_id)

    def test_multiple_compatible_rules_share_one_task_patch(self):
        responsible_rule = self.create_rule(
            name="Responsável por grupo",
            condition_type="GROUP_ID",
            condition_value="101",
        )
        auditor_rule = self.create_rule(
            name="Adicionar observador pelo criador",
            source_user_id="30",
            operation="ADD_AUDITOR",
            target_user_id="99",
            condition_type="CREATED_BY",
            condition_value="40",
        )
        source = audit(
            tasks=[task("1", responsible="10", auditors=("30", "10"), properties={"GROUP_ID": ("101",)})],
            links=[link("1", "10", "responsible"), link("1", "30", "auditor")],
            property_fields={"GROUP_ID": "Grupo"},
        )
        raw = FakeClient.raw("1", responsible="10", auditors=["30", "10"])
        raw["groupId"] = "101"
        plan = preflight_plan(create_rules_plan(source, [responsible_rule, auditor_rule]), FakeClient({"1": raw}))

        self.assertEqual(plan.task_ids, ["1"])
        self.assertEqual(plan.operations[0].patch, {
            "RESPONSIBLE_ID": "50",
            "AUDITORS": ["30", "10", "99"],
        })
        self.assertEqual({row.rule_name for row in plan.preview}, {
            "Responsável por grupo", "Adicionar observador pelo criador",
        })

    def test_all_catalog_operations_generate_only_supported_fields(self):
        cases = [
            ("REPLACE_RESPONSIBLE", "responsible", "10", "RESPONSIBLE_ID", "50"),
            ("REPLACE_ACCOMPLICE", "accomplice", "10", "ACCOMPLICES", ["50", "20"]),
            ("REPLACE_AUDITOR", "auditor", "30", "AUDITORS", ["50", "10"]),
            ("ADD_ACCOMPLICE", "accomplice", "10", "ACCOMPLICES", ["10", "20", "50"]),
            ("ADD_AUDITOR", "auditor", "30", "AUDITORS", ["30", "10", "50"]),
        ]
        for index, (operation, role, source_id, field, expected) in enumerate(cases, 1):
            with self.subTest(operation=operation):
                rule = self.create_rule(
                    name=f"Regra {index}", source_user_id=source_id,
                    operation=operation, target_user_id="50",
                )
                source = audit(
                    tasks=[task("1", responsible="10", accomplices=("10", "20"), auditors=("30", "10"))],
                    links=[link("1", source_id, role)],
                )
                plan = create_rules_plan(source, [rule])
                self.assertEqual(plan.operations[0].patch, {field: expected})

    def test_conflicting_rules_are_blocked_without_order_precedence(self):
        first = self.create_rule(name="Destino A", target_user_id="50")
        second = self.create_rule(name="Destino B", target_user_id="60")
        source = audit(tasks=[task("1", responsible="10")], links=[link("1", "10", "responsible")])
        plan = create_rules_plan(source, [first, second])

        self.assertEqual(plan.task_ids, [])
        self.assertFalse(plan.operations)
        self.assertTrue(all(row.eligibility == "CONFLITO" for row in plan.preview))
        self.assertTrue(all("diferentes" in row.blocking_reason for row in plan.preview))

    def test_rule_without_correspondence_is_reported_without_operation(self):
        rule = self.create_rule(source_user_id="999")
        plan = create_rules_plan(audit(), [rule])
        self.assertFalse(plan.operations)
        self.assertFalse(plan.preview)
        self.assertTrue(any("não encontrou" in item for item in plan.import_warnings))

    def test_invalid_users_and_unsupported_custom_fields_are_blocked(self):
        with self.assertRaisesRegex(MigrationRuleError, "numérico e positivo"):
            self.create_rule(source_user_id="abc")
        with self.assertRaisesRegex(MigrationRuleError, r"UF_\*"):
            self.create_rule(
                condition_type="CUSTOM_FIELD", condition_field_id="OWNER", condition_value="101",
            )

        rule = self.create_rule(
            condition_type="CUSTOM_FIELD", condition_field_id="UF_OWNER", condition_value="101",
        )
        with self.assertRaisesRegex(MigrationValidationError, "não mapeia"):
            create_rules_plan(audit(), [rule])

        source = audit(tasks=[task("1", responsible="10")], links=[link("1", "10", "responsible")])
        plan = create_rules_plan(source, [self.create_rule(name="Usuário ausente", target_user_id="777")])
        plan = preflight_plan(plan, MissingUserClient())
        self.assertFalse(plan.task_ids)
        self.assertEqual(plan.preview[0].eligibility, "DIVERGENTE")

    def test_editing_used_rule_preserves_immutable_previous_plan(self):
        rule = self.create_rule(target_user_id="50")
        source = audit(tasks=[task("1", responsible="10")], links=[link("1", "10", "responsible")])
        plan = preflight_plan(create_rules_plan(source, [rule]), FakeClient())
        self.migrations.create(plan)
        self.rules.mark_used([rule], plan.plan_id)
        previous_digest = plan.digest

        edited = self.rules.edit(rule.rule_id, target_user_id="60")
        stored = self.migrations.read_plan(plan.plan_id)
        self.assertEqual(edited.version, 2)
        self.assertEqual(stored.digest, previous_digest)
        self.assertEqual(stored.rule_snapshots[0]["target_user_id"], "50")
        with self.assertRaisesRegex(MigrationRuleError, "utilizada"):
            self.rules.delete(rule.rule_id)

    def test_dry_run_has_no_write_and_live_requires_approval(self):
        rule = self.create_rule(target_user_id="50")
        source = audit(tasks=[task("1", responsible="10")], links=[link("1", "10", "responsible")])
        client = FakeClient()
        plan = preflight_plan(create_rules_plan(source, [rule]), client)
        self.migrations.create(plan)
        self.rules.mark_used([rule], plan.plan_id)

        dry_report = execute_plan(self.migrations, plan.plan_id, client, dry_run=True)
        self.assertEqual(dry_report["summary"]["dry_run"], 1)
        self.assertEqual(client.updates, [])
        with patch.dict(os.environ, {"BITRIX24_MIGRATION_WRITES_ENABLED": "YES"}):
            with self.assertRaises(MigrationStoreError):
                execute_plan(self.migrations, plan.plan_id, client, dry_run=False, allow_live_writes=True)
        self.assertEqual(client.updates, [])

        self.migrations.approve(plan.plan_id, self.migrations.confirmation_phrase(plan))
        with patch.dict(os.environ, {"BITRIX24_MIGRATION_WRITES_ENABLED": "YES"}):
            report = execute_plan(self.migrations, plan.plan_id, client, dry_run=False, allow_live_writes=True)
        self.assertEqual(report["summary"]["updated_verified"], 1)
        self.assertEqual(client.updates, [(
            "1", {"RESPONSIBLE_ID": "50", "AUDITORS": ["30", "10"]},
        )])


if __name__ == "__main__":
    unittest.main()
