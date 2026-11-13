import os, sqlite3
from pathlib import Path
from .security import hash_password

DB_PATH = Path(os.getenv("AUTH_DB_PATH", "app_auth.db"))

def connect():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def initialize():
    with connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS app_users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'ADMIN',
            must_change_password INTEGER NOT NULL DEFAULT 1,
            is_active INTEGER NOT NULL DEFAULT 1,
            force_change_on_first_login INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_login_at TEXT NULL,
            password_changed_at TEXT NULL
        )""")
        # Safe, additive migration for databases created by earlier app versions.
        columns = {r['name'] for r in c.execute("PRAGMA table_info(app_users)").fetchall()}
        migrations = {
            'force_change_on_first_login': "ALTER TABLE app_users ADD COLUMN force_change_on_first_login INTEGER NOT NULL DEFAULT 1",
            'created_at': "ALTER TABLE app_users ADD COLUMN created_at TEXT NOT NULL DEFAULT ''",
            'last_login_at': "ALTER TABLE app_users ADD COLUMN last_login_at TEXT NULL",
            'password_changed_at': "ALTER TABLE app_users ADD COLUMN password_changed_at TEXT NULL"
        }
        for name, sql in migrations.items():
            if name not in columns:
                c.execute(sql)
        c.execute("""CREATE TABLE IF NOT EXISTS market_symbols (
            symbol_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ins_code TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL COLLATE NOCASE,
            company_name TEXT NOT NULL DEFAULT '',
            isin TEXT NOT NULL DEFAULT '',
            market_board TEXT NOT NULL DEFAULT 'نامشخص',
            flow TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            is_active INTEGER NOT NULL DEFAULT 1,
            last_synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS IX_market_symbols_symbol ON market_symbols(symbol COLLATE NOCASE)")
        c.execute("CREATE INDEX IF NOT EXISTS IX_market_symbols_company ON market_symbols(company_name COLLATE NOCASE)")
        symbol_columns = {r['name'] for r in c.execute("PRAGMA table_info(market_symbols)").fetchall()}
        if 'instrument_code_is_placeholder' not in symbol_columns:
            c.execute("ALTER TABLE market_symbols ADD COLUMN instrument_code_is_placeholder INTEGER NOT NULL DEFAULT 0")
        c.execute("""CREATE TABLE IF NOT EXISTS market_daily_data (
            market_data_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ins_code TEXT NOT NULL,
            symbol TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            trade_count TEXT NOT NULL DEFAULT '',
            trade_volume TEXT NOT NULL DEFAULT '',
            trade_value TEXT NOT NULL DEFAULT '',
            yesterday_price TEXT NOT NULL DEFAULT '',
            first_price TEXT NOT NULL DEFAULT '',
            last_trade_price TEXT NOT NULL DEFAULT '',
            last_trade_change TEXT NOT NULL DEFAULT '',
            last_trade_percent TEXT NOT NULL DEFAULT '',
            closing_price TEXT NOT NULL DEFAULT '',
            closing_change TEXT NOT NULL DEFAULT '',
            closing_percent TEXT NOT NULL DEFAULT '',
            low_price TEXT NOT NULL DEFAULT '',
            high_price TEXT NOT NULL DEFAULT '',
            eps TEXT NOT NULL DEFAULT '',
            pe_ratio TEXT NOT NULL DEFAULT '',
            buy_count TEXT NOT NULL DEFAULT '',
            buy_volume TEXT NOT NULL DEFAULT '',
            buy_price TEXT NOT NULL DEFAULT '',
            sell_price TEXT NOT NULL DEFAULT '',
            sell_volume TEXT NOT NULL DEFAULT '',
            sell_count TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'TSETMC MarketWatchPlus'
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS IX_market_daily_data_symbol_date ON market_daily_data(symbol, fetched_at)")
        c.execute("""CREATE TABLE IF NOT EXISTS major_shareholders (
            shareholder_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ins_code TEXT NOT NULL,
            shareholder_code TEXT NOT NULL DEFAULT '',
            shareholder_name TEXT NOT NULL,
            share_count TEXT NOT NULL DEFAULT '',
            ownership_percent TEXT NOT NULL DEFAULT '',
            as_of_date TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'TSETMC',
            raw_json TEXT NOT NULL DEFAULT '{}',
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ins_code, shareholder_code, as_of_date, shareholder_name)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS capital_increase_history (
            capital_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ins_code TEXT NOT NULL,
            event_date TEXT NOT NULL DEFAULT '',
            old_share_count TEXT NOT NULL DEFAULT '',
            new_share_count TEXT NOT NULL DEFAULT '',
            change_percent TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'TSETMC',
            raw_json TEXT NOT NULL DEFAULT '{}',
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ins_code, event_date, old_share_count, new_share_count, event_type)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS codal_watchlist (
            watchlist_id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL COLLATE NOCASE UNIQUE,
            company_name TEXT NOT NULL DEFAULT '',
            is_selected INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        row = c.execute("SELECT user_id FROM app_users WHERE username='admin'").fetchone()
        if row is None:
            c.execute("INSERT INTO app_users(username,password_hash,role,must_change_password,is_active,force_change_on_first_login) VALUES(?,?,?,?,?,?)",
                      ("admin", hash_password("admin"), "ADMIN", 1, 1, 1))

def get_user(username):
    with connect() as c:
        return c.execute("SELECT * FROM app_users WHERE username=? AND is_active=1", (username,)).fetchone()

def list_users():
    with connect() as c:
        return c.execute("SELECT user_id, username, role, must_change_password, is_active, force_change_on_first_login, created_at, last_login_at, password_changed_at FROM app_users ORDER BY user_id").fetchall()

def create_user(username, password, role="USER", force_change=True):
    with connect() as c:
        c.execute("INSERT INTO app_users(username,password_hash,role,must_change_password,is_active,force_change_on_first_login,password_changed_at) VALUES(?,?,?,?,?,?,NULL)",
                  (username, hash_password(password), role, 1 if force_change else 0, 1, 1 if force_change else 0))

def update_user_policy(user_id, force_change):
    with connect() as c:
        c.execute("UPDATE app_users SET force_change_on_first_login=?, must_change_password=CASE WHEN ?=0 THEN 0 ELSE must_change_password END WHERE user_id=?", (1 if force_change else 0, 1 if force_change else 0, user_id))

def set_user_active(user_id, active):
    with connect() as c:
        c.execute("UPDATE app_users SET is_active=? WHERE user_id=?", (1 if active else 0, user_id))

def force_password_reset(user_id):
    with connect() as c:
        c.execute("UPDATE app_users SET must_change_password=1 WHERE user_id=?", (user_id,))

def record_login(user_id):
    with connect() as c:
        c.execute("UPDATE app_users SET last_login_at=CURRENT_TIMESTAMP WHERE user_id=?", (user_id,))

def set_password(user_id, password):
    with connect() as c:
        c.execute("UPDATE app_users SET password_hash=?, must_change_password=0, password_changed_at=CURRENT_TIMESTAMP WHERE user_id=?",
                  (hash_password(password), user_id))


def list_codal_watchlist():
    with connect() as c:
        return c.execute("SELECT watchlist_id, symbol, company_name, is_selected, created_at FROM codal_watchlist ORDER BY symbol COLLATE NOCASE").fetchall()

def add_codal_symbol(symbol, company_name=""):
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("نماد خالی است")
    with connect() as c:
        c.execute("INSERT INTO codal_watchlist(symbol, company_name, is_selected) VALUES(?,?,1) ON CONFLICT(symbol) DO UPDATE SET company_name=CASE WHEN excluded.company_name<>'' THEN excluded.company_name ELSE codal_watchlist.company_name END", (symbol, (company_name or "").strip()))

def delete_codal_symbol(watchlist_id):
    with connect() as c:
        c.execute("DELETE FROM codal_watchlist WHERE watchlist_id=?", (int(watchlist_id),))

def set_codal_symbols_selected(selected_ids):
    ids = {int(x) for x in selected_ids if str(x).isdigit()}
    with connect() as c:
        c.execute("UPDATE codal_watchlist SET is_selected=0")
        if ids:
            c.executemany("UPDATE codal_watchlist SET is_selected=1 WHERE watchlist_id=?", [(x,) for x in ids])


def upsert_market_symbols(rows):
    """Upsert symbol identity and append any supplied market snapshot.

    Excel MarketWatchPlus lacks official InsCode and board columns. If an
    official code is later received for the same symbol, it replaces the local
    placeholder. Missing symbols are deactivated only after a large successful
    full-market sync.
    """
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    daily_fields = (
        "trade_count", "trade_volume", "trade_value", "yesterday_price", "first_price",
        "last_trade_price", "last_trade_change", "last_trade_percent", "closing_price",
        "closing_change", "closing_percent", "low_price", "high_price", "eps", "pe_ratio",
        "buy_count", "buy_volume", "buy_price", "sell_price", "sell_volume", "sell_count"
    )
    with connect() as c:
        for r in rows:
            symbol = (r.get("symbol") or "").strip()
            incoming_code = str(r.get("ins_code") or "").strip()
            placeholder = int(r.get("instrument_code_is_placeholder", 0))
            if not symbol or not incoming_code:
                continue
            existing = c.execute("SELECT ins_code, instrument_code_is_placeholder FROM market_symbols WHERE symbol=? COLLATE NOCASE", (symbol,)).fetchone()
            if existing:
                old_code = existing["ins_code"]
                old_placeholder = int(existing["instrument_code_is_placeholder"] or 0)
                if old_placeholder and not placeholder:
                    # Replace local synthetic key with an official TSETMC code.
                    conflict = c.execute("SELECT 1 FROM market_symbols WHERE ins_code=? AND symbol<>? COLLATE NOCASE", (incoming_code, symbol)).fetchone()
                    if not conflict:
                        c.execute("UPDATE market_symbols SET ins_code=? WHERE symbol=? COLLATE NOCASE", (incoming_code, symbol))
                        # Keep local snapshots attached to the same symbol after its synthetic key is replaced.
                        c.execute("UPDATE market_daily_data SET ins_code=? WHERE ins_code=?", (incoming_code, old_code))
                        old_code = incoming_code
                elif not old_placeholder and placeholder:
                    # Excel data must not downgrade an existing official code.
                    incoming_code = old_code
                    placeholder = old_placeholder
                elif not old_placeholder and not placeholder and old_code != incoming_code:
                    # Keep the symbol unique if the official identifier changed.
                    conflict = c.execute("SELECT 1 FROM market_symbols WHERE ins_code=? AND symbol<>? COLLATE NOCASE", (incoming_code, symbol)).fetchone()
                    if not conflict:
                        c.execute("UPDATE market_symbols SET ins_code=? WHERE symbol=? COLLATE NOCASE", (incoming_code, symbol))
                    else:
                        incoming_code = old_code
            c.execute("""INSERT INTO market_symbols(ins_code,symbol,company_name,isin,market_board,flow,raw_json,is_active,last_synced_at,updated_at,instrument_code_is_placeholder)
                VALUES(?,?,?,?,?,?,?,1,?,?,?)
                ON CONFLICT(ins_code) DO UPDATE SET symbol=excluded.symbol,
                company_name=CASE WHEN excluded.company_name<>'' THEN excluded.company_name ELSE market_symbols.company_name END,
                isin=CASE WHEN excluded.isin<>'' THEN excluded.isin ELSE market_symbols.isin END,
                market_board=CASE WHEN excluded.market_board<>'نامشخص' THEN excluded.market_board ELSE market_symbols.market_board END,
                flow=CASE WHEN excluded.flow<>'' THEN excluded.flow ELSE market_symbols.flow END,
                raw_json=excluded.raw_json, is_active=1, last_synced_at=excluded.last_synced_at,
                updated_at=excluded.updated_at,
                instrument_code_is_placeholder=CASE WHEN excluded.instrument_code_is_placeholder=0 THEN 0 ELSE market_symbols.instrument_code_is_placeholder END""",
                (incoming_code, symbol, r.get("company_name", ""), r.get("isin", ""), r.get("market_board", "نامشخص"), r.get("flow", ""), r.get("raw_json", "{}"), now, now, placeholder))
            daily = r.get("daily_data") or {}
            if daily:
                values = [str(daily.get(field, "") or "") for field in daily_fields]
                columns = ",".join(daily_fields)
                placeholders = ",".join("?" for _ in daily_fields)
                c.execute(f"INSERT INTO market_daily_data(ins_code,symbol,fetched_at,{columns},source) VALUES(?,?,?,{placeholders},?)",
                          [incoming_code, symbol, now, *values, "TSETMC MarketWatchPlus Excel"])
        if rows:
            # Only deactivate absent symbols after a complete, plausible full-market result.
            active_codes = [str(r.get("ins_code") or "") for r in rows if r.get("ins_code")]
            placeholders = ",".join("?" for _ in active_codes)
            if len(active_codes) >= 100 and placeholders:
                # Avoid deactivating official-code rows when incoming Excel keys are placeholders.
                symbols = [str(r.get("symbol") or "") for r in rows if r.get("symbol")]
                symbol_placeholders = ",".join("?" for _ in symbols)
                if symbol_placeholders:
                    c.execute(f"UPDATE market_symbols SET is_active=0, updated_at=? WHERE symbol NOT IN ({symbol_placeholders}) COLLATE NOCASE", [now, *symbols])
    return len([r for r in rows if r.get("symbol") and r.get("ins_code")])


def list_market_symbols(search="", limit=5000):
    with connect() as c:
        q = (search or "").strip()
        if q:
            like = f"%{q}%"
            return c.execute("SELECT symbol_id,ins_code,symbol,company_name,market_board,isin FROM market_symbols WHERE is_active=1 AND (symbol LIKE ? OR company_name LIKE ?) ORDER BY symbol COLLATE NOCASE LIMIT ?", (like,like,int(limit))).fetchall()
        return c.execute("SELECT symbol_id,ins_code,symbol,company_name,market_board,isin FROM market_symbols WHERE is_active=1 ORDER BY symbol COLLATE NOCASE LIMIT ?", (int(limit),)).fetchall()


def market_symbol_count():
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM market_symbols WHERE is_active=1").fetchone()[0]


def get_market_symbol(symbol_id):
    with connect() as c:
        return c.execute("SELECT * FROM market_symbols WHERE symbol_id=? AND is_active=1", (int(symbol_id),)).fetchone()
