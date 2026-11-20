"""Synchronize the local symbol master exclusively from TSETMC APIs.

Codal workbooks are consumers of this registry; they must never create symbol
identities. This module only fetches official TSETMC InsCode records.
"""
import gzip
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request

BASE = "https://cdn.tsetmc.com/api"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# The last verified full-market snapshot contained more than 3,200 instruments.
# A first-time sync must therefore be substantially larger than a small,
# syntactically valid but truncated API response.
MIN_EXPECTED_MARKET_ROWS = 2500
MIN_PREVIOUS_SNAPSHOT_RATIO = 0.80


def _request_bytes(url, timeout=45):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                 "Referer": "https://www.tsetmc.com/"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def _decode_body(raw):
    """Decode an HTTP body, transparently handling GZIP-compressed responses."""
    if raw[:2] == bytes((31, 139)):
        raw = gzip.decompress(raw)
    return raw


def _request_json(url, timeout=45):
    raw = _decode_body(_request_bytes(url, timeout))
    return json.loads(raw.decode("utf-8-sig", errors="replace"))


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
    title = _first(item, "flowTitle", "flowName", "marketTitle", "marketName",
                   "cgrValCotTitle")
    if title:
        return str(title).strip()
    flow = str(_first(item, "flow", "flowCode", "market", "marketCode"))
    return {
        "1": "بورس",
        "2": "فرابورس",
        "3": "بورس کالا",
        "4": "بازار پایه فرابورس",
        "5": "بورس انرژی",
    }.get(flow, "نامشخص")


def _normalize_api(payload):
    """Normalize official instrument rows from TSETMC GetMarketWatch.

    insCode is the official instrument identifier. insCode2/3/4 are not
    substituted for it and are not treated as additional instruments.
    """
    if isinstance(payload, dict):
        rows = payload.get("marketwatch")
        if rows is None:
            rows = payload.get("marketWatch", payload)
    else:
        rows = payload

    normalized = {}
    for row in _walk_dicts(rows):
        instrument = row.get("instrument") if isinstance(row.get("instrument"), dict) else {}
        merged = dict(row)
        merged.update(instrument)

        code = str(_first(merged, "insCode", "instrumentCode", "InsCode")).strip()
        symbol = str(_first(
            merged, "lVal18AFC", "lva", "symbol", "shortName", "Symbol"
        )).strip()
        name = str(_first(
            merged, "lVal30", "lvc", "companyName", "longName", "CompanyName"
        )).strip()
        isin = str(_first(merged, "cIsin", "insID", "isin", "ISIN")).strip()
        if not code.isdigit() or code == "0" or not symbol:
            continue

        normalized[code] = {
            "ins_code": code,
            "symbol": symbol,
            "company_name": name,
            "isin": isin,
            "market_board": _board(merged),
            "flow": str(_first(merged, "flow", "flowCode")),
            "source": "TSETMC",
            "raw_json": json.dumps(row, ensure_ascii=False),
        }
    return list(normalized.values())


