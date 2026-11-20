"""Read-only schema diagnostic for TSETMC's missing 473 instruments.

HTTP GET only. No application imports, database access, or file writes.
The report summarizes JSON structure instead of dumping large API payloads.

Run from repository root:
    python diagnostics/diagnose_tsetmc_missing_473_v2.py

Save output locally if desired:
    python diagnostics/diagnose_tsetmc_missing_473_v2.py > diagnostics/diagnose_tsetmc_missing_473_v2_output.txt
"""
import argparse
import csv
import gzip
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

BASE = "https://cdn.tsetmc.com/api"
OLD_REPORT = "diagnostics/data/tsetmc_missing_symbols_report.csv"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
OPTION_URL = BASE + "/Instrument/GetInstrumentOptionMarketWatch/{}"
FUTURE_URL = BASE + "/Instrument/GetInstrumentFutureMarketWatch/0"
CODE_KEYS = (
    "insCode", "InsCode", "ins_code", "instrumentCode",
    "callInsCode", "putInsCode", "call_ins_code", "put_ins_code",
)
SYMBOL_KEYS = (
    "lVal18AFC", "lva", "symbol", "shortName", "Symbol",
    "instrumentName", "ticker", "callSymbol", "putSymbol",
)


def normalize(value):
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.replace("\u064a", "\u06cc").replace("\u0643", "\u06a9")
    value = value.replace("\u200c", "").replace("\u200f", "").replace("\u200e", "")
    return re.sub(r"\s+", "", value).strip()


def get_bytes(url, timeout):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.tsetmc.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
        status = getattr(response, "status", 200)
        content_type = response.headers.get("Content-Type", "")
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return status, content_type, body


def get_json(url, timeout):
    status, content_type, body = get_bytes(url, timeout)
    payload = json.loads(body.decode("utf-8-sig", errors="replace"))
    return status, content_type, len(body), payload


