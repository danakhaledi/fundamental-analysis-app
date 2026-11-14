"""Official TSETMC industry/company registry synchronization.

Industry and company identity are sourced only from TSETMC identifiers.
This module does not infer either value from company names or Codal line items.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

from .market_symbols import BASE, _request_json, _walk_dicts
from .authdb import connect as sqlite_connect
from .appconfig import get_connection_string


def _value(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _clean_code(value):
    if value is None:
        return ""
    value = str(value).strip()
    if value.endswith(".0") and value[:-2].isdigit():
        value = value[:-2]
    return value if value and value not in {"0", "None", "null"} else ""


def parse_industries(payload):
    """Return authoritative TSETMC IndustrialGroup code/name pairs."""
    result = {}
    for row in _walk_dicts(payload):
        kind = str(_value(row, "type", "dataType", "staticDataType") or "").strip().lower()
        if kind != "industrialgroup":
            continue
        code = _clean_code(_value(row, "code", "cSecVal", "value"))
        name = str(_value(row, "name", "lSecVal", "title") or "").strip()
        if code and name:
            previous = result.get(code)
            if previous and previous != name:
                raise ValueError(f"TSETMC static data contains conflicting names for industry code {code}.")
            result[code] = name
    if not result:
        raise ValueError("TSETMC static data did not contain any IndustrialGroup records.")
    return [{"industry_code": code, "industry_name": result[code]} for code in sorted(result)]


def parse_instrument_identity(payload, ins_code):
    """Extract cSocCSAC as issuer key and sector.cSecVal as optional industry code.

    cComVal, cIsin and instrumentID are deliberately not used as company keys.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"Instrument {ins_code}: response root is not an object.")
    identity = payload.get("instrumentIdentity")
    if not isinstance(identity, dict):
        raise ValueError(f"Instrument {ins_code}: instrumentIdentity object is missing.")

    company_code = _clean_code(identity.get("cSocCSAC"))
    if not company_code:
        raise ValueError(f"Instrument {ins_code}: cSocCSAC is missing.")

    sector = identity.get("sector")
    if not isinstance(sector, dict):
        sector = {}
    industry_code = _clean_code(sector.get("cSecVal"))
    industry_name = str(sector.get("lSecVal") or "").strip()

    return {
        "ins_code": str(ins_code),
        "company_code": company_code,
        "industry_code": industry_code,
        "industry_name": industry_name,
        "raw_json": json.dumps(payload, ensure_ascii=False),
    }


def fetch_industries():
    payload = _request_json(BASE + "/StaticData/GetStaticData", timeout=60)
    return parse_industries(payload)


def fetch_instrument_identity(ins_code):
    payload = _request_json(BASE + "/Instrument/GetInstrumentIdentity/" + quote(str(ins_code)), timeout=30)
    return parse_instrument_identity(payload, ins_code)


