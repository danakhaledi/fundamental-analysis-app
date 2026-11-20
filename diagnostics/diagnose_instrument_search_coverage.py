"""Read-only coverage probe for TSETMC InstrumentSearch.

GET-only. No application imports, database access, or file writes.
Run from repository root:
    python diagnostics/diagnose_instrument_search_coverage.py > diagnostics/instrument_search_coverage_result.json

The probe searches each Persian/Arabic alphabet character independently,
measures result counts and overlap, and compares the union with MarketWatch.
This is an empirical coverage test, not proof of a complete instrument registry.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

BASE = "https://cdn.tsetmc.com/api"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Persian letters plus Arabic forms commonly found in TSETMC instrument names.
QUERY_TERMS = list("اآبپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی") + ["ي", "ك"]
MARKET_WATCH_URL = (
    BASE + "/ClosingPrice/GetMarketWatch?market=0&industrialGroup="
    "&paperTypes%5B0%5D=1&paperTypes%5B1%5D=2&paperTypes%5B2%5D=3"
    "&paperTypes%5B3%5D=4&paperTypes%5B4%5D=5&paperTypes%5B5%5D=6"
    "&paperTypes%5B6%5D=7&paperTypes%5B7%5D=8&paperTypes%5B8%5D=9"
    "&showTraded=false&withBestLimits=false&hEven=0&RefID=0"
)


def get_json(url: str, timeout: int = 40) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.tsetmc.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8-sig", errors="replace")
            result: dict[str, Any] = {
                "http_status": response.status,
                "content_type": response.headers.get("Content-Type"),
            }
        try:
            result["data"] = json.loads(raw)
        except Exception as exc:
            result["parse_error"] = f"{type(exc).__name__}: {exc}"
            result["body_preview"] = raw[:1000]
        return result
    except urllib.error.HTTPError as exc:
        return {"http_status": exc.code, "error": f"HTTPError: {exc}"}
    except Exception as exc:
        return {"http_status": None, "error": f"{type(exc).__name__}: {exc}"}


def rows_from(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        rows = payload.get("instrumentSearch", payload.get("InstrumentSearch", []))
    else:
        rows = payload
    return rows if isinstance(rows, list) else []


def market_codes(payload: Any) -> set[str]:
    rows = payload.get("marketwatch", []) if isinstance(payload, dict) else payload
    codes: set[str] = set()
    if not isinstance(rows, list):
        return codes
    for row in rows:
        if not isinstance(row, dict):
            continue
        instrument = row.get("instrument")
        merged = dict(row)
        if isinstance(instrument, dict):
            merged.update(instrument)
        code = str(merged.get("insCode") or merged.get("InsCode") or "").strip()
        if code.isdigit() and code != "0":
            codes.add(code)
    return codes


def main() -> None:
    started = datetime.now().astimezone()
    report: dict[str, Any] = {
        "diagnostic": "tsetmc_instrument_search_coverage_read_only",
        "read_only": True,
        "http_method": "GET",
        "database_access": False,
        "file_writes_by_script": False,
        "run_at_local": started.isoformat(timespec="seconds"),
        "query_strategy": "one request per Persian/Arabic alphabet character",
        "query_terms": QUERY_TERMS,
        "queries": [],
    }
    union: dict[str, dict] = {}
    errors = 0

    for term in QUERY_TERMS:
        url = BASE + "/Instrument/GetInstrumentSearch/" + urllib.parse.quote(term, safe="")
        response = get_json(url)
        rows = rows_from(response.get("data")) if "data" in response else []
        valid_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("insCode") or "").strip()
            if code.isdigit() and code != "0":
                valid_rows.append(row)
                union.setdefault(code, row)
        if response.get("http_status") != 200 or response.get("parse_error"):
            errors += 1
        report["queries"].append({
            "query": term,
            "http_status": response.get("http_status"),
            "error": response.get("error"),
            "parse_error": response.get("parse_error"),
            "result_count": len(valid_rows),
            "distinct_inscode_count": len({
                str(row.get("insCode")) for row in valid_rows
            }),
            "matches": valid_rows,
        })
        time.sleep(0.3)

    watch_response = get_json(MARKET_WATCH_URL, timeout=60)
    watch_codes = market_codes(watch_response.get("data")) if "data" in watch_response else set()
    search_codes = set(union)
    report["summary"] = {
        "query_count": len(QUERY_TERMS),
        "query_errors": errors,
        "unique_instrument_count_union": len(search_codes),
        "max_results_in_single_query": max(
            (q["result_count"] for q in report["queries"]), default=0
        ),
        "queries_with_40_or_more_results": [
            q["query"] for q in report["queries"] if q["result_count"] >= 40
        ],
        "market_watch_http_status": watch_response.get("http_status"),
        "market_watch_instrument_count": len(watch_codes),
        "market_watch_codes_found_by_search_union": len(search_codes & watch_codes),
        "market_watch_codes_not_found_by_search_union": len(watch_codes - search_codes),
        "search_union_codes_not_in_market_watch": len(search_codes - watch_codes),
        "market_watch_codes_not_found_sample": sorted(watch_codes - search_codes)[:100],
        "search_codes_not_in_market_watch_sample": sorted(search_codes - watch_codes)[:100],
        "interpretation_note": (
            "A union of character searches is not necessarily a complete registry: "
            "the endpoint may cap results per query or apply undocumented matching rules. "
            "Compare counts and overlaps; do not treat this result as proof of completeness."
        ),
    }
    report["unique_instruments"] = list(union.values())
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
