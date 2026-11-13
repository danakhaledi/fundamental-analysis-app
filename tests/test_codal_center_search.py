import io
import json
import unittest
from unittest.mock import patch
from app.codal_center import search_codal_reports


def response(items):
    return io.BytesIO(json.dumps({"Letters": items}, ensure_ascii=False).encode("utf-8"))


class CodalSearchPaginationTests(unittest.TestCase):
    @patch("app.codal_center.urllib.request.urlopen")
    def test_search_reads_all_pages_and_stops_on_empty_page(self, mocked):
        page1 = [{"TracingNo":"1","Title":"گزارش فعالیت ماهانه شرکت اول","Symbol":"AAA","PublishDateTime":"1405/01/01"}]
        page2 = [{"TracingNo":"2","Title":"گزارش فعالیت ماهانه شرکت دوم","Symbol":"BBB","PublishDateTime":"1405/01/02"}]
        mocked.side_effect = [response(page1), response(page2), response([])]
        found = search_codal_reports(kind="monthly", page_size=50)
        self.assertEqual({x["TracingNo"] for x in found}, {"1","2"})
        self.assertEqual(mocked.call_count, 3)

    @patch("app.codal_center.urllib.request.urlopen")
    def test_repeated_api_page_does_not_loop_forever(self, mocked):
        same = [{"TracingNo":"1","Title":"گزارش فعالیت ماهانه شرکت اول","Symbol":"AAA","PublishDateTime":"1405/01/01"}]
        mocked.side_effect = [response(same), response(same)]
        found = search_codal_reports(kind="monthly", page_size=50)
        self.assertEqual(len(found), 1)
        self.assertEqual(mocked.call_count, 2)


if __name__ == "__main__":
    unittest.main()