def _load_and_cache_identities(batch_size=500, max_workers=12):
    """Fetch a resumable batch of identities and persist only official IDs locally."""
    batch_size = max(1, min(int(batch_size), 1000))
    max_workers = max(1, min(int(max_workers), 20))
    with sqlite_connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS tsetmc_identity_cache (
            ins_code TEXT PRIMARY KEY,
            company_code TEXT NOT NULL DEFAULT '',
            company_key_source TEXT NOT NULL DEFAULT 'cComVal',
            industry_code TEXT NOT NULL DEFAULT '',
            industry_name TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            error TEXT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        cache_columns = {row["name"] for row in db.execute("PRAGMA table_info(tsetmc_identity_cache)").fetchall()}
        if "company_key_source" not in cache_columns:
            db.execute("ALTER TABLE tsetmc_identity_cache ADD COLUMN company_key_source TEXT NOT NULL DEFAULT 'cComVal'")
        pending = db.execute("""
            SELECT s.ins_code
            FROM market_symbols s
            LEFT JOIN tsetmc_identity_cache c ON c.ins_code=s.ins_code
            WHERE s.instrument_code_is_placeholder=0
              AND s.ins_code GLOB '[0-9]*'
              AND (c.ins_code IS NULL OR c.status<>'SUCCESS' OR c.company_key_source<>'cSocCSAC')
            ORDER BY s.symbol_id
            LIMIT ?
        """, (batch_size,)).fetchall()

    def fetch_one(row):
        code = str(row["ins_code"])
        try:
            return fetch_instrument_identity(code), None
        except Exception as exc:
            return {"ins_code": code}, f"{type(exc).__name__}: {str(exc)[:300]}"

    results = []
    if pending:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(fetch_one, row) for row in pending]
            for future in as_completed(futures):
                results.append(future.result())

    with sqlite_connect() as db:
        for identity, error in results:
            if error:
                db.execute("""INSERT OR REPLACE INTO tsetmc_identity_cache
                    (ins_code,company_code,company_key_source,industry_code,industry_name,raw_json,status,error,updated_at)
                    VALUES(?, '', 'cSocCSAC', '', '', '{}', 'ERROR', ?, CURRENT_TIMESTAMP)""",
                    identity["ins_code"], error)
            else:
                db.execute("""INSERT OR REPLACE INTO tsetmc_identity_cache
                    (ins_code,company_code,company_key_source,industry_code,industry_name,raw_json,status,error,updated_at)
                    VALUES(?,?,'cSocCSAC',?,?,?,'SUCCESS',NULL,CURRENT_TIMESTAMP)""",
                    identity["ins_code"], identity["company_code"], identity["industry_code"],
                    identity["industry_name"], identity["raw_json"])
        total = db.execute("""SELECT COUNT(*) FROM market_symbols
            WHERE instrument_code_is_placeholder=0 AND ins_code GLOB '[0-9]*'""").fetchone()[0]
        successful = db.execute("SELECT COUNT(*) FROM tsetmc_identity_cache WHERE status='SUCCESS' AND company_key_source='cSocCSAC'").fetchone()[0]
        remaining = db.execute("""SELECT COUNT(*) FROM market_symbols s
            LEFT JOIN tsetmc_identity_cache c ON c.ins_code=s.ins_code
            WHERE s.instrument_code_is_placeholder=0 AND s.ins_code GLOB '[0-9]*'
              AND (c.ins_code IS NULL OR c.status<>'SUCCESS' OR c.company_key_source<>'cSocCSAC')""").fetchone()[0]
    return {
        "attempted": len(pending),
        "successfully_resolved": len(results) - sum(1 for _, error in results if error),
        "total_official_symbols": int(total),
        "cached_identities": int(successful),
        "remaining_identities": int(remaining),
        "errors": [error for _, error in results if error][:10],
    }


