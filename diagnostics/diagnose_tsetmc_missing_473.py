"""Read-only diagnostic for the 473 symbols missing from the TSETMC snapshot.

This script performs HTTP GET requests only. It does not import the application
database layer and does not read, create, update, or delete any database data.
It prints a JSON report to stdout; it does not write output files.

Run from the repository root:
    python diagnostics/diagnose_tsetmc_missing_473.py

Optional:
    python diagnostics/diagnose_tsetmc_missing_473.py --report diagnostics/data/tsetmc_missing_symbols_report.csv
"""
import argparse
import csv
import gzip
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

BASE = "https://cdn.tsetmc.com/api"
OLD_URL = "https://old.tsetmc.com/tsev2/data/MarketWatchInit.aspx?h=0&r=0"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MARKET_WATCH_URL = (
    BASE + "/ClosingPrice/GetMarketWatch?market=0&industrialGroup="
    "&paperTypes%5B0%5D=1&paperTypes%5B1%5D=2&paperTypes%5B2%5D=3"
    "&paperTypes%5B3%5D=4&paperTypes%5B4%5D=5&paperTypes%5B5%5D=6"
    "&paperTypes%5B6%5D=7&paperTypes%5B7%5D=8&paperTypes%5B8%5D=9"
    "&showTraded=false&withBestLimits=false&hEven=0&RefID=0"
)

CODE_KEYS = ("insCode", "InsCode", "ins_code", "instrumentCode")
SYMBOL_KEYS = (
    "lVal18AFC", "lva", "symbol", "shortName", "Symbol",
    "instrumentName", "ticker",
)


def request_bytes(url, timeout):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.tsetmc.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        status = getattr(response, "status", 200)
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return status, body


def request_json(url, timeout):
    status, body = request_bytes(url, timeout)
    return status, json.loads(body.decode("utf-8-sig", errors="replace"))


def normalize_text(value):
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.replace("\u064a", "\u06cc").replace("\u0643", "\u06a9")
    value = value.replace("\u200c", "").replace("\u200f", "").replace("\u200e", "")
    return re.sub(r"\s+", "", value).strip()


def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def first_value(item, keys):
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return ""


def extract_instruments(payload):
    """Extract instrument-like dictionaries without assuming one response wrapper."""
    found = {}
    for item in walk_dicts(payload):
        code = str(first_value(item, CODE_KEYS)).strip()
        symbol = str(first_value(item, SYMBOL_KEYS)).strip()
        if code.isdigit() and code != "0":
            found[(code, normalize_text(symbol))] = {
                "ins_code": code,
                "symbol": symbol,
                "company_name": str(
                    first_value(item, ("lVal30", "lvc", "companyName", "longName"))
                ).strip(),
            }
    return list(found.values())


