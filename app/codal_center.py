"""Codal report discovery and Excel download helpers for the web app.
This module intentionally does not write financial rows to SQL Server.
"""
import json
import re
import urllib.parse
import urllib.request
import html as html_lib
from datetime import datetime
from pathlib import Path

def _digits(text):
    table = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    return str(text or "").translate(table)

def report_kind(title):
    t = str(title or "")
    if "گزارش فعالیت ماهانه" in t:
        return "MONTHLY_ACTIVITY"
    financial_terms = ("صورت‌های مالی", "صورتهای مالی", "صورت مالی", "صورت‌های مالی")
    if any(x in t for x in financial_terms):
        return "FINANCIAL_STATEMENT"
    return None

def search_codal_reports(symbol="", kind="both", max_pages=None, page_size=50, from_date=""):
    """Search Codal and return reports matching requested type. No database writes."""
    api = "https://search.codal.ir/api/search/v2/q"
    result = []
    # Monthly activity is known to use LetterType=58. For financial statements, search
    # broadly and classify by title instead of assuming one LetterType for every issuer.
    letter_types = [58] if kind == "monthly" else [None]
    seen = set()
    page_limit = min(10000, int(max_pages)) if max_pages is not None else 10000
    page_size = max(50, min(200, int(page_size or 50)))
    for letter_type in letter_types:
        previous_page_signatures = set()
        for page in range(1, page_limit + 1):
            params = {"PageNumber": page, "PageSize": page_size, "Sort": "Date"}
            if symbol.strip():
                params["Symbol"] = symbol.strip()
            if from_date.strip():
                params["FromDate"] = _digits(from_date.strip())
            if letter_type is not None:
                params["LetterType"] = letter_type
            url = api + "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json"
            })
            with urllib.request.urlopen(req, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8-sig"))
            letters = payload.get("Letters", []) or []
            if not letters:
                break
            page_signature = tuple(str(x.get("TracingNo") or x.get("Url") or x.get("Title") or "") for x in letters)
            if page_signature in previous_page_signatures:
                break
            previous_page_signatures.add(page_signature)
            for letter in letters:
                title = letter.get("Title", "")
                detected = report_kind(title)
                if detected is None:
                    continue
                if kind == "monthly" and detected != "MONTHLY_ACTIVITY":
                    continue
                if kind == "financial" and detected != "FINANCIAL_STATEMENT":
                    continue
                key = str(letter.get("TracingNo") or letter.get("Url") or title)
                if key in seen:
                    continue
                seen.add(key)
                result.append({
                    "TracingNo": str(letter.get("TracingNo") or ""),
                    "LetterID": str(letter.get("LetterID") or letter.get("LetterId") or letter.get("letterId") or ""),
                    "Symbol": letter.get("Symbol") or symbol.strip(),
                    "CompanyName": letter.get("CompanyName") or "",
                    "Title": title,
                    "PublishDate": letter.get("PublishDateTime") or letter.get("PublishDate") or "",
                    "Url": letter.get("Url") or "",
                    "HasExcel": bool(letter.get("HasExcel")),
                    "ExcelUrl": letter.get("ExcelUrl") or "",
                    "HasHtml": bool(letter.get("HasHtml")),
                    "Kind": detected,
                })
            # Some Codal responses cap page size below the requested value. Continue until
            # an empty page or a repeated raw page.
        else:
            if max_pages is None:
                raise RuntimeError("جست‌وجوی کدال به سقف ایمنی ۱۰٬۰۰۰ صفحه رسید؛ برای جلوگیری از ثبت نتیجه ناقص، جست‌وجو متوقف شد.")
    result.sort(key=lambda x: (x["PublishDate"], x["TracingNo"]), reverse=True)
    return result

def search_codal_for_symbols(symbols=None, kind="monthly", max_pages=None, page_size=50, from_date=""):
    """Search selected symbols, or search Codal globally when symbols is empty."""
    combined, seen = [], set()
    symbols = list(symbols or [])
    search_targets = symbols if symbols else [""]
    for symbol in search_targets:
        for item in search_codal_reports(symbol=symbol, kind=kind, max_pages=max_pages, page_size=page_size, from_date=from_date):
            key = item.get("TracingNo") or item.get("Url") or item.get("Title")
            if key not in seen:
                seen.add(key)
                combined.append(item)
    if from_date.strip():
        cutoff = _digits(from_date.strip()).replace("-", "/").replace(".", "/")[:10]
        def date_key(value):
            raw = _digits(value).replace("-", "/").replace(".", "/")
            m = re.search(r"(\d{4}/\d{1,2}/\d{1,2})", raw)
            return tuple(int(part) for part in m.group(1).split("/")) if m else ()
        try:
            cutoff_key = tuple(int(part) for part in cutoff.split("/"))
            combined = [item for item in combined if date_key(item.get("PublishDate", "")) and date_key(item.get("PublishDate", "")) >= cutoff_key]
        except ValueError:
            pass
    combined.sort(key=lambda x: (x.get("PublishDate", ""), x.get("TracingNo", "")), reverse=True)
    return combined

def _letter_serial(report):
    """Extract the Codal LetterSerial from the report page URL."""
    url = html_lib.unescape(str(report.get("Url") or ""))
    match = re.search(r"(?:[?&])LetterSerial=([^&]+)", url, flags=re.I)
    return urllib.parse.unquote(match.group(1)) if match else ""


def _is_office_html(data: bytes) -> bool:
    """Codal's Excel service commonly returns Excel-compatible HTML tables, not XLSX bytes."""
    sample = data[:200000].decode("utf-8", errors="replace").lower()
    if "<html" not in sample and "<table" not in sample:
        return False
    # A real Excel export from Codal contains workbook-like tables; a generic error/login page does not.
    return "<table" in sample and ("<td" in sample or "<tr" in sample) and not any(
        marker in sample[:12000] for marker in ("captcha", "access denied", "درخواست نامعتبر", "صفحه یافت نشد")
    )


def _looks_like_excel(data: bytes) -> bool:
    """Recognize binary Excel workbooks and Codal's HTML-table Excel exports."""
    head = data[:4096].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if data.startswith(b"PK\x03\x04") or data.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        return True
    if head.startswith((b"<!doctype html", b"<html", b"<?xml", b"<head", b"<body")):
        return _is_office_html(data)
    return _is_office_html(data)


def _download_real_excel(report, timeout=30):
    """Download the actual Excel export using LetterSerial; never treat the Excel icon as a file."""
    candidates = []
    letter_id = str(report.get("LetterID") or "").strip()
    serial = _letter_serial(report)
    raw = html_lib.unescape(str(report.get("ExcelUrl") or "")).strip()

    # The API's ExcelUrl may be missing or incorrectly mapped to the static ExportToExcel.jpg icon.
    # The authoritative download endpoint is based on LetterSerial.
    if serial:
        encoded = urllib.parse.quote(serial, safe="=%")
        candidates.append(f"https://excel.codal.ir/service/Excel/GetAll/{encoded}/0")
    if raw and not raw.lower().endswith("/images/exporttoexcel.jpg") and "ExportToExcel.jpg" not in raw:
        candidates.append(urllib.parse.urljoin("https://www.codal.ir/", raw))
    page_url = html_lib.unescape(str(report.get("Url") or "")).strip()
    if page_url:
        candidates.append(urllib.parse.urljoin("https://www.codal.ir/", page_url))
    if letter_id:
        for host in ("https://www.codal.ir", "https://codal.ir"):
            candidates.extend([
                f"{host}/Reports/Excel/DownloadExcel.aspx?LetterID={urllib.parse.quote(letter_id)}",
                f"{host}/Reports/DownloadFile.aspx?LetterID={urllib.parse.quote(letter_id)}",
            ])

    tried = set()
    last_problem = "پیوست Excel معتبر دریافت نشد؛ شناسه یا نشانی واقعی پیوست پیدا نشد"
    index = 0
    while index < len(candidates) and index < 20:
        url = candidates[index]
        index += 1
        if not url or url in tried or "ExportToExcel.jpg" in url:
            continue
        tried.add(url)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel,application/octet-stream,text/html,*/*",
                "Referer": urllib.parse.urljoin("https://www.codal.ir/", page_url) if page_url else "https://www.codal.ir/",
            })
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = response.read()
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "")
            if _looks_like_excel(data):
                return data, final_url
            head = data[:4096].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
            if content_type.lower().startswith("image/") or head.startswith((b"\xff\xd8\xff", b"\x89png")):
                last_problem = f"پاسخ تصویر به‌جای Excel بود (Content-Type: {content_type}; URL: {final_url})"
            elif b"<html" in head[:1024] or head.startswith((b"<!doctype html", b"<head", b"<body")):
                last_problem = f"صفحه HTML فاقد جدول Excel دریافت شد (Content-Type: {content_type or 'نامشخص'}؛ URL: {final_url})"
            else:
                last_problem = f"محتوای پاسخ Excel معتبر نیست (Content-Type: {content_type or 'نامشخص'}؛ URL: {final_url})"
        except Exception as exc:
            last_problem = f"{type(exc).__name__}: {str(exc)[:180]} (URL: {url})"
    raise ValueError(last_problem)

def download_excel_reports(reports, download_root: Path):
    """Download only Excel attachments; persist a metadata manifest; never import to SQL."""
    download_root.mkdir(parents=True, exist_ok=True)
    saved, skipped, failed = [], [], []
    manifest_path = download_root / "manifest.json"
    try:
        existing = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    except Exception:
        existing = []
    by_trace = {str(x.get("TracingNo")): x for x in existing}
    for report in reports:
        trace = re.sub(r"[^0-9A-Za-z_-]", "", str(report.get("TracingNo") or ""))
        if not trace or not report.get("HasExcel") or not (report.get("ExcelUrl") or report.get("LetterID") or report.get("Url")):
            skipped.append({"symbol": report.get("Symbol") or report.get("CompanyName") or "نماد نامشخص", "title": report.get("Title", ""), "reason": "شناسه/نشانی پیوست Excel موجود نیست"})
            continue
        symbol = re.sub(r"[^0-9A-Za-zآ-ی_-]", "_", str(report.get("Symbol") or "unknown"))[:50]
        ext = ".xls"
        filename = f"{symbol}_{trace}{ext}"
        target = download_root / filename
        if target.exists() and target.stat().st_size > 0:
            existing_bytes = target.read_bytes()
            if _looks_like_excel(existing_bytes):
                skipped.append({"file": filename, "reason": "قبلاً دانلود شده و قالب فایل معتبر است"})
                by_trace[trace] = {**report, "FileName": filename, "DownloadedAt": by_trace.get(trace, {}).get("DownloadedAt", "")}
                continue
            # Previous versions could save an HTML error page under an .xls filename.
            target.unlink(missing_ok=True)
        try:
            data, actual_url = _download_real_excel(report, timeout=30)
            target.write_bytes(data)
            item = {**report, "FileName": filename, "DownloadedAt": datetime.now().isoformat(timespec="seconds"), "ResolvedExcelUrl": actual_url}
            by_trace[trace] = item
            saved.append(item)
        except Exception as exc:
            failed.append({
                "symbol": report.get("Symbol") or report.get("CompanyName") or "نماد نامشخص",
                "company": report.get("CompanyName", ""),
                "title": report.get("Title", ""),
                "tracing_no": trace,
                "stage": "DOWNLOAD",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            })
    manifest_path.write_text(json.dumps(list(by_trace.values()), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"saved": saved, "skipped": skipped, "failed": failed, "manifest": str(manifest_path)}


def extract_downloaded_reports(reports, download_root: Path):
    """Download selected reports and extract workbook rows into a reviewable JSON package.

    This is a staging/export operation only: it deliberately does not insert financial data
    into SQL Server until column/product mapping has been validated.
    """
    import csv, io
    from openpyxl import load_workbook
    from datetime import datetime
    download_result = download_excel_reports(reports, download_root)
    manifest_path = download_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    selected = {str(x.get("TracingNo")): x for x in reports}
    manifest_by_trace = {str(x.get("TracingNo")): x for x in manifest}
    extracted, failed = [], list(download_result.get("failed", []))
    for trace, report in selected.items():
        item = manifest_by_trace.get(trace, {})
        filename = item.get("FileName")
        if not filename:
            # Do not silently omit reports that failed or were skipped during download.
            if not any(str(x.get("tracing_no") or "") == trace for x in failed):
                failed.append({
                    "symbol": report.get("Symbol") or report.get("CompanyName") or "نماد نامشخص",
                    "company": report.get("CompanyName", ""),
                    "title": report.get("Title", ""),
                    "tracing_no": trace,
                    "stage": "DOWNLOAD",
                    "error": "فایلی برای استخراج در manifest پیدا نشد؛ وضعیت دانلود/وجود پیوست را بررسی کنید.",
                })
            continue
        file_path = download_root / filename
        try:
            data = file_path.read_bytes()
            if not _looks_like_excel(data):
                raise ValueError("فایل دانلودشده Excel واقعی نیست؛ احتمالاً صفحه HTML خطا یا دسترسی کدال ذخیره شده است. دوباره دانلود کنید و URL پیوست را بررسی کنید.")
            sheets = []
            parse_errors = []
            # Detect the actual file signature instead of trusting the .xls/.xlsx suffix.
            # A valid XLSX is a ZIP container; legacy XLS uses the OLE compound signature.
            if data.startswith(b"PK\x03\x04"):
                try:
                    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
                    try:
                        for ws in workbook.worksheets:
                            rows = []
                            for row in ws.iter_rows(values_only=True):
                                vals = [None if v is None else str(v).strip() for v in row]
                                if any(v not in (None, "") for v in vals):
                                    rows.append(vals)
                            if rows:
                                sheets.append({"sheet": ws.title, "rows": rows})
                    finally:
                        workbook.close()
                except Exception as exc:
                    parse_errors.append(f"XLSX/{type(exc).__name__}: {exc}")
            elif data.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
                try:
                    import xlrd
                    book = xlrd.open_workbook(file_contents=data)
                    for ws in book.sheets():
                        rows = []
                        for ri in range(ws.nrows):
                            vals = [str(ws.cell_value(ri, ci)).strip() if ws.cell_value(ri, ci) != "" else None for ci in range(ws.ncols)]
                            if any(v not in (None, "") for v in vals):
                                rows.append(vals)
                        if rows:
                            sheets.append({"sheet": ws.name, "rows": rows})
                except Exception as exc:
                    parse_errors.append(f"XLS/{type(exc).__name__}: {exc}")
            elif _is_office_html(data):
                # Codal's modern "Excel" endpoint serves HTML tables compatible with Excel.
                from html.parser import HTMLParser
                class _TableParser(HTMLParser):
                    def __init__(self):
                        super().__init__(convert_charrefs=True)
                        self.tables, self.table, self.row, self.cell = [], None, None, None
                    def handle_starttag(self, tag, attrs):
                        tag = tag.lower()
                        if tag == "table":
                            self.table = []
                            self.tables.append(self.table)
                        elif tag == "tr" and self.table is not None:
                            self.row = []
                            self.table.append(self.row)
                        elif tag in ("td", "th") and self.row is not None:
                            self.cell = []
                    def handle_data(self, data):
                        if self.cell is not None:
                            self.cell.append(data)
                    def handle_endtag(self, tag):
                        tag = tag.lower()
                        if tag in ("td", "th") and self.cell is not None:
                            value = " ".join(" ".join(self.cell).split()).strip()
                            self.row.append(value or None)
                            self.cell = None
                        elif tag == "tr" and self.row is not None:
                            self.row = None
                        elif tag == "table":
                            self.table = None
                parser = _TableParser()
                parser.feed(data.decode("utf-8-sig", errors="replace"))
                for index, table in enumerate(parser.tables, start=1):
                    rows = [row for row in table if any(value not in (None, "") for value in row)]
                    if rows:
                        sheets.append({"sheet": f"Table_{index}", "rows": rows})
            else:
                sample = data[:160].decode("utf-8", errors="replace").replace("\r", " ").replace("\n", " ")
                raise ValueError(f"امضای فایل با XLS/XLSX و جدول‌های Excel کدال سازگار نیست. شروع محتوا: {sample!r}")
            if parse_errors:
                raise ValueError("خواندن فایل Excel ناموفق بود: " + " | ".join(parse_errors)[:1400])
            if not sheets:
                raise ValueError("فایل Excel باز شد، اما هیچ شیت دارای داده‌ای پیدا نشد.")
            title = str(report.get("Title", ""))
            kind = str(report.get("Kind", ""))
            # Use source labels as hints only; no guessed database mapping.
            category = "SALES" if kind == "MONTHLY_ACTIVITY" else "FINANCIAL_STATEMENT"
            extracted.append({"TracingNo": trace, "Symbol": report.get("Symbol", ""),
                              "CompanyName": report.get("CompanyName", ""), "Title": title,
                              "PublishDate": report.get("PublishDate", ""), "Kind": kind,
                              "DataCategory": category, "FileName": filename, "Sheets": sheets,
                              "MappingStatus": "NEEDS_REVIEW"})
        except Exception as exc:
            failed.append({
                "symbol": report.get("Symbol") or report.get("CompanyName") or "نماد نامشخص",
                "company": report.get("CompanyName", ""),
                "title": report.get("Title", ""),
                "tracing_no": trace,
                "file": filename,
                "stage": "EXTRACT",
                "error": f"EXTRACT_{type(exc).__name__}: {str(exc)[:1000]}",
            })
    extracted_traces = {str(x.get("TracingNo") or "") for x in extracted}
    failed = [x for x in failed if str(x.get("tracing_no") or "") not in extracted_traces]
    bundle = {"CreatedAt": datetime.now().isoformat(timespec="seconds"), "ReportCount": len(extracted),
              "MappingPolicy": "Source rows are parsed into internal Stage tables, validated, then transferred to final tables only when valid.",
              "Reports": extracted, "Errors": failed}
    output = download_root / ("codal_extracted_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json")
    output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"download": download_result, "extracted": extracted, "failed": failed, "export_path": str(output), "export_name": output.name}