def sync_tsetmc_registry(batch_size=500, max_workers=12):
    """Synchronize official industries, companies and MarketSymbols.CompanyID.

    This function writes only dbo.Industries, dbo.Companies and
    dbo.MarketSymbols.CompanyID. It does not touch Codal stages or any
    production, sales, material, or resource tables.
    """
    industries = fetch_industries()
    fetch_result = _load_and_cache_identities(batch_size=batch_size, max_workers=max_workers)

    cs = get_connection_string()
    if not cs:
        raise RuntimeError("اتصال SQL Server تنظیم نشده است.")
    import pyodbc

    with sqlite_connect() as db:
        symbols = [dict(row) for row in db.execute("""
            SELECT ins_code,symbol,company_name
            FROM market_symbols
            WHERE instrument_code_is_placeholder=0 AND ins_code GLOB '[0-9]*'
            ORDER BY symbol,ins_code
        """).fetchall()]
        identities = [dict(row) for row in db.execute("""
            SELECT ins_code,company_code,industry_code,industry_name
            FROM tsetmc_identity_cache WHERE status='SUCCESS' AND company_key_source='cSocCSAC'
            ORDER BY company_code,ins_code
        """).fetchall()]

    symbol_by_code = {str(row["ins_code"]): row for row in symbols}
    grouped = {}
    for identity in identities:
        code = str(identity["company_code"] or "").strip()
        industry_code = str(identity["industry_code"] or "").strip()
        symbol = symbol_by_code.get(str(identity["ins_code"]))
        if not code or not industry_code or not symbol:
            continue
        grouped.setdefault(code, []).append((identity, symbol))

    inserted_industries = updated_industries = inserted_companies = existing_companies = 0
    linked_symbols = unlinked_symbols = industry_conflicts = company_conflicts = symbol_conflicts = 0
    errors = list(fetch_result["errors"])
    with pyodbc.connect(cs, timeout=20) as conn:
        cur = conn.cursor()
        for table in ("dbo.Industries", "dbo.Companies", "dbo.MarketSymbols"):
            cur.execute("SELECT OBJECT_ID(?, 'U')", table)
            if cur.fetchone()[0] is None:
                raise RuntimeError(f"جدول {table} وجود ندارد.")
        try:
            industry_ids = {}
            for industry in industries:
                code, name = industry["industry_code"], industry["industry_name"]
                cur.execute("SELECT IndustryID,IndustryName FROM dbo.Industries WITH (UPDLOCK,HOLDLOCK) WHERE IndustryCode=?", code)
                existing = cur.fetchone()
                if existing:
                    industry_ids[code] = int(existing[0])
                    if str(existing[1]).strip() != name:
                        cur.execute("SELECT IndustryID FROM dbo.Industries WHERE IndustryName=? AND IndustryCode<>?", name, code)
                        if cur.fetchone():
                            errors.append(f"Industry name conflict for official code {code}: {name}")
                        else:
                            cur.execute("UPDATE dbo.Industries SET IndustryName=?,IsActive=1 WHERE IndustryID=?", name, int(existing[0]))
                            updated_industries += 1
                    else:
                        cur.execute("UPDATE dbo.Industries SET IsActive=1 WHERE IndustryID=?", int(existing[0]))
                else:
                    cur.execute("SELECT IndustryID FROM dbo.Industries WHERE IndustryName=?", name)
                    if cur.fetchone():
                        errors.append(f"Industry name already belongs to another code: {name}")
                        continue
                    cur.execute("""INSERT INTO dbo.Industries(IndustryCode,IndustryName,IsActive)
                        OUTPUT INSERTED.IndustryID VALUES(?,?,1)""", code, name)
                    industry_ids[code] = int(cur.fetchone()[0])
                    inserted_industries += 1

            company_ids = {}
            for company_code, entries in grouped.items():
                industry_codes = {str(item[0]["industry_code"]) for item in entries}
                if len(industry_codes) != 1:
                    industry_conflicts += 1
                    errors.append(f"Company code {company_code} has multiple official industry codes: {sorted(industry_codes)}")
                    continue
                industry_code = next(iter(industry_codes))
                industry_id = industry_ids.get(industry_code)
                if industry_id is None:
                    errors.append(f"Company code {company_code}: industry code {industry_code} is absent from TSETMC static data.")
                    continue

                cur.execute("""SELECT CompanyID,IndustryID,Symbol FROM dbo.Companies WITH (UPDLOCK,HOLDLOCK)
                    WHERE TSETMCCompanyCode=?""", company_code)
                existing = cur.fetchone()
                if existing:
                    company_id = int(existing[0])
                    if int(existing[1]) != industry_id:
                        industry_conflicts += 1
                        errors.append(f"Company code {company_code}: existing IndustryID conflicts with TSETMC industry {industry_code}.")
                        continue
                    existing_companies += 1
                else:
                    ordered = sorted((item[1] for item in entries), key=lambda row: (str(row["symbol"]), str(row["ins_code"])))
                    representative = ordered[0]
                    # First try to adopt a pre-existing Companies row by exact
                    # representative-symbol identity, never by company-name similarity.
                    matching_ids = set()
                    for candidate in ordered:
                        cur.execute("SELECT CompanyID,IndustryID,TSETMCCompanyCode FROM dbo.Companies WITH (UPDLOCK,HOLDLOCK) WHERE Symbol=?", candidate["symbol"])
                        owner = cur.fetchone()
                        if owner:
                            matching_ids.add((int(owner[0]), int(owner[1]), str(owner[2] or "").strip()))
                    if len(matching_ids) > 1:
                        symbol_conflicts += 1
                        errors.append(f"Company code {company_code}: TSETMC symbols map to multiple pre-existing Companies rows.")
                        continue
                    if matching_ids:
                        owner_id, owner_industry, owner_code = next(iter(matching_ids))
                        if owner_code and owner_code != company_code:
                            symbol_conflicts += 1
                            errors.append(f"Company code {company_code}: existing Companies row has a different official company code {owner_code}.")
                            continue
                        if owner_industry != industry_id:
                            industry_conflicts += 1
                            errors.append(f"Company code {company_code}: existing Companies row has an IndustryID conflicting with TSETMC.")
                            continue
                        cur.execute("UPDATE dbo.Companies SET TSETMCCompanyCode=?,IsActive=1 WHERE CompanyID=? AND TSETMCCompanyCode IS NULL",
                                    company_code, owner_id)
                        company_id = owner_id
                        existing_companies += 1
                    else:
                        names = [str(item[1]["company_name"] or "").strip() for item in entries]
                        company_name = next((name for name in names if name), "") or str(representative["symbol"])
                        cur.execute("""INSERT INTO dbo.Companies
                            (Symbol,CompanyName,IndustryID,TSETMCCompanyCode,IsActive)
                            OUTPUT INSERTED.CompanyID VALUES(?,?,?,?,1)""",
                            representative["symbol"], company_name, industry_id, company_code)
                        company_id = int(cur.fetchone()[0])
                        inserted_companies += 1
                company_ids[company_code] = company_id

            for company_code, entries in grouped.items():
                company_id = company_ids.get(company_code)
                if company_id is None:
                    continue
                for identity, symbol in entries:
                    cur.execute("SELECT CompanyID FROM dbo.MarketSymbols WHERE InsCode=?", identity["ins_code"])
                    market_row = cur.fetchone()
                    if not market_row:
                        unlinked_symbols += 1
                        continue
                    current_company_id = market_row[0]
                    if current_company_id is None:
                        cur.execute("UPDATE dbo.MarketSymbols SET CompanyID=? WHERE InsCode=? AND CompanyID IS NULL",
                                    company_id, identity["ins_code"])
                        linked_symbols += 1
                    elif int(current_company_id) == company_id:
                        pass
                    else:
                        company_conflicts += 1
                        errors.append(f"InsCode {identity['ins_code']} is already linked to CompanyID {current_company_id}; expected {company_id}.")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        **fetch_result,
        "industries_from_tsetmc": len(industries),
        "industries_inserted": inserted_industries,
        "industries_updated": updated_industries,
        "companies_inserted": inserted_companies,
        "companies_existing": existing_companies,
        "symbols_linked": linked_symbols,
        "symbols_without_market_row": unlinked_symbols,
        "industry_conflicts": industry_conflicts,
        "company_link_conflicts": company_conflicts,
        "representative_symbol_conflicts": symbol_conflicts,
        "errors": errors[:30],
    }

