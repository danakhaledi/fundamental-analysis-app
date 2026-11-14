import unittest

from app.tsetmc_registry import parse_industries, parse_instrument_identity


class TSETMCRegistryParserTests(unittest.TestCase):
    def test_parse_industrial_groups_only(self):
        payload = {
            "staticData": [
                {"type": "IndustrialGroup", "code": 27, "name": "فلزات اساسی"},
                {"type": "PaperType", "code": 1, "name": "سهام"},
            ]
        }
        self.assertEqual(parse_industries(payload), [
            {"industry_code": "27", "industry_name": "فلزات اساسی"}
        ])

    def test_reject_empty_industry_payload(self):
        with self.assertRaises(ValueError):
            parse_industries({"staticData": [{"type": "PaperType", "code": 1, "name": "سهام"}]})

    def test_parse_official_company_and_industry_codes(self):
        payload = {
            "instrumentIdentity": {
                "insCode": "123456",
                "cComVal": "COMP-42",
                "sector": {"cSecVal": "27 ", "lSecVal": "فلزات اساسی"},
            }
        }
        self.assertEqual(parse_instrument_identity(payload, "123456"), {
            "ins_code": "123456",
            "company_code": "COMP-42",
            "industry_code": "27",
            "industry_name": "فلزات اساسی",
            "raw_json": __import__("json").dumps(payload, ensure_ascii=False),
        })

    def test_reject_missing_company_code(self):
        payload = {
            "instrumentIdentity": {
                "insCode": "123456",
                "sector": {"cSecVal": "27 ", "lSecVal": "فلزات اساسی"},
            }
        }
        with self.assertRaises(ValueError):
            parse_instrument_identity(payload, "123456")

    def test_reject_conflicting_industry_codes(self):
        payload = {
            "instrumentIdentity": {
                "cComVal": "COMP-42",
                "sector": {"cSecVal": "27 ", "lSecVal": "فلزات اساسی"},
                "other": {"cSecVal": "28", "lSecVal": "صنعت دیگر"},
            }
        }
        with self.assertRaises(ValueError):
            parse_instrument_identity(payload, "123456")


if __name__ == "__main__":
    unittest.main()
