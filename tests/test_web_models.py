import unittest
from pathlib import Path

from bitrix24_task_auditor.web_models import (
    QuerySpec, QueryUnit, QueryValidationError, build_query_units, build_task_params, query_summary,
)


class WebModelTests(unittest.TestCase):
    def test_user_and_role_selection_builds_cartesian_units(self):
        spec = QuerySpec(user_ids=("10", "20"), roles=("responsible", "auditor"))
        spec.validate(project_root=Path.cwd())
        units = build_query_units(spec)
        self.assertEqual([(unit.user_id, unit.role) for unit in units], [
            ("10", "responsible"), ("10", "auditor"), ("20", "responsible"), ("20", "auditor"),
        ])

    def test_validated_filters_are_mapped_to_api(self):
        spec = QuerySpec(
            user_ids=("7",), roles=("creator",), group_ids=("3",), statuses=("2", "3"),
            priorities=("2",), deadline_from="2026-01-01", deadline_to="2026-01-31",
        )
        spec.validate(project_root=Path.cwd())
        params = build_task_params(spec, QueryUnit("7", "creator"))
        self.assertEqual(params["filter"]["CREATED_BY"], "7")
        self.assertEqual(params["filter"]["GROUP_ID"], ["3"])
        self.assertEqual(params["filter"]["STATUS"], ["2", "3"])
        self.assertNotIn("params", params)
        self.assertNotIn("DELETE", params)

    def test_independent_query_and_summary(self):
        spec = QuerySpec(statuses=("5",), roles=())
        spec.validate(project_root=Path.cwd())
        self.assertEqual(len(build_query_units(spec)), 1)
        self.assertEqual(query_summary(spec)["consultas_planejadas"], 1)

    def test_invalid_users_roles_fields_and_paths_are_rejected(self):
        cases = [
            QuerySpec(user_ids=("abc",), roles=("responsible",)),
            QuerySpec(user_ids=("10",), roles=()),
            QuerySpec(standard_fields=("PASSWORD",)),
            QuerySpec(custom_fields=("UF_UNKNOWN",)),
            QuerySpec(output_directory="../outside"),
            QuerySpec(deadline_from="2026-02-01", deadline_to="2026-01-01"),
        ]
        for spec in cases:
            with self.subTest(spec=spec), self.assertRaises(QueryValidationError):
                spec.validate(confirmed_custom_fields=(), project_root=Path.cwd())

    def test_confirmed_custom_field_is_accepted(self):
        spec = QuerySpec(custom_fields=("UF_AUTO_1",))
        spec.validate(confirmed_custom_fields=("UF_AUTO_1",), project_root=Path.cwd())

    def test_legacy_limits_are_ignored_when_loading_saved_model(self):
        spec = QuerySpec.from_dict({"max_pages": 100, "max_records": 5000, "report_name": "legado"})
        self.assertNotIn("max_pages", spec.to_dict())
        self.assertNotIn("max_records", spec.to_dict())
        self.assertEqual(spec.report_name, "legado")


if __name__ == "__main__":
    unittest.main()
