"""Read-only v3 diagnostic: match the 427 legacy derivative rows to TSETMC option/future APIs.

HTTP GET only. Reads the committed CSV report; no application imports, database access,
or file writes. Expands each option-market pair into separate PUT and CALL instruments.

Run from repository root:
    python diagnostics/diagnose_tsetmc_missing_473_v3.py

Optional output redirection:
    python diagnostics/diagnose_tsetmc_missing_473_v3.py > diagnostics/diagnose_tsetmc_missing_473_v3_output.txt
"""
import argparse
import csv
import gzip
import json
import re
import time
import unicodedata
import urllib.request
from collections import Counter, defaultdict

BASE = "https://cdn.tsetmc.com/api"
DEFAULT_REPORT = "diagnostics/data/tsetmc_missing_symbols_report.csv"
OPTION_URL = BASE + "/Instrument/GetInstrumentOptionMarketWatch/{}"
FUTURE_URL = BASE + "/Instrument/GetInstrumentFutureMarketWatch/0"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def norm(value):
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.replace("\u064a", "\u06cc").replace("\u0643", "\u06a9")
    value = value.replace("\u200c", "").replace("\u200f", "").replace("\u200e", "")
    return re.sub(r"\s+", "", value).strip()


def get_json(url, timeout):
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
        content_type = response.headers.get("Content-Type", "")
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return status, content_type, json.loads(body.decode("utf-8-sig", errors="replace"))


def load_derivatives(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"ins_code", "symbol", "market_board"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("CSV must contain: " + ", ".join(sorted(required)))
    rows = [row for row in rows if "مشتقه" in row.get("market_board", "")]
    for row in rows:
        row["ins_code"] = str(row.get("ins_code", "")).strip()
        row["_symbol_norm"] = norm(row.get("symbol"))
    return rows


def option_records(payload, market):
    """Expand instrumentOptMarketWatch pair rows into individual PUT and CALL records."""
    if not isinstance(payload, dict):
        raise ValueError("Option API payload is not a JSON object")
    items = payload.get("instrumentOptMarketWatch")
    if not isinstance(items, list):
        raise ValueError("Missing array: instrumentOptMarketWatch")

    records = []
    for pair in items:
        if not isinstance(pair, dict):
            continue
        for side in ("P", "C"):
            code = str(pair.get("insCode_" + side) or "").strip()
            symbol = str(pair.get("lVal18AFC_" + side) or "").strip()
            if not code.isdigit() or code == "0":
                continue
            records.append({
                "source": "option",
                "market_api": market,
                "side": "PUT" if side == "P" else "CALL",
                "ins_code": code,
                "symbol": symbol,
                "symbol_norm": norm(symbol),
                "company_name": str(pair.get("lVal30_" + side) or "").strip(),
                "underlying_ins_code": str(pair.get("uaInsCode") or "").strip(),
                "underlying_symbol": str(pair.get("lval30_UA") or "").strip(),
                "begin_date": pair.get("beginDate"),
                "end_date": pair.get("endDate"),
                "remaining_days": pair.get("remainedDay"),
                "strike_price": pair.get("strikePrice"),
                "contract_size": pair.get("contractSize"),
            })
    return len(items), records


def future_records(payload):
    if not isinstance(payload, dict):
        raise ValueError("Future API payload is not a JSON object")
    items = payload.get("instrumentFtMarketWatch")
    if not isinstance(items, list):
        raise ValueError("Missing array: instrumentFtMarketWatch")
    records = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("insCode") or "").strip()
        symbol = str(item.get("lVal18AFC") or "").strip()
        if not code.isdigit() or code == "0":
            continue
        records.append({
            "source": "future",
            "market_api": 0,
            "side": "FUTURE",
            "ins_code": code,
            "symbol": symbol,
            "symbol_norm": norm(symbol),
            "company_name": str(item.get("lVal30") or "").strip(),
            "underlying_ins_code": str(item.get("uaInsCode") or "").strip(),
            "underlying_symbol": str(item.get("lVal30_UA") or "").strip(),
            "begin_date": item.get("beginDate"),
            "end_date": item.get("endDate"),
            "remaining_days": item.get("remainedDay"),
            "strike_price": None,
            "contract_size": item.get("contractSize"),
        })
    return len(items), records