def load_missing_report(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"ins_code", "symbol", "market_board"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(
            "CSV must contain columns: " + ", ".join(sorted(required))
        )
    return rows


def summarize_source(name, records, missing):
    by_code = {r["ins_code"]: r for r in records if r["ins_code"]}
    by_symbol = defaultdict(list)
    for record in records:
        key = normalize_text(record.get("symbol"))
        if key:
            by_symbol[key].append(record)

    exact = []
    same_symbol = []
    for old in missing:
        old_code = str(old.get("ins_code", "")).strip()
        old_symbol = normalize_text(old.get("symbol"))
        if old_code in by_code:
            exact.append(old)
        elif old_symbol and old_symbol in by_symbol:
            same_symbol.append({
                "old_symbol": old.get("symbol", ""),
                "old_ins_code": old_code,
                "api_matches": by_symbol[old_symbol],
            })

    return {
        "source": name,
        "normalized_instrument_records": len(records),
        "exact_ins_code_matches": len(exact),
        "same_symbol_different_ins_code_matches": len(same_symbol),
        "exact_match_examples": [
            {"symbol": r.get("symbol"), "ins_code": r.get("ins_code")}
            for r in exact[:30]
        ],
        "different_code_examples": same_symbol[:30],
    }


def legacy_market_watch_records(timeout):
    status, body = request_bytes(OLD_URL, timeout)
    text = body.decode("utf-8-sig", errors="replace")
    sections = text.split("@")
    if len(sections) < 3:
        raise ValueError("Legacy MarketWatchInit response has fewer than 3 sections")
    records = []
    for line in sections[2].split(";"):
        columns = line.strip().split(",")
        if len(columns) < 18:
            continue
        code, symbol, name = columns[0].strip(), columns[2].strip(), columns[3].strip()
        if code.isdigit() and code != "0" and symbol:
            records.append({
                "ins_code": code,
                "symbol": symbol,
                "company_name": name,
            })
    return status, records


def run(args):
    missing = load_missing_report(args.report)
    derivative = [r for r in missing if "مشتقه" in r.get("market_board", "")]
    non_derivative = [r for r in missing if r not in derivative]

    result = {
        "read_only": True,
        "database_access": False,
        "missing_report": args.report,
        "missing_report_rows": len(missing),
        "derivative_rows": len(derivative),
        "non_derivative_rows": len(non_derivative),
        "sources": [],
        "non_derivative_search": {
            "requested_symbols": len(non_derivative),
            "found_symbols": [],
            "not_found_symbols": [],
            "errors": [],
        },
    }

    # Recheck the two bulk endpoints used by the application.
    try:
        status, payload = request_json(MARKET_WATCH_URL, args.timeout)
        records = extract_instruments(payload)
        result["sources"].append(
            {"http_status": status, **summarize_source("GetMarketWatch", records, missing)}
        )
    except Exception as exc:
        result["sources"].append({
            "source": "GetMarketWatch",
            "error": f"{type(exc).__name__}: {exc}",
        })

    try:
        status, records = legacy_market_watch_records(args.timeout)
        result["sources"].append(
            {"http_status": status, **summarize_source("MarketWatchInit", records, missing)}
        )
    except Exception as exc:
        result["sources"].append({
            "source": "MarketWatchInit",
            "error": f"{type(exc).__name__}: {exc}",
        })

    # Options watch is a paired-contract feed; test All, TSE and IFB separately.
    for market in (0, 1, 2):
        name = f"GetInstrumentOptionMarketWatch/{market}"
        try:
            status, payload = request_json(
                f"{BASE}/Instrument/GetInstrumentOptionMarketWatch/{market}",
                args.timeout,
            )
            records = extract_instruments(payload)
            result["sources"].append(
                {"http_status": status, **summarize_source(name, records, derivative)}
            )
        except Exception as exc:
            result["sources"].append({
                "source": name,
                "error": f"{type(exc).__name__}: {exc}",
            })

    # Futures have a separate watch endpoint; keep it distinct from options.
    name = "GetInstrumentFutureMarketWatch/0"
    try:
        status, payload = request_json(
            f"{BASE}/Instrument/GetInstrumentFutureMarketWatch/0",
            args.timeout,
        )
        records = extract_instruments(payload)
        result["sources"].append(
            {"http_status": status, **summarize_source(name, records, derivative)}
        )
    except Exception as exc:
        result["sources"].append({
            "source": name,
            "error": f"{type(exc).__name__}: {exc}",
        })

    # Search the 46 non-derivative symbols individually to avoid assuming
    # that a bulk watch contains every active or recently expired instrument.
    for index, old in enumerate(non_derivative, start=1):
        symbol = str(old.get("symbol", "")).strip()
        if not symbol:
            result["non_derivative_search"]["not_found_symbols"].append(symbol)
            continue
        url = f"{BASE}/Instrument/GetInstrumentSearch/{urllib.parse.quote(symbol, safe='')}"
        try:
            status, payload = request_json(url, args.timeout)
            matches = extract_instruments(payload)
            key = normalize_text(symbol)
            exact_symbol_matches = [
                row for row in matches if normalize_text(row.get("symbol")) == key
            ]
            result["non_derivative_search"]["found_symbols" if exact_symbol_matches else "not_found_symbols"].append(
                {
                    "symbol": symbol,
                    "old_ins_code": str(old.get("ins_code", "")).strip(),
                    "http_status": status,
                    "matches": exact_symbol_matches[:10] if exact_symbol_matches else matches[:5],
                } if exact_symbol_matches or matches else symbol
            )
        except Exception as exc:
            result["non_derivative_search"]["errors"].append({
                "symbol": symbol,
                "error": f"{type(exc).__name__}: {exc}",
            })
        if index < len(non_derivative):
            time.sleep(args.delay)

    # A compact summary of the historical report's market classification.
    result["missing_by_market_board"] = dict(
        Counter(r.get("market_board", "(blank)") for r in missing)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        default="diagnostics/data/tsetmc_missing_symbols_report.csv",
        help="Path to the committed 473-row CSV report",
    )
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument("--delay", type=float, default=0.15)
    args = parser.parse_args()
    try:
        run(args)
    except Exception as exc:
        print(f"DIAGNOSTIC_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
