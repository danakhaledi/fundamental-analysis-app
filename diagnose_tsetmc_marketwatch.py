from app.market_symbols import (
    BASE,
    _request_json,
    _normalize_api,
    _fetch_legacy_market_watch,
)
from app.authdb import connect

TEST_CODES = {
    "50247622569476338",
    "24533545253247926",
    "27208339390162910",
    "48128383807088318",
}

URL = (
    BASE + "/ClosingPrice/GetMarketWatch?market=0&industrialGroup="
    "&paperTypes%5B0%5D=1&paperTypes%5B1%5D=2&paperTypes%5B2%5D=3"
    "&paperTypes%5B3%5D=4&paperTypes%5B4%5D=5&paperTypes%5B5%5D=6"
    "&paperTypes%5B6%5D=7&paperTypes%5B7%5D=8&paperTypes%5B8%5D=9"
    "&showTraded=false&withBestLimits=false&hEven=0&RefID=0"
)

def show_source(name, rows):
    by_code = {str(row.get("ins_code", "")): row for row in rows}
    print("")
    print("SOURCE:", name)
    print("TOTAL_NORMALIZED_ROWS:", len(rows))
    print("TEST_CODE_MATCHES:", len(TEST_CODES.intersection(by_code)))
    for code in sorted(TEST_CODES):
        row = by_code.get(code)
        if row:
            print(
                "FOUND:",
                code,
                "| SYMBOL:", repr(row.get("symbol")),
                "| COMPANY:", repr(row.get("company_name")),
                "| ISIN:", repr(row.get("isin")),
            )
        else:
            print("MISSING:", code)

    text_matches = [
        row for row in rows
        if "بوعلی" in str(row.get("symbol", ""))
        or "بوعلي" in str(row.get("symbol", ""))
        or "بوعلی" in str(row.get("company_name", ""))
        or "بوعلي" in str(row.get("company_name", ""))
        or "وبوعلی" in str(row.get("symbol", ""))
        or "وبوعلي" in str(row.get("symbol", ""))
    ]
    print("TEXT_MATCHES:", len(text_matches))
    for row in text_matches[:20]:
        print(
            "TEXT_ROW:",
            row.get("ins_code"),
            "|", repr(row.get("symbol")),
            "|", repr(row.get("company_name")),
        )

def main():
    print("READ_ONLY_DIAGNOSTIC: no database/API data will be written")

    try:
        payload = _request_json(URL, timeout=60)
        new_rows = _normalize_api(payload)
        show_source("NEW_GETMARKETWATCH_API", new_rows)
    except Exception as exc:
        print("NEW_GETMARKETWATCH_API_ERROR:", type(exc).__name__, str(exc)[:500])

    try:
        legacy_rows = _fetch_legacy_market_watch()
        show_source("LEGACY_MARKETWATCHINIT_API", legacy_rows)
    except Exception as exc:
        print("LEGACY_MARKETWATCHINIT_API_ERROR:", type(exc).__name__, str(exc)[:500])

    try:
        with connect() as db:
            rows = db.execute(
                """
                SELECT ins_code, symbol, company_name, is_active
                FROM market_symbols
                WHERE ins_code IN (?, ?, ?, ?)
                   OR symbol LIKE ?
                   OR symbol LIKE ?
                   OR company_name LIKE ?
                ORDER BY symbol
                """,
                (
                    *sorted(TEST_CODES),
                    "%بوعلی%",
                    "%وبوعلی%",
                    "%بوعلی%",
                ),
            ).fetchall()
        print("")
        print("SOURCE: LOCAL_SQLITE")
        print("LOCAL_MATCHES:", len(rows))
        for row in rows:
            print(dict(row))
    except Exception as exc:
        print("LOCAL_SQLITE_ERROR:", type(exc).__name__, str(exc)[:500])

if __name__ == "__main__":
    main()
