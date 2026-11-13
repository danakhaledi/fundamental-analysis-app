"""Incremental TSETMC daily OHLCV history loader for SQL Server."""
from datetime import datetime
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

API = "https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceDailyList/{}/0"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
BATCH_SIZE = 100
MAX_WORKERS = 8


def _get_history(ins_code):
    req = urllib.request.Request(API.format(ins_code), headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Referer": "https://www.tsetmc.com/"})
    with urllib.request.urlopen(req, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8-sig"))
    rows = payload.get("closingPriceDaily") if isinstance(payload, dict) else None
    if rows is None:
        raise ValueError("پاسخ سرویس فاقد closingPriceDaily است")
    result = []
    for row in rows:
        d = str(row.get("dEven", ""))
        if len(d) != 8 or not d.isdigit():
            continue
        trade_date = datetime.strptime(d, "%Y%m%d").date()
        # Keep the source's native numeric representation; blank/missing values remain NULL.
        def val(*keys):
            for key in keys:
                value = row.get(key)
                if value is not None and value != "":
                    return str(value)
            return None
        result.append({
            "TradeDate": trade_date, "TradeCount": val("zTotTran"),
            "TradeVolume": val("qTotTran5J"), "TradeValue": val("qTotCap"),
            "YesterdayPrice": val("priceYesterday"), "FirstPrice": val("priceFirst"),
            "LastTradePrice": val("pDrCotVal"), "ClosingPrice": val("pClosing"),
            "ClosingChange": val("priceChange"), "LowPrice": val("priceMin"),
            "HighPrice": val("priceMax"), "Source": "TSETMC ClosingPriceDailyList"
        })
    return result


def history_status():
    from .appconfig import get_connection_string
    cs = get_connection_string()
    if not cs:
        raise RuntimeError("اتصال SQL Server تنظیم نشده است.")
    import pyodbc
    with pyodbc.connect(cs, timeout=10) as conn:
        cur = conn.cursor()
        cur.execute("SELECT OBJECT_ID('dbo.MarketSymbols','U'), OBJECT_ID('dbo.MarketDailyData','U')")
        a, b = cur.fetchone()
        if not a or not b:
            raise RuntimeError("جداول MarketSymbols و MarketDailyData باید در SQL Server موجود باشند.")
        cur.execute("SELECT COUNT_BIG(*) FROM dbo.MarketSymbols WHERE TRY_CONVERT(BIGINT, LTRIM(RTRIM(InsCode))) IS NOT NULL")
        symbols = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT_BIG(DISTINCT InsCode), COUNT_BIG(*), MIN(TradeDate), MAX(TradeDate) FROM dbo.MarketDailyData WHERE TradeDate IS NOT NULL")
        row = cur.fetchone()
        return {"symbols": symbols, "symbols_with_history": int(row[0]), "history_rows": int(row[1]), "min_date": row[2], "max_date": row[3]}


def sync_history_batch(mode="backfill"):
    """Process a resumable batch. mode=backfill processes symbols with no history; update cycles through all symbols."""
    from .appconfig import get_connection_string
    from .authdb import connect as sqlite_connect
    cs = get_connection_string()
    if not cs:
        raise RuntimeError("اتصال SQL Server تنظیم نشده است.")
    import pyodbc
    with sqlite_connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS market_history_sync_state (mode TEXT PRIMARY KEY, last_ins_code TEXT NOT NULL DEFAULT '')")
        state = db.execute("SELECT last_ins_code FROM market_history_sync_state WHERE mode=?", (mode,)).fetchone()
        last_code = state[0] if state else ""
    with pyodbc.connect(cs, timeout=20) as conn:
        cur = conn.cursor()
        cur.execute("SELECT OBJECT_ID('dbo.MarketDailyData','U')")
        if not cur.fetchone()[0]:
            raise RuntimeError("جدول dbo.MarketDailyData وجود ندارد.")
        if mode == "backfill":
            cur.execute("""SELECT TOP (?) s.InsCode, s.Symbol FROM dbo.MarketSymbols s
                WHERE TRY_CONVERT(BIGINT, LTRIM(RTRIM(s.InsCode))) IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM dbo.MarketDailyData d WHERE d.InsCode=s.InsCode AND d.TradeDate IS NOT NULL)
                AND s.InsCode > ? ORDER BY s.InsCode""", BATCH_SIZE, last_code)
        else:
            cur.execute("""SELECT TOP (?) InsCode, Symbol FROM dbo.MarketSymbols
                WHERE TRY_CONVERT(BIGINT, LTRIM(RTRIM(InsCode))) IS NOT NULL
                AND InsCode > ? ORDER BY InsCode""", BATCH_SIZE, last_code)
        symbols = [(str(r[0]), str(r[1])) for r in cur.fetchall()]
        if not symbols:
            # Empty batches are valid (e.g. symbols have not yet been migrated to SQL Server,
            # all eligible symbols are already backfilled, or the cursor reached the end).
            with sqlite_connect() as db:
                if last_code:
                    db.execute("INSERT INTO market_history_sync_state(mode,last_ins_code) VALUES(?, '') ON CONFLICT(mode) DO UPDATE SET last_ins_code=''", (mode,))
            cur.execute("SELECT COUNT_BIG(*) FROM dbo.MarketSymbols")
            total_symbols = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT_BIG(*) FROM dbo.MarketSymbols WHERE TRY_CONVERT(BIGINT, LTRIM(RTRIM(InsCode))) IS NOT NULL")
            eligible_symbols = int(cur.fetchone()[0])
            if total_symbols == 0:
                message = "جدول dbo.MarketSymbols خالی است؛ ابتدا انتقال نمادها از SQLite به SQL Server را از صفحه انتقال اطلاعات بازار انجام دهید."
            elif eligible_symbols == 0:
                message = "هیچ نمادی با InsCode معتبر پیدا نشد؛ کد ابزار نمادها را بررسی کنید."
            elif mode == "backfill":
                message = "نماد واجد شرایط دیگری بدون تاریخچه باقی نمانده است."
            else:
                message = "به انتهای فهرست نمادها رسیدیم؛ شمارنده دوره به‌روزرسانی بازنشانی شد."
            return {"processed": 0, "rows_added": 0, "rows_updated": 0, "errors": [], "cycle_complete": True, "reset": bool(last_code), "message": message}
        added = updated = 0
        errors = []
        # Fetch network data concurrently, but keep SQL writes serialized on one connection.
        # This reduces wall-clock time without opening hundreds of requests at once.
        fetched = {}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            future_map = {pool.submit(_get_history, ins_code): (ins_code, symbol) for ins_code, symbol in symbols}
            for future in as_completed(future_map):
                ins_code, symbol = future_map[future]
                try:
                    fetched[ins_code] = future.result()
                except Exception as exc:
                    errors.append(f"{symbol} ({ins_code}): {type(exc).__name__}: {str(exc)[:160]}")
        for ins_code, symbol in symbols:
            if ins_code not in fetched:
                continue
            try:
                rows = fetched[ins_code]
                for r in rows:
                    cur.execute("SELECT MarketDataID FROM dbo.MarketDailyData WHERE InsCode=? AND TradeDate=?", ins_code, r["TradeDate"])
                    found = cur.fetchone()
                    if found:
                        cur.execute("""UPDATE dbo.MarketDailyData SET Symbol=?, TradeCount=COALESCE(?,N''), TradeVolume=COALESCE(?,N''), TradeValue=COALESCE(?,N''),
                            YesterdayPrice=COALESCE(?,N''), FirstPrice=COALESCE(?,N''), LastTradePrice=COALESCE(?,N''), ClosingPrice=COALESCE(?,N''),
                            ClosingChange=COALESCE(?,N''), LowPrice=COALESCE(?,N''), HighPrice=COALESCE(?,N''), Source=?, FetchedAt=SYSDATETIME()
                            WHERE MarketDataID=?""", symbol, r["TradeCount"], r["TradeVolume"], r["TradeValue"], r["YesterdayPrice"], r["FirstPrice"], r["LastTradePrice"], r["ClosingPrice"], r["ClosingChange"], r["LowPrice"], r["HighPrice"], r["Source"], found[0])
                        updated += 1
                    else:
                        cur.execute("""INSERT INTO dbo.MarketDailyData (InsCode,Symbol,FetchedAt,TradeDate,TradeCount,TradeVolume,TradeValue,YesterdayPrice,FirstPrice,LastTradePrice,ClosingPrice,ClosingChange,LowPrice,HighPrice,Source)
                            VALUES (?,?,SYSDATETIME(),?,?,?,?,?,?,?,?,?,?,?,?)""", ins_code, symbol, r["TradeDate"], r["TradeCount"] or "", r["TradeVolume"] or "", r["TradeValue"] or "", r["YesterdayPrice"] or "", r["FirstPrice"] or "", r["LastTradePrice"] or "", r["ClosingPrice"] or "", r["ClosingChange"] or "", r["LowPrice"] or "", r["HighPrice"] or "", r["Source"])
                        added += 1
                conn.commit()
            except Exception as exc:
                conn.rollback()
                errors.append(f"{symbol} ({ins_code}): SQL {type(exc).__name__}: {str(exc)[:160]}")
        new_last = symbols[-1][0]
        with sqlite_connect() as db:
            db.execute("INSERT INTO market_history_sync_state(mode,last_ins_code) VALUES(?,?) ON CONFLICT(mode) DO UPDATE SET last_ins_code=excluded.last_ins_code", (mode, new_last))
        # Backfill completes when fewer than a full batch returned. Update mode wraps on next click.
        return {"processed": len(symbols), "rows_added": added, "rows_updated": updated, "errors": errors, "cycle_complete": len(symbols) < BATCH_SIZE, "last_symbol": symbols[-1][1], "batch_size": BATCH_SIZE, "workers": MAX_WORKERS, "fetch_success": len(fetched)}