def load_report(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"ins_code", "symbol", "market_board"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("CSV must contain: " + ", ".join(sorted(required)))
    return rows


def scalar(value):
    return value is None or isinstance(value, (str, int, float, bool))


def json_shape(payload, max_samples=5):
    """Describe nested JSON shape and sample scalar fields without dumping payload."""
    key_counts = Counter()
    arrays = []
    samples = []
    visited = 0

    def walk(value, path="$", depth=0):
        nonlocal visited
        if depth > 10:
            return
        if isinstance(value, dict):
            visited += 1
            for key, child in value.items():
                key_counts[str(key)] += 1
                if len(samples) < max_samples and scalar(child):
                    text = str(child) if child is not None else None
                    if text is not None and len(text) > 100:
                        text = text[:100] + "…"
                    samples.append({"path": path + "." + str(key), "value": text})
                walk(child, path + "." + str(key), depth + 1)
        elif isinstance(value, list):
            arrays.append({
                "path": path,
                "length": len(value),
                "first_item_type": type(value[0]).__name__ if value else None,
                "first_item_keys": (
                    sorted(str(k) for k in value[0].keys())
                    if value and isinstance(value[0], dict) else []
                ),
            })
            for index, child in enumerate(value[:3]):
                walk(child, f"{path}[{index}]", depth + 1)

    walk(payload)
    return {
        "top_level_type": type(payload).__name__,
        "top_level_keys": sorted(str(k) for k in payload.keys())
        if isinstance(payload, dict) else [],
        "dict_nodes_visited": visited,
        "key_frequency": dict(key_counts.most_common(80)),
        "arrays": arrays[:40],
        "scalar_samples": samples,
    }


def first(item, keys):
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return ""


def extract_records(payload):
    """Best-effort extraction; shape report remains authoritative for unknown schemas."""
    found = {}
    def walk(value):
        if isinstance(value, dict):
            code = str(first(value, CODE_KEYS)).strip()
            symbol = str(first(value, SYMBOL_KEYS)).strip()
            if code.isdigit() and code != "0":
                key = (code, normalize(symbol))
                found[key] = {
                    "ins_code": code,
                    "symbol": symbol,
                    "company_name": str(first(
                        value, ("lVal30", "lvc", "companyName", "longName")
                    )).strip(),
                }
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(payload)
    return list(found.values())


def summarize_against(rows, records):
    codes = {item["ins_code"] for item in records}
    symbols = defaultdict(list)
    for item in records:
        key = normalize(item.get("symbol"))
        if key:
            symbols[key].append(item)

    exact, changed, absent = [], [], []
    for old in rows:
        code = str(old.get("ins_code", "")).strip()
        symbol = normalize(old.get("symbol"))
        if code in codes:
            exact.append(old.get("symbol", ""))
        elif symbol in symbols:
            changed.append({
                "old_symbol": old.get("symbol", ""),
                "old_ins_code": code,
                "matches": symbols[symbol][:5],
            })
        else:
            absent.append(old.get("symbol", ""))
    return {
        "extracted_records": len(records),
        "exact_ins_code_matches": len(exact),
        "same_symbol_different_ins_code_matches": len(changed),
        "no_match": len(absent),
        "changed_code_examples": changed[:20],
        "no_match_examples": absent[:20],
    }


def inspect_endpoint(name, url, comparison_rows, timeout):
    try:
        status, content_type, byte_count, payload = get_json(url, timeout)
        records = extract_records(payload)
        return {
            "source": name,
            "http_status": status,
            "content_type": content_type,
            "response_bytes": byte_count,
            "json_shape": json_shape(payload),
            "instrument_extraction": summarize_against(comparison_rows, records),
        }
    except Exception as exc:
        return {"source": name, "error": f"{type(exc).__name__}: {exc}"}


def inspect_searches(rows, timeout, delay):
    results = []
    for index, old in enumerate(rows):
        symbol = str(old.get("symbol", "")).strip()
        url = BASE + "/Instrument/GetInstrumentSearch/" + urllib.parse.quote(
            symbol, safe=""
        )
        item = {
            "requested_symbol": symbol,
            "old_ins_code": str(old.get("ins_code", "")).strip(),
        }
        try:
            status, content_type, byte_count, payload = get_json(url, timeout)
            records = extract_records(payload)
            exact = [
                rec for rec in records
                if normalize(rec.get("symbol")) == normalize(symbol)
            ]
            item.update({
                "http_status": status,
                "response_bytes": byte_count,
                "extracted_records": len(records),
                "exact_symbol_matches": exact[:10],
                "old_ins_code_found": any(
                    rec["ins_code"] == item["old_ins_code"] for rec in exact
                ),
                "json_shape": json_shape(payload, max_samples=3),
            })
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
        results.append(item)
        if index + 1 < len(rows):
            time.sleep(delay)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=OLD_REPORT)
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument(
        "--search-limit", type=int, default=0,
        help="Limit individual non-derivative searches; 0 means all",
    )
    args = parser.parse_args()

    missing = load_report(args.report)
    derivatives = [r for r in missing if "مشتقه" in r.get("market_board", "")]
    non_derivatives = [r for r in missing if r not in derivatives]
    search_rows = (
        non_derivatives[:args.search_limit] if args.search_limit else non_derivatives
    )

    report = {
        "diagnostic": "tsetmc_missing_473_v2",
        "read_only": True,
        "database_access": False,
        "file_writes": False,
        "missing_report_rows": len(missing),
        "derivative_rows": len(derivatives),
        "non_derivative_rows": len(non_derivatives),
        "option_endpoints": [
            inspect_endpoint(
                f"GetInstrumentOptionMarketWatch/{market}",
                OPTION_URL.format(market),
                derivatives,
                args.timeout,
            )
            for market in (0, 1, 2)
        ],
        "future_endpoint": inspect_endpoint(
            "GetInstrumentFutureMarketWatch/0",
            FUTURE_URL,
            derivatives,
            args.timeout,
        ),
        "non_derivative_searches": {
            "requested": len(search_rows),
            "results": inspect_searches(search_rows, args.timeout, args.delay),
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
