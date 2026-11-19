from .appconfig import get_connection_string

VIEWS = {
    "vw_MonthlySales": "dbo.vw_MonthlySales",
    "vw_ForecastEffectiveValues": "dbo.vw_ForecastEffectiveValues",
    "vw_ForecastCalculatedValues": "dbo.vw_ForecastCalculatedValues",
    "vw_CurrentTTM": "dbo.vw_CurrentTTM",
    "vw_CurrentPE": "dbo.vw_CurrentPE",
}

def status():
    cs = get_connection_string()
    if not cs:
        return False, "SQLSERVER_CONNECTION_STRING در فایل .env تنظیم نشده است."
    try:
        import pyodbc
        with pyodbc.connect(cs, timeout=4) as c:
            c.cursor().execute("SELECT 1").fetchone()
        return True, "اتصال SQL Server برقرار است."
    except Exception as e:
        return False, f"اتصال برقرار نشد ({type(e).__name__}). تنظیمات و درایور ODBC را بررسی کنید."

def read_view(key, limit=100):
    if key not in VIEWS:
        return [], [], "View در فهرست مجاز نیست."
    cs = get_connection_string()
    if not cs:
        return [], [], "اتصال SQL Server تنظیم نشده است. مقدار SQLSERVER_CONNECTION_STRING را در .env وارد کنید."
    try:
        import pyodbc
        query = f"SELECT TOP ({int(limit)}) * FROM {VIEWS[key]}"
        with pyodbc.connect(cs, timeout=5) as c:
            cur = c.cursor()
            cur.execute(query)
            cols = [x[0] for x in cur.description] if cur.description else []
            rows = [tuple(r) for r in cur.fetchall()]
        return cols, rows, None
    except Exception as e:
        return [], [], f"خواندن View ناموفق بود ({type(e).__name__}). وجود View و مجوز SELECT را بررسی کنید."


def market_migration_counts():
    """Return local counts and destination counts without modifying either database."""
    from .authdb import connect as sqlite_connect
    local = {}
    with sqlite_connect() as db:
        local["symbols"] = db.execute("SELECT COUNT(*) FROM market_symbols WHERE is_active=1 AND ins_code<>'' AND ins_code NOT GLOB '*[^0-9]*'").fetchone()[0]
        local["daily"] = db.execute("SELECT COUNT(*) FROM market_daily_data").fetchone()[0]
    cs = get_connection_string()
    if not cs:
        raise RuntimeError("اتصال SQL Server تنظیم نشده است.")
    import pyodbc
    with pyodbc.connect(cs, timeout=8) as conn:
        cur = conn.cursor()
        for table, key in (("dbo.MarketSymbols", "symbols"), ("dbo.MarketDailyData", "daily")):
            cur.execute("SELECT OBJECT_ID(?, 'U')", table)
            if cur.fetchone()[0] is None:
                raise RuntimeError(f"جدول {table} وجود ندارد؛ ابتدا اسکریپت ایجاد جداول را اجرا کنید.")
            cur.execute(f"SELECT COUNT_BIG(*) FROM {table}")
            local[key + "_destination"] = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT_BIG(*) FROM dbo.MarketSymbols WHERE LocalSymbolID IS NOT NULL")
        local["symbols_already_imported"] = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT_BIG(*) FROM dbo.MarketDailyData WHERE LocalMarketDataID IS NOT NULL")
        local["daily_already_imported"] = int(cur.fetchone()[0])
    return local


