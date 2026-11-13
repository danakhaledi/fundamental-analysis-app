import json
import unittest
from datetime import datetime
from app.codal_state import _json_load, _parse_datetime, _iso


class CodalStateSerializationTests(unittest.TestCase):
    def test_json_fields_decode_without_losing_persian_text(self):
        value = {"TracingNo": "123", "Symbol": "بوعلی", "Sheets": [{"name": "Table_2"}]}
        encoded = json.dumps(value, ensure_ascii=False)
        self.assertEqual(_json_load(encoded), value)

    def test_datetime_round_trip(self):
        parsed = _parse_datetime("2026-11-10T11:30:00")
        self.assertEqual(parsed, datetime(2026, 11, 10, 11, 30, 0))
        self.assertEqual(_iso(parsed), "2026-11-10T11:30:00")

    def test_empty_or_invalid_json_is_safe(self):
        self.assertIsNone(_json_load(None))
        self.assertIsNone(_json_load("not-json"))


if __name__ == "__main__":
    unittest.main()
