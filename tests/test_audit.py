import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bitrix24_task_auditor.audit import audit_tasks, build_summary, build_user_report
from bitrix24_task_auditor.config import AppConfig, AuditConfig, BitrixConfig, CustomFieldConfig
from bitrix24_task_auditor.models import Task, User


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 10, tzinfo=timezone.utc)
        self.config = AppConfig(
            BitrixConfig(),
            AuditConfig(
                stale_after_hours=48,
                due_soon_hours=24,
                checkpoint_path=Path(".state/c"),
                spool_path=Path(".state/s"),
                output_directory=Path("output"),
            ),
            roles={"responsible": ("7",), "creator": (), "accomplice": (), "auditor": ()},
            custom_fields=(CustomFieldConfig("UF_AUTO_COST", "Custo", ("responsible",)),),
        )

    def test_open_task_rules_and_custom_field(self):
        task = Task(
            id="1", title="Teste", status="3", responsible_id="7", created_by="2",
            changed_at=self.now - timedelta(days=4), deadline=self.now - timedelta(hours=1),
            custom_fields={"UF_AUTO_COST": ""},
        )
        findings = audit_tasks([task], self.config, now=self.now)
        self.assertEqual({item.code for item in findings}, {"OVERDUE", "STALE", "MISSING_CUSTOM_FIELD"})
        custom = next(item for item in findings if item.code == "MISSING_CUSTOM_FIELD")
        self.assertEqual((custom.user_id, custom.role), ("7", "responsible"))

    def test_closed_late_and_no_open_only_rules(self):
        task = Task(
            id="2", title="Fechada", status="5", responsible_id="7", created_by="2",
            deadline=self.now - timedelta(days=2), closed_at=self.now - timedelta(days=1),
            custom_fields={"UF_AUTO_COST": 10},
        )
        findings = audit_tasks([task], self.config, now=self.now)
        self.assertEqual([item.code for item in findings], ["CLOSED_LATE"])

    def test_user_report_aggregates_roles_and_findings(self):
        task = Task(id="1", title="", status="3", responsible_id="7", created_by="2")
        findings = audit_tasks([task], self.config, now=self.now)
        rows = build_user_report([task], [User("7", "Ana", True)], findings)
        ana = next(row for row in rows if row["user_id"] == "7")
        self.assertEqual(ana["tasks_as_responsible"], 1)
        self.assertGreater(ana["total_findings"], 0)
        summary = build_summary([task], findings)
        self.assertEqual(summary["tasks"], 1)

    def test_custom_field_accepts_bitrix_camel_case(self):
        task = Task.from_api({"ID": "3", "ufAutoCost": "ok"}, ("UF_AUTO_COST",))
        self.assertEqual(task.custom_fields["UF_AUTO_COST"], "ok")


if __name__ == "__main__":
    unittest.main()
