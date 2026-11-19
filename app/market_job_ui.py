"""Persistent UI settings, status and log helpers for the daily market job."""
import json, os, subprocess, sys, threading
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / 'logs'
LOG_FILE = LOG_DIR / 'market_daily_sync.log'
TASK_NAME = 'FundamentalWebApp-DailyMarketSync'
_guard = threading.Lock()

def read_log(limit=300):
    if not LOG_FILE.exists(): return []
    try: return LOG_FILE.read_text(encoding='utf-8', errors='replace').splitlines()[-limit:]
    except OSError as exc: return [f'خطا در خواندن لاگ: {exc}']

def task_status():
    try:
        p = subprocess.run(['schtasks','/Query','/TN',TASK_NAME,'/FO','LIST','/V'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=8)
        if p.returncode: return {'exists':False,'status':'تعریف نشده','raw':''}
        raw = p.stdout
        status = 'تعریف شده'
        for line in raw.splitlines():
            if 'Status:' in line or 'وضعیت:' in line:
                status = line.split(':',1)[-1].strip(); break
        return {'exists':True,'status':status,'raw':raw}
    except Exception as exc: return {'exists':False,'status':f'قابل بررسی نیست ({type(exc).__name__})','raw':''}

def configure_task(hour, minute):
    if not (0 <= hour <= 23 and 0 <= minute <= 59): raise ValueError('ساعت اجرا معتبر نیست.')
    py = ROOT / '.venv' / 'Scripts' / 'python.exe'
    runner = ROOT / 'run_market_daily_sync.py'
    if not py.exists(): raise RuntimeError('محیط .venv پیدا نشد؛ ابتدا run_windows.bat را اجرا کنید.')
    # Use a small wrapper batch file to pass settings reliably to Task Scheduler.
    wrapper = ROOT / 'run_market_daily_task.bat'
    wrapper.write_text('@echo off\r\ncd /d "'+str(ROOT)+'"\r\n"'+str(py)+'" "'+str(runner)+'"\r\n', encoding='utf-8')
    command = f'cmd.exe /c ""{wrapper}""'
    p = subprocess.run(['schtasks','/Create','/F','/SC','DAILY','/TN',TASK_NAME,'/ST',f'{hour:02d}:{minute:02d}','/TR',command], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20)
    if p.returncode: raise RuntimeError((p.stderr or p.stdout or 'ساخت زمان‌بندی ناموفق بود.')[-500:])
    return p.stdout.strip() or 'زمان‌بندی روزانه ذخیره شد.'

def run_now():
    with _guard:
        py = ROOT / '.venv' / 'Scripts' / 'python.exe'; runner = ROOT / 'run_market_daily_sync.py'
        if not py.exists(): raise RuntimeError('محیط .venv پیدا نشد؛ ابتدا run_windows.bat را اجرا کنید.')
        flags = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
        subprocess.Popen([str(py), str(runner)], cwd=str(ROOT), creationflags=flags, close_fds=True)

def delete_task():
    p = subprocess.run(['schtasks','/Delete','/F','/TN',TASK_NAME], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
    if p.returncode: raise RuntimeError((p.stderr or p.stdout or 'حذف زمان‌بندی ناموفق بود.')[-300:])
    return p.stdout.strip()
