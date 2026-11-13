import unittest
from app.codal_registry import infer_industry_code, ensure_company_and_document, _period, _jalali_to_gregorian


class CodalRegistryTests(unittest.TestCase):
    def test_niriz_report_items_classify_as_steel(self):
        code = infer_industry_code(
            "", "نی‌ریز", ["آهن اسفنجی", "بیلت", "گندله", "قراضه", "گاز"]
        )
        self.assertEqual(code, "STEEL")

    def test_company_name_has_priority(self):
        code = infer_industry_code(
            "شرکت پتروشیمی نمونه", "نماد", ["گندله", "آهن اسفنجی"]
        )
        self.assertEqual(code, "PETROCHEMICAL")

    def test_unknown_industry_is_not_guessed(self):
        self.assertIsNone(infer_industry_code("شرکت سرمایه‌گذاری نمونه", "نماد", ["خدمات"]))

    def test_period_and_date_conversion(self):
        self.assertEqual(_period("گزارش فعالیت ماهانه منتهی به ۱۴۰۵/۰۴/۳۱"), (1405, 4, 31))
        self.assertEqual(_jalali_to_gregorian(1405, 4, 1), (2026, 6, 22))


class FakeCursor:
    def __init__(self):
        self.row = None
        self.rows = []
        self.calls = []

    def execute(self, sql, *params):
        self.calls.append((sql, params))
        compact = " ".join(sql.split())
        self.row = None
        self.rows = []
        if "FROM dbo.Companies WITH" in compact:
            self.row = None
        elif "FROM dbo.Industries" in compact:
            self.row = (3,) if params == ("STEEL",) else None
        elif "FROM dbo.DataSources" in compact:
            self.rows = [(7,)]
        elif "FROM dbo.SourceDocuments WITH" in compact and "ExternalDocumentID" in compact:
            self.row = None
        elif "FROM dbo.SourceDocuments WITH" in compact and "IsCurrent=1" in compact:
            self.row = None
        elif "INSERT INTO dbo.Companies" in compact:
            self.row = (77,)
        elif "INSERT INTO dbo.SourceDocuments" in compact:
            self.row = (555,)
        return self

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class CodalRegistryDatabaseFlowTests(unittest.TestCase):
    def test_missing_company_and_document_are_created_in_order(self):
        cur = FakeCursor()
        report = {
            "Symbol": "نی‌ریز",
            "CompanyName": "شرکت مجتمع فولاد نی‌ریز",
            "TracingNo": "1631870",
            "Title": "گزارش فعالیت ماهانه منتهی به ۱۴۰۵/۰۴/۳۱",
            "Kind": "MONTHLY_ACTIVITY",
        }
        result = ensure_company_and_document(
            cur, report, ["آهن اسفنجی", "بیلت", "گندله", "قراضه", "گاز"]
        )
        self.assertEqual(result, (77, 555, 7, 1405, 4, 31))
        company_insert = next(x for x in cur.calls if "INSERT INTO dbo.Companies" in x[0])
        self.assertEqual(company_insert[1], ("نی‌ریز", "شرکت مجتمع فولاد نی‌ریز", 3))
        source_lookup = next(x for x in cur.calls if "FROM dbo.DataSources" in x[0])
        self.assertIn("SourceCode='CODAL'", source_lookup[0])
        doc_insert = next(x for x in cur.calls if "INSERT INTO dbo.SourceDocuments" in x[0])
        self.assertEqual(doc_insert[1][0:2], (7, 77))


if __name__ == "__main__":
    unittest.main()