def _fetch_legacy_market_watch():
    """Fetch the legacy TSETMC bulk endpoint; no local or Excel fallback."""
    urls = [
        "https://old.tsetmc.com/tsev2/data/MarketWatchInit.aspx?h=0&r=0",
        "http://www.tsetmc.com/tsev2/data/MarketWatchInit.aspx?h=0&r=0",
    ]
    errors = []
    raw = None
    for url in urls:
        try:
            body = _decode_body(_request_bytes(url, timeout=60))
            candidate = body.decode("utf-8-sig", errors="replace")
            sections = candidate.split("@")
            if len(sections) >= 3 and len(sections[2]) > 1000:
                raw = candidate
                break
            errors.append(f"{url}: پاسخ خالی یا نامعتبر")
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")

    if raw is None:
        raise RuntimeError("دریافت API قدیمی TSETMC ناموفق بود: " + "؛ ".join(errors))

    sections = raw.split("@")
    if len(sections) < 3:
        raise RuntimeError("ساختار پاسخ API قدیمی TSETMC معتبر نیست.")
    candidates = sections[2].split(";")
    normalized = {}
    for line in candidates:
        cols = line.strip().split(",")
        if len(cols) < 18:
            continue
        code, isin, symbol, name = (
            cols[0].strip(), cols[1].strip(), cols[2].strip(), cols[3].strip()
        )
        flow = cols[17].strip()
        if not code.isdigit() or code == "0" or not symbol:
            continue
        normalized[code] = {
            "ins_code": code,
            "symbol": symbol,
            "company_name": name,
            "isin": isin,
            "market_board": {
                "1": "بورس", "2": "فرابورس", "3": "بورس کالا",
                "4": "بازار پایه فرابورس",
            }.get(flow, "نامشخص"),
            "flow": flow,
            "source": "TSETMC",
            "raw_json": json.dumps(
                {"source": "TSETMC MarketWatchInit", "columns": cols},
                ensure_ascii=False,
            ),
        }

    if not normalized:
        raise RuntimeError("API قدیمی TSETMC هیچ ابزار معاملاتی معتبری برنگرداند.")
    return list(normalized.values())



def _normalize_symbol_text(value):
    """Normalize Persian/Arabic spelling variants for exact symbol matching."""
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.replace("ي", "ی").replace("ى", "ی").replace("ك", "ک")
    value = value.replace("\u200c", "").replace("\u200f", "").replace("\u200e", "")
    return re.sub(r"\s+", "", value).strip()


def _fetch_missing_non_derivative_symbols_from_search(market_watch_codes):
    """Supplement MarketWatch with exact TSETMC search matches for known non-derivative symbols.

    The local registry supplies only the symbol names to search; identity and
    metadata are accepted exclusively from exact TSETMC InstrumentSearch rows.
    Suspended instruments are intentionally not filtered by trading status.
    Derivatives are deferred to the separate derivatives integration step.
    """
    try:
        from .authdb import connect
        with connect() as db:
            existing_rows = db.execute(
                "SELECT ins_code, symbol, market_board FROM market_symbols "
                "WHERE is_active=1 AND ins_code<>'' "
                "AND ins_code NOT GLOB '*[^0-9]*'"
            ).fetchall()
    except Exception:
        return []

    missing_symbols = {}
    for row in existing_rows:
        code = str(row["ins_code"] or "").strip()
        symbol = str(row["symbol"] or "").strip()
        board = str(row["market_board"] or "")
        if not code or code in market_watch_codes or not symbol:
            continue
        if "مشتقه" in board:
            continue
        normalized = _normalize_symbol_text(symbol)
        if normalized:
            missing_symbols.setdefault(normalized, symbol)

    found = {}
    for normalized, symbol in missing_symbols.items():
        url = BASE + "/Instrument/GetInstrumentSearch/" + urllib.parse.quote(symbol, safe="")
        try:
            payload = _request_json(url, timeout=30)
        except Exception:
            continue

        rows = payload
        if isinstance(payload, dict):
            rows = payload.get("instrumentSearch")
            if rows is None:
                rows = payload.get("InstrumentSearch")
            if rows is None:
                rows = payload

        for row in _walk_dicts(rows):
            instrument = row.get("instrument") if isinstance(row.get("instrument"), dict) else {}
            merged = dict(row)
            merged.update(instrument)

            code = str(_first(merged, "insCode", "InsCode", "instrumentCode")).strip()
            found_symbol = str(_first(
                merged, "lVal18AFC", "symbol", "shortName", "Symbol"
            )).strip()
            if not code.isdigit() or code == "0":
                continue
            if _normalize_symbol_text(found_symbol) != normalized:
                continue

            found[code] = {
                "ins_code": code,
                "symbol": found_symbol,
                "company_name": str(_first(
                    merged, "lVal30", "companyName", "longName", "CompanyName"
                )).strip(),
                "isin": str(_first(merged, "cIsin", "insID", "isin", "ISIN")).strip(),
                "market_board": _board(merged),
                "flow": str(_first(merged, "flow", "flowCode")),
                "source": "TSETMC",
                "raw_json": json.dumps(row, ensure_ascii=False),
            }

        time.sleep(0.10)

    return list(found.values())

