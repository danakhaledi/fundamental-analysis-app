"""Codal monthly report resource extraction and SQL staging.

Only Table_2 (purchases) and Table_3 (resource/energy consumption) are handled here.
Rows are staged, validated, then upserted to the final table only when the full report is valid.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .appconfig import get_connection_string
from .codal_registry import ensure_company_and_document

_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _digits(value: Any) -> str:
    return str(value or "").translate(_DIGIT_MAP)


def _norm(value: Any) -> str:
    text = str(value or "").strip().replace("ي", "ی").replace("ى", "ی").replace("ك", "ک")
    text = re.sub(r"[\u200c\u200e\u200f\s]+", "", text)
    return text.lower()


def _number(value: Any):
    """Parse a source numeric cell; empty/dash cells remain NULL, never zero."""
    if value is None:
        return None
    s = _digits(value).strip().replace("٬", ",").replace("٫", ".").replace(",", "")
    if s in ("", "-", "—", "–", "...", "…"):
        return None
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1]
    s = s.replace("−", "-")
    try:
        n = Decimal(s)
        return -n if negative else n
    except (InvalidOperation, TypeError, ValueError):
        return None


def _jalali_to_gregorian(jy: int, jm: int, jd: int):
    # Integer algorithm; returns (gy, gm, gd).
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33 + 3) // 4) + jd
    if jm < 7:
        days += (jm - 1) * 31
    else:
        days += (jm - 7) * 30 + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        gy += 100 * ((days - 1) // 36524)
        days = (days - 1) % 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    month_days = [0, 31, 29 if (gy % 4 == 0 and (gy % 100 != 0 or gy % 400 == 0)) else 28,
                  31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 1
    while gm <= 12 and gd > month_days[gm]:
        gd -= month_days[gm]
        gm += 1
    return gy, gm, gd


def _period(title: str):
    title = _digits(title)
    m = re.search(r"منتهی\s*به\s*(\d{4})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})", title)
    if not m:
        raise ValueError("تاریخ پایان دوره مالی از عنوان گزارش قابل تشخیص نیست.")
    year, month, day = map(int, m.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError("تاریخ پایان دوره مالی نامعتبر است.")
    return year, month, day



def _publish_date(report, fallback):
    raw = _digits(report.get("PublishDate", ""))
    match = re.search(r"(\d{4})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})", raw)
    if not match:
        return fallback
    gy, gm, gd = _jalali_to_gregorian(*map(int, match.groups()))
    return datetime(gy, gm, gd)

def _unit_code(raw: Any, product_unit: Any = None):
    # Preserve the unit stated by the report; never relabel kg as ton or otherwise convert.
    raw_text = str(raw or "").strip()
    u = _norm(raw_text)
    if not u:
        return str(product_unit).strip().upper() if product_unit else None
    if "و" in u and any(token in u for token in ("کیلوگرم", "عدد", "تن", "مترمکعب")):
        return None
    if "مگاوات" in u or u == "mwh": return "MWH"
    if "استاندارد" in u and ("مترمکعب" in u or "m3" in u or "sm3" in u): return "SM3"
    if "مترمکعب" in u or u in ("m3", "sm3"): return "M3" if u != "sm3" else "SM3"
    if u in ("تن", "ton", "tons"): return "TON"
    if "کیلوگرم" in u or u == "kg": return "KG"
    if "لیتر" in u or u == "l": return "L"
    if "عدد" in u or u == "pcs": return "PCS"
    if "کیلووات" in u or u == "kwh": return "KWH"
    return None


def _find_sheet(report, name):
    for sheet in report.get("Sheets", []) or []:
        if _norm(sheet.get("sheet")) == _norm(name):
            return sheet.get("rows") or []
    return []


def _resource_rows(report):
    """Return source-faithful rows: current-month values only, not YTD or forecast."""
    output = []
    year, month, _ = _period(report.get("Title", ""))

    # Table_2: columns 0/1 are name/unit; current-month purchase is the 4th 3-column block.
    purchase = _find_sheet(report, "Table_2")
    if purchase:
        top_header = purchase[0] if purchase else []
        if not any("خریددورهیکماهه" in _norm(cell) for cell in top_header):
            raise ValueError("ساختار Table_2 با الگوی خرید ماهانه مورد انتظار مطابقت ندارد؛ از استخراج با اندیس ثابت خودداری شد.")
        section = None
        for row_no, row in enumerate(purchase, start=1):
            row = list(row or [])
            label = str(row[0] or "").strip() if row else ""
            label_n = _norm(label)
            if "مواداولیهداخلی" in label_n:
                section = "DOMESTIC"
                continue
            if "مواداولیه وارداتی".replace(" ", "") in label_n or "مواداولیهوارداتی" in label_n:
                section = "IMPORT"
                continue
            if not section or not label or "جمع" in label_n or label_n in ("شرح", "ناممواد"):
                continue
            # Current-month block in the 20-column Codal Table_2 layout: 11,12,13.
            cells = (row + [None] * 20)[:20]
            adj_qty, adj_rate, adj_amount = (_number(cells[5]), _number(cells[6]), _number(cells[7]))
            qty, rate, amount = (_number(cells[11]), _number(cells[12]), _number(cells[13]))
            if qty is None and rate is None and amount is None and adj_qty is None and adj_rate is None and adj_amount is None:
                continue
            unit_raw = cells[1]
            if _norm(unit_raw) in ("--", "—", "-"):
                # Codal uses a dash in the unit column for section/subtotal rows.
                continue
            output.append({
                "sheet": "Table_2", "row_no": row_no, "name": label,
                "unit_raw": unit_raw, "unit": _unit_code(unit_raw),
                "transaction_type": "PURCHASE", "purchase_type": section,
                "resource_category": None, "year": year, "month": month,
                "quantity": qty, "rate": rate, "amount": amount,
                "adjustment_qty": adj_qty, "adjustment_rate": adj_rate, "adjustment_amount": adj_amount,
            })

    # Table_3: industry/category/resource/unit then seven 3-column blocks.
    # The current-month block is columns 13,14,15; later YTD/forecast blocks are excluded.
    consumption = _find_sheet(report, "Table_3")
    if consumption:
        top_header = consumption[0] if consumption else []
        if not any("دورهیکماهه" in _norm(cell) for cell in top_header):
            raise ValueError("ساختار Table_3 با الگوی مصرف ماهانه مورد انتظار مطابقت ندارد؛ از استخراج با اندیس ثابت خودداری شد.")
        for row_no, row in enumerate(consumption, start=1):
            cells = (list(row or []) + [None] * 26)[:26]
            category, name, unit_raw = cells[1], cells[2], cells[3]
            if not name or _norm(name) in ("نوعانرژی", "شرح"):
                continue
            name_n = _norm(name)
            if "جمع" in name_n or "کل" == name_n:
                continue
            adj_qty, adj_rate, adj_amount = (_number(cells[7]), _number(cells[8]), _number(cells[9]))
            qty, rate, amount = (_number(cells[13]), _number(cells[14]), _number(cells[15]))
            if qty is None and rate is None and amount is None and adj_qty is None and adj_rate is None and adj_amount is None:
                continue
            output.append({
                "sheet": "Table_3", "row_no": row_no, "name": str(name).strip(),
                "unit_raw": unit_raw, "unit": _unit_code(unit_raw),
                "transaction_type": "CONSUMPTION", "purchase_type": None,
                "resource_category": str(category).strip() if category not in (None, "") else None,
                "year": year, "month": month,
                "quantity": qty, "rate": rate, "amount": amount,
                "adjustment_qty": adj_qty, "adjustment_rate": adj_rate, "adjustment_amount": adj_amount,
            })
    return output


def stage_resource_reports(reports):
    """Resolve SQL metadata/mappings and replace only this report's resource Stage rows."""
    cs = get_connection_string()
    if not cs:
        raise RuntimeError("اتصال SQL Server تنظیم نشده است.")
    import pyodbc

    summaries, failures = [], []
    with pyodbc.connect(cs, timeout=15) as conn:
        cur = conn.cursor()
        # Validate all required objects before making any changes.
        for table in ("dbo.Companies", "dbo.Industries", "dbo.DataSources", "dbo.SourceDocuments", "dbo.ProductMappings",
                      "dbo.Products", "dbo.CompanyResourceTransactions_Stage"):
            cur.execute("SELECT OBJECT_ID(?, 'U')", table)
            if cur.fetchone()[0] is None:
                raise RuntimeError(f"جدول {table} در SQL Server وجود ندارد.")

        cur.execute("""SELECT pm.SourceProductName, pm.NormalizedSourceProductName, pm.ProductID, p.Unit
                       FROM dbo.ProductMappings pm
                       LEFT JOIN dbo.Products p ON p.ProductID=pm.ProductID
                       WHERE pm.IsActive=1""")
        mappings = {}
        ambiguous_mappings = set()
        for source_name, normalized, product_id, unit in cur.fetchall():
            for key in (normalized, _norm(source_name)):
                key = _norm(key)
                if not key:
                    continue
                candidate = (int(product_id), unit)
                if key in mappings and mappings[key][0] != candidate[0]:
                    ambiguous_mappings.add(key)
                else:
                    mappings[key] = candidate
        # Exact master-name matches are also resolvable; no fuzzy mapping is performed.
        cur.execute("SELECT ProductID, ProductName, Unit FROM dbo.Products WHERE IsActive=1")
        for product_id, product_name, unit in cur.fetchall():
            key = _norm(product_name)
            candidate = (int(product_id), unit)
            if key in mappings and mappings[key][0] != candidate[0]:
                ambiguous_mappings.add(key)
            else:
                mappings.setdefault(key, candidate)

        for report in reports:
            symbol = str(report.get("Symbol") or "").strip()
            trace = str(report.get("TracingNo") or "").strip()
            try:
                if report.get("Kind") != "MONTHLY_ACTIVITY":
                    raise ValueError("ثبت منابع فقط برای گزارش فعالیت ماهانه انجام می‌شود.")
                if not symbol or not trace:
                    raise ValueError("نماد یا شناسه پیگیری گزارش موجود نیست.")
                fiscal_year, fiscal_month, end_day = _period(report.get("Title", ""))
                rows = _resource_rows(report)
                if not rows:
                    raise ValueError("از Table_2 و Table_3 هیچ ردیف دارای مقدار ماه جاری استخراج نشد.")

                company_id, source_document_id, source_id, fiscal_year, fiscal_month, end_day = \
                    ensure_company_and_document(cur, report, [r["name"] for r in rows])

                staged = []
                for r in rows:
                    source_key = _norm(r["name"])
                    mapped = mappings.get(source_key)
                    product_id, product_unit = mapped if mapped else (None, None)
                    unit = _unit_code(r.get("unit_raw"), product_unit)
                    problems = []
                    if source_key in ambiguous_mappings:
                        product_id = None
                        problems.append("نگاشت نام منبع مبهم است و به بیش از یک ProductID اشاره می‌کند")
                    elif product_id is None:
                        problems.append("ProductMapping پیدا نشد")
                    if unit is None:
                        problems.append("واحد گزارش خالی، ترکیبی یا ناشناخته است")
                    elif product_unit and str(unit).upper() != str(product_unit).strip().upper():
                        problems.append(f"واحد گزارش ({unit}) با واحد مرجع محصول ({product_unit}) متفاوت است؛ تبدیل انجام نشده")
                    if all(r.get(k) is None for k in ("quantity", "rate", "amount", "adjustment_qty", "adjustment_rate", "adjustment_amount")):
                        problems.append("مقادیر ماه جاری و اصلاحات همگی خالی هستند")
                    status = "INVALID" if problems else "VALID"
                    staged.append((source_document_id, company_id, product_id, r["name"], r["sheet"], r["row_no"],
                                   r["year"], r["month"], r["transaction_type"], r["purchase_type"],
                                   r["resource_category"], r["quantity"], r["rate"], r["amount"],
                                   r.get("adjustment_qty"), r.get("adjustment_rate"), r.get("adjustment_amount"),
                                   unit, "IRR", status,
                                   "؛ ".join(problems) if problems else None))

                # Idempotent refresh of resource-stage rows for this document only.
                cur.execute("DELETE FROM dbo.CompanyResourceTransactions_Stage WHERE SourceDocumentID=?", source_document_id)
                cur.fast_executemany = True
                cur.executemany("""INSERT INTO dbo.CompanyResourceTransactions_Stage
                    (SourceDocumentID, CompanyID, ProductID, ProductNameRaw, SheetName, SourceRowNo,
                     FiscalYear, FiscalMonth, TransactionType, PurchaseType, ResourceCategory,
                     Quantity, Rate, Amount, AdjustmentQty, AdjustmentRate, AdjustmentAmount,
                     Unit, Currency, ValidationStatus, ValidationMessage)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", staged)
                valid_count = sum(1 for x in staged if x[-2] == "VALID")
                invalid_count = len(staged) - valid_count
                conn.commit()
                transferred = 0
                if invalid_count == 0:
                    # Stage is internal: replace this document's final rows atomically after full validation.
                    try:
                        cur.execute("""SELECT CompanyID,ProductID,SourceDocumentID,FiscalYear,FiscalMonth,
                                               TransactionType,PurchaseType,ResourceCategory,Quantity,Rate,Amount,
                                               AdjustmentQty,AdjustmentRate,AdjustmentAmount,Unit,Currency
                                        FROM dbo.CompanyResourceTransactions_Stage
                                        WHERE SourceDocumentID=? AND ValidationStatus='VALID'""", source_document_id)
                        final_rows = cur.fetchall()
                        for final_row in final_rows:
                            (co,pid,doc_id,fy,fm,tx_type,purchase_type,category,qty,rate,amount,
                             adj_qty,adj_rate,adj_amount,unit,currency) = final_row
                            cur.execute("""SELECT 1 FROM dbo.CompanyResourceTransactions
                                WHERE SourceDocumentID=? AND ProductID=? AND TransactionType=?
                                  AND ((PurchaseType=? ) OR (PurchaseType IS NULL AND ? IS NULL))
                                  AND ((ResourceCategory=? ) OR (ResourceCategory IS NULL AND ? IS NULL))
                                  AND ((Currency=? ) OR (Currency IS NULL AND ? IS NULL))
                                  AND ((Unit=? ) OR (Unit IS NULL AND ? IS NULL))""",
                                doc_id,pid,tx_type,purchase_type,purchase_type,category,category,
                                currency,currency,unit,unit)
                            exists = cur.fetchone() is not None
                            if exists:
                                cur.execute("""UPDATE dbo.CompanyResourceTransactions SET
                                    CompanyID=?,FiscalYear=?,FiscalMonth=?,Quantity=?,Rate=?,Amount=?,
                                    AdjustmentQty=?,AdjustmentRate=?,AdjustmentAmount=?
                                    WHERE SourceDocumentID=? AND ProductID=? AND TransactionType=?
                                      AND ((PurchaseType=? ) OR (PurchaseType IS NULL AND ? IS NULL))
                                      AND ((ResourceCategory=? ) OR (ResourceCategory IS NULL AND ? IS NULL))
                                      AND ((Currency=? ) OR (Currency IS NULL AND ? IS NULL))
                                      AND ((Unit=? ) OR (Unit IS NULL AND ? IS NULL))""",
                                    co,fy,fm,qty,rate,amount,adj_qty,adj_rate,adj_amount,
                                    doc_id,pid,tx_type,purchase_type,purchase_type,category,category,
                                    currency,currency,unit,unit)
                            else:
                                cur.execute("""INSERT dbo.CompanyResourceTransactions
                                    (CompanyID,ProductID,SourceDocumentID,FiscalYear,FiscalMonth,TransactionType,
                                     PurchaseType,ResourceCategory,Quantity,Rate,Amount,AdjustmentQty,AdjustmentRate,
                                     AdjustmentAmount,Unit,Currency)
                                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                    co,pid,doc_id,fy,fm,tx_type,purchase_type,category,qty,rate,amount,
                                    adj_qty,adj_rate,adj_amount,unit,currency)
                            transferred += 1
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                issues = [{"sheet": x[4], "row_no": x[5], "name": x[3], "message": x[20]}
                          for x in staged if x[19] != "VALID"]
                summaries.append({"symbol": symbol, "title": report.get("Title", ""), "tracing_no": trace,
                                  "source_document_id": source_document_id, "rows": len(staged),
                                  "valid": valid_count, "invalid": invalid_count, "transferred": transferred,
                                  "purchases": sum(1 for x in staged if x[8] == "PURCHASE"),
                                  "consumption": sum(1 for x in staged if x[8] == "CONSUMPTION"),
                                  "issues": issues})
            except Exception as exc:
                conn.rollback()
                failures.append({"symbol": symbol or "نماد نامشخص", "title": report.get("Title", ""),
                                 "tracing_no": trace, "stage": "RESOURCE_STAGE",
                                 "error": f"{type(exc).__name__}: {str(exc)[:700]}"})
    return {"summaries": summaries, "failed": failures,
            "total_rows": sum(x["rows"] for x in summaries),
            "valid_rows": sum(x["valid"] for x in summaries),
            "invalid_rows": sum(x["invalid"] for x in summaries),
            "transferred_rows": sum(x.get("transferred", 0) for x in summaries)}
