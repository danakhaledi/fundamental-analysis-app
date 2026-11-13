"""Global Codal product/material discovery, staging and master mapping."""
from __future__ import annotations

import hashlib
from datetime import datetime

from .appconfig import get_connection_string
from .codal_resource_stage import (
    _find_sheet, _norm, _period, _unit_code, _jalali_to_gregorian, _publish_date,
)


def _is_summary(name):
    n = _norm(name)
    return (not n or "جمع" in n or n.startswith(("فروش", "درآمد", "برگشت", "تخفیف", "تخفيف"))
            or n in ("شرح", "ناممحصول", "ناممواد", "نوعانرژی"))


def _source_names(report):
    found = []
    t1 = _find_sheet(report, "Table_1")
    section = None
    for row_no, row in enumerate(t1[2:], start=3):
        cells = list(row or []) + [None] * 26
        label = str(cells[0] or "").strip()
        n = _norm(label)
        if "فروشداخلی" in n:
            section = "DOMESTIC"
            continue
        if "فروشصادراتی" in n:
            section = "EXPORT"
            continue
        if _is_summary(label):
            continue
        if not section:
            continue
        if len(cells) > 25 and _norm(cells[25]) not in ("تولید", "توليد", ""):
            continue
        unit = _unit_code(cells[1])
        if unit or any(cells[i] not in (None, "") for i in range(13, 17)):
            found.append((label, "Table_1", row_no, "PRODUCT", unit))

    t2 = _find_sheet(report, "Table_2")
    section = None
    for row_no, row in enumerate(t2[2:], start=3):
        cells = list(row or []) + [None] * 20
        label = str(cells[0] or "").strip()
        n = _norm(label)
        if "مواداولیهداخلی" in n:
            section = "DOMESTIC"
            continue
        if "مواداولیهوارداتی" in n:
            section = "IMPORT"
            continue
        if not section or _is_summary(label):
            continue
        unit = _unit_code(cells[1])
        if unit or any(cells[i] not in (None, "") for i in range(11, 14)):
            found.append((label, "Table_2", row_no, "PURCHASE_MATERIAL", unit))

    t3 = _find_sheet(report, "Table_3")
    for row_no, row in enumerate(t3[2:], start=3):
        cells = list(row or []) + [None] * 24
        label = str(cells[2] or "").strip()
        if _is_summary(label):
            continue
        unit = _unit_code(cells[3])
        if unit or any(cells[i] not in (None, "") for i in range(13, 16)):
            found.append((label, "Table_3", row_no, "CONSUMED_RESOURCE", unit))

    # Keep source occurrences, not only distinct names: users need row-level traceability.
    return found


def _ensure_document(cur, report, item_names=None):
    """Shared company/document registration retained as a compatibility wrapper."""
    from .codal_registry import ensure_company_and_document
    return ensure_company_and_document(cur, report, item_names=item_names)


def _map_source_names(names, mappings, products):
    """Pure lookup helper retained for diagnostics and unit tests."""
    staged = []
    auto_aliases = []
    for name, table, row_no, item_type, unit in names:
        key = _norm(name)
        candidates = mappings.get(key, []) or products.get(key, [])
        unique = {int(x[0]): x[1] for x in candidates}
        problems = []
        product_id = None
        product_unit = None
        if len(unique) == 1:
            product_id, product_unit = next(iter(unique.items()))
            if unit and product_unit and unit.upper() != str(product_unit).strip().upper():
                problems.append(f"واحد منبع ({unit}) با واحد محصول مرجع ({product_unit}) متفاوت است")
            elif key not in mappings:
                auto_aliases.append((name, key, product_id))
        elif len(unique) > 1:
            problems.append("نام منبع به بیش از یک ProductID نگاشت شده است")
        else:
            problems.append("محصول/ماده متناظر در Products یا ProductMappings پیدا نشد")
        staged.append({
            "name": name, "sheet": table, "row_no": row_no, "item_type": item_type,
            "unit": unit, "product_id": product_id, "product_unit": product_unit,
            "validation_status": "ERROR" if problems else "VALID",
            "validation_message": "؛ ".join(problems) if problems else None,
        })
    return staged, auto_aliases


