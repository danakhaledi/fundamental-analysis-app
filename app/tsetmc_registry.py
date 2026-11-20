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
    """Extract issuer, parent sector and lowest-level official TSETMC subsector."""
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
    sub_sector = identity.get("subSector")
    if not isinstance(sub_sector, dict):
        sub_sector = {}
    parent_code = _clean_code(sector.get("cSecVal"))
    parent_name = str(sector.get("lSecVal") or "").strip()
    child_code = _clean_code(sub_sector.get("cSoSecVal"))
    child_name = str(sub_sector.get("lSoSecVal") or "").strip()
    return {
        "ins_code": str(ins_code), "company_code": company_code,
        "industry_code": child_code or parent_code, "industry_name": child_name or parent_name,
        "parent_industry_code": parent_code, "parent_industry_name": parent_name,
        "sub_industry_code": child_code, "sub_industry_name": child_name,
        "raw_json": json.dumps(payload, ensure_ascii=False),
    }


def build_industry_hierarchy(identities):
    """Build official parent/child rows from sector and subSector identity fields."""
    parents, children = {}, {}
    def add(mapping, code, name, label):
        code, name = _clean_code(code), str(name or "").strip()
        if not code or not name:
            raise ValueError(f"TSETMC {label} code/name is missing.")
        if code in mapping and mapping[code] != name:
            raise ValueError(f"TSETMC has conflicting names for {label} code {code}.")
        mapping[code] = name
    for identity in identities:
        parent_code = _clean_code(identity.get("parent_industry_code"))
        parent_name = identity.get("parent_industry_name")
        child_code = _clean_code(identity.get("sub_industry_code"))
        child_name = identity.get("sub_industry_name")
        if not parent_code:
            if child_code:
                raise ValueError(f"Subindustry {child_code} has no official parent sector code.")
            continue
        add(parents, parent_code, parent_name, "parent industry")
        if child_code and child_code != parent_code:
            add(children, child_code, child_name, "subindustry")
            prior_parent = children.get(child_code + "\0parent")
            if prior_parent and prior_parent != parent_code:
                raise ValueError(f"Subindustry {child_code} is linked to multiple parent sectors.")
            children[child_code + "\0parent"] = parent_code
    result = [{"industry_code": c, "industry_name": n, "parent_industry_code": None}
              for c, n in parents.items()]
    for code, name in children.items():
        if code.endswith("\0parent"):
            continue
        parent_code = children.get(code + "\0parent")
        if parent_code not in parents:
            raise ValueError(f"Subindustry {code} references unknown parent sector {parent_code}.")
        result.append({"industry_code": code, "industry_name": name, "parent_industry_code": parent_code})
    return sorted(result, key=lambda row: (row["parent_industry_code"] is not None, row["industry_code"]))


