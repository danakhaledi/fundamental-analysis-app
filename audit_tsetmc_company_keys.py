"""Read-only audit of TSETMC company-identity candidate fields.

Reads the existing app_auth.db in SQLite read-only mode and requests the
TSETMC InstrumentIdentity endpoint for each official local instrument.
It does not initialize or modify SQLite and never connects to SQL Server.

Run from the repository root:
    python audit_tsetmc_company_keys.py

Optional smaller test:
    python audit_tsetmc_company_keys.py --limit 100

The report compares cComVal, (sector.cSecVal, cComVal), cSocCSAC and cIsin.
No candidate is treated as an approved company key by this script.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

from app.dotenv import load_env

load_env()

from app.authdb import DB_PATH
from app.market_symbols import BASE, _request_json


def clean(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def identity_fields(payload: object, ins_code: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Response root is not a JSON object")
    identity = payload.get("instrumentIdentity")
    if not isinstance(identity, dict):
        raise ValueError("instrumentIdentity object is missing")
    sector = identity.get("sector")
    if not isinstance(sector, dict):
        sector = {}
    return {
        "ins_code": ins_code,
        "symbol": clean(identity.get("lVal18AFC")),
        "company_name": clean(identity.get("lSoc30") or identity.get("lVal30")),
        "cComVal": clean(identity.get("cComVal")),
        "industry_code": clean(sector.get("cSecVal")),
        "industry_name": clean(sector.get("lSecVal")),
        "cSocCSAC": clean(identity.get("cSocCSAC")),
        "cIsin": clean(identity.get("cIsin")),
        "instrumentID": clean(identity.get("instrumentID")),
        "cValMne": clean(identity.get("cValMne")),
    }


def index_groups(rows: list[dict], key_fields: tuple[str, ...]) -> dict:
    groups = defaultdict(list)
    for row in rows:
        key = tuple(row.get(field, "") for field in key_fields)
        if all(key):
            groups[key].append(row)
    return groups


def describe_collisions(groups: dict, distinguish_fields: tuple[str, ...], limit: int = 30) -> list:
    found = []
    for key, members in groups.items():
        variants = sorted({
            tuple(member.get(field, "") for field in distinguish_fields)
            for member in members
        })
        if len(variants) > 1:
            found.append({
                "key": list(key),
                "distinct_values": [list(value) for value in variants],
                "instruments": [
                    {
                        "ins_code": m["ins_code"],
                        "symbol": m["symbol"],
                        "company_name": m["company_name"],
                        "cComVal": m["cComVal"],
                        "industry_code": m["industry_code"],
                        "cSocCSAC": m["cSocCSAC"],
                        "cIsin": m["cIsin"],
                    }
                    for m in members[:12]
                ],
                "instrument_count": len(members),
            })
    found.sort(key=lambda item: (-len(item["distinct_values"]), -item["instrument_count"]))
    return found[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only TSETMC company-key audit")
    parser.add_argument("--limit", type=int, default=0, help="Optional maximum number of instruments; 0 means all")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent requests (1-6; default 4)")
    parser.add_argument("--timeout", type=int, default=35, help="Per-request timeout in seconds")
    parser.add_argument("--pause", type=float, default=0.0, help="Optional delay between submissions in seconds")
    args = parser.parse_args()

    workers = max(1, min(args.workers, 6))
    db_path = Path(DB_PATH).expanduser().resolve()
    print("mode: READ_ONLY_AUDIT")
    print("sqlite_changes: false")
    print("sqlserver_access: false")
    print("sqlite_database_path:", str(db_path))

    if not db_path.is_file():
        print("status: DATABASE_FILE_NOT_FOUND")
        print("hint: Check AUTH_DB_PATH in .env.")
        return 2

    try:
        with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_symbols'"
            ).fetchone()
            if exists is None:
                print("status: MARKET_SYMBOLS_TABLE_NOT_FOUND")
                return 2
            columns = {row["name"] for row in db.execute("PRAGMA table_info(market_symbols)")}
            required = {"ins_code", "symbol", "company_name", "instrument_code_is_placeholder"}
            missing = sorted(required - columns)
            if missing:
                print("status: SCHEMA_MISMATCH")
                print("missing_columns:", json.dumps(missing))
                return 2
            sql = """
                SELECT ins_code, symbol, company_name
                FROM market_symbols
                WHERE instrument_code_is_placeholder=0
                  AND ins_code GLOB '[0-9]*'
                ORDER BY ins_code
            """
            if args.limit > 0:
                sql += " LIMIT ?"
                source_rows = db.execute(sql, (args.limit,)).fetchall()
            else:
                source_rows = db.execute(sql).fetchall()
    except sqlite3.Error as exc:
        print("status: SQLITE_READ_ERROR")
        print("error:", f"{type(exc).__name__}: {exc}")
        return 2

    # Remove any duplicate InsCode defensively without modifying the source DB.
    instruments = []
    seen = set()
    for row in source_rows:
        code = clean(row["ins_code"])
        if code and code not in seen:
            seen.add(code)
            instruments.append({
                "ins_code": code,
                "local_symbol": clean(row["symbol"]),
                "local_company_name": clean(row["company_name"]),
            })

    print("status: FETCHING")
    print("local_official_instruments:", len(instruments))
    print("workers:", workers)
    results = []
    errors = []
    started = time.monotonic()

    def fetch_one(item):
        url = BASE + "/Instrument/GetInstrumentIdentity/" + quote(item["ins_code"])
        payload = _request_json(url, timeout=max(5, args.timeout))
        fields = identity_fields(payload, item["ins_code"])
        fields["local_symbol"] = item["local_symbol"]
        fields["local_company_name"] = item["local_company_name"]
        return fields

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for item in instruments:
            if args.pause > 0:
                time.sleep(args.pause)
            futures[pool.submit(fetch_one, item)] = item
        completed = 0
        for future in as_completed(futures):
            item = futures[future]
            completed += 1
            try:
                results.append(future.result())
            except Exception as exc:
                errors.append({
                    "ins_code": item["ins_code"],
                    "symbol": item["local_symbol"],
                    "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                })
            if completed % 100 == 0 or completed == len(futures):
                print(f"progress: {completed}/{len(futures)}")

    # Sort for stable, reviewable output.
    results.sort(key=lambda row: (row["ins_code"], row["local_symbol"]))
    errors.sort(key=lambda row: row["ins_code"])

    # Candidate-key tests:
    # 1) cComVal must not map to multiple issuer codes / industries.
    # 2) (industry_code, cComVal) must not map to multiple issuer codes.
    # 3) cSocCSAC should be stable across instruments and distinguish issuers.
    # 4) cIsin is tested only as an instrument-level field, not assumed to be a company key.
    comval_groups = index_groups(results, ("cComVal",))
    composite_groups = index_groups(results, ("industry_code", "cComVal"))
    soccsac_groups = index_groups(results, ("cSocCSAC",))
    isin_groups = index_groups(results, ("cIsin",))

    comval_cross_issuer = describe_collisions(
        comval_groups, ("cSocCSAC", "industry_code"), limit=40
    )
    composite_cross_issuer = describe_collisions(
        composite_groups, ("cSocCSAC",), limit=40
    )
    soccsac_cross_industry = describe_collisions(
        soccsac_groups, ("industry_code", "cComVal"), limit=40
    )
    isin_cross_issuer = describe_collisions(
        isin_groups, ("cSocCSAC",), limit=20
    )

    unique_values = {
        field: len({row[field] for row in results if row.get(field)})
        for field in ("cComVal", "industry_code", "cSocCSAC", "cIsin", "instrumentID")
    }
    missing_values = {
        field: sum(1 for row in results if not row.get(field))
        for field in ("cComVal", "industry_code", "cSocCSAC", "cIsin", "instrumentID")
    }

    report = {
        "summary": {
            "local_official_instruments": len(instruments),
            "identities_received": len(results),
            "request_errors": len(errors),
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "unique_nonempty_values": unique_values,
            "missing_values": missing_values,
            "cComVal_reused_across_issuer_or_industry": len(comval_cross_issuer),
            "industry_plus_cComVal_collisions_across_cSocCSAC": len(composite_cross_issuer),
            "cSocCSAC_maps_to_multiple_industry_or_cComVal_pairs": len(soccsac_cross_industry),
            "cIsin_maps_to_multiple_cSocCSAC_values": len(isin_cross_issuer),
        },
        "collision_examples": {
            "cComVal": comval_cross_issuer,
            "industry_code_plus_cComVal": composite_cross_issuer,
            "cSocCSAC": soccsac_cross_industry,
            "cIsin": isin_cross_issuer,
        },
        "request_errors_sample": errors[:40],
        "identity_rows_sample": results[:30],
        "note": (
            "This is a diagnostic comparison, not proof of legal issuer identity. "
            "cIsin and instrumentID are instrument-level candidates unless TSETMC "
            "documentation establishes otherwise. No database rows were changed."
        ),
    }
    print("========== AUDIT REPORT JSON ==========")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("========== END ==========")
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
