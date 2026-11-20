"""Read-only diagnostic for TSETMC suspended symbols (Kermasha / BPooya).

GET requests only. Does not import the application, access SQLite/SQL Server,
write files, or change synchronization behavior.

Run from repository root:
    python diagnostics/diagnose_tsetmc_suspended_symbols.py

The report is printed to stdout. It compares:
- InstrumentSearch results for Persian/Arabic character variants
- InstrumentInfo and InstrumentState endpoints
- InstrumentStateTop recent state changes
- New GetMarketWatch and legacy MarketWatchInit membership
- Legacy InsCodes from the historical missing-symbol report
"""
from __future__ import annotations

import json
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

TARGETS = {
    "KERMASHA": {
        "label": "کرماشا",
        "legacy_ins_code": "38437201078089290",
        "search_terms": ["کرماشا", "كرماشا"],
    },
    "BPOOYA": {
        "label": "بپویا",
        "legacy_ins_code": "48517714910707769",
        "search_terms": ["بپویا", "بپويا"],
    },
}

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


def request_bytes(url: str, timeout: int = 35) -> tuple[int | None, dict, bytes, str | None]:
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
    if body[:2] == bytes((31, 139)):
        import gzip
        return gzip.decompress(body)
    return body


def get_json(url: str, timeout: int = 35) -> dict[str, Any]:
    status, headers, body, error = request_bytes(url, timeout)
    result: dict[str, Any] = {
        "url": url,
        "http_status": status,
        "content_type": headers.get("Content-Type") or headers.get("content-type"),
    }
    if error:
        result["error"] = error
    try:
        decoded = decode_body(body).decode("utf-8-sig", errors="replace")
        result["data"] = json.loads(decoded)
    except Exception as exc:
        result["parse_error"] = f"{type(exc).__name__}: {exc}"
        result["body_preview"] = decode_body(body)[:1500].decode(
            "utf-8", errors="replace"
        )
    return result


def walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def normalize_text(value: Any) -> str:
    text = str(value or "").strip()
    return (
        text.replace("ك", "ک")
        .replace("ي", "ی")
        .replace("ى", "ی")
        .replace("\u200c", "")
        .replace(" ", "")
    )


