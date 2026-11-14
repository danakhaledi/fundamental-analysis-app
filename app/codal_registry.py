"""Shared Codal company and source-document registration for monthly imports."""
from __future__ import annotations

import re
from datetime import datetime

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _digits(value):
    return str(value or "").translate(_DIGITS)


def _norm(value):
    text = str(value or "").strip().replace("ي", "ی").replace("ى", "ی").replace("ك", "ک")
    return re.sub(r"[\u200c\u200e\u200f\s]+", "", text).lower()


def _period(title):
    text = _digits(title)
    match = re.search(r"منتهی\s*به\s*(\d{4})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})", text)
    if not match:
        raise ValueError("تاریخ پایان دوره مالی از عنوان گزارش قابل تشخیص نیست.")
    year, month, day = map(int, match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError("تاریخ پایان دوره مالی نامعتبر است.")
    return year, month, day


def _jalali_to_gregorian(jy: int, jm: int, jd: int):
    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33 + 3) // 4) + jd
    days += (jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186
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
    month_days = [0, 31, 29 if gy % 4 == 0 and (gy % 100 != 0 or gy % 400 == 0) else 28,
                  31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 1
    while gm <= 12 and gd > month_days[gm]:
        gd -= month_days[gm]
        gm += 1
    return gy, gm, gd


def _publish_date(report, fallback):
    raw = _digits(report.get("PublishDate", ""))
    match = re.search(r"(\d{4})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})", raw)
    if not match:
        return fallback
    return datetime(*_jalali_to_gregorian(*map(int, match.groups())))


def ensure_company_and_document(cur, report, item_names=None):
    """Get/create company and Codal source document; caller owns transaction/commit."""
    symbol = str(report.get("Symbol") or "").strip()
    trace = str(report.get("TracingNo") or "").strip()
    company_name = str(report.get("CompanyName") or "").strip()
    if not symbol or not trace:
        raise ValueError("نماد یا شناسه پیگیری گزارش موجود نیست.")
    if len(symbol) > 40:
        raise ValueError("طول نماد شرکت از ظرفیت Companies.Symbol بیشتر است.")

    # Resolve the report symbol only through an exact, official TSETMC symbol row
    # already linked to a Companies record. Never infer a company or industry from text.
    cur.execute("""SELECT DISTINCT c.CompanyID
                   FROM dbo.MarketSymbols ms
                   INNER JOIN dbo.Companies c ON c.CompanyID=ms.CompanyID
                   WHERE ms.Symbol=? AND ms.InstrumentCodeIsPlaceholder=0
                     AND ms.CompanyID IS NOT NULL""", symbol)
    linked_companies = cur.fetchall()
    if len(linked_companies) != 1:
        raise ValueError(
            f"نماد «{symbol}» باید دقیقاً به یک شرکت از طریق شناسه رسمی TSETMC در dbo.MarketSymbols متصل باشد؛ "
            f"تعداد اتصال معتبر: {len(linked_companies)}. ابتدا همگام‌سازی صنایع، شرکت‌ها و نمادها را اجرا کنید."
        )
    company_id = int(linked_companies[0][0])

    cur.execute("SELECT SourceID FROM dbo.DataSources WHERE SourceCode='CODAL' AND IsActive=1")
    sources = cur.fetchall()
    if len(sources) != 1:
        raise ValueError("منبع فعال CODAL در dbo.DataSources باید دقیقاً یک رکورد داشته باشد.")
    source_id = int(sources[0][0])

    fy, fm, end_day = _period(report.get("Title", ""))
    period_start = f"{fy:04d}/{fm:02d}/01"
    period_end = f"{fy:04d}/{fm:02d}/{end_day:02d}"
    cur.execute("""SELECT SourceDocumentID,CompanyID,PersianPeriodStart,PersianPeriodEnd,
                          VersionNo,IsCorrection
                   FROM dbo.SourceDocuments WITH (UPDLOCK,HOLDLOCK)
                   WHERE SourceID=? AND ExternalDocumentID=?""", source_id, trace)
    doc = cur.fetchone()
    if doc:
        if doc[1] is not None and int(doc[1]) != company_id:
            raise ValueError("شناسه پیگیری قبلاً برای شرکت دیگری ثبت شده است.")
        if doc[2] and str(doc[2]).replace("-", "/") != period_start:
            raise ValueError("شروع دوره گزارش با SourceDocument موجود همخوانی ندارد.")
        if doc[3] and str(doc[3]).replace("-", "/") != period_end:
            raise ValueError("پایان دوره گزارش با SourceDocument موجود همخوانی ندارد.")
        if doc[1] is None:
            cur.execute("UPDATE dbo.SourceDocuments SET CompanyID=? WHERE SourceDocumentID=?", company_id, int(doc[0]))
        return company_id, int(doc[0]), source_id, fy, fm, end_day

    is_correction = "اصلاح" in str(report.get("Title", ""))
    prior_id, version_no = None, 1
    if is_correction:
        cur.execute("""SELECT TOP (1) SourceDocumentID,VersionNo
                       FROM dbo.SourceDocuments WITH (UPDLOCK,HOLDLOCK)
                       WHERE SourceID=? AND CompanyID=? AND DocumentType='MONTHLY_ACTIVITY'
                         AND PersianPeriodStart=? AND PersianPeriodEnd=? AND IsCurrent=1
                       ORDER BY VersionNo DESC,SourceDocumentID DESC""",
                    source_id, company_id, period_start, period_end)
        prior = cur.fetchone()
        if not prior:
            raise ValueError("گزارش اصلاحی است اما سند جاریِ دوره متناظر برای اتصال اصلاحیه پیدا نشد.")
        prior_id, version_no = int(prior[0]), int(prior[1]) + 1
    else:
        cur.execute("""SELECT TOP (1) SourceDocumentID FROM dbo.SourceDocuments WITH (UPDLOCK,HOLDLOCK)
                       WHERE SourceID=? AND CompanyID=? AND DocumentType='MONTHLY_ACTIVITY'
                         AND PersianPeriodStart=? AND PersianPeriodEnd=? AND IsCurrent=1""",
                    source_id, company_id, period_start, period_end)
        if cur.fetchone():
            raise ValueError("برای این شرکت و دوره، سند جاری دیگری با شناسه پیگیری متفاوت ثبت شده است؛ احتمالاً گزارش تکراری یا اصلاحیه است.")

    sy, sm, sd = _jalali_to_gregorian(fy, fm, 1)
    ey, em, ed = _jalali_to_gregorian(fy, fm, end_day)
    fallback = datetime(ey, em, ed)
    document_date = _publish_date(report, fallback)
    cur.execute("""INSERT INTO dbo.SourceDocuments
      (SourceID,CompanyID,DocumentType,DocumentTitle,DocumentDate,ReportPeriodStart,ReportPeriodEnd,
       ExternalDocumentID,DocumentURL,ProcessingStatus,PersianReportDate,PersianPeriodStart,
       PersianPeriodEnd,VersionNo,IsCorrection,CorrectsDocumentID,IsCurrent)
      OUTPUT INSERTED.SourceDocumentID
      VALUES(?,?,'MONTHLY_ACTIVITY',?,?,?,?,?,?,'REVIEW',?,?,?,?,?,?,1)""",
      source_id, company_id, report.get("Title"), document_date, datetime(sy, sm, sd),
      datetime(ey, em, ed), trace, report.get("Url") or report.get("ExcelUrl"),
      _digits(report.get("PublishDate", "")).split(" ")[0] or None,
      period_start, period_end, version_no, 1 if is_correction else 0, prior_id)
    source_document_id = int(cur.fetchone()[0])
    if prior_id is not None:
        cur.execute("UPDATE dbo.SourceDocuments SET IsCurrent=0 WHERE SourceDocumentID=?", prior_id)
    return company_id, source_document_id, source_id, fy, fm, end_day
