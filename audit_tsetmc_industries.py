import json
from collections import defaultdict
from app.authdb import connect


sectors = {}
subsectors = {}
company_groups = defaultdict(list)

errors = []
missing_sector_rows = []
missing_subsector_rows = []

with connect() as db:
    rows = db.execute("""
        SELECT ins_code, company_code, raw_json
        FROM tsetmc_identity_cache
        WHERE status = 'SUCCESS'
          AND company_key_source = 'cSocCSAC'
        ORDER BY company_code, ins_code
    """).fetchall()

    for row in rows:
        ins_code = row["ins_code"]
        company_code = row["company_code"]

        try:
            payload = json.loads(row["raw_json"])
            identity = payload.get("instrumentIdentity") or {}

            sector = identity.get("sector") or {}
            subsector = identity.get("subSector") or {}

            sector_code = str(sector.get("cSecVal") or "").strip()
            sector_name = str(sector.get("lSecVal") or "").strip()
            subsector_code = str(subsector.get("cSoSecVal") or "").strip()
            subsector_name = str(subsector.get("lSoSecVal") or "").strip()

            if sector_code:
                sectors[sector_code] = sector_name
            else:
                missing_sector_rows.append((company_code, ins_code))

            if subsector_code:
                existing = subsectors.get(subsector_code)
                current = {
                    "name": subsector_name,
                    "sector_code": sector_code,
                }
                if existing is not None and existing != current:
                    errors.append((
                        company_code,
                        ins_code,
                        f"Conflicting definition for subsector {subsector_code}: "
                        f"{existing} vs {current}"
                    ))
                else:
                    subsectors[subsector_code] = current
            else:
                missing_subsector_rows.append((company_code, ins_code))

            company_groups[company_code].append({
                "ins_code": ins_code,
                "sector_code": sector_code,
                "sector_name": sector_name,
                "subsector_code": subsector_code,
                "subsector_name": subsector_name,
            })

        except Exception as exc:
            errors.append((company_code, ins_code, repr(exc)))


sector_conflicts = []
subsector_conflicts = []
missing_sector_companies = []
missing_subsector_companies = []

for company_code, items in sorted(company_groups.items()):
    sector_codes = {x["sector_code"] for x in items if x["sector_code"]}
    subsector_codes = {x["subsector_code"] for x in items if x["subsector_code"]}

    if not sector_codes:
        missing_sector_companies.append(company_code)

    if not subsector_codes:
        missing_subsector_companies.append(company_code)

    if len(sector_codes) > 1:
        sector_conflicts.append((company_code, sorted(sector_codes)))

    if len(subsector_codes) > 1:
        subsector_conflicts.append((company_code, sorted(subsector_codes)))


print("\n========== IDENTITY AUDIT ==========")
print("Resolved identity rows:", len(rows))
print("Unique companies:", len(company_groups))
print("Unique sectors:", len(sectors))
print("Unique subsectors:", len(subsectors))
print("Companies without sector:", len(missing_sector_companies))
print("Companies without subsector:", len(missing_subsector_companies))
print("Sector conflicts:", len(sector_conflicts))
print("Subsector conflicts:", len(subsector_conflicts))
print("Rows with parsing/data errors:", len(errors))


print("\n========== SECTORS ==========")
for code, name in sorted(sectors.items()):
    print(f"{code} | {name}")


print("\n========== SUBSECTORS ==========")
for code, item in sorted(subsectors.items()):
    print(f"{code} | Parent: {item['sector_code']} | {item['name']}")


print("\n========== SECTOR CONFLICTS ==========")
for item in sector_conflicts:
    print(item)


print("\n========== SUBSECTOR CONFLICTS ==========")
for item in subsector_conflicts:
    print(item)


print("\n========== COMPANIES WITHOUT SECTOR ==========")
for item in missing_sector_companies:
    print(item)


print("\n========== COMPANIES WITHOUT SUBSECTOR ==========")
for item in missing_subsector_companies:
    print(item)


print("\n========== ROWS WITH MISSING CODES ==========")
print("Missing sector rows:", len(missing_sector_rows))
for item in missing_sector_rows:
    print(item)

print("Missing subsector rows:", len(missing_subsector_rows))
for item in missing_subsector_rows:
    print(item)


print("\n========== PARSING / DATA ERRORS ==========")
for item in errors:
    print(item)


print("\n========== BONA CHECK ==========")
bona_items = company_groups.get("BONA", [])
print("BONA identity rows in cache:", len(bona_items))
for item in bona_items:
    print(item)