def _previous_official_count():
    """Read the last local official-universe size for truncation detection."""
    try:
        from .authdb import connect
        with connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM market_symbols "
                "WHERE is_active=1 AND ins_code<>'' AND ins_code NOT GLOB '*[^0-9]*'"
            ).fetchone()
            return int(row[0] or 0)
    except Exception:
        return 0


def _validate_full_snapshot(rows, previous_count=0):
    if not rows:
        raise RuntimeError("فهرست نمادهای TSETMC خالی است.")
    codes = [str(row.get("ins_code") or "") for row in rows]
    if any(not code.isdigit() or code == "0" for code in codes):
        raise RuntimeError("پاسخ TSETMC شامل شناسه غیررسمی یا غیرعددی است.")
    if len(codes) != len(set(codes)):
        raise RuntimeError("پاسخ TSETMC پس از نرمال‌سازی هنوز شناسه تکراری دارد.")

    minimum = MIN_EXPECTED_MARKET_ROWS
    if previous_count:
        minimum = max(minimum, int(previous_count * MIN_PREVIOUS_SNAPSHOT_RATIO))
    if len(rows) < minimum:
        raise RuntimeError(
            f"پوشش فهرست TSETMC کافی نیست: {len(rows)} ابزار دریافت شد؛ "
            f"حداقل مورد انتظار برای پذیرش این نوبت {minimum} است. "
            "برای جلوگیری از همگام‌سازی ناقص، داده‌ها ثبت نمی‌شوند."
        )


def fetch_all_symbols():
    """Fetch the full instrument universe from TSETMC endpoints only.

    The new and legacy TSETMC endpoints are both queried. Their official
    InsCode sets are combined so a partial response from one endpoint does
    not silently hide instruments returned by the other. Excel files,
    Downloads folders and Codal reports are never used as fallbacks.
    """
    errors = []
    snapshots = []
    url = (
        BASE + "/ClosingPrice/GetMarketWatch?market=0&industrialGroup="
        "&paperTypes%5B0%5D=1&paperTypes%5B1%5D=2&paperTypes%5B2%5D=3"
        "&paperTypes%5B3%5D=4&paperTypes%5B4%5D=5&paperTypes%5B5%5D=6"
        "&paperTypes%5B6%5D=7&paperTypes%5B7%5D=8&paperTypes%5B8%5D=9"
        "&showTraded=false&withBestLimits=false&hEven=0&RefID=0"
    )

    try:
        rows = _normalize_api(_request_json(url, timeout=60))
        if rows:
            snapshots.append(("new_api", rows))
        else:
            errors.append("API جدید TSETMC هیچ رکورد قابل‌شناسایی برنگرداند.")
    except Exception as exc:
        errors.append(f"API جدید TSETMC: {type(exc).__name__}: {exc}")

    try:
        rows = _fetch_legacy_market_watch()
        if rows:
            snapshots.append(("legacy_api", rows))
    except Exception as exc:
        errors.append(f"API قدیمی TSETMC: {type(exc).__name__}: {exc}")

    if not snapshots:
        raise RuntimeError(
            "دریافت فهرست نمادها فقط از APIهای TSETMC ناموفق بود؛ "
            + "؛ ".join(errors)
        )

    # Prefer the new API's richer metadata; add any official InsCode that
    # appears only in the legacy TSETMC response.
    combined = {}
    for source, rows in snapshots:
        for row in rows:
            code = str(row["ins_code"])
            if code not in combined or source == "new_api":
                combined[code] = row

    market_watch_codes = set(combined)
    supplemental_rows = _fetch_missing_non_derivative_symbols_from_search(
        market_watch_codes
    )
    for row in supplemental_rows:
        combined.setdefault(str(row["ins_code"]), row)

    result = list(combined.values())
    _validate_full_snapshot(result, _previous_official_count())
    return sorted(result, key=lambda row: row["ins_code"])
