"""Synchronize the local symbol master from TSETMC public endpoints.

Only the app's SQLite reference table is updated by the caller. This module
never writes to the fundamental SQL Server database.
"""
import json
import urllib.request
from urllib.parse import quote

BASE = "https://cdn.tsetmc.com/api"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _request_bytes(url, timeout=45):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Referer": "https://www.tsetmc.com/"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def _request_json(url, timeout=45):
    raw = _request_bytes(url, timeout).decode("utf-8-sig", errors="replace")
    return json.loads(raw)


def _first(obj, *keys):
    for key in keys:
        value = obj.get(key)
        if value not in (None, ""):
            return value
    return ""


def _walk_dicts(value):
    """Yield nested dictionaries, including instrument sub-objects."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _board(item):
    title = _first(item, "flowTitle", "flowName", "marketTitle", "marketName", "cgrValCotTitle")
    if title:
        return str(title).strip()
    flow = str(_first(item, "flow", "flowCode", "market", "marketCode"))
    return {"1": "بورس", "2": "فرابورس", "3": "بورس کالا", "4": "بازار پایه فرابورس", "5": "بورس انرژی"}.get(flow, "نامشخص")


def _normalize_api(payload):
    # The documented response is normally {"marketwatch": [...]}; tolerate
    # alternate wrappers and nested instrument objects without assuming one schema.
    rows = payload.get("marketwatch", payload) if isinstance(payload, dict) else payload
    normalized, seen = [], set()
    for row in _walk_dicts(rows):
        # Some TSETMC responses put InsCode on the outer market-watch record
        # and names inside its nested instrument object. Merge both layers.
        inst = row.get("instrument") if isinstance(row.get("instrument"), dict) else {}
        merged = dict(row)
        merged.update(inst)
        symbol = str(_first(merged, "lVal18AFC", "symbol", "shortName", "Symbol")).strip()
        name = str(_first(merged, "lVal30", "companyName", "longName", "CompanyName")).strip()
        code = str(_first(merged, "insCode", "instrumentCode", "InsCode", "insCode2")).strip()
        isin = str(_first(merged, "cIsin", "isin", "ISIN")).strip()
        if not symbol or not code or code in seen:
            continue
        # InsCode must be numeric and is typically a long identifier.
        if not code.isdigit():
            continue
        seen.add(code)
        normalized.append({"ins_code": code, "symbol": symbol, "company_name": name, "isin": isin,
                           "market_board": _board(inst), "flow": str(_first(inst, "flow", "flowCode")),
                           "raw_json": json.dumps(row, ensure_ascii=False)})
    return normalized


def _fetch_legacy_market_watch():
    """Fallback to the legacy TSETMC bulk text endpoint.

    MarketWatchInit has @-separated sections; section 3 contains semicolon-
    separated instrument rows with a stable legacy column order.
    """
    urls = [
        "https://old.tsetmc.com/tsev2/data/MarketWatchInit.aspx?h=0&r=0",
        "http://www.tsetmc.com/tsev2/data/MarketWatchInit.aspx?h=0&r=0",
    ]
    last_error = None
    raw = None
    for url in urls:
        try:
            raw = _request_bytes(url, timeout=60).decode("utf-8-sig", errors="replace")
            if "@" in raw and len(raw) > 1000:
                break
        except Exception as exc:
            last_error = exc
            raw = None
    if raw is None:
        raise RuntimeError(f"legacy endpoint unavailable: {type(last_error).__name__ if last_error else 'empty response'}")

    sections = raw.split("@")
    # Section index 2 contains price/instrument rows in the documented legacy format.
    candidates = sections[2].split(";") if len(sections) > 2 else []
    normalized, seen = [], set()
    for line in candidates:
        cols = line.strip().split(",")
        if len(cols) < 18:
            continue
        code, isin, symbol, name = (cols[0].strip(), cols[1].strip(), cols[2].strip(), cols[3].strip())
        flow = cols[17].strip()
        if not code.isdigit() or not symbol or code in seen:
            continue
        seen.add(code)
        board = {"1": "بورس", "2": "فرابورس", "3": "بورس کالا", "4": "بازار پایه فرابورس"}.get(flow, "نامشخص")
        normalized.append({"ins_code": code, "symbol": symbol, "company_name": name, "isin": isin,
                           "market_board": board, "flow": flow,
                           "raw_json": json.dumps({"source": "legacy_market_watch", "columns": cols}, ensure_ascii=False)})
    if len(normalized) < 100:
        raise RuntimeError(f"legacy market-watch returned only {len(normalized)} recognizable rows")
    return normalized



def _header_key(value):
    """Normalize Persian Excel headers for tolerant matching."""
    text = str(value or "").strip().replace("\u200c", " ").replace("\u200f", "").replace("\u200e", "")
    text = text.replace("ي", "ی").replace("ك", "ک")
    return " ".join(text.split()).replace(" ", "").replace("ـ", "")


# Header names supplied from the actual MarketWatchPlus workbook.
_EXCEL_FIELDS = {
    "نماد": "symbol",
    "نام": "company_name",
    "تعداد": "trade_count",
    "حجم": "trade_volume",
    "ارزش": "trade_value",
    "دیروز": "yesterday_price",
    "اولین": "first_price",
    "آخرینمعامله-مقدار": "last_trade_price",
    "آخرینمعامله-تغییر": "last_trade_change",
    "آخرینمعامله-درصد": "last_trade_percent",
    "قیمتپایانی-مقدار": "closing_price",
    "قیمتپایانی-تغییر": "closing_change",
    "قیمتپایانی-درصد": "closing_percent",
    "کمترین": "low_price",
    "بیشترین": "high_price",
    "EPS": "eps",
    "P/E": "pe_ratio",
    "خرید-تعداد": "buy_count",
    "خرید-حجم": "buy_volume",
    "خرید-قیمت": "buy_price",
    "فروش-قیمت": "sell_price",
    "فروش-حجم": "sell_volume",
    "فروش-تعداد": "sell_count",
}


def _cell_text(value):
    if value is None:
        return ""
    # Keep source values as text; do not turn blanks into zero or round numbers.
    return str(value).strip()


def _parse_marketwatch_excel(content, source_label):
    """Parse MarketWatchPlus XLS/XLSX, locating its header row dynamically."""
    from io import BytesIO
    from openpyxl import load_workbook

    if not content or len(content) < 1000:
        raise RuntimeError(f"Excel content too small ({len(content) if content else 0} bytes)")
    if content.startswith(b"PK"):
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
        close_workbook = workbook.close
        sheets = [workbook[name] for name in workbook.sheetnames]
        def row_iter(sheet):
            return sheet.iter_rows(values_only=True)
    elif content[:2] == b"\xd0\xcf":
        import xlrd
        workbook = xlrd.open_workbook(file_contents=content, on_demand=True)
        close_workbook = workbook.release_resources
        sheets = [workbook.sheet_by_index(i) for i in range(workbook.nsheets)]
        def row_iter(sheet):
            return (sheet.row_values(i) for i in range(sheet.nrows))
    else:
        raise RuntimeError(f"response is not XLS/XLSX ({len(content)} bytes; signature={content[:16]!r})")

    normalized, seen = [], set()
    matched_headers = False
    try:
        for sheet in sheets:
            header_map = None
            header_cells = []
            for row in row_iter(sheet):
                cells = [_cell_text(v) for v in row]
                keys = [_header_key(v) for v in cells]
                # MarketWatchPlus has a blank first row and a report-title row;
                # the actual column names are normally on row 3.
                if "نماد" in keys and "نام" in keys and ("حجم" in keys or "ارزش" in keys):
                    header_map = {field: i for i, key in enumerate(keys) if (field := _EXCEL_FIELDS.get(key))}
                    matched_headers = True
                    header_cells = cells[:]
                    continue
                if not header_map or "symbol" not in header_map:
                    continue
                symbol_idx = header_map["symbol"]
                name_idx = header_map.get("company_name")
                symbol = cells[symbol_idx] if symbol_idx < len(cells) else ""
                name = cells[name_idx] if name_idx is not None and name_idx < len(cells) else ""
                if not symbol or _header_key(symbol) == "نماد":
                    continue
                # Exclude repeated title/header rows and malformed records.
                if symbol in {"دیده بان بازار", "نماد"} or len(symbol) > 80:
                    continue
                local_code = "MW_SYMBOL:" + symbol
                if local_code in seen:
                    continue
                seen.add(local_code)
                daily = {}
                for field in _EXCEL_FIELDS.values():
                    idx = header_map.get(field)
                    if idx is not None and idx < len(cells):
                        daily[field] = cells[idx]
                normalized.append({
                    "ins_code": local_code,
                    "instrument_code_is_placeholder": 1,
                    "symbol": symbol,
                    "company_name": name,
                    "isin": "",
                    "market_board": "نامشخص",
                    "flow": "",
                    "daily_data": daily,
                    "raw_json": json.dumps({"source": source_label, "headers": header_cells, "daily_data": daily}, ensure_ascii=False),
                })
    finally:
        close_workbook()

    if len(normalized) < 100:
        raise RuntimeError(f"only {len(normalized)} symbols parsed (headers_matched={matched_headers})")
    return normalized


def _fetch_excel_market_watch():
    """Fetch MarketWatchPlus; if Python's request gets a non-Excel response,
    fall back to the latest browser-downloaded MarketWatchPlus XLSX in Downloads.

    The export starts with a blank row and a title row; its header is usually
    row 3 and symbol records start on row 4. No SQL Server data is written here.
    """
    from pathlib import Path
    urls = [
        "https://old.tsetmc.com/tsev2/excel/MarketWatchPlus.aspx?d=0",
        "https://old.tsetmc.com/tsev2/excel/MarketWatchPlus.aspx?d=0&format=0",
    ]
    errors = []
    for url in urls:
        try:
            content = _request_bytes(url, timeout=90)
            return _parse_marketwatch_excel(content, url)
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")

    # Browser downloads work in cases where direct urllib requests receive a
    # proxy/error page or time out. Use only the clearly named export files.
    downloads = Path.home() / "Downloads"
    candidates = sorted(
        downloads.glob("MarketWatchPlus*.xlsx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ) if downloads.is_dir() else []
    for path in candidates[:3]:
        try:
            rows = _parse_marketwatch_excel(path.read_bytes(), f"local file: {path}")
            return rows
        except Exception as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
    raise RuntimeError("؛ ".join(errors) or "Excel market-watch unavailable")

def fetch_all_symbols():
    """Fetch all available instruments, using legacy bulk data if API schema fails."""
    errors = []
    url = (BASE + "/ClosingPrice/GetMarketWatch?market=0&industrialGroup="
           "&paperTypes%5B0%5D=1&paperTypes%5B1%5D=2&paperTypes%5B2%5D=3"
           "&paperTypes%5B3%5D=4&paperTypes%5B4%5D=5&paperTypes%5B5%5D=6"
           "&paperTypes%5B6%5D=7&paperTypes%5B7%5D=8&paperTypes%5B8%5D=9"
           "&showTraded=false&withBestLimits=false&hEven=0&RefID=0")
    try:
        payload = _request_json(url, timeout=60)
        rows = _normalize_api(payload)
        if len(rows) >= 100:
            return rows
        errors.append(f"new API returned {len(rows)} recognizable rows")
    except Exception as exc:
        errors.append(f"new API: {type(exc).__name__}: {exc}")

    try:
        rows = _fetch_legacy_market_watch()
        if len(rows) >= 100:
            return rows
        errors.append(f"legacy API returned {len(rows)} recognizable rows")
    except Exception as exc:
        errors.append(f"legacy API: {type(exc).__name__}: {exc}")

    try:
        rows = _fetch_excel_market_watch()
        if len(rows) >= 100:
            return rows
        errors.append(f"Excel market-watch returned only {len(rows)} recognizable rows")
    except Exception as exc:
        errors.append(f"Excel market-watch: {type(exc).__name__}: {exc}")

    raise RuntimeError("دریافت فهرست نمادها از TSETMC موفق نبود: " + "؛ ".join(errors))


def _norm_symbol(value):
    return "".join(str(value or "").strip().replace("ي", "ی").replace("ك", "ک").replace("ـ", "").split()).replace("‌", "")

def _search_rows(payload):
    """Extract official instrument rows from InstrumentSearch responses."""
    root = payload
    if isinstance(payload, dict):
        for key in ("instrumentSearch", "instrumentsearch", "instrumentSearchResult", "instrumentInfo"):
            if key in payload:
                root = payload[key]
                break
    out, seen = [], set()
    for row in _walk_dicts(root):
        nested = row.get("instrument") if isinstance(row.get("instrument"), dict) else {}
        merged = dict(row); merged.update(nested)
        code = str(_first(merged, "insCode", "instrumentCode", "InsCode", "insCode2")).strip()
        symbol = str(_first(merged, "lVal18AFC", "symbol", "shortName", "Symbol")).strip()
        name = str(_first(merged, "lVal30", "companyName", "longName", "CompanyName")).strip()
        isin = str(_first(merged, "cIsin", "isin", "ISIN")).strip()
        if code.isdigit() and symbol and code not in seen:
            seen.add(code)
            out.append({"ins_code": code, "symbol": symbol, "company_name": name, "isin": isin,
                        "market_board": _board(merged), "flow": str(_first(merged, "flow", "flowCode")),
                        "raw_json": json.dumps(row, ensure_ascii=False), "instrument_code_is_placeholder": 0})
    return out

def resolve_official_symbols_batch(batch_size=500, max_workers=16):
    """Resolve a resumable batch concurrently, then write results sequentially.

    Network lookups run in a bounded thread pool for speed. SQLite writes remain
    sequential to avoid lock contention and duplicate-code races.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .authdb import connect, upsert_market_symbols

    batch_size = max(1, min(int(batch_size), 800))
    max_workers = max(1, min(int(max_workers), 20))
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS market_symbol_lookup_attempts (symbol_id INTEGER PRIMARY KEY, attempted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, status TEXT NOT NULL DEFAULT 'NO_MATCH')")
        # Revisit unresolved placeholders as well as never-attempted rows.
        # Oldest attempt first makes repeated syncs rotate through the full set
        # instead of getting stuck after every row has one failed attempt.
        pending = db.execute("""SELECT s.symbol_id,s.symbol,s.company_name,s.isin
            FROM market_symbols s
            LEFT JOIN market_symbol_lookup_attempts a ON a.symbol_id=s.symbol_id
            WHERE s.instrument_code_is_placeholder=1
            ORDER BY CASE WHEN a.symbol_id IS NULL THEN 0 ELSE 1 END,
                     COALESCE(a.attempted_at, ''), s.symbol_id
            LIMIT ?""", (batch_size,)).fetchall()

    def lookup(local):
        try:
            url = BASE + "/Instrument/GetInstrumentSearch/" + quote(str(local["symbol"]))
            payload = _request_json(url, timeout=15)
            candidates = _search_rows(payload)
            exact = [r for r in candidates if _norm_symbol(r["symbol"]) == _norm_symbol(local["symbol"])]
            local_isin = str(local["isin"] or "").strip()
            if local_isin:
                same_isin = [r for r in exact if r.get("isin") == local_isin]
                if same_isin:
                    exact = same_isin
            unique_codes = {r["ins_code"] for r in exact}
            if len(unique_codes) == 1:
                chosen = next(r for r in exact if r["ins_code"] in unique_codes)
                chosen["company_name"] = chosen.get("company_name") or local["company_name"] or ""
                chosen["instrument_code_is_placeholder"] = 0
                return int(local["symbol_id"]), "MATCHED", chosen, ""
            if len(unique_codes) > 1:
                return int(local["symbol_id"]), "AMBIGUOUS", None, ""
            return int(local["symbol_id"]), "NO_MATCH", None, ""
        except Exception as exc:
            return int(local["symbol_id"]), "ERROR", None, f"{local['symbol']}: {type(exc).__name__}: {str(exc)[:100]}"

    results = []
    if pending:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(lookup, local) for local in pending]
            for future in as_completed(futures):
                results.append(future.result())
    results.sort(key=lambda x: x[0])

    matched = 0
    errors = []
    attempt_rows = []
    for sid, status, chosen, error in results:
        if chosen is not None:
            # Keep writes sequential to avoid SQLite lock contention.
            upsert_market_symbols([chosen])
            matched += 1
        if status != "ERROR":
            attempt_rows.append((sid, status))
        elif len(errors) < 5:
            errors.append(error)
    with connect() as db:
        db.executemany("INSERT OR REPLACE INTO market_symbol_lookup_attempts(symbol_id,attempted_at,status) VALUES(?,CURRENT_TIMESTAMP,?)", [(sid, status) for sid, status in attempt_rows])
        remaining = db.execute("SELECT COUNT(*) FROM market_symbols WHERE instrument_code_is_placeholder=1").fetchone()[0]
        unattempted = db.execute("""SELECT COUNT(*) FROM market_symbols s LEFT JOIN market_symbol_lookup_attempts a ON a.symbol_id=s.symbol_id
            WHERE s.instrument_code_is_placeholder=1 AND a.symbol_id IS NULL""").fetchone()[0]
    return {"attempted": len(pending), "matched": matched, "remaining_placeholders": int(remaining),
            "unattempted_remaining": int(unattempted), "errors": errors, "batch_size": batch_size, "workers": max_workers}

