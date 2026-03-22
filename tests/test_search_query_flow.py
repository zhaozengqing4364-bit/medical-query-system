import os
import sys
import types
import unittest
from unittest.mock import patch


PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import udid_server  # noqa: E402


class FakeCursor:
    def __init__(self, *, keyword_hit=True):
        self.keyword_hit = keyword_hit
        self.executed = []
        self._rows = []

    def execute(self, sql, params=None):
        normalized_sql = " ".join(str(sql).split())
        normalized_params = list(params or [])
        self.executed.append((normalized_sql, normalized_params))

        if "SELECT 1 FROM products WHERE search_vector @@" in normalized_sql:
            self._rows = [(1,)] if self.keyword_hit else []
            return self

        if normalized_sql.startswith("SELECT COUNT(*) FROM products WHERE"):
            self._rows = [(1,)]
            return self

        if normalized_sql.startswith("SELECT di_code, product_name"):
            self._rows = [(
                "DI001",
                "检测试剂盒A",
                "商品名A",
                "500人份/盒",
                "天津博奥赛斯生物科技股份有限公司",
                "产品描述A",
                "2026-03-01",
                "RSS",
                "2026-03-22T00:00:00+08:00",
                "IVD-001",
                "适用范围A",
            )]
            return self

        raise AssertionError(f"Unexpected SQL: {normalized_sql}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class SearchQueryFlowTests(unittest.TestCase):
    def setUp(self):
        self.client = udid_server.app.test_client()

    def test_search_prefers_postgres_fts_when_search_vector_has_hits(self):
        cursor = FakeCursor(keyword_hit=True)
        fake_data_lake = types.SimpleNamespace(
            conn=FakeConnection(cursor),
            release_thread_connection=lambda: None,
        )

        with patch.object(udid_server, "_require_login", return_value=None), \
             patch.object(udid_server, "is_postgres_backend", return_value=True), \
             patch.object(udid_server, "data_lake", fake_data_lake):
            response = self.client.get("/api/search?keyword=检测试剂盒&page=1&page_size=10")

        payload = response.get_json()
        self.assertTrue(payload["success"])

        executed_sql = [item[0] for item in cursor.executed]
        self.assertTrue(
            any("SELECT 1 FROM products WHERE search_vector @@" in sql for sql in executed_sql),
            executed_sql,
        )
        self.assertTrue(
            any("search_vector @@" in sql and "SELECT COUNT(*)" in sql for sql in executed_sql),
            executed_sql,
        )
        self.assertFalse(
            any("product_name ILIKE ?" in sql and "manufacturer ILIKE ?" in sql and "description ILIKE ?" in sql for sql in executed_sql),
            executed_sql,
        )

    def test_search_returns_scope_and_highlight_keywords_in_normal_mode(self):
        cursor = FakeCursor(keyword_hit=True)
        fake_data_lake = types.SimpleNamespace(
            conn=FakeConnection(cursor),
            release_thread_connection=lambda: None,
        )

        with patch.object(udid_server, "_require_login", return_value=None), \
             patch.object(udid_server, "is_postgres_backend", return_value=True), \
             patch.object(udid_server, "data_lake", fake_data_lake):
            response = self.client.get("/api/search?keyword=检测试剂盒&page=1&page_size=10")

        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["data"][0]["scope"], "适用范围A")
        self.assertEqual(payload["data"][0]["highlightKeywords"], ["检测试剂盒"])

    def test_ai_match_reports_actual_recall_method_from_hybrid_search_metadata(self):
        fake_embedding_service = types.SimpleNamespace(
            hybrid_search=lambda **_kwargs: {
                "results": [
                    {
                        "di_code": "DI001",
                        "product_name": "检测试剂盒A",
                        "manufacturer": "天津博奥赛斯生物科技股份有限公司",
                        "matchScore": 88,
                        "highlightKeywords": ["检测试剂盒"],
                    }
                ],
                "recall_method": "fts_vector",
            }
        )

        with patch.object(udid_server, "_require_login", return_value=None), \
             patch.dict(sys.modules, {"embedding_service": fake_embedding_service}):
            response = self.client.post(
                "/api/ai-match",
                json={"requirement": "检测试剂盒"},
            )

        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["method"], "fts_vector")


if __name__ == "__main__":
    unittest.main()
