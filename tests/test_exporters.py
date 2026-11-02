import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from bitrix24_task_auditor.exporters import export_json, export_xlsx


class ExportTests(unittest.TestCase):
    def test_json_and_valid_xlsx_package(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            json_path = root / "report.json"
            xlsx_path = root / "report.xlsx"
            export_json(json_path, {"ok": True, "name": "ação"})
            export_xlsx(xlsx_path, [("Dados", ["Nome", "Valor"], [["=unsafe", "ação"]])])
            self.assertTrue(json.loads(json_path.read_text(encoding="utf-8"))["ok"])
            with zipfile.ZipFile(xlsx_path) as archive:
                self.assertIsNone(archive.testzip())
                sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
                self.assertIn("'=unsafe", sheet)
                self.assertIn("ação", sheet)


if __name__ == "__main__":
    unittest.main()
