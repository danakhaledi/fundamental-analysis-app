"""Read-only diagnostic for the TSETMC InstrumentIdentity response shape.

This script reads the local SQLite market_symbols table and calls the public
TSETMC identity endpoint directly. It does not write to SQLite or SQL Server.
Run from the repository root:
    python diagnose_tsetmc_identity.py
Optional:
    python diagnose_tsetmc_identity.py --symbols "آردينه,آسيا,آكو,آپ,اخابر,آواك"
"""
from __future__ import annotations

import argparse
import json
from urllib.parse import quote

from app.dotenv import load_env

# Load environment before importing modules that initialize local DB settings.
load_env()

from app.authdb import connect as sqlite_connect
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

    with sqlite_connect() as db:
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

    found = {str(row["symbol"]): row for row in rows}
    print("========== TSETMC IDENTITY RESPONSE DIAGNOSTIC ==========")
    print("mode: READ_ONLY")
    print("sqlite_changes: false")
    print("sqlserver_changes: false")
    print("requested_symbols:", json.dumps(requested, ensure_ascii=False))
    print("matched_local_symbols:", len(rows))

    for symbol in requested:
        row = found.get(symbol)
        if row is None:
            print("\n--- SYMBOL NOT FOUND LOCALLY ---")
            print("symbol:", symbol)
            continue

        ins_code = str(row["ins_code"])
        url = BASE + "/Instrument/GetInstrumentIdentity/" + quote(ins_code)
        print("\n--- INSTRUMENT ---")
        print("symbol:", symbol)
        print("ins_code:", ins_code)
        print("company_name:", row["company_name"])
        try:
            payload = _request_json(url, timeout=max(5, args.timeout))
            print("raw_response:")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:
            print("request_error:", f"{type(exc).__name__}: {exc}")

    print("\n========== END OF DIAGNOSTIC ==========")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