def transfer_market_data():
    """Insert local rows not previously imported; never deletes or updates destination rows."""
    from datetime import datetime
    from .authdb import connect as sqlite_connect
    cs = get_connection_string()
    if not cs:
        raise RuntimeError("اتصال SQL Server تنظیم نشده است.")
    import pyodbc
    with sqlite_connect() as db:
        symbols = [dict(r) for r in db.execute("SELECT * FROM market_symbols WHERE is_active=1 AND ins_code<>'' AND ins_code NOT GLOB '*[^0-9]*' ORDER BY symbol_id").fetchall()]
        daily = [dict(r) for r in db.execute("SELECT * FROM market_daily_data WHERE ins_code<>'' AND ins_code NOT GLOB '*[^0-9]*' ORDER BY market_data_id").fetchall()]
    inserted_symbols = inserted_daily = skipped_symbols = skipped_daily = 0
    with pyodbc.connect(cs, timeout=10) as conn:
        cur = conn.cursor()
        # Validate destination tables before beginning the transaction.
        for table in ("dbo.MarketSymbols", "dbo.MarketDailyData"):
            cur.execute("SELECT OBJECT_ID(?, 'U')", table)
            if cur.fetchone()[0] is None:
                raise RuntimeError(f"جدول {table} وجود ندارد؛ ابتدا اسکریپت ایجاد جداول را اجرا کنید.")
        try:
            for r in symbols:
                cur.execute("SELECT 1 FROM dbo.MarketSymbols WHERE LocalSymbolID = ? OR InsCode = ?", r["symbol_id"], r["ins_code"])
                if cur.fetchone():
                    skipped_symbols += 1
                    continue
                cur.execute("""INSERT INTO dbo.MarketSymbols
                    (LocalSymbolID, InsCode, Symbol, CompanyName, ISIN, MarketBoard, Flow, RawJson,
                     IsActive, LastSyncedAt, UpdatedAt, InstrumentCodeIsPlaceholder)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    r["symbol_id"], r["ins_code"], r["symbol"], r["company_name"], r["isin"],
                    r["market_board"], r["flow"], r["raw_json"], bool(r["is_active"]),
                    _sql_datetime(r.get("last_synced_at")), _sql_datetime(r.get("updated_at")),
                    False)
                inserted_symbols += 1
            for r in daily:
                cur.execute("SELECT 1 FROM dbo.MarketDailyData WHERE LocalMarketDataID = ?", r["market_data_id"])
                if cur.fetchone():
                    skipped_daily += 1
                    continue
                # Parent row must exist by InsCode; otherwise fail and roll back rather than lose referential integrity.
                cur.execute("SELECT 1 FROM dbo.MarketSymbols WHERE InsCode = ?", r["ins_code"])
                if not cur.fetchone():
                    raise RuntimeError(f"نماد {r['symbol']} با InsCode={r['ins_code']} در جدول مقصد موجود نیست؛ انتقال متوقف شد.")
                cur.execute("""INSERT INTO dbo.MarketDailyData
                    (LocalMarketDataID, InsCode, Symbol, FetchedAt, TradeCount, TradeVolume, TradeValue,
                     YesterdayPrice, FirstPrice, LastTradePrice, LastTradeChange, LastTradePercent,
                     ClosingPrice, ClosingChange, ClosingPercent, LowPrice, HighPrice, EPS, PERatio,
                     BuyCount, BuyVolume, BuyPrice, SellPrice, SellVolume, SellCount, Source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    r["market_data_id"], r["ins_code"], r["symbol"], _sql_datetime(r.get("fetched_at")) or datetime.now(),
                    r["trade_count"], r["trade_volume"], r["trade_value"], r["yesterday_price"], r["first_price"],
                    r["last_trade_price"], r["last_trade_change"], r["last_trade_percent"], r["closing_price"],
                    r["closing_change"], r["closing_percent"], r["low_price"], r["high_price"], r["eps"],
                    r["pe_ratio"], r["buy_count"], r["buy_volume"], r["buy_price"], r["sell_price"],
                    r["sell_volume"], r["sell_count"], r["source"])
                inserted_daily += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"inserted_symbols": inserted_symbols, "inserted_daily": inserted_daily,
            "skipped_symbols": skipped_symbols, "skipped_daily": skipped_daily}