def _parse_cached_identities(rows):
    """Re-read raw payloads so legacy cache columns cannot hide subsector codes."""
    parsed_rows = []
    for row in rows:
        try:
            payload = json.loads(row.get("raw_json") or "{}")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid cached TSETMC identity JSON for InsCode {row.get('ins_code')}: {exc}")
        parsed = parse_instrument_identity(payload, row["ins_code"])
        cached_code = _clean_code(row.get("company_code"))
        if cached_code and parsed["company_code"] != cached_code:
            raise ValueError(f"Cached cSocCSAC mismatch for InsCode {row['ins_code']}.")
        parsed_rows.append(parsed)
    return parsed_rows

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
            WHERE s.is_active=1 AND s.ins_code<>'' AND s.ins_code NOT GLOB '*[^0-9]*'
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
                    (identity["ins_code"], error))
            else:
                db.execute("""INSERT OR REPLACE INTO tsetmc_identity_cache
                    (ins_code,company_code,company_key_source,industry_code,industry_name,raw_json,status,error,updated_at)
                    VALUES(?,?,'cSocCSAC',?,?,?,'SUCCESS',NULL,CURRENT_TIMESTAMP)""",
                    (
                        identity["ins_code"],
                        identity["company_code"],
                        identity["industry_code"],
                        identity["industry_name"],
                        identity["raw_json"]
                    ))

        total = db.execute("""SELECT COUNT(*) FROM market_symbols
            WHERE is_active=1 AND ins_code<>'' AND ins_code NOT GLOB '*[^0-9]*'""").fetchone()[0]
        successful = db.execute("""
            SELECT COUNT(*)
            FROM market_symbols s
            INNER JOIN tsetmc_identity_cache c ON c.ins_code = s.ins_code
            WHERE s.is_active=1 AND s.ins_code<>'' AND s.ins_code NOT GLOB '*[^0-9]*'
              AND c.status = 'SUCCESS'
              AND c.company_key_source = 'cSocCSAC'
            """).fetchone()[0]
        remaining = db.execute("""SELECT COUNT(*) FROM market_symbols s
            LEFT JOIN tsetmc_identity_cache c ON c.ins_code=s.ins_code
            WHERE s.is_active=1 AND s.ins_code<>'' AND s.ins_code NOT GLOB '*[^0-9]*'
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
    """Sync the complete observed TSETMC hierarchy, companies and symbol links."""
    fetch_result = _load_and_cache_identities(batch_size=batch_size, max_workers=max_workers)
    if fetch_result["remaining_identities"]:
        return {
            **fetch_result, "sync_deferred": True,
            "industries_from_tsetmc": 0, "parent_industries_from_tsetmc": 0,
            "subindustries_from_tsetmc": 0, "industries_inserted": 0,
            "industries_updated": 0, "companies_inserted": 0,
            "companies_existing": 0, "companies_reclassified": 0,
            "symbols_linked": 0, "symbols_without_market_row": 0,
            "industry_conflicts": 0, "company_link_conflicts": 0,
            "representative_symbol_conflicts": 0, "errors": list(fetch_result["errors"]),
        }
    cs = get_connection_string()
    if not cs:
        raise RuntimeError("اتصال SQL Server تنظیم نشده است.")
    import pyodbc
    with sqlite_connect() as db:
        symbols = [dict(r) for r in db.execute("""SELECT ins_code,symbol,company_name
            FROM market_symbols WHERE is_active=1 AND ins_code<>'' AND ins_code NOT GLOB '*[^0-9]*'
            ORDER BY symbol,ins_code""").fetchall()]
        cached = [dict(r) for r in db.execute("""SELECT ins_code,company_code,raw_json
            FROM tsetmc_identity_cache WHERE status='SUCCESS' AND company_key_source='cSocCSAC'
            ORDER BY company_code,ins_code""").fetchall()]
    symbol_by_ins = {str(r["ins_code"]): r for r in symbols}
    identities = _parse_cached_identities([r for r in cached if str(r["ins_code"]) in symbol_by_ins])
    industries = build_industry_hierarchy(identities)
    grouped = {}
    for identity in identities:
        code = str(identity["company_code"] or "").strip()
        symbol = symbol_by_ins.get(str(identity["ins_code"]))
        if code and symbol:
            grouped.setdefault(code, []).append((identity, symbol))
    inserted_industries = updated_industries = 0
    inserted_companies = existing_companies = companies_reclassified = 0
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
            def upsert_industry(row, parent_id):
                nonlocal inserted_industries, updated_industries
                code, name = row["industry_code"], row["industry_name"]
                cur.execute("""SELECT IndustryID,IndustryName,ParentIndustryRef
                    FROM dbo.Industries WITH (UPDLOCK,HOLDLOCK) WHERE IndustryCode=?""", code)
                existing = cur.fetchone()
                if existing:
                    industry_id = int(existing[0])
                    if str(existing[1]).strip() != name:
                        cur.execute("""SELECT IndustryID FROM dbo.Industries
                            WHERE IndustryName=? AND IndustryCode<>?""", name, code)
                        if cur.fetchone():
                            errors.append(f"Industry name conflict for official code {code}: {name}")
                            return None
                    if str(existing[1]).strip() != name or existing[2] != parent_id:
                        cur.execute("""UPDATE dbo.Industries SET IndustryName=?,ParentIndustryRef=?,IsActive=1
                            WHERE IndustryID=?""", name, parent_id, industry_id)
                        updated_industries += 1
                    else:
                        cur.execute("UPDATE dbo.Industries SET IsActive=1 WHERE IndustryID=?", industry_id)
                    return industry_id
                cur.execute("SELECT IndustryID FROM dbo.Industries WHERE IndustryName=?", name)
                if cur.fetchone():
                    errors.append(f"Industry name already belongs to another code: {name}")
                    return None
                cur.execute("""INSERT INTO dbo.Industries
                    (IndustryCode,IndustryName,ParentIndustryRef,IsActive)
                    OUTPUT INSERTED.IndustryID VALUES(?,?,?,1)""", code, name, parent_id)
                inserted_industries += 1
                return int(cur.fetchone()[0])
            for row in industries:
                if row["parent_industry_code"] is None:
                    value = upsert_industry(row, None)
                    if value is not None:
                        industry_ids[row["industry_code"]] = value
            for row in industries:
                parent_code = row["parent_industry_code"]
                if parent_code is None:
                    continue
                parent_id = industry_ids.get(parent_code)
                if parent_id is None:
                    errors.append(f"Subindustry {row['industry_code']}: parent {parent_code} was not synchronized.")
                    continue
                value = upsert_industry(row, parent_id)
                if value is not None:
                    industry_ids[row["industry_code"]] = value
            company_ids = {}
            for company_code, entries in grouped.items():
                codes = {str(x[0]["industry_code"] or "") for x in entries}
                if len(codes) != 1 or not next(iter(codes), ""):
                    industry_conflicts += 1
                    errors.append(f"Company code {company_code} has multiple or missing lowest-level industry codes: {sorted(codes)}")
                    continue
                industry_code = next(iter(codes))
                industry_id = industry_ids.get(industry_code)
                if industry_id is None:
                    errors.append(f"Company code {company_code}: industry {industry_code} is absent from synchronized hierarchy.")
                    continue
                cur.execute("""SELECT CompanyID,IndustryID,Symbol FROM dbo.Companies WITH (UPDLOCK,HOLDLOCK)
                    WHERE TSETMCCompanyCode=?""", company_code)
                existing = cur.fetchone()
                if existing:
                    company_id = int(existing[0])
                    if existing[1] is None or int(existing[1]) != industry_id:
                        cur.execute("UPDATE dbo.Companies SET IndustryID=? WHERE CompanyID=?", industry_id, company_id)
                        companies_reclassified += 1
                    existing_companies += 1
                else:
                    ordered = sorted((x[1] for x in entries), key=lambda r: (str(r["symbol"]), str(r["ins_code"])))
                    representative = ordered[0]
                    matches = set()
                    for candidate in ordered:
                        cur.execute("""SELECT CompanyID,IndustryID,TSETMCCompanyCode FROM dbo.Companies WITH (UPDLOCK,HOLDLOCK)
                            WHERE Symbol=?""", candidate["symbol"])
                        owner = cur.fetchone()
                        if owner:
                            matches.add((int(owner[0]), owner[1], str(owner[2] or "").strip()))
                    if len(matches) > 1:
                        symbol_conflicts += 1
                        errors.append(f"Company code {company_code}: symbols map to multiple existing Companies rows.")
                        continue
                    if matches:
                        owner_id, owner_industry, owner_code = next(iter(matches))
                        if owner_code and owner_code != company_code:
                            symbol_conflicts += 1
                            errors.append(f"Company code {company_code}: existing Companies row has a different official code {owner_code}.")
                            continue
                        cur.execute("""UPDATE dbo.Companies SET TSETMCCompanyCode=?,IndustryID=?,IsActive=1
                            WHERE CompanyID=? AND (TSETMCCompanyCode IS NULL OR TSETMCCompanyCode=?)""",
                            company_code, industry_id, owner_id, company_code)
                        company_id = owner_id
                        if owner_industry is None or int(owner_industry) != industry_id:
                            companies_reclassified += 1
                        existing_companies += 1
                    else:
                        names = [str(x[1]["company_name"] or "").strip() for x in entries]
                        company_name = next((n for n in names if n), "") or str(representative["symbol"])
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
                    current = market_row[0]
                    if current is None:
                        cur.execute("""UPDATE dbo.MarketSymbols SET CompanyID=?
                            WHERE InsCode=? AND CompanyID IS NULL""", company_id, identity["ins_code"])
                        linked_symbols += 1
                    elif int(current) != company_id:
                        company_conflicts += 1
                        errors.append(f"InsCode {identity['ins_code']} is already linked to CompanyID {current}; expected {company_id}.")
            if errors or industry_conflicts or company_conflicts or symbol_conflicts or unlinked_symbols:
                raise RuntimeError(
                    "TSETMC registry validation failed; SQL Server transaction rolled back: "
                    + " | ".join(errors[:5])
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    parent_count = sum(1 for row in industries if row["parent_industry_code"] is None)
    return {
        **fetch_result, "sync_deferred": False,
        "industries_from_tsetmc": len(industries), "parent_industries_from_tsetmc": parent_count,
        "subindustries_from_tsetmc": len(industries) - parent_count,
        "industries_inserted": inserted_industries, "industries_updated": updated_industries,
        "companies_inserted": inserted_companies, "companies_existing": existing_companies,
        "companies_reclassified": companies_reclassified, "symbols_linked": linked_symbols,
        "symbols_without_market_row": unlinked_symbols, "industry_conflicts": industry_conflicts,
        "company_link_conflicts": company_conflicts, "representative_symbol_conflicts": symbol_conflicts,
        "errors": errors[:30],
    }

def preview_tsetmc_registry(batch_size=1000, max_workers=12):
    """Read-only preview using the same TSETMC parent/child mapping as registry sync."""
    runs = []
    for _ in range(3):
        item = _load_and_cache_identities(batch_size=batch_size, max_workers=max_workers)
        runs.append(item)
        if item["remaining_identities"] == 0 or item["attempted"] == 0:
            break
    fetch_result = {
        "attempted": sum(x["attempted"] for x in runs),
        "successfully_resolved": sum(x["successfully_resolved"] for x in runs),
        "total_official_symbols": runs[-1]["total_official_symbols"],
        "cached_identities": runs[-1]["cached_identities"],
        "remaining_identities": runs[-1]["remaining_identities"],
        "errors": [e for x in runs for e in x["errors"]][:20],
    }
    with sqlite_connect() as db:
        symbols = [dict(r) for r in db.execute("""SELECT ins_code,symbol,company_name FROM market_symbols
            WHERE is_active=1 AND ins_code<>'' AND ins_code NOT GLOB '*[^0-9]*'""").fetchall()]
        cached = [dict(r) for r in db.execute("""SELECT ins_code,company_code,raw_json FROM tsetmc_identity_cache
            WHERE status='SUCCESS' AND company_key_source='cSocCSAC'""").fetchall()]
    symbols_by_ins = {str(r["ins_code"]): r for r in symbols}
    identities = _parse_cached_identities([r for r in cached if str(r["ins_code"]) in symbols_by_ins])
    hierarchy = build_industry_hierarchy(identities)
    official = {str(r["industry_code"]): r["industry_name"] for r in hierarchy}
    cs = get_connection_string()
    if not cs:
        raise RuntimeError("اتصال SQL Server تنظیم نشده است.")
    import pyodbc
    with pyodbc.connect(cs, timeout=20) as conn:
        cur = conn.cursor()
        cur.execute("SELECT CompanyID,IndustryID,Symbol,CompanyName,TSETMCCompanyCode FROM dbo.Companies")
        companies = [dict(zip(("CompanyID","IndustryID","Symbol","CompanyName","TSETMCCompanyCode"), r)) for r in cur.fetchall()]
        cur.execute("SELECT IndustryID,IndustryCode,IndustryName,ParentIndustryRef FROM dbo.Industries")
        db_industries = [dict(zip(("IndustryID","IndustryCode","IndustryName","ParentIndustryRef"), r)) for r in cur.fetchall()]
        cur.execute("SELECT InsCode,CompanyID FROM dbo.MarketSymbols")
        market = {str(r[0]): r[1] for r in cur.fetchall()}
    industry_by_code = {str(r["IndustryCode"] or "").strip(): r for r in db_industries}
    company_by_code = {str(r["TSETMCCompanyCode"] or "").strip(): r for r in companies if r["TSETMCCompanyCode"]}
    grouped = {}
    for identity in identities:
        key = str(identity["company_code"] or "").strip()
        symbol = symbols_by_ins.get(str(identity["ins_code"]))
        if key and symbol:
            grouped.setdefault(key, []).append((identity, symbol))
    details = []
    counts = {"existing": 0, "insert": 0, "adopt": 0, "reclassify": 0, "review": 0,
              "industry_conflict": 0, "industry_missing": 0, "industry_to_insert": 0,
              "industry_unknown": 0, "symbol_link_conflict": 0, "linkable_symbols": 0}
    for key, rows in sorted(grouped.items()):
        codes = sorted({str(x[0]["industry_code"] or "").strip() for x in rows if x[0]["industry_code"]})
        conflict = len(codes) > 1
        code = codes[0] if len(codes) == 1 else ""
        industry = industry_by_code.get(code)
        name = official.get(code, "")
        existing = company_by_code.get(key)
        if conflict: counts["industry_conflict"] += 1
        elif not code: counts["industry_missing"] += 1
        elif not name: counts["industry_unknown"] += 1
        elif not industry: counts["industry_to_insert"] += 1
        action = "EXISTING" if existing else "INSERT"
        company_id = int(existing["CompanyID"]) if existing else None
        issue = ""
        if existing:
            counts["existing"] += 1
            if industry and (existing["IndustryID"] is None or int(existing["IndustryID"]) != int(industry["IndustryID"])):
                action, issue = "RECLASSIFY", "IndustryID با پایین‌ترین سطح رسمی TSETMC همسان نیست و در Sync اصلاح می‌شود"
                counts["reclassify"] += 1
        else:
            names = {str(x[1]["symbol"] or "").strip() for x in rows if x[1]["symbol"]}
            owners = [c for c in companies if str(c["Symbol"] or "").strip() in names]
            owner_ids = {int(c["CompanyID"]) for c in owners}
            owner_keys = {str(c["TSETMCCompanyCode"] or "").strip() for c in owners if c["TSETMCCompanyCode"]}
            if len(owner_ids) > 1 or (owner_keys and owner_keys != {key}):
                action = "REVIEW"; counts["review"] += 1
            elif owners:
                owner = owners[0]; action = "ADOPT"; company_id = int(owner["CompanyID"]); counts["adopt"] += 1
                if industry and (owner["IndustryID"] is None or int(owner["IndustryID"]) != int(industry["IndustryID"])):
                    issue = "IndustryID رکورد موجود هنگام اتصال با طبقه‌بندی رسمی اصلاح می‌شود"
        symbol_names = sorted({str(x[1]["symbol"] or "").strip() for x in rows if x[1]["symbol"]})
        for identity, _symbol in rows:
            ins = str(identity["ins_code"])
            if action != "REVIEW" and ins in market and (market[ins] is None or
                    (company_id is not None and int(market[ins]) == company_id)):
                counts["linkable_symbols"] += 1
            elif ins in market and market[ins] is not None and (company_id is None or int(market[ins]) != company_id):
                counts["symbol_link_conflict"] += 1; issue = "حداقل یک نماد از قبل به شرکت دیگری متصل است"
        details.append({"cSocCSAC": key, "symbols": ", ".join(symbol_names), "industry_code": code or "—",
                        "industry_name": name or (industry["IndustryName"] if industry else "—"),
                        "action": action, "company_id": company_id or "—",
                        "issue": issue or ("تعارض کد صنعت؛ اتصال نیازمند بررسی است" if conflict else "")})
    parents = sum(1 for r in hierarchy if r["parent_industry_code"] is None)
    return {"summary": {"official_symbols": len(symbols), "identities_resolved": len(identities),
                        "parent_industries": parents, "subindustries": len(hierarchy) - parents,
                        "companies_detected": len(grouped), **counts,
                        "identities_remaining": fetch_result["remaining_identities"]},
            "fetch": fetch_result, "companies": details,
            "companies_truncated": 0}