def search_rows(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        rows = payload.get("instrumentSearch")
        if rows is None:
            rows = payload.get("InstrumentSearch")
        if rows is None:
            rows = payload
    else:
        rows = payload
    return [row for row in walk_dicts(rows) if any(
        key in row for key in ("insCode", "lVal18AFC", "lVal30")
    )]


def market_watch_codes(payload: Any) -> set[str]:
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


def legacy_market_watch_codes() -> dict[str, Any]:
    status, headers, body, error = request_bytes(LEGACY_MARKET_WATCH_URL, timeout=45)
    result = {
        "url": LEGACY_MARKET_WATCH_URL,
        "http_status": status,
        "content_type": headers.get("Content-Type") or headers.get("content-type"),
    }
    if error:
        result["error"] = error
        return result
    raw = decode_body(body).decode("utf-8-sig", errors="replace")
    sections = raw.split("@")
    if len(sections) < 3 or len(sections[2]) < 1000:
        result["parse_error"] = "MarketWatchInit response has an unexpected structure."
        result["body_preview"] = raw[:1200]
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
    result["codes"] = codes
    return result


def compact_search_match(row: dict) -> dict:
    return {
        "insCode": row.get("insCode"),
        "insCode2": row.get("insCode2"),
        "insCode3": row.get("insCode3"),
        "insCode4": row.get("insCode4"),
        "symbol": row.get("lVal18AFC"),
        "company_name": row.get("lVal30"),
        "flow": row.get("flow"),
        "flowTitle": row.get("flowTitle"),
        "cIsin": row.get("cIsin"),
        "lastDate": row.get("lastDate"),
        "sourceID": row.get("sourceID"),
        "exact_symbol_match": normalize_text(row.get("lVal18AFC"))
        == normalize_text(row.get("_requested_symbol")),
    }


def main() -> None:
    run_at = datetime.now().astimezone()
    date_code = run_at.strftime("%Y%m%d")
    report: dict[str, Any] = {
        "diagnostic": "tsetmc_suspended_symbols_read_only",
        "read_only": True,
        "http_method": "GET",
        "database_access": False,
        "file_writes": False,
        "run_at_local": run_at.isoformat(timespec="seconds"),
        "state_query_date": date_code,
        "state_date_note": (
            "The machine's current Gregorian date is used. If it is not a TSETMC "
            "trading date, the dated state endpoint may legitimately return no rows."
        ),
        "targets": {},
    }

    new_watch_result = get_json(MARKET_WATCH_URL, timeout=60)
    new_watch_data = new_watch_result.get("data")
    new_watch_codes = market_watch_codes(new_watch_data) if new_watch_data is not None else set()
    report["market_watch_new"] = {
        "http_status": new_watch_result.get("http_status"),
        "error": new_watch_result.get("error"),
        "parse_error": new_watch_result.get("parse_error"),
        "instrument_count": len(new_watch_codes),
    }

    legacy_watch = legacy_market_watch_codes()
    legacy_codes = legacy_watch.pop("codes", set())
    report["market_watch_legacy"] = legacy_watch

    state_top_result = get_json(
        BASE + "/MarketData/GetInstrumentStateTop/500", timeout=45
    )
    state_top_data = state_top_result.get("data")
    state_top_rows = [
        row for row in walk_dicts(state_top_data)
        if any(key in row for key in ("insCode", "InsCode", "cEtaval", "cEtavalTitle"))
    ] if state_top_data is not None else []
    report["instrument_state_top"] = {
        "http_status": state_top_result.get("http_status"),
        "error": state_top_result.get("error"),
        "parse_error": state_top_result.get("parse_error"),
        "row_count": len(state_top_rows),
    }

    for target_key, target in TARGETS.items():
        target_result: dict[str, Any] = {
            "label": target["label"],
            "legacy_ins_code_from_report": target["legacy_ins_code"],
            "searches": [],
        }
        candidate_codes = {target["legacy_ins_code"]}
        seen_search_rows = set()

        for term in target["search_terms"]:
            url = BASE + "/Instrument/GetInstrumentSearch/" + urllib.parse.quote(term, safe="")
            response = get_json(url, timeout=35)
            rows = search_rows(response.get("data")) if response.get("data") is not None else []
            matches = []
            for row in rows:
                code = str(row.get("insCode") or "").strip()
                symbol = str(row.get("lVal18AFC") or "").strip()
                if code.isdigit() and code != "0":
                    candidate_codes.add(code)
                key = (code, symbol, str(row.get("lVal30") or ""))
                if key in seen_search_rows:
                    continue
                seen_search_rows.add(key)
                copied = dict(row)
                copied["_requested_symbol"] = term
                matches.append(compact_search_match(copied))
            target_result["searches"].append({
                "query": term,
                "http_status": response.get("http_status"),
                "error": response.get("error"),
                "parse_error": response.get("parse_error"),
                "matches": matches,
            })

        target_result["instrument_checks"] = []
        for code in sorted(candidate_codes):
            info = get_json(BASE + "/Instrument/GetInstrumentInfo/" + code)
            dated_state = get_json(
                BASE + "/MarketData/GetInstrumentState/" + code + "/" + date_code
            )
            all_states = get_json(
                BASE + "/MarketData/GetInstrumentStateAll/" + code
            )
            top_matches = [
                row for row in state_top_rows
                if str(row.get("insCode") or row.get("InsCode") or "").strip() == code
            ]
            target_result["instrument_checks"].append({
                "ins_code": code,
                "source": (
                    "legacy_report" if code == target["legacy_ins_code"]
                    else "InstrumentSearch"
                ),
                "in_new_market_watch": code in new_watch_codes,
                "in_legacy_market_watch": code in legacy_codes,
                "instrument_info": info,
                "instrument_state_for_machine_date": dated_state,
                "instrument_state_all": all_states,
                "matching_instrument_state_top_rows": top_matches,
            })

        report["targets"][target_key] = target_result

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
