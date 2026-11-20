import os
import json
import uuid
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from .dotenv import load_env
load_env()
from .authdb import initialize, get_user, set_password, list_users, create_user, update_user_policy, set_user_active, force_password_reset, record_login, upsert_market_symbols, list_market_symbols, market_symbol_count, get_market_symbol
from .security import verify_password
from .sqlserver import status, read_view, VIEWS, market_migration_counts, transfer_market_data, transfer_official_market_symbols, official_market_symbol_transfer_status
from .appconfig import save_connection_string, get_connection_string, has_saved_connection
from .codal_center import search_codal_reports, search_codal_for_symbols, download_excel_reports, extract_downloaded_reports
from .codal_resource_stage import stage_resource_reports
from .codal_products_stage import stage_product_reports
from .codal_sales_stage import stage_sales_reports
from .codal_state import initialize as initialize_codal_state, upsert_search_results, list_reports as list_codal_state_reports, get_reports as get_codal_state_reports, update_report as update_codal_state_report, summary as codal_state_summary, migrate_legacy_files as migrate_legacy_codal_files
from .market_symbols import fetch_all_symbols
from .tsetmc_registry import preview_tsetmc_registry, sync_tsetmc_registry
from .historical_prices import history_status, sync_history_batch
from .market_job_ui import read_log, task_status, configure_task, run_now, delete_task, LOG_FILE

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="سامانه تحلیل بنیادی بورس", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("APP_SECRET_KEY", "UNSAFE-DEV-KEY-CHANGE-ME"),
                   same_site="lax", https_only=os.getenv("COOKIE_HTTPS_ONLY", "0") == "1",
                   max_age=28800)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))
CODAL_SEARCH_CACHE = {}

@app.on_event("startup")
def startup():
    initialize()

def user_for(request):
    username = request.session.get("username")
    if not username:
        return None
    user = get_user(username)
    if user is None:
        request.session.clear()
    return user

def market_symbol_rows():
    return [dict(row) for row in list_market_symbols(limit=100000)]

def page(request, template, **context):
    return templates.TemplateResponse(request=request, name=template,
        context={"request": request, "user": user_for(request), **context})

def protect(request):
    user = user_for(request)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if user["must_change_password"] and request.url.path != "/change-password":
        return user, RedirectResponse("/change-password", status_code=303)
    return user, None

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if user_for(request):
        return RedirectResponse("/", status_code=303)
    return page(request, "login.html", error=None)

@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = get_user(username.strip())
    if not user or not verify_password(password, user["password_hash"]):
        return page(request, "login.html", error="نام کاربری یا کلمه عبور صحیح نیست.")
    request.session.clear()
    request.session["username"] = user["username"]
    record_login(user["user_id"])
    return RedirectResponse("/change-password" if user["must_change_password"] else "/", status_code=303)

@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

@app.get("/change-password", response_class=HTMLResponse)
def change_page(request: Request):
    if not user_for(request):
        return RedirectResponse("/login", status_code=303)
    return page(request, "change_password.html", error=None)

