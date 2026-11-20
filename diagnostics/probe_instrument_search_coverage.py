"""Read-only probe of TSETMC InstrumentSearch coverage and query limits.

This is an exploratory diagnostic, not a claim that InstrumentSearch can enumerate
all instruments. It sends GET requests only and does not import the application,
access any database, or write local files.

Run from repository root:
    python diagnostics/probe_instrument_search_coverage.py > diagnostics/instrument_search_coverage_result.json

The report tests empty/wildcard queries and a bounded set of Persian characters.
It records raw response metadata, result counts, unique InsCodes, overlaps, and
whether pagination/total-count metadata is exposed. Results are printed to stdout.
"""
from __future__ import annotations

import json
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

# Persian alphabet; single-character searches are only probes. They are not
# guaranteed to match every instrument name or to be supported by the endpoint.
PERSIAN_PROBE_CHARS = list("اآبپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی")
SPECIAL_PROBES = ["", "*", "%", "الف", "شرکت"]

def request_json(query: str, timeout: int = 30) -> dict[str, Any]:
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
        result["body_preview"] = body[:1000].decode("utf-8", errors="replace")
        return result

    result["top_level_type"] = type(payload).__name__
    if isinstance(payload, dict):
        result["top_level_keys"] = list(payload.keys())
        rows = payload.get("instrumentSearch")
        if rows is None:
            rows = payload.get("InstrumentSearch")
        if rows is None:
            rows = payload.get("data")
        if rows is None:
            rows = []
        # Preserve any server-provided count/paging metadata for inspection.
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

    compact = []
    codes = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("insCode") or "").strip()
        if code.isdigit() and code != "0":
            codes.add(code)
        compact.append({
            "insCode": code or None,
            "symbol": row.get("lVal18AFC"),
            "company_name": row.get("lVal30"),
            "flow": row.get("flow"),
            "flowTitle": row.get("flowTitle"),
            "lastDate": row.get("lastDate"),
        })

    result["row_count"] = len(rows)
    result["valid_inscode_count"] = len(codes)
    result["unique_inscodes"] = sorted(codes)
    result["sample_first_10"] = compact[:10]
    result["sample_last_5"] = compact[-5:] if len(compact) > 10 else []
    return result


def main() -> None:
    started = datetime.now().astimezone()
    queries = list(dict.fromkeys(SPECIAL_PROBES + PERSIAN_PROBE_CHARS))
    probes = [request_json(query) for query in queries]

    all_codes = set()
    per_query = []
    code_frequency: Counter[str] = Counter()
    successful = 0
    for probe in probes:
        codes = set(probe.get("unique_inscodes", []))
        all_codes.update(codes)
        code_frequency.update(codes)
        if probe.get("http_status") == 200 and "parse_error" not in probe:
            successful += 1
        per_query.append({
            "query": probe["query"],
            "http_status": probe.get("http_status"),
            "row_count": probe.get("row_count"),
            "valid_inscode_count": probe.get("valid_inscode_count"),
            "error": probe.get("error"),
            "parse_error": probe.get("parse_error"),
        })

    report = {
        "diagnostic": "tsetmc_instrument_search_coverage_probe",
        "read_only": True,
        "http_method": "GET",
        "database_access": False,
        "file_writes": False,
        "started_at_local": started.isoformat(timespec="seconds"),
        "finished_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
        "warning": (
            "This probe cannot prove exhaustive coverage. Search terms may miss "
            "instruments, endpoint behavior may be capped, and no official "
            "pagination contract has been established."
        ),
        "query_count": len(probes),
        "successful_query_count": successful,
        "distinct_inscode_count_union": len(all_codes),
        "codes_returned_by_multiple_queries_count": sum(
            1 for count in code_frequency.values() if count > 1
        ),
        "query_summary": per_query,
        "all_probe_results": probes,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
