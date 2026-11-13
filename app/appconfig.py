import os, sqlite3, hashlib, base64
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

DB_PATH = Path(os.getenv("AUTH_DB_PATH", "app_auth.db"))

def _fernet():
    secret = os.getenv("APP_SECRET_KEY", "").strip()
    if len(secret) < 32 or secret == "UNSAFE-DEV-KEY-CHANGE-ME":
        raise RuntimeError("APP_SECRET_KEY تنظیم نشده یا ناامن است؛ ابتدا یک کلید تصادفی طولانی در .env قرار دهید.")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)

def _connect():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def save_connection_string(connection_string: str):
    token = _fernet().encrypt(connection_string.encode("utf-8")).decode("ascii")
    with _connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS app_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO app_settings(setting_key,setting_value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value, updated_at=CURRENT_TIMESTAMP", ("sqlserver_connection_encrypted", token))

def _with_long_as_max(connection_string: str) -> str:
    """Force ODBC Driver 18 to bind long strings as (N)VARCHAR(MAX).

    Without LongAsMax=Yes, the SQL Server ODBC driver may bind Unicode
    parameters longer than 2,000 characters as legacy NTEXT. That fails
    against SQL Server collations carrying _SC or _UTF8 flags (error 4189).
    """
    parts = [part.strip() for part in str(connection_string or "").split(";") if part.strip()]
    found = False
    updated = []
    for part in parts:
        key, sep, value = part.partition("=")
        if sep and key.strip().casefold() == "longasmax":
            updated.append("LongAsMax=Yes")
            found = True
        else:
            updated.append(part)
    if not found:
        updated.append("LongAsMax=Yes")
    return ";".join(updated) + ";"


def get_connection_string():
    raw = ""
    try:
        with _connect() as c:
            row = c.execute("SELECT setting_value FROM app_settings WHERE setting_key=?", ("sqlserver_connection_encrypted",)).fetchone()
        if row:
            raw = _fernet().decrypt(row["setting_value"].encode("ascii")).decode("utf-8")
    except (sqlite3.Error, InvalidToken, RuntimeError, ValueError):
        # Fall back to .env when no valid encrypted setting exists.
        pass
    if not raw:
        raw = os.getenv("SQLSERVER_CONNECTION_STRING", "").strip()
    return _with_long_as_max(raw) if raw else ""

def has_saved_connection():
    try:
        with _connect() as c:
            row = c.execute("SELECT 1 FROM app_settings WHERE setting_key=?", ("sqlserver_connection_encrypted",)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
