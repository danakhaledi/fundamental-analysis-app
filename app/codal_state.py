"""SQL Server-backed persistent state for the Codal workflow.

The workflow table is part of the application's existing SQL Server database.
The optional SQLite file is read only during a one-time migration from v32.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .appconfig import get_connection_string

TABLE = "dbo.CodalReportWorkflow"
META_TABLE = "dbo.CodalWorkflowMeta"
_ALLOWED_COLUMNS = {
    "download_status": "DownloadStatus",
    "download_file": "DownloadFile",
    "download_error": "DownloadError",
    "downloaded_at": "DownloadedAt",
    "extract_status": "ExtractStatus",
    "extraction_json": "ExtractionJson",
    "extract_error": "ExtractError",
    "extracted_at": "ExtractedAt",
    "resource_status": "ResourceStatus",
    "resource_result_json": "ResourceResultJson",
    "resource_error": "ResourceError",
    "resource_processed_at": "ResourceProcessedAt",
    "product_status": "ProductStatus",
    "product_result_json": "ProductResultJson",
    "product_error": "ProductError",
    "product_processed_at": "ProductProcessedAt",
    "sales_status": "SalesStatus",
    "sales_result_json": "SalesResultJson",
    "sales_error": "SalesError",
    "sales_processed_at": "SalesProcessedAt",
}


def _connect():
    cs = get_connection_string()
    if not cs:
        raise RuntimeError("اتصال SQL Server تنظیم نشده است؛ مقدار اتصال را در تنظیمات برنامه بررسی کنید.")
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError("درایور pyodbc نصب نیست.") from exc
    return pyodbc.connect(cs, timeout=15)


def _iso(value):
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else value


def _parse_datetime(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    raw = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except ValueError:
        return None


def _json_load(value):
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def initialize(_legacy_db_path=None):
    """Validate that the SQL Server workflow tables were installed by the supplied SQL script."""
    with _connect() as conn:
        cur = conn.cursor()
        for table in (TABLE, META_TABLE):
            cur.execute("SELECT OBJECT_ID(?, 'U')", table)
            if cur.fetchone()[0] is None:
                raise RuntimeError(
                    f"جدول {table} در پایگاه SQL Server وجود ندارد. ابتدا فایل "
                    "00_CreateCodalWorkflow.sql را روی پایگاه داده اصلی اجرا کنید."
                )


def upsert_search_results(_legacy_db_path, reports):
    now = datetime.now()
    with _connect() as conn:
        cur = conn.cursor()
        _require_tables(cur)
        for report in reports:
            trace = str(report.get("TracingNo") or "").strip()
            if not trace:
                continue
            payload = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
            cur.execute(f"SELECT 1 FROM {TABLE} WHERE TracingNo=?", trace)
            if cur.fetchone():
                # LongAsMax=Yes on the ODBC connection ensures long strings are
                # bound as (n)varchar(max), not legacy (n)text.
                cur.execute(f"UPDATE {TABLE} SET ReportJson=?, UpdatedAt=? WHERE TracingNo=?",
                            payload, now, trace)
            else:
                cur.execute(f"""INSERT INTO {TABLE}
                    (TracingNo, ReportJson, DiscoveredAt, UpdatedAt)
                    VALUES (?, ?, ?, ?)""", trace, payload, now, now)
        conn.commit()


def _require_tables(cur):
    for table in (TABLE, META_TABLE):
        cur.execute("SELECT OBJECT_ID(?, 'U')", table)
        if cur.fetchone()[0] is None:
            raise RuntimeError(f"جدول {table} در SQL Server وجود ندارد؛ اسکریپت ایجاد گردش کار کدال را اجرا کنید.")


def list_reports(_legacy_db_path=None, *, statuses=None, page=None, page_size=None):
    with _connect() as conn:
        cur = conn.cursor()
        _require_tables(cur)
        query = f"""SELECT TracingNo, ReportJson, DiscoveredAt,
            DownloadStatus, DownloadFile, DownloadError, DownloadedAt,
            ExtractStatus, ExtractionJson, ExtractError, ExtractedAt,
            ResourceStatus, ResourceResultJson, ResourceError, ResourceProcessedAt,
            ProductStatus, ProductResultJson, ProductError, ProductProcessedAt,
            SalesStatus, SalesResultJson, SalesError, SalesProcessedAt, UpdatedAt
            FROM {TABLE}"""
        clauses, args = [], []
        status_columns = {
            "download_status": "DownloadStatus",
            "extract_status": "ExtractStatus",
            "resource_status": "ResourceStatus",
            "product_status": "ProductStatus",
            "sales_status": "SalesStatus",
        }
        for key, values in (statuses or {}).items():
            col = status_columns.get(key)
            vals = list(values or [])
            if col and vals:
                clauses.append(f"{col} IN ({','.join('?' for _ in vals)})")
                args.extend(vals)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY DiscoveredAt DESC, TracingNo DESC"
        if page is not None and page_size is not None:
            page = max(1, int(page))
            page_size = max(1, min(200, int(page_size)))
            count_query = f"SELECT COUNT_BIG(*) FROM {TABLE}" + (" WHERE " + " AND ".join(clauses) if clauses else "")
            cur.execute(count_query, *args)
            total_count = int(cur.fetchone()[0] or 0)
            page = min(page, max(1, (total_count + page_size - 1) // page_size))
            query += " OFFSET ? ROWS FETCH NEXT ? ROWS ONLY"
            args.extend([(page - 1) * page_size, page_size])
            cur.execute(query, *args)
        else:
            total_count = None
            cur.execute(query, *args)
        names = [d[0] for d in cur.description]
        raw_rows = [dict(zip(names, row)) for row in cur.fetchall()]

    output = []
    for row in raw_rows:
        item = {
            "tracing_no": str(row["TracingNo"]),
            "report": _json_load(row["ReportJson"]) or {},
            "discovered_at": _iso(row["DiscoveredAt"]),
            "download_status": row["DownloadStatus"],
            "download_file": row["DownloadFile"],
            "download_error": row["DownloadError"],
            "downloaded_at": _iso(row["DownloadedAt"]),
            "extract_status": row["ExtractStatus"],
            "extraction": _json_load(row["ExtractionJson"]),
            "extract_error": row["ExtractError"],
            "extracted_at": _iso(row["ExtractedAt"]),
            "resource_status": row["ResourceStatus"],
            "resource_result": _json_load(row["ResourceResultJson"]),
            "resource_error": row["ResourceError"],
            "resource_processed_at": _iso(row["ResourceProcessedAt"]),
            "product_status": row["ProductStatus"],
            "product_result": _json_load(row["ProductResultJson"]),
            "product_error": row["ProductError"],
            "product_processed_at": _iso(row["ProductProcessedAt"]),
            "sales_status": row["SalesStatus"],
            "sales_result": _json_load(row["SalesResultJson"]),
            "sales_error": row["SalesError"],
            "sales_processed_at": _iso(row["SalesProcessedAt"]),
            "updated_at": _iso(row["UpdatedAt"]),
        }
        output.append(item)
    return (output, total_count) if page is not None and page_size is not None else output


def get_reports(_legacy_db_path, traces):
    wanted = {str(x).strip() for x in traces if str(x).strip()}
    return [x for x in list_reports() if x["tracing_no"] in wanted]


def update_report(_legacy_db_path, trace: str, **fields):
    updates = {key: value for key, value in fields.items() if key in _ALLOWED_COLUMNS}
    if not updates:
        return
    columns, values = [], []
    for key, value in updates.items():
        columns.append(f"{_ALLOWED_COLUMNS[key]}=?")
        if key.endswith("_at"):
            value = _parse_datetime(value)
        elif key.endswith("_error") and value is not None:
            value = str(value)[:4000]
        elif key == "download_file" and value is not None:
            value = str(value)[:500]
        values.append(value)
    columns.append("UpdatedAt=?")
    values.extend([datetime.now(), str(trace)])
    with _connect() as conn:
        cur = conn.cursor()
        _require_tables(cur)
        cur.execute(f"UPDATE {TABLE} SET {', '.join(columns)} WHERE TracingNo=?", *values)
        conn.commit()


def summary(_legacy_db_path=None):
    with _connect() as conn:
        cur = conn.cursor()
        _require_tables(cur)
        cur.execute(f"""SELECT COUNT_BIG(*),
            SUM(CASE WHEN DownloadStatus='SUCCESS' THEN 1 ELSE 0 END),
            SUM(CASE WHEN DownloadStatus='FAILED' THEN 1 ELSE 0 END),
            SUM(CASE WHEN ExtractStatus='SUCCESS' THEN 1 ELSE 0 END),
            SUM(CASE WHEN ExtractStatus='FAILED' THEN 1 ELSE 0 END),
            SUM(CASE WHEN ResourceStatus='SUCCESS' THEN 1 ELSE 0 END),
            SUM(CASE WHEN ResourceStatus='FAILED' THEN 1 ELSE 0 END),
            SUM(CASE WHEN ProductStatus='SUCCESS' THEN 1 ELSE 0 END),
            SUM(CASE WHEN ProductStatus='FAILED' THEN 1 ELSE 0 END),
            SUM(CASE WHEN SalesStatus='SUCCESS' THEN 1 ELSE 0 END),
            SUM(CASE WHEN SalesStatus='FAILED' THEN 1 ELSE 0 END)
            FROM {TABLE}""")
        row = cur.fetchone()
    vals = [int(x or 0) for x in row]
    return dict(zip(("total", "downloaded", "download_failed", "extracted", "extract_failed",
                     "resource_staged", "resource_failed", "product_success", "product_failed",
                     "sales_success", "sales_failed"), vals))


def _meta_done(cur, key):
    cur.execute(f"SELECT MetaValue FROM {META_TABLE} WHERE MetaKey=?", key)
    row = cur.fetchone()
    return row[0] if row else None


def _set_meta(cur, key, value):
    cur.execute(f"""UPDATE {META_TABLE} SET MetaValue=? WHERE MetaKey=?""", value, key)
    if cur.rowcount == 0:
        cur.execute(f"INSERT INTO {META_TABLE}(MetaKey, MetaValue) VALUES (?, ?)" , key, value)


def migrate_legacy_files(db_path: Path, data_dir: Path):
    """One-time import of v32 SQLite state and v30/v31 JSON manifest/export files into SQL Server."""
    with _connect() as conn:
        cur = conn.cursor()
        _require_tables(cur)
        if _meta_done(cur, "v32_sqlite_migrated"):
            return

    # Import the v32 SQLite workflow database, if this build was previously run.
    legacy_db = Path(db_path) if db_path else None
    if legacy_db and legacy_db.exists():
        try:
            with sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True) as old:
                old.row_factory = sqlite3.Row
                exists = old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='codal_reports'").fetchone()
                if exists:
                    for oldrow in old.execute("SELECT * FROM codal_reports"):
                        row = dict(oldrow)
                        report = _json_load(row.get("report_json")) or {}
                        upsert_search_results(None, [report])
                        update_report(None, row.get("tracing_no", ""),
                            download_status=row.get("download_status", "PENDING"),
                            download_file=row.get("download_file"), download_error=row.get("download_error"),
                            downloaded_at=row.get("downloaded_at"),
                            extract_status=row.get("extract_status", "PENDING"),
                            extraction_json=row.get("extraction_json"), extract_error=row.get("extract_error"),
                            extracted_at=row.get("extracted_at"),
                            resource_status=row.get("resource_status", "PENDING"),
                            resource_result_json=row.get("resource_result_json"),
                            resource_error=row.get("resource_error"),
                            resource_processed_at=row.get("resource_processed_at"))
        except sqlite3.Error as exc:
            raise RuntimeError(f"خواندن پایگاه وضعیت قدیمی v32 ناموفق بود: {exc}") from exc

    # Import the v30/v31 manifest and extracted report bundles as a fallback/source of older history.
    manifest_path = Path(data_dir) / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    except Exception:
        manifest = []
    for item in manifest:
        trace = str(item.get("TracingNo") or "").strip()
        if not trace:
            continue
        upsert_search_results(None, [item])
        filename = item.get("FileName")
        file_path = Path(data_dir) / str(filename) if filename else None
        if file_path and file_path.is_file() and file_path.stat().st_size:
            update_report(None, trace, download_status="SUCCESS", download_file=str(filename),
                          downloaded_at=item.get("DownloadedAt") or datetime.now().isoformat(timespec="seconds"))

    for bundle_path in sorted(Path(data_dir).glob("codal_extracted_*.json")):
        try:
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for report in bundle.get("Reports", []) or []:
            trace = str(report.get("TracingNo") or "").strip()
            if not trace:
                continue
            upsert_search_results(None, [report])
            update_report(None, trace, download_status="SUCCESS", download_file=report.get("FileName"),
                          extract_status="SUCCESS", extraction_json=json.dumps(report, ensure_ascii=False),
                          extracted_at=bundle.get("CreatedAt") or datetime.now().isoformat(timespec="seconds"))
        for failure in bundle.get("Errors", []) or []:
            trace = str(failure.get("tracing_no") or "").strip()
            if trace:
                update_report(None, trace, extract_status="FAILED", extract_error=failure.get("error", "خطای استخراج قبلی"))

    with _connect() as conn:
        cur = conn.cursor()
        _set_meta(cur, "v32_sqlite_migrated", datetime.now().isoformat(timespec="seconds"))
        conn.commit()