def compact_record(record):
    return {
        key: record.get(key)
        for key in (
            "source", "market_api", "side", "ins_code", "symbol", "company_name",
            "underlying_ins_code", "underlying_symbol", "begin_date", "end_date",
            "remaining_days", "strike_price", "contract_size",
        )
    }


def compare(old_rows, records):
    by_code = defaultdict(list)
    by_symbol = defaultdict(list)
    for record in records:
        by_code[record["ins_code"]].append(record)
        if record["symbol_norm"]:
            by_symbol[record["symbol_norm"]].append(record)

    results = []
    counts = Counter()
    for old in old_rows:
        exact = by_code.get(old["ins_code"], [])
        same_symbol = by_symbol.get(old["_symbol_norm"], [])
        if exact:
            status = "EXACT_INS_CODE"
            candidates = exact
        elif same_symbol:
            status = "SAME_SYMBOL_DIFFERENT_INS_CODE"
            candidates = same_symbol
        else:
            status = "NO_MATCH"
            candidates = []
        counts[status] += 1
        results.append({
            "old_symbol": old.get("symbol", ""),
            "old_ins_code": old["ins_code"],
            "old_company_name": old.get("company_name", ""),
            "old_market_board": old.get("market_board", ""),
            "status": status,
            "candidate_count": len(candidates),
            "candidates": [compact_record(item) for item in candidates[:8]],
        })
    return counts, results


def fetch_options(market, timeout):
    status, content_type, payload = get_json(OPTION_URL.format(market), timeout)
    pair_count, records = option_records(payload, market)
    return {
        "market": market,
        "http_status": status,
        "content_type": content_type,
        "pair_rows": pair_count,
        "expanded_instruments": len(records),
    }, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument("--delay", type=float, default=0.2)
    args = parser.parse_args()

    old_rows = load_derivatives(args.report)
    option_meta = []
    option_all = []
    errors = []

    for market in (0, 1, 2):
        try:
            meta, records = fetch_options(market, args.timeout)
            option_meta.append(meta)
            option_all.extend(records)
        except Exception as exc:
            errors.append({
                "endpoint": "GetInstrumentOptionMarketWatch/" + str(market),
                "error": type(exc).__name__ + ": " + str(exc),
            })
        if market != 2:
            time.sleep(args.delay)

    future_meta = {}
    future_all = []
    try:
        status, content_type, payload = get_json(FUTURE_URL, args.timeout)
        raw_count, future_all = future_records(payload)
        future_meta = {
            "http_status": status,
            "content_type": content_type,
            "raw_rows": raw_count,
            "extracted_instruments": len(future_all),
        }
    except Exception as exc:
        errors.append({
            "endpoint": "GetInstrumentFutureMarketWatch/0",
            "error": type(exc).__name__ + ": " + str(exc),
        })

    # Market 0 is the all-market option feed. Prefer it for the overall count;
    # add the futures feed, and deduplicate repeated InsCodes across endpoints.
    combined_by_code = {}
    for record in option_all:
        if record["market_api"] == 0:
            combined_by_code.setdefault(record["ins_code"], record)
    for record in future_all:
        combined_by_code.setdefault(record["ins_code"], record)
    combined = list(combined_by_code.values())

    overall_counts, overall_results = compare(old_rows, combined)
    option_counts, _ = compare(old_rows, [
        record for record in combined if record["source"] == "option"
    ])
    future_counts, _ = compare(old_rows, future_all)

    report = {
        "diagnostic": "tsetmc_missing_473_v3",
        "read_only": True,
        "database_access": False,
        "file_writes": False,
        "matching_rule": (
            "Exact InsCode first; otherwise normalized exact symbol. "
            "Arabic Yeh/Kaf variants and zero-width characters are normalized."
        ),
        "legacy_derivative_rows": len(old_rows),
        "option_endpoints": option_meta,
        "future_endpoint": future_meta,
        "option_comparison": dict(option_counts),
        "future_comparison": dict(future_counts),
        "combined_unique_instrument_count": len(combined),
        "combined_comparison": dict(overall_counts),
        "results_by_legacy_symbol": overall_results,
        "endpoint_errors": errors,
        "notes": [
            "Option API rows are call/put pairs; each pair is expanded into two instruments.",
            "Market 0 is used as the all-market option feed in the combined comparison.",
            "beginDate/endDate/remainedDay are reported as returned by TSETMC; this script does not infer active/expired status.",
            "A missing match means absent from the queried current market-watch payloads, not proof of permanent delisting.",
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
