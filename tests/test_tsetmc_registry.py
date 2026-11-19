import json
import unittest

from app.tsetmc_registry import build_industry_hierarchy, parse_industries, parse_instrument_identity


class TSETMCRegistryParserTests(unittest.TestCase):
    def test_parse_industrial_groups_only(self):
        payload = {"staticData": [
            {"type": "IndustrialGroup", "code": 27, "name": "فلزات اساسی"},
            {"type": "PaperType", "code": 1, "name": "سهام"},
        ]}
        self.assertEqual(parse_industries(payload), [{"industry_code": "27", "industry_name": "فلزات اساسی"}])

    def test_reject_empty_industry_payload(self):
        with self.assertRaises(ValueError):
            parse_industries({"staticData": [{"type": "PaperType", "code": 1, "name": "سهام"}]})

    def test_parse_parent_and_lowest_level_subindustry(self):
        payload = {"instrumentIdentity": {
            "insCode": "123456", "cComVal": "NOT_COMPANY", "cSocCSAC": "ISSUER-42",
            "sector": {"cSecVal": "44 ", "lSecVal": "محصولات شیمیایی"},
            "subSector": {"cSoSecVal": "4411 ", "lSoSecVal": "تولید مواد شیمیایی پایه"},
        }}
        parsed = parse_instrument_identity(payload, "123456")
        self.assertEqual(parsed["company_code"], "ISSUER-42")
        self.assertEqual(parsed["parent_industry_code"], "44")
        self.assertEqual(parsed["sub_industry_code"], "4411")
        self.assertEqual(parsed["industry_code"], "4411")
        self.assertEqual(parsed["industry_name"], "تولید مواد شیمیایی پایه")
        self.assertEqual(parsed["raw_json"], json.dumps(payload, ensure_ascii=False))

    def test_parent_fallback_when_no_subsector(self):
        parsed = parse_instrument_identity({"instrumentIdentity": {
            "cSocCSAC": "ISSUER-42", "sector": {"cSecVal": "27", "lSecVal": "فلزات اساسی"},
        }}, "123456")
        self.assertEqual(parsed["industry_code"], "27")
        self.assertEqual(parsed["sub_industry_code"], "")

    def test_build_parent_child_hierarchy(self):
        rows = [
            {"parent_industry_code": "44", "parent_industry_name": "محصولات شیمیایی",
             "sub_industry_code": "4411", "sub_industry_name": "تولید مواد شیمیایی پایه"},
            {"parent_industry_code": "44", "parent_industry_name": "محصولات شیمیایی",
             "sub_industry_code": "4412", "sub_industry_name": "سایر محصولات شیمیایی"},
            {"parent_industry_code": "27", "parent_industry_name": "فلزات اساسی",
             "sub_industry_code": "2711", "sub_industry_name": "تولید آهن و فولاد"},
        ]
        result = {x["industry_code"]: x for x in build_industry_hierarchy(rows)}
        self.assertEqual(len(result), 5)
        self.assertIsNone(result["44"]["parent_industry_code"])
        self.assertEqual(result["4411"]["parent_industry_code"], "44")
        self.assertEqual(result["4412"]["parent_industry_code"], "44")
        self.assertEqual(result["2711"]["parent_industry_code"], "27")

    def test_expected_scale_51_parents_129_subindustries(self):
        rows = []
        for index in range(51):
            parent = str(10 + index)
            for child_index in range(3 if index < 27 else 2):
                rows.append({"parent_industry_code": parent, "parent_industry_name": f"گروه {parent}",
                             "sub_industry_code": f"{parent}{child_index + 1:02d}",
                             "sub_industry_name": f"زیرگروه {parent}-{child_index + 1}"})
        hierarchy = build_industry_hierarchy(rows)
        self.assertEqual(sum(x["parent_industry_code"] is None for x in hierarchy), 51)
        self.assertEqual(sum(x["parent_industry_code"] is not None for x in hierarchy), 129)

    def test_reject_conflicting_subindustry_parent(self):
        rows = [
            {"parent_industry_code": "44", "parent_industry_name": "محصولات شیمیایی",
             "sub_industry_code": "4411", "sub_industry_name": "تولید مواد شیمیایی پایه"},
            {"parent_industry_code": "27", "parent_industry_name": "فلزات اساسی",
             "sub_industry_code": "4411", "sub_industry_name": "تولید مواد شیمیایی پایه"},
        ]
        with self.assertRaises(ValueError):
            build_industry_hierarchy(rows)

    def test_reject_missing_company_code(self):
        with self.assertRaises(ValueError):
            parse_instrument_identity({"instrumentIdentity": {
                "sector": {"cSecVal": "27", "lSecVal": "فلزات اساسی"},
            }}, "123456")

    def test_missing_industry_allowed_but_missing_company_code_rejected(self):
        parsed = parse_instrument_identity(
            {"instrumentIdentity": {"cSocCSAC": "ISSUER-42", "cComVal": "OTHER"}}, "123456")
        self.assertEqual(parsed["company_code"], "ISSUER-42")
        self.assertEqual(parsed["industry_code"], "")
        with self.assertRaises(ValueError):
            parse_instrument_identity({"instrumentIdentity": {"cComVal": "COMP-42"}}, "123456")


if __name__ == "__main__":
    unittest.main()