def _generated_product_code(normalized_name):
    """Stable, deterministic system code; it is not a business/product code from Codal."""
    digest = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()[:40].upper()
    return "CODAL_" + digest


def _ensure_mapping(cur, source_name, normalized_name, product_id, mappings, product_unit):
    existing = mappings.get(normalized_name, [])
    ids = {int(item[0]) for item in existing}
    if ids and ids != {int(product_id)}:
        raise ValueError(f"نام «{source_name}» قبلاً به ProductID دیگری نگاشت شده است؛ بررسی دستی لازم است.")
    if not ids:
        cur.execute("""SELECT ProductID FROM dbo.ProductMappings WITH (UPDLOCK, HOLDLOCK)
                       WHERE IsActive=1 AND
                         (NormalizedSourceProductName=? OR SourceProductName=?)""",
                    normalized_name, source_name)
        db_ids = {int(row[0]) for row in cur.fetchall()}
        if db_ids and db_ids != {int(product_id)}:
            raise ValueError(f"نام «{source_name}» در ProductMappings به محصول دیگری متصل است.")
        if not db_ids:
            cur.execute("""INSERT INTO dbo.ProductMappings
                           (SourceProductName,ProductID,IsActive,NormalizedSourceProductName)
                           VALUES (?, ?, 1, ?)""", source_name, int(product_id), normalized_name)
        mappings.setdefault(normalized_name, []).append((int(product_id), product_unit))
        return 1
    return 0


def _create_product(cur, source_name, normalized_name, unit, products, mappings):
    if not source_name or len(source_name) > 400:
        raise ValueError("نام محصول خالی است یا از ظرفیت ستون ProductName بیشتر است.")
    if unit and len(unit) > 30:
        raise ValueError(f"واحد «{unit}» از ظرفیت ستون Products.Unit بیشتر است.")

    product_code = _generated_product_code(normalized_name)
    cur.execute("""SELECT ProductID,ProductName,Unit,IsActive
                   FROM dbo.Products WITH (UPDLOCK, HOLDLOCK)
                   WHERE ProductCode=?""", product_code)
    existing = cur.fetchone()
    if existing:
        if _norm(existing[1]) != normalized_name:
            raise RuntimeError("تعارض غیرمنتظره در ProductCode تولیدشده؛ ایجاد محصول متوقف شد.")
        if not bool(existing[3]):
            raise ValueError("محصول متناظر با این نام در Products غیرفعال است؛ فعال‌سازی باید آگاهانه انجام شود.")
        product_id, product_unit = int(existing[0]), existing[2]
    else:
        cur.execute("""INSERT INTO dbo.Products(ProductCode,ProductName,Unit,IsActive)
                       OUTPUT INSERTED.ProductID
                       VALUES (?, ?, ?, 1)""", product_code, source_name, unit)
        product_id = int(cur.fetchone()[0])
        product_unit = unit

    products.setdefault(normalized_name, []).append((product_id, product_unit))
    mapping_added = _ensure_mapping(
        cur, source_name, normalized_name, product_id, mappings, product_unit
    )
    return product_id, product_unit, mapping_added


