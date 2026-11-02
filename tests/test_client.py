import unittest

from bitrix24_task_auditor.client import (
    BitrixClient, BitrixError, PaginationError, extract_page, extract_page_info, paginate, validate_page_transition,
)
from bitrix24_task_auditor.config import BitrixConfig


class FakeClient:
    def __init__(self):
        self.starts = []

    def call(self, method, params):
        self.starts.append(params["start"])
        if params["start"] == 0:
            return {"result": {"tasks": [{"id": "1"}]}, "next": 50}
        return {"result": {"tasks": [{"id": "2"}]}}


class ClientTests(unittest.TestCase):
    def test_read_only_allowlist(self):
        client = BitrixClient("https://portal/rest/1/token/", BitrixConfig(), opener=lambda *a, **k: None)
        with self.assertRaises(BitrixError):
            client.call("tasks.task.add", {})

    def test_paginate(self):
        client = FakeClient()
        pages = []
        values = paginate(client, "tasks.task.list", {}, item_key="tasks", on_page=lambda rows, nxt: pages.append((len(rows), nxt)))
        self.assertEqual([row["id"] for row in values], ["1", "2"])
        self.assertEqual(client.starts, [0, 50])
        self.assertEqual(pages, [(1, 50), (1, None)])

    def test_extract_rejects_bad_shape(self):
        with self.assertRaises(BitrixError):
            extract_page({"result": {"tasks": "bad"}}, "tasks")

    def test_page_metadata_and_absent_next_contract(self):
        page = extract_page_info({"result": {"tasks": [{"id": "1"}]}, "total": 1}, "tasks")
        self.assertEqual(page.total, 1)
        self.assertIsNone(page.next_cursor)
        self.assertFalse(page.next_present)
        validate_page_transition(0, page)

    def test_total_contradiction_is_rejected(self):
        page = extract_page_info({"result": {"tasks": [{"id": "1"}]}, "total": 10}, "tasks")
        with self.assertRaises(PaginationError):
            validate_page_transition(0, page)

    def test_invalid_and_regressive_cursors_are_rejected(self):
        with self.assertRaises(PaginationError):
            extract_page_info({"result": {"tasks": []}, "next": "invalid"}, "tasks")
        page = extract_page_info({"result": {"tasks": []}, "next": 50}, "tasks")
        with self.assertRaises(PaginationError):
            validate_page_transition(50, page)


if __name__ == "__main__":
    unittest.main()