@app.post("/change-password", response_class=HTMLResponse)
def change_password(request: Request, current_password: str = Form(...),
                    new_password: str = Form(...), confirm_password: str = Form(...)):
    user = user_for(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    error = None
    if not verify_password(current_password, user["password_hash"]):
        error = "کلمه عبور فعلی صحیح نیست."
    elif len(new_password) < 10:
        error = "کلمه عبور جدید باید حداقل ۱۰ نویسه داشته باشد."
    elif new_password == "admin":
        error = "کلمه عبور پیش‌فرض قابل استفاده نیست."
    elif new_password != confirm_password:
        error = "تکرار کلمه عبور مطابقت ندارد."
    if error:
        return page(request, "change_password.html", error=error)
    set_password(user["user_id"], new_password)
    return RedirectResponse("/", status_code=303)

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    user, redir = protect(request)
    if redir: return redir
    connected, message = status()
    return page(request, "dashboard.html", connected=connected, message=message)

@app.get("/sales", response_class=HTMLResponse)
def sales(request: Request):
    user, redir = protect(request)
    if redir: return redir
    cols, rows, error = read_view("vw_MonthlySales")
    return page(request, "data.html", title="فروش ماهانه", view="dbo.vw_MonthlySales",
                columns=cols, rows=rows, error=error)

@app.get("/forecast", response_class=HTMLResponse)
def forecast(request: Request):
    user, redir = protect(request)
    if redir: return redir
    c1, r1, e1 = read_view("vw_ForecastEffectiveValues")
    c2, r2, e2 = read_view("vw_ForecastCalculatedValues")
    return page(request, "forecast.html", columns=c1, rows=r1, error=e1,
                calc_columns=c2, calc_rows=r2, calc_error=e2)

@app.get("/valuation", response_class=HTMLResponse)
def valuation(request: Request):
    user, redir = protect(request)
    if redir: return redir
    blocks = []
    for key in ("vw_CurrentTTM", "vw_CurrentPE"):
        c, r, e = read_view(key, 50)
        blocks.append({"view": VIEWS[key], "columns": c, "rows": r, "error": e})
    return page(request, "valuation.html", blocks=blocks)



@app.get("/symbols", response_class=HTMLResponse)
def symbols_page(request: Request, notice: str = "", error: str = ""):
    user, redir = protect(request)
    if redir: return redir
    return page(request, "symbols.html", count=market_symbol_count(), notice=notice, error=error,
                is_admin=user["role"] == "ADMIN", refreshed=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

@app.get("/symbols/registry-preview", response_class=HTMLResponse)
def symbols_registry_preview(request: Request):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN":
        return RedirectResponse("/symbols?error=admin", status_code=303)
    try:
        preview = preview_tsetmc_registry(batch_size=1000, max_workers=12)
        return page(request, "tsetmc_registry_preview.html", preview=preview, error=None)
    except Exception as exc:
        return page(request, "tsetmc_registry_preview.html", preview=None,
                    error=f"پیش‌نمایش انجام نشد: {type(exc).__name__}: {str(exc)[:300]}")

@app.post("/symbols/registry-sync", response_class=HTMLResponse)
def symbols_registry_sync(request: Request):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN":
        return RedirectResponse("/symbols?error=admin", status_code=303)
    try:
        transfer_status = official_market_symbol_transfer_status()
        if not transfer_status["local_count"]:
            raise RuntimeError("فهرست محلی نمادها خالی است؛ ابتدا فهرست را از TSETMC تازه‌سازی کنید.")
        if transfer_status["missing_count"]:
            examples = "، ".join(transfer_status["missing_examples"])
            raise RuntimeError(
                f"{transfer_status['missing_count']} نماد رسمی هنوز در dbo.MarketSymbols نیست. "
                "ابتدا عملیات «انتقال نمادهای رسمی به SQL Server» را جداگانه اجرا کنید. "
                f"نمونه InsCodeها: {examples}"
            )
        # Resolve every official instrument identity and validate the complete
        # company/industry mapping before the first SQL Server write.
        preview = preview_tsetmc_registry(batch_size=1000, max_workers=12)
        summary = preview["summary"]
        blockers = {
            "identities_remaining": int(summary.get("identities_remaining") or 0),
            "identity_count_mismatch": int(
                int(summary.get("official_symbols") or 0) !=
                int(summary.get("identities_resolved") or 0)
            ),
            "industry_conflict": int(summary.get("industry_conflict") or 0),
            "industry_missing": int(summary.get("industry_missing") or 0),
            "industry_unknown": int(summary.get("industry_unknown") or 0),
            "review": int(summary.get("review") or 0),
            "symbol_link_conflict": int(summary.get("symbol_link_conflict") or 0),
        }
        blockers = {key: value for key, value in blockers.items() if value}
        if blockers:
            detail = "، ".join(f"{key}={value}" for key, value in blockers.items())
            raise RuntimeError(
                "پیش‌بررسی رجیستری کامل و بدون تعارض نیست؛ هیچ تغییری در SQL Server انجام نشد. "
                + detail
            )

        totals = {
            "attempted": 0, "successfully_resolved": 0, "remaining_identities": 0,
            "industries_from_tsetmc": 0, "parent_industries_from_tsetmc": 0,
            "subindustries_from_tsetmc": 0, "industries_inserted": 0, "industries_updated": 0,
            "companies_inserted": 0, "companies_existing": 0, "companies_reclassified": 0, "symbols_linked": 0,
            "symbols_without_market_row": 0, "industry_conflicts": 0,
            "company_link_conflicts": 0, "representative_symbol_conflicts": 0,
            "errors": []
        }
        previous_remaining = None
        for pass_no in range(20):
            result = sync_tsetmc_registry(batch_size=500, max_workers=12)
            for key in totals:
                if key == "errors":
                    totals[key].extend(result.get(key, []))
                elif key in result:
                    if key == "industries_from_tsetmc":
                        totals[key] = result[key]
                    else:
                        totals[key] += int(result.get(key) or 0)
            remaining = int(result.get("remaining_identities", 0))
            totals["remaining_identities"] = remaining
            if remaining == 0 or int(result.get("attempted", 0)) == 0:
                break
            if previous_remaining is not None and remaining >= previous_remaining:
                break
            previous_remaining = remaining
        if totals["remaining_identities"]:
            notice = (
                f"دریافت هویت‌های TSETMC کامل نشد؛ {totals['remaining_identities']} هویت باقی مانده است. "
                "برای جلوگیری از ثبت طبقه‌بندی ناقص، همگام‌سازی صنایع و شرکت‌ها در SQL Server انجام نشد."
            )
        else:
            notice = (
                f"رجیستری رسمی پردازش شد؛ صنایع: {totals['industries_from_tsetmc']} "
                f"(سرگروه: {totals['parent_industries_from_tsetmc']}، زیرگروه: {totals['subindustries_from_tsetmc']})، "
                f"شرکت جدید: {totals['companies_inserted']}، شرکت موجود: {totals['companies_existing']}، "
                f"بازطبقه‌بندی‌شده: {totals['companies_reclassified']}، "
                f"نماد متصل‌شده: {totals['symbols_linked']}، "
                f"تعارض‌ها: {totals['industry_conflicts'] + totals['company_link_conflicts'] + totals['representative_symbol_conflicts']}."
            )
        if totals["errors"]:
            notice += " خطاهای نمونه: " + " | ".join(totals["errors"][:3])
        if totals["remaining_identities"] or totals["errors"] or totals["industry_conflicts"] or totals["company_link_conflicts"] or totals["representative_symbol_conflicts"]:
            raise RuntimeError(
                "همگام‌سازی رجیستری به علت خطای اعتبارسنجی متوقف شد: "
                + " | ".join(totals["errors"][:5])
            )
        return RedirectResponse("/symbols?notice=" + quote(notice[:1500]), status_code=303)
    except Exception as exc:
        return RedirectResponse("/symbols?error=" + quote(
            f"همگام‌سازی صنایع و شرکت‌ها ناموفق بود: {type(exc).__name__}: {str(exc)[:300]}"), status_code=303)


@app.get("/data-management", response_class=HTMLResponse)
def data_management_page(request: Request):
    user, redir = protect(request)
    if redir: return redir
    return page(request, "data_management.html", is_admin=user["role"] == "ADMIN")


@app.get("/symbols/auto-job", response_class=HTMLResponse)
def market_auto_job_page(request: Request, notice: str = "", error: str = ""):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN": return RedirectResponse("/symbols?error=admin", status_code=303)
    logs = read_log()
    def latest_stage(keys, default):
        for line in reversed(logs):
            if any(key in line for key in keys):
                return line.split("|", 1)[-1].strip()
        return default
    return page(request, "market_auto_job.html", task=task_status(), logs=logs, notice=notice, error=error,
        stage_symbols=latest_stage(("SYMBOLS:", "START: daily"), "هنوز اجرا نشده"),
        stage_transfer=latest_stage(("SQL_SYMBOL_TRANSFER",), "در انتظار انتقال نمادهای رسمی"),
        stage_backfill=latest_stage(("BACKFILL_PASS_START", "BACKFILL pass=", "backfill did not"), "در انتظار اجرا"),
        stage_update=latest_stage(("PRICE_UPDATE:", "price update cycle"), "در انتظار اجرا"),
        stage_final=latest_stage(("FINAL_STATUS:", "END: daily", "FATAL:"), "در انتظار پایان مراحل قبلی"))

@app.get("/symbols/auto-job/log-download")
def market_auto_job_log_download(request: Request):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN": return RedirectResponse("/symbols?error=admin", status_code=303)
    if not LOG_FILE.exists():
        return RedirectResponse("/symbols/auto-job?error=" + quote("هنوز فایل لاگ ایجاد نشده است."), status_code=303)
    return FileResponse(str(LOG_FILE), filename="market_daily_sync.log", media_type="text/plain; charset=utf-8")

@app.post("/symbols/auto-job/configure")
def market_auto_job_configure(request: Request, hour: int = Form(7), minute: int = Form(0)):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN": return RedirectResponse("/symbols?error=admin", status_code=303)
    try:
        message = configure_task(hour, minute)
        return RedirectResponse("/symbols/auto-job?notice=" + quote(message[:200]), status_code=303)
    except Exception as exc:
        return RedirectResponse("/symbols/auto-job?error=" + quote(f"تنظیم جاب انجام نشد: {type(exc).__name__}: {str(exc)[:250]}"), status_code=303)

@app.post("/symbols/auto-job/run")
def market_auto_job_run(request: Request):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN": return RedirectResponse("/symbols?error=admin", status_code=303)
    try:
        run_now()
        return RedirectResponse("/symbols/auto-job?notice=" + quote("اجرای جاب شروع شد؛ برای مشاهده مراحل و خطاها، لاگ را رفرش کنید."), status_code=303)
    except Exception as exc:
        return RedirectResponse("/symbols/auto-job?error=" + quote(f"شروع جاب ناموفق بود: {type(exc).__name__}: {str(exc)[:250]}"), status_code=303)

@app.post("/symbols/auto-job/delete")
def market_auto_job_delete(request: Request):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN": return RedirectResponse("/symbols?error=admin", status_code=303)
    try:
        delete_task()
        return RedirectResponse("/symbols/auto-job?notice=" + quote("زمان‌بندی روزانه حذف شد؛ فایل لاگ حفظ شده است."), status_code=303)
    except Exception as exc:
        return RedirectResponse("/symbols/auto-job?error=" + quote(f"حذف زمان‌بندی ناموفق بود: {type(exc).__name__}: {str(exc)[:250]}"), status_code=303)

@app.get("/symbols/sql-migration", response_class=HTMLResponse)
def symbols_sql_migration_preview(request: Request):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN":
        return RedirectResponse("/symbols?error=admin", status_code=303)
    try:
        counts = market_migration_counts()
        return page(request, "market_migration.html", counts=counts, result=None, error=None)
    except Exception as exc:
        return page(request, "market_migration.html", counts=None, result=None,
                    error=f"بررسی انجام نشد: {str(exc)[:300]}")

@app.post("/symbols/sql-migration", response_class=HTMLResponse)
def symbols_sql_migration_transfer(request: Request, confirm_transfer: str = Form("")):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN":
        return RedirectResponse("/symbols?error=admin", status_code=303)
    if confirm_transfer != "yes":
        return RedirectResponse("/symbols/sql-migration", status_code=303)
    try:
        result = transfer_market_data()
        counts = market_migration_counts()
        return page(request, "market_migration.html", counts=counts, result=result, error=None)
    except Exception as exc:
        try: counts = market_migration_counts()
        except Exception: counts = None
        return page(request, "market_migration.html", counts=counts, result=None,
                    error=f"انتقال انجام نشد و تراکنش SQL Server بازگردانی شد: {str(exc)[:300]}")

@app.get("/symbols/history", response_class=HTMLResponse)
def symbols_history_page(request: Request, mode: str = "", result: str = "", error: str = ""):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN":
        return RedirectResponse("/symbols?error=admin", status_code=303)
    try:
        stats = history_status()
        return page(request, "market_history.html", stats=stats, result=None, error=error)
    except Exception as exc:
        return page(request, "market_history.html", stats=None, result=None,
                    error=error or f"وضعیت تاریخچه خوانده نشد: {type(exc).__name__}: {str(exc)[:240]}")

@app.post("/symbols/history/sync", response_class=HTMLResponse)
def symbols_history_sync(request: Request, mode: str = Form("backfill"),
                          symbols: str = Form("")):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN":
        return RedirectResponse("/symbols?error=admin", status_code=303)
    if mode not in ("backfill", "update"):
        mode = "backfill"
    try:
        tokens = [x.strip().upper() for x in re.split(r"[,،;\s]+", symbols or "") if x.strip()]
        selected_codes = None
        if tokens:
            available = market_symbol_rows()
            by_symbol = {}
            for row in available:
                by_symbol.setdefault(str(row["symbol"]).strip().upper(), []).append(str(row["ins_code"]))
            missing = [token for token in tokens if token not in by_symbol]
            if missing:
                raise ValueError("این نمادها در فهرست فعال SQLite پیدا نشدند: " + "، ".join(missing[:20]))
            selected_codes = sorted({code for token in tokens for code in by_symbol[token]})
        transfer_status = official_market_symbol_transfer_status(selected_codes)
        if transfer_status["missing_count"]:
            examples = "، ".join(transfer_status["missing_examples"])
            raise RuntimeError(
                f"{transfer_status['missing_count']} نماد انتخابی هنوز در dbo.MarketSymbols نیست. "
                "ابتدا از صفحه مدیریت نمادها عملیات انتقال نمادهای رسمی به SQL Server را اجرا کنید. "
                f"نمونه InsCodeها: {examples}"
            )
        result = sync_history_batch(mode, ins_codes=selected_codes)
        if selected_codes:
            result["selected_symbols"] = len(selected_codes)
            result["selection"] = "selected"
        else:
            result["selection"] = "all"
        stats = history_status()
        return page(request, "market_history.html", stats=stats, result=result, error=None)
    except Exception as exc:
        try: stats = history_status()
        except Exception: stats = None
        return page(request, "market_history.html", stats=stats, result=None,
                    error=f"دریافت/ثبت تاریخچه کامل نشد: {type(exc).__name__}: {str(exc)[:260]}")

@app.post("/symbols/refresh", response_class=HTMLResponse)
@app.post("/symbols/sync", response_class=HTMLResponse, include_in_schema=False)
def symbols_refresh(request: Request):
    """Refresh the local SQLite symbol master only; never writes to SQL Server."""
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN":
        return RedirectResponse("/symbols?error=admin", status_code=303)
    try:
        rows = fetch_all_symbols()
        processed = upsert_market_symbols(rows)
        details = (
            f"فهرست رسمی TSETMC در SQLite محلی به‌روزرسانی شد؛ "
            f"دریافت‌شده: {len(rows)}، پردازش‌شده (درج/به‌روزرسانی): {processed}، "
            f"کل نمادهای فعال محلی: {market_symbol_count()}. "
            "در این عملیات هیچ تغییری در SQL Server انجام نشد."
        )
        return RedirectResponse("/symbols?notice=" + quote(details[:1400]), status_code=303)
    except Exception as exc:
        return RedirectResponse("/symbols?error=" + quote(
            f"به‌روزرسانی محلی نمادها انجام نشد: {type(exc).__name__}: {str(exc)[:240]}"),
            status_code=303)


@app.post("/symbols/sql-transfer", response_class=HTMLResponse)
def symbols_sql_transfer(request: Request):
    """Explicitly transfer the local official symbol master to SQL Server."""
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN":
        return RedirectResponse("/symbols?error=admin", status_code=303)
    try:
        result = transfer_official_market_symbols()
        details = (
            f"انتقال نمادهای رسمی به SQL Server انجام شد؛ "
            f"نمادهای بررسی‌شده: {result['official_symbols_found']}، "
            f"درج جدید: {result['inserted_symbols']}، "
            f"به‌روزرسانی: {result['updated_symbols']}، "
            f"رکوردهای legacy غیرفعال‌شده: {result['legacy_placeholders_archived']}."
        )
        return RedirectResponse("/symbols?notice=" + quote(details[:1400]), status_code=303)
    except Exception as exc:
        return RedirectResponse("/symbols?error=" + quote(
            f"انتقال نمادها به SQL Server انجام نشد: {type(exc).__name__}: {str(exc)[:260]}"),
            status_code=303)

def _codal_data_dir():
    return ROOT.parent / "data" / "codal_downloads"


def _codal_state_db():
    return _codal_data_dir() / "codal_workflow.db"


def _codal_page_context(request, *, result=None, error=None, symbol="", kind="monthly",
                        page_no=1, page_size=50, from_date="", selected_market_ids=None):
    reports = []
    workflow_summary = {}
    total_reports = 0
    try:
        initialize_codal_state(_codal_state_db())
        migrate_legacy_codal_files(_codal_state_db(), _codal_data_dir())
        state_rows, total_reports = list_codal_state_reports(
            _codal_state_db(), page=page_no, page_size=page_size)
        for item in state_rows:
            r = dict(item["report"])
            r.update({
                "DownloadStatus": item["download_status"], "DownloadError": item.get("download_error"),
                "ExtractStatus": item["extract_status"], "ExtractError": item.get("extract_error"),
                "ProductStatus": item["product_status"], "ProductError": item.get("product_error"),
                "ProductResult": item.get("product_result"),
                "SalesStatus": item["sales_status"], "SalesError": item.get("sales_error"),
                "SalesResult": item.get("sales_result"),
                "ResourceStatus": item["resource_status"], "ResourceError": item.get("resource_error"),
                "ResourceResult": item.get("resource_result"),
            })
            reports.append(r)
        workflow_summary = codal_state_summary(_codal_state_db())
    except Exception as exc:
        error = error or f"وضعیت مرکز کدال از SQL Server خوانده نشد: {type(exc).__name__}: {str(exc)[:350]}"
    page_count = max(1, (int(total_reports) + int(page_size) - 1) // int(page_size))
    page_no = max(1, min(int(page_no), page_count))
    return page(request, "codal.html", reports=reports, result=result, error=error,
                symbol=symbol, kind=kind, page_no=page_no, page_size=page_size,
                total_reports=total_reports, page_count=page_count, from_date=from_date,
                is_admin=True, market_symbols=market_symbol_rows(),
                selected_market_ids=selected_market_ids or [], cache_key="",
                workflow_summary=workflow_summary)


@app.get("/codal", response_class=HTMLResponse)
def codal_page(request: Request, page_no: int = 1, page_size: int = 50):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN":
        return page(request, "codal.html", reports=[], result=None,
                    error="برای مرکز دریافت کدال دسترسی مدیر لازم است.", symbol="", kind="monthly",
                    page_no=1, page_size=50, total_reports=0, page_count=1,
                    from_date="", is_admin=False, market_symbols=market_symbol_rows(),
                    selected_market_ids=[], cache_key="", workflow_summary={})
    page_size = max(50, min(int(page_size or 50), 200))
    return _codal_page_context(request, page_no=max(1, page_no), page_size=page_size)


@app.post("/codal", response_class=HTMLResponse)
def codal_action(request: Request, action: str = Form(...), symbol: str = Form(""),
                 kind: str = Form("monthly"), page_size: int = Form(50),
                 market_symbol_ids: list[str] = Form([]), selected_traces: list[str] = Form([]),
                 cache_key: str = Form(""), from_date: str = Form("")):
    user, redir = protect(request)
    if redir: return redir
    page_size = max(50, min(int(page_size or 50), 200))
    if user["role"] != "ADMIN":
        return _codal_page_context(request, error="برای دریافت گزارش‌های کدال دسترسی مدیر لازم است.",
                                   symbol=symbol, kind=kind, page_size=page_size, from_date=from_date,
                                   selected_market_ids=market_symbol_ids)
    if kind not in ("monthly", "financial", "both"): kind = "monthly"
    result, error = None, None
    data_dir = _codal_data_dir()
    db_path = _codal_state_db()
    chosen_rows = []
    try:
        initialize_codal_state(db_path)
        migrate_legacy_codal_files(db_path, data_dir)
        if action == "search":
            selected_market_ids = {int(x) for x in market_symbol_ids if str(x).isdigit()}
            selected_symbols = []
            for market_id in selected_market_ids:
                row = get_market_symbol(market_id)
                if row:
                    selected_symbols.append(row["symbol"])
            found = search_codal_for_symbols(selected_symbols, kind=kind, max_pages=None,
                                              page_size=page_size, from_date=from_date)
            upsert_search_results(db_path, found)
            result = {"operation": "search", "found": len(found),
                      "message": "جست‌وجوی تمام صفحات کدال انجام و نتایج در SQL Server ذخیره شد."}
        elif action in ("download_extract", "stage_products", "stage_sales", "stage_resources"):
            stored = list_codal_state_reports(db_path)
            selected = {str(x).strip() for x in selected_traces if str(x).strip()}
            chosen_rows = [x for x in stored if not selected or x["tracing_no"] in selected]
            if not chosen_rows:
                error = "گزارشی برای این عملیات انتخاب نشده یا هنوز جست‌وجویی ثبت نشده است."
            elif action == "download_extract":
                eligible = [x for x in chosen_rows if x["extract_status"] != "SUCCESS"]
                extraction = extract_downloaded_reports([x["report"] for x in eligible], data_dir) if eligible else {
                    "extracted": [], "failed": [], "export_path": None}
                extracted_by = {str(x.get("TracingNo")): x for x in extraction.get("extracted", [])}
                failed_by = {str(x.get("tracing_no")): x for x in extraction.get("failed", [])}
                manifest_path = data_dir / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
                manifest_by = {str(x.get("TracingNo")): x for x in manifest}
                for row in eligible:
                    trace = row["tracing_no"]
                    if trace in extracted_by:
                        item = extracted_by[trace]
                        update_codal_state_report(db_path, trace, download_status="SUCCESS",
                            download_file=item.get("FileName"), download_error=None,
                            downloaded_at=datetime.now().isoformat(timespec="seconds"),
                            extract_status="SUCCESS", extraction_json=json.dumps(item, ensure_ascii=False),
                            extract_error=None, extracted_at=datetime.now().isoformat(timespec="seconds"))
                    else:
                        failure = failed_by.get(trace, {"error": "دانلود/استخراج نتیجه‌ای برای این گزارش برنگرداند."})
                        manifest_item = manifest_by.get(trace)
                        if manifest_item and manifest_item.get("FileName"):
                            update_codal_state_report(db_path, trace, download_status="SUCCESS",
                                download_file=manifest_item.get("FileName"), download_error=None)
                            update_codal_state_report(db_path, trace, extract_status="FAILED",
                                extract_error=failure.get("error", "خطای استخراج"))
                        else:
                            update_codal_state_report(db_path, trace, download_status="FAILED",
                                download_error=failure.get("error", "خطای دانلود"))
                            update_codal_state_report(db_path, trace, extract_status="FAILED",
                                extract_error=failure.get("error", "خطای استخراج"))
                result = {"operation": "download_extract", "extracted": extraction.get("extracted", []),
                          "failed": extraction.get("failed", []), "already_done": len(chosen_rows)-len(eligible),
                          "export_name": Path(extraction["export_path"]).name if extraction.get("export_path") else None}
            else:
                eligible = []
                for row in chosen_rows:
                    if row["extract_status"] != "SUCCESS" or not row.get("extraction"):
                        continue
                    if row["extraction"].get("Kind") != "MONTHLY_ACTIVITY":
                        continue
                    status_key = {"stage_products": "product_status",
                                  "stage_sales": "sales_status",
                                  "stage_resources": "resource_status"}[action]
                    if row.get(status_key) != "SUCCESS":
                        # Older cached extractions did not persist CompanyName; recover it
                        # from the original Codal search result without forcing a re-download.
                        report_data = dict(row["extraction"])
                        if not report_data.get("CompanyName"):
                            report_data["CompanyName"] = (row.get("report") or {}).get("CompanyName", "")
                        eligible.append((row["tracing_no"], report_data))
                reports_to_process = [item[1] for item in eligible]
                if action == "stage_products":
                    operation = stage_product_reports(reports_to_process) if eligible else {
                        "summaries": [], "failed": [], "total_rows": 0, "valid_rows": 0,
                        "invalid_rows": 0, "products_created": 0, "mappings_added": 0}
                    status_key, error_key, result_key, time_key = (
                        "product_status", "product_error", "product_result_json", "product_processed_at")
                    summary_label = "product"
                elif action == "stage_sales":
                    operation = stage_sales_reports(reports_to_process) if eligible else {
                        "summaries": [], "failed": [], "total_rows": 0, "valid_rows": 0,
                        "invalid_rows": 0, "transferred_rows": 0}
                    status_key, error_key, result_key, time_key = (
                        "sales_status", "sales_error", "sales_result_json", "sales_processed_at")
                    summary_label = "sales"
                else:
                    operation = stage_resource_reports(reports_to_process) if eligible else {
                        "summaries": [], "failed": [], "total_rows": 0, "valid_rows": 0,
                        "invalid_rows": 0, "transferred_rows": 0}
                    status_key, error_key, result_key, time_key = (
                        "resource_status", "resource_error", "resource_result_json", "resource_processed_at")
                    summary_label = "resource"
                summaries = {str(x.get("tracing_no")): x for x in operation.get("summaries", [])}
                failures = {str(x.get("tracing_no")): x for x in operation.get("failed", [])}
                for trace, report in eligible:
                    if trace in failures:
                        update_codal_state_report(db_path, trace, **{
                            status_key: "FAILED", error_key: failures[trace].get("error", "خطای پردازش"),
                            result_key: None, time_key: None})
                    elif trace in summaries:
                        sm = summaries[trace]
                        invalid = int(sm.get("invalid", 0))
                        transferred = int(sm.get("transferred", 0))
                        row_count = int(sm.get("rows", 0))
                        success = invalid == 0
                        if action in ("stage_sales", "stage_resources"):
                            success = success and transferred == row_count
                        message = None if success else (
                            f"{invalid} ردیف نیازمند بررسی است." if invalid else
                            f"انتقال کامل انجام نشد؛ ردیف‌های منتقل‌شده: {transferred} از {row_count}.")
                        update_codal_state_report(db_path, trace, **{
                            status_key: "SUCCESS" if success else "FAILED",
                            error_key: message,
                            result_key: json.dumps(sm, ensure_ascii=False),
                            time_key: datetime.now().isoformat(timespec="seconds") if success else None})
                    else:
                        update_codal_state_report(db_path, trace, **{
                            status_key: "FAILED", error_key: "عملیات برای این گزارش نتیجه‌ای برنگرداند.",
                            result_key: None, time_key: None})
                result = {"operation": action, summary_label: operation,
                          "failed": operation.get("failed", []),
                          "already_done": len(chosen_rows)-len(eligible)}
                if not eligible:
                    result["message"] = "گزارش فعالیت ماهانه استخراج‌شده و آماده‌ای برای این عملیات انتخاب نشده است."
        else:
            error = "عملیات درخواستی معتبر نیست."
    except Exception as exc:
        error = f"عملیات مرکز کدال ناموفق بود ({type(exc).__name__}): {str(exc)[:500]}"
        detail = f"{type(exc).__name__}: {str(exc)[:700]}"
        status_key = {"stage_products": "product_status", "stage_sales": "sales_status",
                      "stage_resources": "resource_status"}.get(action)
        error_key = {"stage_products": "product_error", "stage_sales": "sales_error",
                     "stage_resources": "resource_error"}.get(action)
        result_key = {"stage_products": "product_result_json", "stage_sales": "sales_result_json",
                      "stage_resources": "resource_result_json"}.get(action)
        time_key = {"stage_products": "product_processed_at", "stage_sales": "sales_processed_at",
                    "stage_resources": "resource_processed_at"}.get(action)
        if action == "download_extract":
            for row in chosen_rows:
                trace = row.get("tracing_no")
                if trace and row.get("extract_status") != "SUCCESS":
                    update_codal_state_report(db_path, trace, download_status="FAILED",
                        download_error=detail, extract_status="FAILED", extract_error=detail)
        elif status_key and error_key:
            for row in chosen_rows:
                trace = row.get("tracing_no")
                if trace and row.get(status_key) != "SUCCESS":
                    update_codal_state_report(db_path, trace, **{
                        status_key: "FAILED", error_key: detail,
                        result_key: None, time_key: None})
    return _codal_page_context(request, result=result, error=error, symbol=symbol, kind=kind,
                               page_no=1, page_size=page_size, from_date=from_date,
                               selected_market_ids=market_symbol_ids)


@app.get("/codal/export/{filename}")
def codal_export(request: Request, filename: str):
    user, redir = protect(request)
    if redir:
        return redir
    if user["role"] != "ADMIN":
        return RedirectResponse("/", status_code=303)
    # Restrict downloads to generated extraction JSON files inside the Codal data folder.
    if not re.fullmatch(r"codal_extracted_[0-9]{8}_[0-9]{6}\.json", filename):
        return RedirectResponse("/codal", status_code=303)
    target = ROOT.parent / "data" / "codal_downloads" / filename
    if not target.is_file():
        return RedirectResponse("/codal", status_code=303)
    return FileResponse(target, media_type="application/json", filename=filename)

@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, notice: str = "", error: str = ""):
    user, redir = protect(request)
    if redir: return redir
    connected, message = status()
    users = list_users() if user["role"] == "ADMIN" else []
    cs = get_connection_string()
    return page(request, "settings.html", connected=connected, message=message,
                configured=bool(cs), saved_in_app=has_saved_connection(),
                users=users, notice=notice, error=error, is_admin=user["role"] == "ADMIN")

@app.post("/settings/connection/test")
def settings_connection_test(request: Request, server: str = Form(...), database: str = Form(...),
                             auth_mode: str = Form(...), username: str = Form(""), password: str = Form(""),
                             driver: str = Form(...), trust_server_certificate: str = Form("no")):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN": return RedirectResponse("/settings?error=دسترسی+مجاز+نیست", status_code=303)
    cs, err = build_sql_connection(server, database, auth_mode, username, password, driver, trust_server_certificate == "yes")
    if err: return RedirectResponse("/settings?error=" + err, status_code=303)
    try:
        import pyodbc
        with pyodbc.connect(cs, timeout=5) as conn:
            conn.cursor().execute("SELECT 1").fetchone()
        return RedirectResponse("/settings?notice=آزمون+اتصال+موفق+بود؛+برای+ماندگاری+تنظیمات+آن+را+ذخیره+کنید", status_code=303)
    except Exception as exc:
        # Avoid leaking server names, usernames, passwords, or driver details.
        return RedirectResponse("/settings?error=آزمون+اتصال+ناموفق+بود؛+سرور،+دیتابیس،+روش+احراز+هویت،+درایور+ODBC+و+مجوزها+را+بررسی+کنید+(" + type(exc).__name__ + ")", status_code=303)

@app.post("/settings/connection/save")
def settings_connection_save(request: Request, server: str = Form(...), database: str = Form(...),
                             auth_mode: str = Form(...), username: str = Form(""), password: str = Form(""),
                             driver: str = Form(...), trust_server_certificate: str = Form("no")):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN": return RedirectResponse("/settings?error=دسترسی+مجاز+نیست", status_code=303)
    cs, err = build_sql_connection(server, database, auth_mode, username, password, driver, trust_server_certificate == "yes")
    if err: return RedirectResponse("/settings?error=" + err, status_code=303)
    try:
        save_connection_string(cs)
    except Exception as exc:
        return RedirectResponse("/settings?error=ذخیره+تنظیمات+ناموفق+بود+؛+APP_SECRET_KEY+را+بررسی+کنید+(" + type(exc).__name__ + ")", status_code=303)
    return RedirectResponse("/settings?notice=تنظیمات+اتصال+به+صورت+رمزگذاری‌شده+ذخیره+شد", status_code=303)

def build_sql_connection(server, database, auth_mode, username, password, driver, trust_cert):
    server, database = server.strip(), database.strip()
    if not server or not database:
        return None, "نام+سرور+و+دیتابیس+الزامی+است"
    if auth_mode not in ("sql", "windows"):
        return None, "روش+احراز+هویت+نامعتبر+است"
    if driver not in ("ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server"):
        return None, "درایور+ODBC+نامعتبر+است"
    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}", f"DATABASE={database}", "Encrypt=yes", f"TrustServerCertificate={'yes' if trust_cert else 'no'}", "Connection Timeout=5"]
    if auth_mode == "sql":
        if not username.strip() or not password:
            return None, "نام+کاربری+و+رمز+عبور+SQL+الزامی+است"
        # ODBC braced values safely preserve semicolons and closing braces.
        def brace(value): return "{" + value.replace("}", "}}") + "}"
        parts.extend([f"UID={brace(username.strip())}", f"PWD={brace(password)}"])
    else:
        parts.append("Trusted_Connection=yes")
    return ";".join(parts) + ";", None

@app.post("/settings/users/create")
def settings_create_user(request: Request, username: str = Form(...), password: str = Form(...),
                         force_change_on_first_login: str = Form("no")):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN": return RedirectResponse("/settings?error=دسترسی+مجاز+نیست", status_code=303)
    username = username.strip()
    if len(username) < 3 or len(username) > 50 or not username.replace("_", "").replace("-", "").isalnum():
        return RedirectResponse("/settings?error=نام+کاربری+باید+حداقل+۳+نویسه+و+فقط+شامل+حروف+و+عدد+و+خط+تیره+باشد", status_code=303)
    if len(password) < 10 or password == "admin":
        return RedirectResponse("/settings?error=رمز+اولیه+باید+حداقل+۱۰+نویسه+باشد", status_code=303)
    try:
        create_user(username, password, "USER", force_change_on_first_login == "yes")
    except Exception:
        return RedirectResponse("/settings?error=این+نام+کاربری+قبلاً+ثبت+شده+یا+اطلاعات+نامعتبر+است", status_code=303)
    return RedirectResponse("/settings?notice=کاربر+جدید+ایجاد+شد", status_code=303)

@app.post("/settings/users/{user_id}/policy")
def settings_user_policy(request: Request, user_id: int, force_change_on_first_login: str = Form("no")):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN": return RedirectResponse("/settings?error=دسترسی+مجاز+نیست", status_code=303)
    update_user_policy(user_id, force_change_on_first_login == "yes")
    return RedirectResponse("/settings?notice=سیاست+تغییر+رمز+ذخیره+شد", status_code=303)

@app.post("/settings/users/{user_id}/reset-password")
def settings_reset_password(request: Request, user_id: int):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN": return RedirectResponse("/settings?error=دسترسی+مجاز+نیست", status_code=303)
    force_password_reset(user_id)
    return RedirectResponse("/settings?notice=تغییر+رمز+در+ورود+بعدی+اجباری+شد", status_code=303)

@app.post("/settings/users/{user_id}/active")
def settings_user_active(request: Request, user_id: int, active: str = Form(...)):
    user, redir = protect(request)
    if redir: return redir
    if user["role"] != "ADMIN": return RedirectResponse("/settings?error=دسترسی+مجاز+نیست", status_code=303)
    if user_id == user["user_id"] and active != "yes":
        return RedirectResponse("/settings?error=امکان+غیرفعال+کردن+حساب+خودتان+وجود+ندارد", status_code=303)
    set_user_active(user_id, active == "yes")
    return RedirectResponse("/settings?notice=وضعیت+کاربر+ذخیره+شد", status_code=303)