def preview_tsetmc_registry(batch_size=1000, max_workers=12):
    """Read-only SQL Server preview; identity cache is refreshed locally."""
    industries = fetch_industries()
    fetch_runs = []
    for _ in range(3):
        fetch_result = _load_and_cache_identities(batch_size=batch_size, max_workers=max_workers)
        fetch_runs.append(fetch_result)
        if fetch_result["remaining_identities"] == 0 or fetch_result["attempted"] == 0:
            break
    fetch_result = {
        "attempted": sum(x["attempted"] for x in fetch_runs),
        "successfully_resolved": sum(x["successfully_resolved"] for x in fetch_runs),
        "total_official_symbols": fetch_runs[-1]["total_official_symbols"],
        "cached_identities": fetch_runs[-1]["cached_identities"],
        "remaining_identities": fetch_runs[-1]["remaining_identities"],
        "errors": [e for x in fetch_runs for e in x["errors"]][:20],
    }
    official_industries = {str(x["industry_code"]): x["industry_name"] for x in industries}
    with sqlite_connect() as db:
        symbols = [dict(r) for r in db.execute(
            "SELECT ins_code,symbol,company_name FROM market_symbols WHERE instrument_code_is_placeholder=0 AND ins_code GLOB '[0-9]*'").fetchall()]
        identities = [dict(r) for r in db.execute(
            "SELECT ins_code,company_code,industry_code,industry_name FROM tsetmc_identity_cache WHERE status='SUCCESS' AND company_key_source='cSocCSAC'").fetchall()]
    cs = get_connection_string()
    if not cs:
        raise RuntimeError("اتصال SQL Server تنظیم نشده است.")
    import pyodbc
    with pyodbc.connect(cs, timeout=20) as conn:
        cur = conn.cursor()
        cur.execute("SELECT CompanyID,IndustryID,Symbol,CompanyName,TSETMCCompanyCode FROM dbo.Companies")
        companies = [dict(zip(("CompanyID","IndustryID","Symbol","CompanyName","TSETMCCompanyCode"), r)) for r in cur.fetchall()]
        cur.execute("SELECT IndustryID,IndustryCode,IndustryName FROM dbo.Industries")
        db_industries = [dict(zip(("IndustryID","IndustryCode","IndustryName"), r)) for r in cur.fetchall()]
        cur.execute("SELECT InsCode,CompanyID FROM dbo.MarketSymbols")
        market = {str(r[0]): r[1] for r in cur.fetchall()}
    symbols_by_ins = {str(r["ins_code"]): r for r in symbols}
    industries_by_code = {str(r["IndustryCode"] or "").strip(): r for r in db_industries}
    companies_by_key = {str(r["TSETMCCompanyCode"] or "").strip(): r for r in companies if r["TSETMCCompanyCode"]}
    grouped = {}
    for identity in identities:
        key = str(identity["company_code"] or "").strip()
        symbol = symbols_by_ins.get(str(identity["ins_code"]))
        if key and symbol:
            grouped.setdefault(key, []).append((identity, symbol))
    details = []
    counts = {"existing": 0, "insert": 0, "adopt": 0, "review": 0, "industry_conflict": 0, "industry_missing": 0,
              "industry_to_insert": 0, "symbol_link_conflict": 0, "linkable_symbols": 0}
    for key, rows in sorted(grouped.items()):
        codes = sorted({str(x[0]["industry_code"] or "").strip() for x in rows if x[0]["industry_code"]})
        conflict = len(codes) > 1
        code = codes[0] if len(codes) == 1 else ""
        industry = industries_by_code.get(code)
        official_industry_name = official_industries.get(code, "")
        existing = companies_by_key.get(key)
        if conflict:
            counts["industry_conflict"] += 1
        elif not code:
            counts["industry_missing"] += 1
        elif not official_industry_name:
            counts["industry_to_insert"] += 1
        action = "EXISTING" if existing else "INSERT"
        company_id = int(existing["CompanyID"]) if existing else None
        symbol_names = sorted({str(x[1]["symbol"] or "").strip() for x in rows if x[1]["symbol"]})
        if existing:
            counts["existing"] += 1
        else:
            owners = [c for c in companies if str(c["Symbol"] or "").strip() in symbol_names]
            owner_ids = {int(c["CompanyID"]) for c in owners}
            owner_keys = {str(c["TSETMCCompanyCode"] or "").strip() for c in owners if c["TSETMCCompanyCode"]}
            if len(owner_ids) > 1 or (owner_keys and owner_keys != {key}):
                action = "REVIEW"
                counts["review"] += 1
            elif owners:
                owner = owners[0]
                if owner["IndustryID"] is not None and industry and int(owner["IndustryID"]) != int(industry["IndustryID"]):
                    action = "REVIEW"
                    counts["review"] += 1
                else:
                    action = "ADOPT"
                    company_id = int(owner["CompanyID"])
                    counts["adopt"] += 1
            else:
                counts["insert"] += 1
        issue = "تعارض کد صنعت؛ صنعت متصل نمی‌شود" if conflict else (
            "کد صنعت موجود نیست؛ اتصال صنعت به تعویق می‌افتد" if not code else
            "کد صنعت در جدول Industries یافت نشد" if not industry else "")
        if existing and industry and existing["IndustryID"] is not None and int(existing["IndustryID"]) != int(industry["IndustryID"]):
            issue = "IndustryID موجود با صنعت رسمی مغایرت دارد"
            counts["industry_conflict"] += 1
        ins_codes = sorted({str(x[0]["ins_code"]) for x in rows})
        for ins in ins_codes:
            current = market.get(ins)
            if current is None or (company_id is not None and int(current) == company_id):
                counts["linkable_symbols"] += 1
            elif company_id is not None:
                counts["symbol_link_conflict"] += 1
                issue = "حداقل یک نماد از قبل به شرکت دیگری متصل است"
        details.append({"cSocCSAC": key, "symbols": ", ".join(symbol_names),
                        "industry_code": code or "—", "industry_name": (industry["IndustryName"] if industry else official_industry_name or "—"),
                        "action": action, "company_id": company_id or "—", "issue": issue})
    return {"summary": {"official_symbols": len(symbols), "identities_resolved": len(identities),
                        "companies_detected": len(grouped), **counts,
                        "identities_remaining": fetch_result["remaining_identities"]},
            "fetch": fetch_result, "companies": details[:250],
            "companies_truncated": max(0, len(details)-250)}
