import copy
import sys
import types
import unittest
from unittest.mock import patch

from app.codal_products_stage import (
    _generated_product_code,
    _map_source_names,
    stage_product_reports,
)


class CodalProductMappingTests(unittest.TestCase):
    def test_exact_global_product_match_and_unknown_name_in_lookup_helper(self):
        names = [
            ("بنزن", "Table_1", 4, "PRODUCT", "TON"),
            ("ماده ناشناخته", "Table_2", 5, "PURCHASE_MATERIAL", "TON"),
        ]
        staged, aliases = _map_source_names(
            names,
            mappings={},
            products={"بنزن": [(101, "TON")]},
        )
        self.assertEqual(staged[0]["product_id"], 101)
        self.assertEqual(staged[0]["validation_status"], "VALID")
        self.assertIsNone(staged[1]["product_id"])
        self.assertEqual(staged[1]["validation_status"], "ERROR")
        self.assertEqual(aliases, [("بنزن", "بنزن", 101)])

    def test_product_code_is_stable_and_fits_schema(self):
        code = _generated_product_code("آهناسفنجی")
        self.assertEqual(code, _generated_product_code("آهناسفنجی"))
        self.assertTrue(code.startswith("CODAL_"))
        self.assertLessEqual(len(code), 50)

    def test_unknown_company_report_creates_global_product_and_mapping(self):
        class Cursor:
            def __init__(self, connection):
                self.connection = connection
                self.rows = []
                self.last = None
                self.history = []

            def execute(self, sql, *params):
                self.history.append(sql)
                q = " ".join(sql.split())
                self.rows = []
                if "SELECT OBJECT_ID" in q:
                    self.rows = [(1,)]
                elif "DECLARE @drop_fk" in q:
                    self.rows = []
                elif "SELECT pm.SourceProductName" in q:
                    self.rows = [
                        (m["source"], m["normalized"], m["product_id"],
                         next((p["unit"] for p in self.connection.products if p["id"] == m["product_id"]), None))
                        for m in self.connection.mappings
                    ]
                elif "FROM dbo.Products WITH (UPDLOCK, HOLDLOCK)" in q:
                    code = params[0]
                    self.rows = [(p["id"], p["name"], p["unit"], p["active"]) for p in self.connection.products if p["code"] == code]
                elif q.startswith("SELECT ProductID,ProductName,Unit,IsActive FROM dbo.Products"):
                    self.rows = [(p["id"], p["name"], p["unit"], p["active"]) for p in self.connection.products]
                elif q.startswith("DELETE FROM dbo.CodalProductStage"):
                    trace = params[0]
                    self.connection.stage = [x for x in self.connection.stage if x["trace"] != trace]
                elif q.startswith("INSERT INTO dbo.CodalProductStage"):
                    symbol, trace, title, name, sheet, row_no, item_type, unit = params
                    self.connection.stage.append({
                        "symbol": symbol, "trace": trace, "title": title, "name": name,
                        "sheet": sheet, "row_no": row_no, "item_type": item_type,
                        "unit": unit, "product_id": None, "status": "PENDING",
                    })
                elif q.startswith("INSERT INTO dbo.Products"):
                    code, name, unit = params
                    pid = self.connection.next_product_id
                    self.connection.next_product_id += 1
                    self.connection.products.append({"id": pid, "code": code, "name": name, "unit": unit, "active": True})
                    self.rows = [(pid,)]
                elif "FROM dbo.ProductMappings WITH (UPDLOCK, HOLDLOCK)" in q:
                    normalized, source = params
                    self.rows = [(m["product_id"],) for m in self.connection.mappings
                                 if m["active"] and (m["normalized"] == normalized or m["source"] == source)]
                elif q.startswith("INSERT INTO dbo.ProductMappings"):
                    source, pid, normalized = params
                    self.connection.mappings.append({"source": source, "product_id": pid,
                                                      "normalized": normalized, "active": True})
                elif q.startswith("UPDATE dbo.CodalProductStage"):
                    pid, status, message, trace, sheet, row_no = params
                    for item in self.connection.stage:
                        if item["trace"] == trace and item["sheet"] == sheet and item["row_no"] == row_no:
                            item.update(product_id=pid, status=status, message=message)
                else:
                    raise AssertionError("Unexpected SQL: " + q)
                return self

            def fetchone(self):
                return self.rows.pop(0) if self.rows else None

            def fetchall(self):
                result, self.rows = list(self.rows), []
                return result

        class Connection:
            def __init__(self):
                self.products = [{"id": 101, "code": "BENZENE", "name": "بنزن", "unit": "TON", "active": True}]
                self.mappings = []
                self.stage = []
                self.next_product_id = 102
                self.cur = Cursor(self)
                self.snapshot = None

            def __enter__(self): return self
            def __exit__(self, *args): return False
            def cursor(self): return self.cur
            def commit(self):
                self.snapshot = copy.deepcopy((self.products, self.mappings, self.stage, self.next_product_id))
            def rollback(self):
                if self.snapshot:
                    self.products, self.mappings, self.stage, self.next_product_id = copy.deepcopy(self.snapshot)

        connection = Connection()
        fake_pyodbc = types.SimpleNamespace(connect=lambda *a, **k: connection)
        report = {
            "Kind": "MONTHLY_ACTIVITY", "Symbol": "نماد خارج از Companies",
            "TracingNo": "1631870",
            "Title": "گزارش فعالیت ماهانه دوره ۱ ماهه منتهی به ۱۴۰۴/۰۹/۳۰ (اصلاحیه)",
            "Sheets": [{"sheet": "Table_1", "rows": [
                ["شرح", "دوره"], ["نام محصول", "واحد"],
                ["فروش داخلی:"] + [None] * 25,
                ["بنزن", "تن"] + [None] * 24,
                ["ماده جدید", "تن"] + [None] * 24,
                ["ماده جدید", "تن"] + [None] * 24,
            ]}],
        }
        with patch("app.codal_products_stage.get_connection_string", return_value="fake"):
            with patch.dict(sys.modules, {"pyodbc": fake_pyodbc}):
                result = stage_product_reports([report])

        self.assertEqual(result["failed"], [])
        self.assertEqual(result["summaries"][0]["valid"], 3)
        self.assertEqual(result["products_created"], 1)
        self.assertEqual(result["mappings_added"], 2)
        self.assertEqual(len(connection.products), 2)
        self.assertEqual(len(connection.mappings), 2)
        self.assertEqual(len(connection.stage), 3)
        self.assertTrue(all(x["status"] == "VALID" and x["product_id"] is not None for x in connection.stage))

        all_sql = "\n".join(connection.cur.history)
        self.assertNotIn("SELECT CompanyID FROM dbo.Companies", all_sql)
        self.assertNotIn("SELECT SourceDocumentID", all_sql)
        self.assertIn("sys.foreign_keys", all_sql)  # only removes legacy company/document FKs
        self.assertIn("INSERT INTO dbo.CodalProductStage", all_sql)
        self.assertIn("COL_LENGTH(N'dbo.CodalProductStage', N'TracingNo')", all_sql)
        self.assertIn("ALTER COLUMN SourceDocumentID BIGINT NULL", all_sql)
        self.assertIn("ValidationStatus IN ('PENDING','VALID','ERROR')", all_sql)


class ProductStageMigrationBatchTests(unittest.TestCase):
    def test_runtime_migration_defers_trace_index_compilation(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "app" / "codal_products_stage.py").read_text(encoding="utf-8-sig")
        self.assertIn("ALTER TABLE dbo.CodalProductStage ADD TracingNo NVARCHAR(50) NULL", source)
        self.assertIn("EXEC sys.sp_executesql N'", source)
        self.assertIn("ON dbo.CodalProductStage(TracingNo, SourceTable, SourceRowNo)", source)

    def test_sql_migration_defers_trace_index_compilation(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "03_CodalWorkflow_v34.sql").read_text(encoding="utf-8-sig")
        self.assertIn("ALTER TABLE dbo.CodalProductStage ADD TracingNo NVARCHAR(50) NULL", source)
        self.assertIn("EXEC sys.sp_executesql N'", source)
        self.assertIn("ON dbo.CodalProductStage(TracingNo,SourceTable,SourceRowNo)", source)


if __name__ == "__main__":
    unittest.main()
