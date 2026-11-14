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
                "cComVal": "SHOULD-NOT-BE-USED",
                "cSocCSAC": "ISSUER-42",
                "sector": {"cSecVal": "27 ", "lSecVal": "فلزات اساسی"},
            }
        }
        self.assertEqual(parse_instrument_identity(payload, "123456"), {
            "ins_code": "123456",
            "company_code": "ISSUER-42",
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

    def test_industry_code_is_read_only_from_sector_object(self):
        payload = {
            "instrumentIdentity": {
                "cComVal": "SHOULD-NOT-BE-USED",
                "cSocCSAC": "ISSUER-42",
                "cSocCSAC": "ISSUER-42",
                "sector": {"cSecVal": "27 ", "lSecVal": "فلزات اساسی"},
            }
        }
        parsed = parse_instrument_identity(payload, "123456")
        self.assertEqual(parsed["industry_code"], "27")

    def test_missing_industry_is_allowed_but_missing_cSocCSAC_is_not(self):
        payload = {"instrumentIdentity": {"cSocCSAC": "ISSUER-42", "cComVal": "OTHER"}}
        parsed = parse_instrument_identity(payload, "123456")
        self.assertEqual(parsed["company_code"], "ISSUER-42")
        self.assertEqual(parsed["industry_code"], "")
        with self.assertRaises(ValueError):
            parse_instrument_identity({"instrumentIdentity": {"cComVal": "COMP-42"}}, "123456")


if __name__ == "__main__":
    unittest.main()
