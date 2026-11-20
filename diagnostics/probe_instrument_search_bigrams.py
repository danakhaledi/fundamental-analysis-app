"""Bounded two-character coverage scan for TSETMC InstrumentSearch.

Read-only diagnostic: HTTP GET only, no application imports, database access,
or local file writes. JSON is written to stdout.

Run from repository root:
    python diagnostics/probe_instrument_search_bigrams.py > diagnostics/instrument_search_bigrams_result.json

Optional controls:
    --delay 0.15          Seconds between requests (default: 0.15)
    --timeout 20          Per-request timeout in seconds (default: 20)
    --start-index 0       Start position in deterministic bigram list
    --max-queries 0       Maximum requests; 0 means all remaining combinations

The scan tests all ordered two-character combinations from the Persian alphabet
used by the previous probe. Exactly 40 rows is flagged as a potentially capped
response, not proof of an endpoint limit. This scan cannot prove exhaustive
instrument coverage: names may contain other scripts/digits, and any saturated
query may hide additional matches.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from typing import Any

BASE = "https://cdn.tsetmc.com/api/Instrument/GetInstrumentSearch/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
PERSIAN_CHARS = list("اآبپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی")


def request_json(query: str, timeout: int) -> dict[str, Any]:
    url = BASE + urllib.parse.quote(query, safe="")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.tsetmc.com/",
        },
    )
    result: dict[str, Any] = {"query": query, "url": url}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result["http_status"] = response.status
            result["content_type"] = response.headers.get("Content-Type")
            body = response.read()
    except urllib.error.HTTPError as exc:
        result["http_status"] = exc.code
        result["error"] = f"HTTPError: {exc}"
        body = exc.read()
    except Exception as exc:
        result["http_status"] = None
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    try:
        payload = json.loads(body.decode("utf-8-sig", errors="replace"))
    except Exception as exc:
        result["parse_error"] = f"{type(exc).__name__}: {exc}"
        result["body_preview"] = body[:300].decode("utf-8", errors="replace")
        return result

    if isinstance(payload, dict):
        rows = payload.get("instrumentSearch")
        if rows is None:
            rows = payload.get("InstrumentSearch")
        if rows is None:
            rows = payload.get("data")
        metadata = {
            key: value for key, value in payload.items()
            if key not in ("instrumentSearch", "InstrumentSearch", "data")
        }
        if metadata:
            result["response_metadata"] = metadata
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    if not isinstance(rows, list):
        result["rows_container_type"] = type(rows).__name__
        rows = []

    instruments = []
    codes = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("insCode") or "").strip()
        if not code.isdigit() or code == "0":
            continue
        codes.append(code)
        instruments.append({
            "insCode": code,
            "symbol": row.get("lVal18AFC"),
            "company_name": row.get("lVal30"),
            "flow": row.get("flow"),
            "flowTitle": row.get("flowTitle"),
            "lastDate": row.get("lastDate"),
        })

    result["row_count"] = len(rows)
    result["valid_inscode_count"] = len(set(codes))
    result["potentially_capped"] = len(rows) == 40
    result["inscodes"] = sorted(set(codes))
    result["instruments"] = instruments
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--max-queries", type=int, default=0)
    args = parser.parse_args()

    all_queries = [a + b for a in PERSIAN_CHARS for b in PERSIAN_CHARS]
    start = max(0, min(args.start_index, len(all_queries)))
    selected = all_queries[start:]
    if args.max_queries > 0:
        selected = selected[:args.max_queries]

    started = datetime.now().astimezone()
    summaries = []
    instrument_map: dict[str, dict[str, Any]] = {}
    code_frequency: Counter[str] = Counter()
    successful = 0
    saturated = []

    for index, query in enumerate(selected):
        probe = request_json(query, args.timeout)
        codes = probe.get("inscodes", [])
        code_frequency.update(codes)

        if probe.get("http_status") == 200 and "parse_error" not in probe:
            successful += 1
        if probe.get("potentially_capped"):
            saturated.append(query)

        for instrument in probe.get("instruments", []):
            code = instrument["insCode"]
            if code not in instrument_map:
                instrument_map[code] = {
                    **instrument,
                    "matched_queries": [],
                }
            if query not in instrument_map[code]["matched_queries"]:
                instrument_map[code]["matched_queries"].append(query)

        summaries.append({
            "query": query,
            "http_status": probe.get("http_status"),
            "row_count": probe.get("row_count"),
            "valid_inscode_count": probe.get("valid_inscode_count"),
            "potentially_capped": probe.get("potentially_capped", False),
            "error": probe.get("error"),
            "parse_error": probe.get("parse_error"),
            "inscodes": codes,
            "response_metadata": probe.get("response_metadata"),
        })

        if args.delay > 0 and index + 1 < len(selected):
            time.sleep(args.delay)

    report = {
        "diagnostic": "tsetmc_instrument_search_two_character_scan",
        "read_only": True,
        "http_method": "GET",
        "database_access": False,
        "file_writes_by_script": False,
        "started_at_local": started.isoformat(timespec="seconds"),
        "finished_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
        "alphabet": PERSIAN_CHARS,
        "total_possible_queries": len(all_queries),
        "start_index": start,
        "queries_attempted": len(selected),
        "next_start_index": start + len(selected),
        "successful_query_count": successful,
        "failed_query_count": len(selected) - successful,
        "distinct_inscode_count_union": len(instrument_map),
        "codes_returned_by_multiple_queries_count": sum(
            1 for count in code_frequency.values() if count > 1
        ),
        "potentially_capped_query_count": len(saturated),
        "potentially_capped_queries": saturated,
        "warning": (
            "A response with exactly 40 rows is only flagged as potentially capped. "
            "This scan is not proof of exhaustive coverage. It does not cover all "
            "possible Latin, numeric, punctuation, or longer-query cases, and "
            "saturated queries may omit matching instruments."
        ),
        "query_summary": summaries,
        "instruments": list(instrument_map.values()),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
