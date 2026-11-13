import unittest
from decimal import Decimal
from app.codal_sales_stage import _sales_rows
from app.codal_products_stage import _source_names


class CodalSalesParserTests(unittest.TestCase):
    def test_current_month_columns_and_sales_sections(self):
        header0 = ["شرح", "از ابتدای سال مالی", "اصلاحات",
                   "از ابتدای سال مالی اصلاح شده", "دوره یک ماهه منتهی به ۱۴۰۵/۰۷/۳۰"]
        header1 = [None] * 24
        header1[11:15] = ["تعداد تولید", "تعداد فروش", "نرخ فروش (ریال)", "مبلغ فروش (میلیون ریال)"]
        domestic = ["فروش داخلی:"] + [None] * 25
        row = ["بنزن", "تن"] + [None] * 24
        row[13:17] = ["۸,۳۳۰", "۱۱,۵۶۳", "۱,۸۵۴,۳۱۹,۷۲۷", "۲۱,۴۴۱,۴۹۹"]
        row[25] = "توليد"
        export = ["فروش صادراتی:"] + [None] * 25
        exp_row = ["بنزن", "تن"] + [None] * 24
        exp_row[13:17] = ["۰", "۰", "۰", "۰"]
        report = {
            "Title": "گزارش فعالیت ماهانه دوره ۱ ماهه منتهی به ۱۴۰۵/۰۷/۳۰",
            "Sheets": [{"sheet": "Table_1", "rows": [header0, header1, domestic, row, export, exp_row]}],
        }
        values = _sales_rows(report)
        self.assertEqual(len(values), 2)
        self.assertEqual(values[0]["sales_type"], "DOMESTIC")
        self.assertEqual(values[0]["production"], Decimal("8330"))
        self.assertEqual(values[0]["sales_qty"], Decimal("11563"))
        self.assertEqual(values[0]["sales_rate"], Decimal("1854319727"))
        self.assertEqual(values[0]["sales_amount"], Decimal("21441499"))
        self.assertEqual(values[1]["sales_type"], "EXPORT")
        self.assertEqual(values[1]["sales_qty"], Decimal("0"))

    def test_product_material_and_resource_names_are_extracted(self):
        t1 = [
            ["شرح", "YTD", "اصلاحات", "YTD اصلاح شده", "دوره یک ماهه منتهی"],
            ["نام محصول", "واحد"] + ["ستون"] * 24,
            ["فروش داخلی:"] + [None] * 25,
            ["بنزن", "تن"] + [None] * 24,
        ]
        t2 = [
            ["شرح", "واحد", "YTD", "اصلاحات", "YTD اصلاح شده", "خرید دوره یک ماهه منتهی"],
            ["مقدار", "نرخ", "مبلغ"] * 6,
            ["مواد اولیه داخلی"] + [None] * 19,
            ["نفتا", "تن"] + [None] * 18,
        ]
        t3 = [
            ["صنعت", "طبقه بندی", "نوع انرژی", "واحد اندازه گیری", "YTD", "اصلاحات", "اصلاح شده", "دوره یک ماهه منتهی"],
            ["مقدار", "نرخ", "مبلغ"] * 7,
            ["محصولات شیمیایی", "خوراک", "نفتا", "تن"] + [None] * 20,
        ]
        names = _source_names({"Sheets": [
            {"sheet": "Table_1", "rows": t1},
            {"sheet": "Table_2", "rows": t2},
            {"sheet": "Table_3", "rows": t3},
        ]})
        self.assertEqual({x[1] for x in names}, {"Table_1", "Table_2", "Table_3"})
        self.assertTrue(any(x[0] == "بنزن" and x[3] == "PRODUCT" for x in names))
        self.assertTrue(any(x[0] == "نفتا" and x[3] == "PURCHASE_MATERIAL" for x in names))
        self.assertTrue(any(x[0] == "نفتا" and x[3] == "CONSUMED_RESOURCE" for x in names))


if __name__ == "__main__":
    unittest.main()