def _sql_datetime(value):
    if not value:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def transfer_official_market_symbols():
    """Sync official numeric TSETMC InsCode records to SQL Server.

    Legacy placeholder parents are only deactivated and detached from their
    local IDs. They and their historical children are not deleted or rewritten.
    """
    from .authdb import connect as sqlite_connect

    cs = get_connection_string()
    if not cs:
        raise RuntimeError("اتصال SQL Server تنظیم نشده است.")
    import pyodbc

    with sqlite_connect() as db:
        rows = [dict(r) for r in db.execute(
            """SELECT * FROM market_symbols
               WHERE is_active=1 AND ins_code<>'' AND ins_code NOT GLOB '*[^0-9]*'
               ORDER BY symbol_id"""
        ).fetchall()]

    inserted = updated = skipped = archived_legacy = 0
    with pyodbc.connect(cs, timeout=15) as conn:
        cur = conn.cursor()
        cur.execute("SELECT OBJECT_ID(?, 'U')", "dbo.MarketSymbols")
        if cur.fetchone()[0] is None:
            raise RuntimeError("جدول dbo.MarketSymbols وجود ندارد.")

        try:
            for row in rows:
                ins_code = str(row["ins_code"]).strip()
                if not ins_code.isdigit():
                    skipped += 1
                    continue

                cur.execute(
                    """SELECT MarketSymbolID,InsCode,InstrumentCodeIsPlaceholder,LocalSymbolID
                       FROM dbo.MarketSymbols WHERE InsCode=?""",
                    ins_code,
                )
                code_match = cur.fetchone()

                cur.execute(
                    """SELECT MarketSymbolID,InsCode,InstrumentCodeIsPlaceholder,LocalSymbolID
                       FROM dbo.MarketSymbols WHERE LocalSymbolID=?""",
                    row["symbol_id"],
                )
                local_match = cur.fetchone()

                if local_match and bool(local_match[2]) and (
                    not code_match or int(code_match[0]) != int(local_match[0])
                ):
                    # Preserve the old parent and its daily rows; release its local
                    # key so the official InsCode can be attached without deleting data.
                    cur.execute(
                        """UPDATE dbo.MarketSymbols
                           SET LocalSymbolID=NULL,IsActive=0
                           WHERE MarketSymbolID=? AND InstrumentCodeIsPlaceholder=1""",
                        int(local_match[0]),
                    )
                    archived_legacy += 1
                    local_match = None

                params = (
                    row["symbol"], row["company_name"], row["isin"], row["market_board"],
                    row["flow"], row["raw_json"], bool(row["is_active"]),
                    _sql_datetime(row.get("last_synced_at")),
                    _sql_datetime(row.get("updated_at")),
                )

                if code_match:
                    code_id = int(code_match[0])
                    current_local_id = code_match[3]
                    if current_local_id not in (None, row["symbol_id"]):
                        skipped += 1
                        continue
                    cur.execute(
                        """UPDATE dbo.MarketSymbols
                           SET LocalSymbolID=?,Symbol=?,CompanyName=?,ISIN=?,MarketBoard=?,
                               Flow=?,RawJson=?,IsActive=?,LastSyncedAt=?,UpdatedAt=?,
                               InstrumentCodeIsPlaceholder=0
                           WHERE MarketSymbolID=?""",
                        row["symbol_id"], *params, code_id,
                    )
                    updated += 1
                    continue

                if local_match:
                    # An official local identity must not silently change InsCode.
                    if str(local_match[1]).strip() != ins_code or bool(local_match[2]):
                        skipped += 1
                        continue
                    cur.execute(
                        """UPDATE dbo.MarketSymbols
                           SET Symbol=?,CompanyName=?,ISIN=?,MarketBoard=?,Flow=?,RawJson=?,
                               IsActive=?,LastSyncedAt=?,UpdatedAt=?,InstrumentCodeIsPlaceholder=0
                           WHERE MarketSymbolID=?""",
                        *params, int(local_match[0]),
                    )
                    updated += 1
                    continue

                cur.execute(
                    """INSERT INTO dbo.MarketSymbols
                       (LocalSymbolID,InsCode,Symbol,CompanyName,ISIN,MarketBoard,Flow,RawJson,
                        IsActive,LastSyncedAt,UpdatedAt,InstrumentCodeIsPlaceholder)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
                    row["symbol_id"], ins_code, *params,
                )
                inserted += 1

            # Any legacy placeholders not matched in this run are also hidden from
            # active market consumers, while retaining their rows and history.
            cur.execute(
                """UPDATE dbo.MarketSymbols SET IsActive=0
                   WHERE InstrumentCodeIsPlaceholder=1 AND IsActive<>0"""
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "official_symbols_found": len(rows),
        "inserted_symbols": inserted,
        "updated_symbols": updated,
        "skipped_symbols": skipped,
        "legacy_placeholders_archived": archived_legacy,
    }
