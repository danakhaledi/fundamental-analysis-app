"""Read-only diagnostic for the TSETMC InstrumentIdentity response shape.

This script opens the existing local SQLite database in read-only mode and
calls the public TSETMC identity endpoint directly. It never initializes,
creates, or writes to SQLite, and it does not access SQL Server.

Run from the repository root:
    python diagnose_tsetmc_identity.py
Optional:
    python diagnose_tsetmc_identity.py --symbols "آردينه,آسيا,آكو,آپ,اخابر,آواك"
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

from app.dotenv import load_env

# Load environment before importing modules that initialize local DB settings.
load_env()

from app.authdb import DB_PATH
from app.market_symbols import BASE, _request_json


DEFAULT_SYMBOLS = ["آردينه", "آسيا", "آكو", "آپ", "اخابر", "آواك"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only TSETMC identity response diagnostic")
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated exact local symbols to inspect",
    )
    parser.add_argument("--timeout", type=int, default=40)
    args = parser.parse_args()

    requested = [item.strip() for item in args.symbols.split(",") if item.strip()]
    if not requested:
        raise SystemExit("No symbols were supplied.")

    db_path = Path(DB_PATH).expanduser().resolve()
    print("========== TSETMC IDENTITY RESPONSE DIAGNOSTIC ==========")
    print("mode: READ_ONLY")
    print("sqlite_changes: false")
    print("sqlserver_changes: false")
    print("sqlite_database_path:", str(db_path))

    # Never use sqlite3.connect(path) without mode=ro here: that can create
    # a new empty database when the configured path does not exist.
    if not db_path.is_file():
        print("database_status: FILE_NOT_FOUND")
        print("action: Check AUTH_DB_PATH in .env and confirm app_auth.db is in the repository root.")
        return 2

    uri = db_path.as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            table = db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("market_symbols",),
            ).fetchone()

            if table is None:
                print("database_status: TABLE_NOT_FOUND")
                print("missing_table: market_symbols")
                print("action: This is not the application database expected by this diagnostic. No initialization was run.")
                return 2

            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(market_symbols)").fetchall()
            }
            required = {"symbol", "ins_code", "company_name", "instrument_code_is_placeholder"}
            missing_columns = sorted(required - columns)
            if missing_columns:
                print("database_status: TABLE_SCHEMA_MISMATCH")
                print("missing_columns:", json.dumps(missing_columns, ensure_ascii=False))
                return 2

            placeholders = ",".join("?" for _ in requested)
            rows = db.execute(
                f"""
                SELECT symbol, ins_code, company_name
                FROM market_symbols
                WHERE symbol IN ({placeholders})
                  AND instrument_code_is_placeholder = 0
                ORDER BY symbol, ins_code
                """,
                requested,
            ).fetchall()
    except sqlite3.Error as exc:
        print("database_status: SQLITE_READ_ERROR")
        print("database_error:", f"{type(exc).__name__}: {exc}")
        return 2

    print("database_status: OK_READ_ONLY")
    print("requested_symbols:", json.dumps(requested, ensure_ascii=False))
    print("matched_local_rows:", len(rows))

    rows_by_symbol: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        rows_by_symbol.setdefault(str(row["symbol"]), []).append(row)

    for symbol in requested:
        matching_rows = rows_by_symbol.get(symbol, [])
        if not matching_rows:
            print("\\n--- SYMBOL NOT FOUND LOCALLY ---")
            print("symbol:", symbol)
            continue

        for row in matching_rows:
            ins_code = str(row["ins_code"])
            url = BASE + "/Instrument/GetInstrumentIdentity/" + quote(ins_code)
            print("\\n--- INSTRUMENT ---")
            print("symbol:", symbol)
            print("ins_code:", ins_code)
            print("company_name:", row["company_name"])
            try:
                payload = _request_json(url, timeout=max(5, args.timeout))
                print("raw_response:")
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            except Exception as exc:
                print("request_error:", f"{type(exc).__name__}: {exc}")

    print("\\n========== END OF DIAGNOSTIC ==========")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
