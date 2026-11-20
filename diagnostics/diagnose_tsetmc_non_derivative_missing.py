"""Read-only diagnostic for the 46 non-derivative symbols missing from TSETMC sync.

HTTP GET only. Reads the historical missing-symbol CSV; does not import the app,
access SQLite/SQL Server, or write files. Results are printed as JSON to stdout.

Run from repository root:
    python diagnostics/diagnose_tsetmc_non_derivative_missing.py

Optional output redirection:
    python diagnostics/diagnose_tsetmc_non_derivative_missing.py > diagnostics/non_derivative_missing_result.json
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any

BASE = "https://cdn.tsetmc.com/api"
DEFAULT_REPORT = "diagnostics/data/tsetmc_missing_symbols_report.csv"
MARKET_WATCH_URL = (
    BASE + "/ClosingPrice/GetMarketWatch?market=0&industrialGroup="
    "&paperTypes%5B0%5D=1&paperTypes%5B1%5D=2&paperTypes%5B2%5D=3"
    "&paperTypes%5B3%5D=4&paperTypes%5B4%5D=5&paperTypes%5B5%5D=6"
    "&paperTypes%5B6%5D=7&paperTypes%5B7%5D=8&paperTypes%5B8%5D=9"
    "&showTraded=false&withBestLimits=false&hEven=0&RefID=0"
)
LEGACY_MARKET_WATCH_URL = (
    "https://old.tsetmc.com/tsev2/data/MarketWatchInit.aspx?h=0&r=0"
)
STATE_TOP_URL = BASE + "/MarketData/GetInstrumentStateTop/500"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def normalize(value: Any) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.replace("ي", "ی").replace("ى", "ی").replace("ك", "ک")
    value = value.replace("\u200c", "").replace("\u200f", "").replace("\u200e", "")
    return re.sub(r"\s+", "", value).strip()


def request_bytes(url: str, timeout: int) -> tuple[int | None, dict, bytes, str | None]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.tsetmc.com/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read(), None
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, body, (
            f"HTTPError: {exc}"
        )
    except Exception as exc:
        return None, {}, b"", f"{type(exc).__name__}: {exc}"


def decode_body(body: bytes) -> bytes:
    return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body


def get_json(url: str, timeout: int) -> dict[str, Any]:
    status, headers, body, error = request_bytes(url, timeout)
    result: dict[str, Any] = {
        "url": url,
        "http_status": status,
        "content_type": headers.get("Content-Type") or headers.get("content-type"),
    }
    if error:
        result["error"] = error
    try:
        result["data"] = json.loads(
            decode_body(body).decode("utf-8-sig", errors="replace")
        )
    except Exception as exc:
        result["parse_error"] = f"{type(exc).__name__}: {exc}"
        result["body_preview"] = decode_body(body)[:1200].decode(
            "utf-8", errors="replace"
        )
    return result


def get_text(url: str, timeout: int) -> dict[str, Any]:
    status, headers, body, error = request_bytes(url, timeout)
    result: dict[str, Any] = {
        "url": url,
        "http_status": status,
        "content_type": headers.get("Content-Type") or headers.get("content-type"),
    }
    if error:
        result["error"] = error
        return result
    raw = decode_body(body).decode("utf-8-sig", errors="replace")
    sections = raw.split("@")
    if len(sections) < 3 or len(sections[2]) < 1000:
        result["parse_error"] = "Unexpected MarketWatchInit response structure."
        result["body_preview"] = raw[:1200]
        result["codes"] = []
        return result
    codes = set()
    for line in sections[2].split(";"):
        cols = line.strip().split(",")
        if len(cols) < 18:
            continue
        code = cols[0].strip()
        if code.isdigit() and code != "0":
            codes.add(code)
    result["parsed_instrument_count"] = len(codes)
    result["codes"] = sorted(codes)
    return result


def walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def extract_search_rows(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        rows = payload.get("instrumentSearch")
        if rows is None:
            rows = payload.get("InstrumentSearch")
        if rows is None:
            rows = payload
    else:
        rows = payload
    return [
        row for row in walk_dicts(rows)
        if any(key in row for key in ("insCode", "lVal18AFC", "lVal30"))
    ]


def extract_market_watch_codes(payload: Any) -> set[str]:
    if isinstance(payload, dict):
        rows = payload.get("marketwatch")
        if rows is None:
            rows = payload.get("marketWatch", payload)
    else:
        rows = payload
    codes = set()
    for row in walk_dicts(rows):
        instrument = row.get("instrument") if isinstance(row.get("instrument"), dict) else {}
        merged = dict(row)
        merged.update(instrument)
        code = str(merged.get("insCode") or merged.get("InsCode") or "").strip()
        if code.isdigit() and code != "0":
            codes.add(code)
    return codes


def load_non_derivative_rows(path: str) -> tuple[list[dict], list[dict]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"ins_code", "symbol", "market_board"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("CSV must contain: " + ", ".join(sorted(required)))

    selected = []
    excluded = []
    seen = set()
    for row in rows:
        board = str(row.get("market_board") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        if "مشتقه" in board:
            continue
        if normalize(symbol) == normalize("کالا3"):
            excluded.append({
                "symbol": symbol,
                "ins_code": str(row.get("ins_code") or "").strip(),
                "reason": "Previously confirmed fully delisted; excluded from this diagnostic.",
            })
            continue
        code = str(row.get("ins_code") or "").strip()
        key = (code, normalize(symbol))
        if key in seen:
            continue
        seen.add(key)
        row["_symbol_norm"] = normalize(symbol)
        row["_ins_code"] = code
        selected.append(row)
    return selected, excluded


def compact_instrument_info(response: dict[str, Any]) -> dict[str, Any]:
    data = response.get("data")
    if data is None:
        return {key: value for key, value in response.items() if key != "url"}
    rows = list(walk_dicts(data))
    preferred = (
        "insCode", "lVal18AFC", "lVal30", "cIsin", "flow", "flowTitle",
        "cGrValCotTitle", "cComVal", "lastDate", "instrumentState",
    )
    matches = []
    for row in rows:
        compact = {key: row[key] for key in preferred if key in row}
        if compact:
            matches.append(compact)
    return {
        "http_status": response.get("http_status"),
        "error": response.get("error"),
        "parse_error": response.get("parse_error"),
        "matching_fields": matches[:10],
        "top_level_keys": list(data.keys())[:30] if isinstance(data, dict) else None,
    }


def compact_state_response(response: dict[str, Any], ins_code: str) -> dict[str, Any]:
    data = response.get("data")
    if data is None:
        return {key: value for key, value in response.items() if key != "url"}
    rows = []
    state_keys = (
        "insCode", "InsCode", "dEven", "hEven", "cEtaval", "cEtavalTitle",
        "state", "stateTitle", "cValMne",
    )
    for row in walk_dicts(data):
        if any(key in row for key in ("cEtaval", "cEtavalTitle", "stateTitle")):
            compact = {key: row[key] for key in state_keys if key in row}
            rows.append(compact)
    return {
        "http_status": response.get("http_status"),
        "error": response.get("error"),
        "parse_error": response.get("parse_error"),
        "state_rows_found": len(rows),
        "state_rows": rows[:100],
        "payload_top_level_keys": list(data.keys())[:30] if isinstance(data, dict) else None,
        "payload_preview": None if rows else json.dumps(
            data, ensure_ascii=False, default=str
        )[:2500],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--timeout", type=int, default=40)
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--max-symbols", type=int, default=0,
                        help="Optional limit for a small test run; 0 means all.")
    args = parser.parse_args()

    old_rows, excluded_rows = load_non_derivative_rows(args.report)
    if args.max_symbols > 0:
        old_rows = old_rows[:args.max_symbols]

    new_watch = get_json(MARKET_WATCH_URL, timeout=max(args.timeout, 60))
    new_watch_codes = (
        extract_market_watch_codes(new_watch["data"])
        if new_watch.get("data") is not None else set()
    )
    legacy_watch = get_text(LEGACY_MARKET_WATCH_URL, timeout=max(args.timeout, 45))
    legacy_codes = set(legacy_watch.pop("codes", []))
    state_top = get_json(STATE_TOP_URL, timeout=max(args.timeout, 45))
    state_top_rows = [
        row for row in walk_dicts(state_top.get("data"))
        if any(key in row for key in ("insCode", "InsCode", "cEtaval", "cEtavalTitle"))
    ] if state_top.get("data") is not None else []

    results = []
    errors = []
    market_counts = Counter(row.get("market_board", "") for row in old_rows)

    for index, old in enumerate(old_rows, start=1):
        symbol = str(old.get("symbol") or "").strip()
        old_code = old["_ins_code"]
        search_url = BASE + "/Instrument/GetInstrumentSearch/" + urllib.parse.quote(
            symbol, safe=""
        )
        search_response = get_json(search_url, args.timeout)
        search_rows = (
            extract_search_rows(search_response["data"])
            if search_response.get("data") is not None else []
        )

        exact_matches = []
        candidates = {old_code}
        for row in search_rows:
            code = str(row.get("insCode") or "").strip()
            found_symbol = str(row.get("lVal18AFC") or "").strip()
            if code.isdigit() and code != "0" and normalize(found_symbol) == old["_symbol_norm"]:
                candidates.add(code)
                exact_matches.append({
                    "ins_code": code,
                    "symbol": found_symbol,
                    "company_name": row.get("lVal30"),
                    "isin": row.get("cIsin"),
                    "flow": row.get("flow"),
                    "flow_title": row.get("flowTitle"),
                    "last_date": row.get("lastDate"),
                })

        candidate_results = []
        for code in sorted(candidates):
            info = get_json(BASE + "/Instrument/GetInstrumentInfo/" + code, args.timeout)
            time.sleep(args.delay)
            states = get_json(BASE + "/MarketData/GetInstrumentStateAll/" + code, args.timeout)
            top_matches = [
                row for row in state_top_rows
                if str(row.get("insCode") or row.get("InsCode") or "").strip() == code
            ]
            candidate_results.append({
                "ins_code": code,
                "source": "legacy_report" if code == old_code else "exact_symbol_search",
                "in_new_market_watch": code in new_watch_codes,
                "in_legacy_market_watch": code in legacy_codes,
                "instrument_info": compact_instrument_info(info),
                "instrument_state_all": compact_state_response(states, code),
                "state_top_matches": top_matches[:30],
            })
            time.sleep(args.delay)

        results.append({
            "symbol": symbol,
            "legacy_ins_code": old_code,
            "company_name": old.get("company_name"),
            "market_board": old.get("market_board"),
            "flow": old.get("flow"),
            "search_http_status": search_response.get("http_status"),
            "search_error": search_response.get("error"),
            "search_parse_error": search_response.get("parse_error"),
            "exact_symbol_search_matches": exact_matches,
            "candidate_instruments": candidate_results,
        })
        if search_response.get("error") or search_response.get("parse_error"):
            errors.append({
                "symbol": symbol,
                "endpoint": "Instrument/GetInstrumentSearch",
                "error": search_response.get("error") or search_response.get("parse_error"),
            })
        if index < len(old_rows):
            time.sleep(args.delay)

    report = {
        "diagnostic": "tsetmc_non_derivative_missing_symbols_read_only",
        "read_only": True,
        "http_method": "GET",
        "database_access": False,
        "application_imports": False,
        "file_writes": False,
        "input_report": args.report,
        "historical_non_derivative_rows_selected": len(results),
        "excluded_rows": excluded_rows,
        "market_board_counts": dict(market_counts),
        "market_watch_new": {
            "http_status": new_watch.get("http_status"),
            "error": new_watch.get("error"),
            "parse_error": new_watch.get("parse_error"),
            "instrument_count": len(new_watch_codes),
        },
        "market_watch_legacy": {
            key: value for key, value in legacy_watch.items() if key != "url"
        },
        "instrument_state_top": {
            "http_status": state_top.get("http_status"),
            "error": state_top.get("error"),
            "parse_error": state_top.get("parse_error"),
            "row_count": len(state_top_rows),
        },
        "results": results,
        "endpoint_errors": errors,
        "interpretation_notes": [
            "Search matches are retained only when the normalized symbol is an exact match.",
            "InstrumentStateAll and StateTop payloads are reported as returned; the script does not infer a trading status from undocumented codes.",
            "Absence from either MarketWatch feed does not prove permanent delisting.",
            "A successful InstrumentInfo or state response is evidence that TSETMC still recognizes the InsCode, not by itself proof that the instrument should be active in the registry.",
            "A zero-row state response is not interpreted as an active or suspended state.",
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