def _resolve_occurrences(cur, names, mappings, products, inactive_products):
    staged = []
    created_products = 0
    mappings_added = 0

    for name, table, row_no, item_type, unit in names:
        key = _norm(name)
        candidates = mappings.get(key, []) or products.get(key, [])
        unique = {int(x[0]): x[1] for x in candidates}
        product_id = None
        product_unit = None
        problems = []

        if len(unique) == 1:
            product_id, product_unit = next(iter(unique.items()))
            if unit and product_unit and unit.upper() != str(product_unit).strip().upper():
                problems.append(f"واحد منبع ({unit}) با واحد محصول مرجع ({product_unit}) متفاوت است")
            elif key not in mappings:
                mappings_added += _ensure_mapping(
                    cur, name, key, product_id, mappings, product_unit
                )
        elif len(unique) > 1:
            problems.append("نام منبع به بیش از یک ProductID نگاشت شده است؛ بررسی دستی لازم است")
        elif inactive_products.get(key):
            inactive_ids = sorted({int(x[0]) for x in inactive_products[key]})
            problems.append(
                "محصولی با این نام در Products غیرفعال است "
                f"(ProductID: {', '.join(map(str, inactive_ids))})؛ فعال‌سازی باید آگاهانه انجام شود"
            )
        else:
            product_id, product_unit, mapping_added = _create_product(
                cur, name, key, unit, products, mappings
            )
            created_products += 1
            mappings_added += mapping_added

        staged.append({
            "name": name,
            "sheet": table,
            "row_no": row_no,
            "item_type": item_type,
            "unit": unit,
            "product_id": product_id,
            "product_unit": product_unit,
            "validation_status": "ERROR" if problems else "VALID",
            "validation_message": "؛ ".join(problems) if problems else None,
        })

    return staged, created_products, mappings_added


def _persist_stage_rows(cur, report, names):
    """Write raw extracted occurrences to the generic product stage before matching."""
    trace = str(report.get("TracingNo") or "").strip()
    symbol = str(report.get("Symbol") or "").strip() or None
    title = str(report.get("Title") or "")[:500]
    cur.execute("DELETE FROM dbo.CodalProductStage WHERE TracingNo=?", trace)
    for name, table, row_no, item_type, unit in names:
        cur.execute("""INSERT INTO dbo.CodalProductStage
            (SourceDocumentID,CompanyID,Symbol,TracingNo,ReportTitle,ProductNameRaw,
             SourceTable,SourceRowNo,ItemType,Unit,ProductID,ValidationStatus,ValidationMessage)
            VALUES (NULL,NULL,?,?,?,?,?,?,?, ?,NULL,'PENDING',NULL)""",
            symbol, trace, title, name, table, row_no, item_type, unit)


def _update_stage_rows(cur, report, staged):
    trace = str(report.get("TracingNo") or "").strip()
    for item in staged:
        cur.execute("""UPDATE dbo.CodalProductStage
                       SET ProductID=?,ValidationStatus=?,ValidationMessage=?
                       WHERE TracingNo=? AND SourceTable=? AND SourceRowNo=?""",
                    item["product_id"], item["validation_status"], item["validation_message"],
                    trace, item["sheet"], item["row_no"])



