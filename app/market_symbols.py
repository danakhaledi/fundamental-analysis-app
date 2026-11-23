"""Synchronize the local symbol master exclusively from TSETMC APIs.

Codal workbooks are consumers of this registry; they must never create symbol
identities. This module only fetches official TSETMC InsCode records.
"""
import gzip
import concurrent.futures
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

def _fetch_bulk_ins_codes():
    """Return the official InsCode universe exposed by TSETMC bulk parameters."""
    payload = _request_json(BASE + "/MarketData/GetInstValueAllInstAllParam", timeout=180)
    rows = payload.get("instValueAllInstAllParam", []) if isinstance(payload, dict) else []
    codes = {str(row.get("insCode") or "").strip() for row in rows if isinstance(row, dict)}
    codes = {code for code in codes if code.isdigit() and code != "0"}
    if len(codes) < MIN_EXPECTED_MARKET_ROWS:
        raise RuntimeError(f"پاسخ انبوه TSETMC ناقص یا نامعتبر است: {len(codes)} شناسه یکتا")
    return codes


def _normalize_instrument_info(payload, expected_code):
    """Normalize a single GetInstrumentInfo response and verify its InsCode."""
    item = payload.get("instrumentInfo") if isinstance(payload, dict) else None
    if not isinstance(item, dict):
        raise ValueError("ساختار instrumentInfo در پاسخ TSETMC معتبر نیست")
    code = str(item.get("insCode") or "").strip()
    symbol = str(item.get("lVal18AFC") or "").strip()
    if code != str(expected_code) or not code.isdigit() or not symbol:
        raise ValueError(f"مشخصات هویتی با InsCode درخواستی تطابق ندارد: {expected_code}")
    flow = str(item.get("flow") if item.get("flow") is not None else "").strip()
    board = str(item.get("flowTitle") or "").strip() or _board(item)
    return {
        "ins_code": code,
        "symbol": symbol,
        "company_name": str(item.get("lVal30") or "").strip(),
        "isin": str(item.get("cIsin") or "").strip(),
        "market_board": board,
        "flow": flow,
        "source": "TSETMC",
        "raw_json": json.dumps(payload, ensure_ascii=False),
    }


def _fetch_one_instrument_info(code):
    """Fetch one instrument identity with bounded retries."""
    url = BASE + "/Instrument/GetInstrumentInfo/" + urllib.parse.quote(str(code), safe="")
    last_error = None
    for attempt in range(3):
        try:
            return _normalize_instrument_info(_request_json(url, timeout=30), code)
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"{code}: {type(last_error).__name__}: {last_error}")


def _fetch_related_company_symbols():
    """Fetch supplementary instrument metadata from all published industrial groups."""
    payload = _request_json(BASE + "/StaticData/GetStaticData", timeout=60)
    static_rows = payload.get("staticData", []) if isinstance(payload, dict) else []
    groups = [row for row in static_rows if isinstance(row, dict) and row.get("type") == "IndustrialGroup"]
    found = {}
    errors = []
    for group in groups:
        group_code = str(group.get("code") or "").strip()
        if not group_code:
            continue
        try:
            data = _request_json(BASE + "/ClosingPrice/GetRelatedCompany/" + urllib.parse.quote(group_code, safe=""), timeout=30)
            rows = data.get("relatedCompany", []) if isinstance(data, dict) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                instrument = row.get("instrument") if isinstance(row.get("instrument"), dict) else {}
                merged = dict(row)
                merged.update(instrument)
                code = str(_first(merged, "insCode", "InsCode")).strip()
                symbol = str(_first(merged, "lVal18AFC", "symbol")).strip()
                if not code.isdigit() or code == "0" or not symbol:
                    continue
                found[code] = {
                    "ins_code": code, "symbol": symbol,
                    "company_name": str(_first(merged, "lVal30", "companyName")).strip(),
                    "isin": str(_first(merged, "cIsin", "isin")).strip(),
                    "market_board": _board(merged),
                    "flow": str(_first(merged, "flow", "flowCode")),
                    "source": "TSETMC",
                    "raw_json": json.dumps(row, ensure_ascii=False),
                }
        except Exception as exc:
            errors.append(f"{group_code}: {type(exc).__name__}: {exc}")
    if not found and errors:
        raise RuntimeError("دریافت اطلاعات گروه‌های صنعتی ناموفق بود: " + "؛ ".join(errors[:5]))
    return found


