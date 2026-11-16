
import json

from app.tsetmc_registry import fetch_instrument_identity


# چهار نماد رسمی بوعلی
INSTRUMENTS = {
    "50247622569476338": "بوعلی",
    "24533545253247926": "بوعلی2",
    "27208339390162910": "بوعلی3",
    "48128383807088318": "بوعلی4",
}

EXPECTED_COMPANY_CODE = "BONA"
EXPECTED_SECTOR_CODE = "44"
EXPECTED_SUBSECTOR_CODE = "4411"


results = []
errors = []

print("\n========== BUALI TSETMC IDENTITY AUDIT ==========")

for ins_code, expected_symbol in INSTRUMENTS.items():
    print(f"\nChecking: {expected_symbol} | InsCode: {ins_code}")

    try:
        result = fetch_instrument_identity(ins_code)
        payload = json.loads(result["raw_json"])

        identity = payload.get("instrumentIdentity") or {}
        sector = identity.get("sector") or {}
        subsector = identity.get("subSector") or {}

        row = {
            "symbol": expected_symbol,
            "ins_code": ins_code,
            "company_code": str(
                identity.get("cSocCSAC") or ""
            ).strip(),
            "sector_code": str(
                sector.get("cSecVal") or ""
            ).strip(),
            "sector_name": str(
                sector.get("lSecVal") or ""
            ).strip(),
            "subsector_code": str(
                subsector.get("cSoSecVal") or ""
            ).strip(),
            "subsector_name": str(
                subsector.get("lSoSecVal") or ""
            ).strip(),
        }

        results.append(row)

        for key, value in row.items():
            print(f"{key}: {value}")

    except Exception as exc:
        errors.append({
            "symbol": expected_symbol,
            "ins_code": ins_code,
            "error": repr(exc),
        })
        print("ERROR:", repr(exc))


# --------------------------------------------------
# Validation
# --------------------------------------------------

print("\n========== VALIDATION ==========")

checks = {
    "All four instruments received": len(results) == 4,
    "No API/parsing errors": len(errors) == 0,
    "All company codes are BONA": (
        len(results) == 4
        and all(
            r["company_code"] == EXPECTED_COMPANY_CODE
            for r in results
        )
    ),
    "All sector codes are 44": (
        len(results) == 4
        and all(
            r["sector_code"] == EXPECTED_SECTOR_CODE
            for r in results
        )
    ),
    "All subsector codes are 4411": (
        len(results) == 4
        and all(
            r["subsector_code"] == EXPECTED_SUBSECTOR_CODE
            for r in results
        )
    ),
    "All sector names are consistent": (
        len({r["sector_name"] for r in results}) == 1
        and all(r["sector_name"] for r in results)
    ),
    "All subsector names are consistent": (
        len({r["subsector_name"] for r in results}) == 1
        and all(r["subsector_name"] for r in results)
    ),
}

for description, passed in checks.items():
    print(
        ("PASS" if passed else "REVIEW"),
        "|",
        description
    )

print("\n========== ERRORS ==========")

for error in errors:
    print(error)

print("\n========== FINAL RESULT ==========")

if all(checks.values()):
    print("PASS: BUALI INDUSTRY IDENTITY VERIFIED.")
else:
    print("REVIEW REQUIRED: DO NOT MIGRATE INDUSTRIES YET.")

print(
    "\nRead-only API audit. "
    "No SQL Server or SQLite writes were performed."
)