def _ensure_product_stage_schema(cur):
    """Apply the minimal v34.3+ generic-stage migration automatically at runtime."""
    cur.execute("""
    IF OBJECT_ID(N'dbo.CodalProductStage', N'U') IS NULL
        THROW 51020, N'dbo.CodalProductStage وجود ندارد؛ ساختار اولیه گردش کار کدال را ایجاد کنید.', 1;

    DECLARE @drop_fk NVARCHAR(MAX) = N'';
    SELECT @drop_fk = @drop_fk +
        N'ALTER TABLE dbo.CodalProductStage DROP CONSTRAINT ' + QUOTENAME(fk.name) + N';'
    FROM sys.foreign_keys fk
    WHERE fk.parent_object_id = OBJECT_ID(N'dbo.CodalProductStage')
      AND fk.referenced_object_id IN
          (OBJECT_ID(N'dbo.Companies'), OBJECT_ID(N'dbo.SourceDocuments'));
    IF @drop_fk <> N'' EXEC sys.sp_executesql @drop_fk;

    IF COL_LENGTH(N'dbo.CodalProductStage', N'SourceDocumentID') IS NOT NULL
        ALTER TABLE dbo.CodalProductStage ALTER COLUMN SourceDocumentID BIGINT NULL;
    IF COL_LENGTH(N'dbo.CodalProductStage', N'CompanyID') IS NOT NULL
        ALTER TABLE dbo.CodalProductStage ALTER COLUMN CompanyID INT NULL;

    IF COL_LENGTH(N'dbo.CodalProductStage', N'Symbol') IS NULL
        ALTER TABLE dbo.CodalProductStage ADD Symbol NVARCHAR(100) NULL;
    IF COL_LENGTH(N'dbo.CodalProductStage', N'TracingNo') IS NULL
        ALTER TABLE dbo.CodalProductStage ADD TracingNo NVARCHAR(50) NULL;
    IF COL_LENGTH(N'dbo.CodalProductStage', N'ReportTitle') IS NULL
        ALTER TABLE dbo.CodalProductStage ADD ReportTitle NVARCHAR(500) NULL;

    DECLARE @drop_ck NVARCHAR(MAX) = N'';
    SELECT @drop_ck = @drop_ck +
        N'ALTER TABLE dbo.CodalProductStage DROP CONSTRAINT ' + QUOTENAME(cc.name) + N';'
    FROM sys.check_constraints cc
    WHERE cc.parent_object_id = OBJECT_ID(N'dbo.CodalProductStage')
      AND cc.definition LIKE N'%ValidationStatus%';
    IF @drop_ck <> N'' EXEC sys.sp_executesql @drop_ck;

    ALTER TABLE dbo.CodalProductStage WITH CHECK ADD CONSTRAINT CK_CodalProductStage_Status
        CHECK (ValidationStatus IN ('PENDING','VALID','ERROR'));

    IF COL_LENGTH(N'dbo.CodalProductStage', N'TracingNo') IS NOT NULL
       AND NOT EXISTS (
            SELECT 1 FROM sys.indexes
            WHERE object_id = OBJECT_ID(N'dbo.CodalProductStage')
              AND name = N'UX_CodalProductStage_Trace_Row'
       )
    BEGIN
        /* Defer column binding until after the preceding ALTER TABLE. */
        EXEC sys.sp_executesql N'
            CREATE UNIQUE INDEX UX_CodalProductStage_Trace_Row
            ON dbo.CodalProductStage(TracingNo, SourceTable, SourceRowNo)
            WHERE TracingNo IS NOT NULL AND SourceRowNo IS NOT NULL;';
    END;

    /* Backfill reports that v34.2 had marked successful without populating
       the generic product stage. This marker makes the reset one-time. */
    IF OBJECT_ID(N'dbo.CodalReportWorkflow', N'U') IS NOT NULL
       AND OBJECT_ID(N'dbo.CodalWorkflowMeta', N'U') IS NOT NULL
       AND COL_LENGTH(N'dbo.CodalReportWorkflow', N'ProductStatus') IS NOT NULL
       AND COL_LENGTH(N'dbo.CodalWorkflowMeta', N'MetaKey') IS NOT NULL
       AND COL_LENGTH(N'dbo.CodalWorkflowMeta', N'MetaValue') IS NOT NULL
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM dbo.CodalWorkflowMeta
                       WHERE MetaKey=N'v34_3_product_stage_reset')
        BEGIN
            UPDATE dbo.CodalReportWorkflow
            SET ProductStatus='PENDING', ProductError=NULL,
                ProductResultJson=NULL, ProductProcessedAt=NULL,
                UpdatedAt=SYSDATETIME()
            WHERE ProductStatus='SUCCESS';

            INSERT dbo.CodalWorkflowMeta(MetaKey,MetaValue)
            VALUES (N'v34_3_product_stage_reset',
                    CONVERT(NVARCHAR(30),SYSDATETIME(),126));
        END;
    END;
    """)
    return True


