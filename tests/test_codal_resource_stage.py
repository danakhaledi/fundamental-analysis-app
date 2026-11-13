import unittest
from decimal import Decimal
from app.codal_resource_stage import _number, _period, _resource_rows, _unit_code


class ResourceParserTests(unittest.TestCase):
    def test_numeric_cells_preserve_null_and_zero(self):
        self.assertIsNone(_number(None))
        self.assertIsNone(_number("  "))
        self.assertEqual(_number("۰"), Decimal("0"))
        self.assertEqual(_number("۱,۲۳۴٫۵۰"), Decimal("1234.50"))
        self.assertEqual(_number("(۱۲۳)"), Decimal("-123"))

    def test_units_are_not_silently_converted(self):
        self.assertEqual(_unit_code("مترمکعب", "M3"), "M3")
        self.assertEqual(_unit_code("تن", "TON"), "TON")
        self.assertEqual(_unit_code("کیلوگرم و عدد", "TON"), None)

    def test_table2_and_table3_current_month_and_adjustments(self):
        purchase = [None] * 20
        purchase[0], purchase[1] = "گاز", "مترمکعب"
        purchase[5], purchase[6], purchase[7] = "۲", "۲,۳۷۶", "۴,۷۵۲"
        purchase[11], purchase[12], purchase[13] = "۱۰۰", "۲,۳۷۶", "۲۳۷,۶۰۰"
        table2 = [
            ["شرح", "واحد", "YTD", "اصلاحات", "YTD اصلاح شده", "خرید دوره یک ماهه منتهی به ۱۴۰۵/۰۷/۳۰"],
            ["مقدار", "نرخ", "مبلغ"] * 6,
            ["مواد اولیه داخلی"] + [None] * 19,
            purchase,
        ]
        consumption = [None] * 26
        consumption[1:4] = ["سوخت", "گاز طبیعی", "مترمکعب"]
        consumption[7:10] = ["۳", "۲,۳۷۶", "۷,۱۲۸"]
        consumption[13:16] = ["۲۰۰", "۲,۳۷۶", "۴۷۵,۲۰۰"]
        table3 = [
            ["صنعت", "طبقه بندی", "نوع انرژی", "واحد اندازه گیری", "از ابتدای سال مالی", "اصلاحات", "اصلاح شده", "دوره یک ماهه منتهی به ۱۴۰۵/۰۷/۳۰"],
            ["مقدار مصرف", "نرخ", "مبلغ"] * 7,
            consumption,
        ]
        report = {
            "Title": "گزارش فعالیت ماهانه دوره ۱ ماهه منتهی به ۱۴۰۵/۰۷/۳۰",
            "Sheets": [{"sheet": "Table_2", "rows": table2}, {"sheet": "Table_3", "rows": table3}],
        }
        self.assertEqual(_period(report["Title"]), (1405, 7, 30))
        rows = _resource_rows(report)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["transaction_type"], "PURCHASE")
        self.assertEqual(rows[0]["purchase_type"], "DOMESTIC")
        self.assertEqual(rows[0]["quantity"], Decimal("100"))
        self.assertEqual(rows[0]["adjustment_qty"], Decimal("2"))
        self.assertEqual(rows[1]["transaction_type"], "CONSUMPTION")
        self.assertEqual(rows[1]["resource_category"], "سوخت")
        self.assertEqual(rows[1]["quantity"], Decimal("200"))
        self.assertEqual(rows[1]["adjustment_qty"], Decimal("3"))


if __name__ == "__main__":
    unittest.main()
