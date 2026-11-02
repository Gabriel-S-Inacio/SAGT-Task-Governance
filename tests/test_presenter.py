import unittest

from bitrix24_task_auditor.presenter import filter_result_rows, result_indicators, result_rows


class PresenterTests(unittest.TestCase):
    def test_result_rendering_and_local_filters(self):
        payload = {
            "tasks": [{"id": "1", "title": "Revisar contrato", "status": "3", "priority": "2", "deadline": "2026-01-01", "group_id": "9", "custom_fields": {"UF_AUTO_1": "A"}, "extra_fields": {"DESCRIPTION": "Texto"}}],
            "links": [{"task_id": "1", "user_id": "7", "role": "responsible"}],
            "users": [{"user_id": "7", "name": "Ana"}],
            "findings": [{"severity": "high"}],
        }
        rows = result_rows(payload)
        self.assertEqual(rows[0]["Status"], "Em andamento")
        self.assertIn("Ana", rows[0]["Usuário analisado"])
        self.assertEqual(rows[0]["DESCRIPTION"], "Texto")
        self.assertEqual(filter_result_rows(rows, search="contrato", roles=["responsible"]), rows)
        self.assertEqual(filter_result_rows(rows, search="inexistente"), [])
        indicators = result_indicators(payload)
        self.assertEqual((indicators["unique_tasks"], indicators["links"], indicators["exceptions"]), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