def _load_cached_symbol_rows():
    """Read existing official identities as a recovery/cache layer, never as discovery."""
    try:
        from .authdb import connect
        with connect() as db:
            rows = db.execute(
                "SELECT ins_code,symbol,company_name,isin,market_board,flow,raw_json "
                "FROM market_symbols WHERE ins_code<>'' AND ins_code NOT GLOB '*[^0-9]*'"
            ).fetchall()
        return {str(row["ins_code"]): {
            "ins_code": str(row["ins_code"]), "symbol": str(row["symbol"] or ""),
            "company_name": str(row["company_name"] or ""), "isin": str(row["isin"] or ""),
            "market_board": str(row["market_board"] or "نامشخص"), "flow": str(row["flow"] or ""),
            "source": "TSETMC", "raw_json": str(row["raw_json"] or "{}"),
        } for row in rows if str(row["symbol"] or "").strip()}
    except Exception:
        return {}


def _fetch_missing_instrument_info(codes, max_workers=6):
    """Fetch missing instrument metadata with bounded concurrency and retries."""
    found = []
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one_instrument_info, code): code for code in sorted(codes)}
        for future in concurrent.futures.as_completed(futures):
            code = futures[future]
            try:
                found.append(future.result())
            except Exception as exc:
                errors.append(str(exc))
    return found, errors

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
    """Discover the broad TSETMC instrument universe and resolve official identities.

    Bulk parameter InsCodes are the discovery source. MarketWatch and industrial
    group responses supplement metadata and coverage; cached SQLite identities
    are used only for already-known InsCodes. Missing identities are resolved
    through GetInstrumentInfo with bounded concurrency. No snapshot is returned
    if any discovered InsCode remains unresolved.
    """
    errors = []
    combined = {}

    # 1) Discover instruments independently of MarketWatch and the local database.
    bulk_codes = _fetch_bulk_ins_codes()

    # 2) Reuse already-known identities for codes in the bulk universe.
    cached = _load_cached_symbol_rows()
    for code in bulk_codes:
        if code in cached:
            combined[code] = cached[code]

    # 3) Supplement discovery/metadata from industrial groups.
    try:
        related = _fetch_related_company_symbols()
        for code, row in related.items():
            combined[code] = row
    except Exception as exc:
        errors.append(f"گروه‌های صنعتی: {type(exc).__name__}: {exc}")

    # 4) MarketWatch adds current trading metadata but is not treated as complete.
    url = (
        BASE + "/ClosingPrice/GetMarketWatch?market=0&industrialGroup="
        "&paperTypes%5B0%5D=1&paperTypes%5B1%5D=2&paperTypes%5B2%5D=3"
        "&paperTypes%5B3%5D=4&paperTypes%5B4%5D=5&paperTypes%5B5%5D=6"
        "&paperTypes%5B6%5D=7&paperTypes%5B7%5D=8&paperTypes%5B8%5D=9"
        "&showTraded=false&withBestLimits=false&hEven=0&RefID=0"
    )
    try:
        for row in _normalize_api(_request_json(url, timeout=60)):
            combined[row["ins_code"]] = row
    except Exception as exc:
        errors.append(f"MarketWatch جدید: {type(exc).__name__}: {exc}")
    try:
        for row in _fetch_legacy_market_watch():
            combined.setdefault(row["ins_code"], row)
    except Exception as exc:
        errors.append(f"MarketWatch قدیمی: {type(exc).__name__}: {exc}")

    # 5) Resolve every bulk InsCode not covered by MarketWatch, groups, or cache.
    missing = sorted(code for code in bulk_codes if code not in combined)
    if missing:
        resolved, info_errors = _fetch_missing_instrument_info(missing)
        for row in resolved:
            combined[row["ins_code"]] = row
        if info_errors:
            unresolved = [code for code in missing if code not in combined]
            sample = "؛ ".join(info_errors[:8])
            raise RuntimeError(
                f"تکمیل مشخصات هویتی کامل نشد: {len(unresolved)} از {len(bulk_codes)} شناسه حل‌نشده است. "
                f"نمونه خطاها: {sample}. هیچ Snapshot ناقصی برای ثبت برگردانده نشد."
            )

    # Any supplemental rows outside the bulk source are retained, but the bulk
    # source itself must be fully represented before the snapshot is accepted.
    unresolved = sorted(code for code in bulk_codes if code not in combined)
    if unresolved:
        raise RuntimeError(
            f"فهرست ابزارها ناقص است: {len(unresolved)} شناسه از منبع انبوه مشخصات هویتی ندارند؛ ثبت متوقف شد."
        )

    result = list(combined.values())
    _validate_full_snapshot(result, _previous_official_count())
    return sorted(result, key=lambda row: row["ins_code"])