def stage_product_reports(reports):
    """Extract -> generic stage -> resolve/create master product -> persist mapping."""
    cs = get_connection_string()
    if not cs:
        raise RuntimeError("اتصال SQL Server تنظیم نشده است.")
    import pyodbc

    summaries, failed = [], []
    with pyodbc.connect(cs, timeout=15) as conn:
        cur = conn.cursor()
        for table in ("dbo.Products", "dbo.ProductMappings", "dbo.CodalProductStage"):
            cur.execute("SELECT OBJECT_ID(?, 'U')", table)
            if cur.fetchone()[0] is None:
                raise RuntimeError(
                    f"جدول {table} در SQL Server وجود ندارد؛ ابتدا ساختار پایه دیتابیس را ایجاد کنید."
                )

        # Upgrade the legacy company-bound stage in-app; users do not need to
        # run a separate SQL patch for every application release.
        _ensure_product_stage_schema(cur)
        conn.commit()

        cur.execute("""SELECT pm.SourceProductName,pm.NormalizedSourceProductName,pm.ProductID,p.Unit
                       FROM dbo.ProductMappings pm
                       INNER JOIN dbo.Products p ON p.ProductID=pm.ProductID AND p.IsActive=1
                       WHERE pm.IsActive=1""")
        mappings = {}
        for source_name, normalized_name, product_id, unit in cur.fetchall():
            for candidate_name in (source_name, normalized_name):
                key = _norm(candidate_name)
                if key:
                    mappings.setdefault(key, []).append((int(product_id), unit))

        cur.execute("SELECT ProductID,ProductName,Unit,IsActive FROM dbo.Products")
        products = {}
        inactive_products = {}
        for product_id, product_name, unit, is_active in cur.fetchall():
            target = products if bool(is_active) else inactive_products
            target.setdefault(_norm(product_name), []).append((int(product_id), unit))

        for report in reports:
            symbol = str(report.get("Symbol") or "").strip()
            trace = str(report.get("TracingNo") or "").strip()
            try:
                if report.get("Kind") != "MONTHLY_ACTIVITY":
                    raise ValueError("عملیات محصولات/مواد فقط برای گزارش فعالیت ماهانه است.")
                if not trace:
                    raise ValueError("شناسه پیگیری گزارش موجود نیست.")
                names = _source_names(report)
                if not names:
                    raise ValueError("هیچ نام محصول/ماده‌ای در Table_1، Table_2 یا Table_3 پیدا نشد.")

                # The source occurrences are staged first. Matching and master updates
                # happen immediately afterwards in the same report transaction.
                _persist_stage_rows(cur, report, names)
                # Use per-report working copies: if this report rolls back, its newly
                # generated ProductIDs must not leak into the in-memory cache used by
                # later reports in the same batch.
                working_mappings = {key: list(value) for key, value in mappings.items()}
                working_products = {key: list(value) for key, value in products.items()}
                staged, created_products, mappings_added = _resolve_occurrences(
                    cur, names, working_mappings, working_products, inactive_products
                )
                _update_stage_rows(cur, report, staged)
                conn.commit()
                mappings = working_mappings
                products = working_products

                valid = sum(1 for item in staged if item["validation_status"] == "VALID")
                invalid = len(staged) - valid
                issues = [
                    {"sheet": item["sheet"], "row_no": item["row_no"],
                     "name": item["name"], "message": item["validation_message"]}
                    for item in staged if item["validation_status"] != "VALID"
                ]
                summaries.append({
                    "symbol": symbol,
                    "title": report.get("Title", ""),
                    "tracing_no": trace,
                    "rows": len(staged),
                    "valid": valid,
                    "invalid": invalid,
                    "issues": issues,
                    "items": staged,
                    "products_created": created_products,
                    "mappings_added": mappings_added,
                })
            except Exception as exc:
                conn.rollback()
                failed.append({
                    "symbol": symbol or "نماد نامشخص",
                    "title": report.get("Title", ""),
                    "tracing_no": trace,
                    "stage": "PRODUCT_STAGE",
                    "error": f"{type(exc).__name__}: {str(exc)[:700]}",
                })

    return {
        "summaries": summaries,
        "failed": failed,
        "total_rows": sum(item["rows"] for item in summaries),
        "valid_rows": sum(item["valid"] for item in summaries),
        "invalid_rows": sum(item["invalid"] for item in summaries),
        "products_created": sum(item["products_created"] for item in summaries),
        "mappings_added": sum(item["mappings_added"] for item in summaries),
    }
