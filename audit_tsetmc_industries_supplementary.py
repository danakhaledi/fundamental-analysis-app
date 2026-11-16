
import json
from collections import defaultdict

from app.authdb import connect


sector_names_by_code = defaultdict(set)
subsector_definitions_by_code = defaultdict(set)

companies = set()
missing_values = []
parse_errors = []

# --------------------------------------------------
# 1. Read successful identity records from SQLite
# --------------------------------------------------

with connect() as db:
    rows = db.execute("""
        SELECT
            ins_code,
            company_code,
            raw_json
        FROM tsetmc_identity_cache
        WHERE status = 'SUCCESS'
          AND company_key_source = 'cSocCSAC'
        ORDER BY company_code, ins_code
    """).fetchall()

    for row in rows:
        ins_code = str(row["ins_code"] or "").strip()
        company_code = str(row["company_code"] or "").strip()

        companies.add(company_code)

        try:
            payload = json.loads(row["raw_json"])
            identity = payload.get("instrumentIdentity") or {}

            sector = identity.get("sector") or {}
            subsector = identity.get("subSector") or {}

            sector_code = str(
                sector.get("cSecVal") or ""
            ).strip()

            sector_name = str(
                sector.get("lSecVal") or ""
            ).strip()

            subsector_code = str(
                subsector.get("cSoSecVal") or ""
            ).strip()

            subsector_name = str(
                subsector.get("lSoSecVal") or ""
            ).strip()

            # Missing values
            if not sector_code or not sector_name:
                missing_values.append((
                    "SECTOR",
                    company_code,
                    ins_code,
                    sector_code,
                    sector_name
                ))

            if not subsector_code or not subsector_name:
                missing_values.append((
                    "SUBSECTOR",
                    company_code,
                    ins_code,
                    subsector_code,
                    subsector_name
                ))

            # Collect definitions
            if sector_code and sector_name:
                sector_names_by_code[sector_code].add(
                    sector_name
                )

            if subsector_code:
                subsector_definitions_by_code[
                    subsector_code
                ].add((
                    subsector_name,
                    sector_code
                ))

        except Exception as exc:
            parse_errors.append((
                company_code,
                ins_code,
                repr(exc)
            ))


# --------------------------------------------------
# 2. Detect conflicting definitions
# --------------------------------------------------

sector_name_conflicts = {
    code: sorted(names)
    for code, names in sector_names_by_code.items()
    if len(names) > 1
}

subsector_definition_conflicts = {
    code: sorted(definitions)
    for code, definitions
    in subsector_definitions_by_code.items()
    if len(definitions) > 1
}


# --------------------------------------------------
# 3. Detect missing or unknown parent sectors
# --------------------------------------------------

known_sector_codes = set(sector_names_by_code)

orphan_subsectors = {}

for subsector_code, definitions in (
    subsector_definitions_by_code.items()
):
    missing_parents = sorted({
        sector_code
        for _, sector_code in definitions
        if (
            not sector_code
            or sector_code not in known_sector_codes
        )
    })

    if missing_parents:
        orphan_subsectors[subsector_code] = missing_parents


# --------------------------------------------------
# 4. Informational checks:
#    Same name with multiple codes
# --------------------------------------------------

sector_name_multi_codes = defaultdict(set)

for code, names in sector_names_by_code.items():
    for name in names:
        sector_name_multi_codes[name].add(code)

same_sector_name_different_codes = {
    name: sorted(codes)
    for name, codes in sector_name_multi_codes.items()
    if len(codes) > 1
}


subsector_name_multi_codes = defaultdict(set)

for code, definitions in (
    subsector_definitions_by_code.items()
):
    for name, _ in definitions:
        if name:
            subsector_name_multi_codes[name].add(code)

same_subsector_name_different_codes = {
    name: sorted(codes)
    for name, codes in subsector_name_multi_codes.items()
    if len(codes) > 1
}


# --------------------------------------------------
# 5. Print audit summary
# --------------------------------------------------

print("\n========== SUPPLEMENTARY INDUSTRY AUDIT ==========")

print("Resolved identity rows:", len(rows))
print("Unique companies:", len(companies))
print("Unique sector codes:", len(sector_names_by_code))
print(
    "Unique subsector codes:",
    len(subsector_definitions_by_code)
)

print("Missing sector/subsector values:", len(missing_values))
print("Sector code/name conflicts:", len(sector_name_conflicts))
print(
    "Subsector code/name/parent conflicts:",
    len(subsector_definition_conflicts)
)
print(
    "Subsectors with missing/unknown parent:",
    len(orphan_subsectors)
)
print("Parsing errors:", len(parse_errors))


# --------------------------------------------------
# 6. Detailed results
# --------------------------------------------------

print("\n========== SECTOR CODE / NAME CONFLICTS ==========")

for code, names in sorted(sector_name_conflicts.items()):
    print(code, "|", " <> ".join(names))


print("\n========== SUBSECTOR CODE / NAME / PARENT CONFLICTS ==========")

for code, definitions in sorted(
    subsector_definition_conflicts.items()
):
    print(code, "|", definitions)


print("\n========== SUBSECTORS WITH MISSING / UNKNOWN PARENT ==========")

for code, parents in sorted(orphan_subsectors.items()):
    print(code, "| Parent codes:", parents)


print("\n========== MISSING SECTOR / SUBSECTOR VALUES ==========")

for item in missing_values:
    print(item)


print("\n========== SAME SECTOR NAME WITH MULTIPLE CODES (INFO) ==========")

for name, codes in sorted(
    same_sector_name_different_codes.items()
):
    print(name, "|", codes)


print("\n========== SAME SUBSECTOR NAME WITH MULTIPLE CODES (INFO) ==========")

for name, codes in sorted(
    same_subsector_name_different_codes.items()
):
    print(name, "|", codes)


print("\n========== PARSING ERRORS ==========")

for item in parse_errors:
    print(item)


# --------------------------------------------------
# 7. Final conclusion
# --------------------------------------------------

print("\n========== AUDIT CONCLUSION ==========")

if (
    not missing_values
    and not sector_name_conflicts
    and not subsector_definition_conflicts
    and not orphan_subsectors
    and not parse_errors
):
    print(
        "PASS: No structural inconsistency found "
        "in audited cache rows."
    )
else:
    print(
        "REVIEW REQUIRED: Resolve reported issues "
        "before SQL migration."
    )

print(
    "Note: This audit covers only rows currently "
    "present in SQLite cache."
)

print(
    "BONA is not added by this script; its official "
    "instruments require a separate identity check."
)
