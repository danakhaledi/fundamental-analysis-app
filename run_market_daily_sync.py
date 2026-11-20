"""Daily market sync runner for Windows Task Scheduler; logs all runs and errors."""
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from app.dotenv import load_env
load_env()

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "market_daily_sync.log"
LOCK_FILE = ROOT / "market_daily_sync.lock"


def log(message):
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} | {message}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def env_int(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def main():
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
    except FileExistsError:
        log("SKIP: lock file exists; preventing overlapping runs.")
        return 2

    try:
        from app.market_symbols import fetch_all_symbols
        from app.authdb import upsert_market_symbols, market_symbol_count
        from app.sqlserver import transfer_official_market_symbols
        from app.historical_prices import history_status, sync_history_batch

        max_backfill_passes = env_int("DAILY_BACKFILL_PASSES", 3, 1, 10)
        log("START: daily market synchronization")

        # Refresh the symbol list; fail the run rather than silently proceeding on empty data.
        symbols = fetch_all_symbols()
        if not symbols:
            raise RuntimeError("TSETMC returned an empty symbol list; refusing to treat this as a successful sync.")
        saved = upsert_market_symbols(symbols)
        log(f"SYMBOLS: fetched={len(symbols)}, upserted={saved}, local_total={market_symbol_count()}")

        try:
            transfer = transfer_official_market_symbols()
            log("SQL_SYMBOL_TRANSFER: " + json.dumps(transfer, ensure_ascii=False, default=str))
        except Exception as exc:
            log(f"ERROR SQL_SYMBOL_TRANSFER: {type(exc).__name__}: {exc}")

        # Backfill in batches until a full pass completes. Retry a bounded number of passes
        # so persistent network/server errors are logged without creating an infinite job.
        backfill_done = False
        for pass_no in range(1, max_backfill_passes + 1):
            pass_errors = 0
            log(f"BACKFILL_PASS_START: {pass_no}/{max_backfill_passes}")
            while True:
                result = sync_history_batch("backfill")
                errors = result.get("errors", []) or []
                pass_errors += len(errors)
                log(f"BACKFILL pass={pass_no}: {json.dumps(result, ensure_ascii=False, default=str)}")
                if result.get("cycle_complete"):
                    backfill_done = True
                    break
                if result.get("processed", 0) == 0:
                    break
            if backfill_done and pass_errors == 0:
                break
            if pass_no < max_backfill_passes:
                log(f"WARNING: backfill pass had {pass_errors} reported errors or did not complete; retrying a new pass.")
        if not backfill_done:
            log("ERROR: backfill did not reach a confirmed end state within configured passes.")

        # Update all official symbols once per cycle, in <=100-symbol batches, until the cursor wraps.
        update_done = False
        update_errors = 0
        while True:
            result = sync_history_batch("update")
            errors = result.get("errors", []) or []
            update_errors += len(errors)
            log("PRICE_UPDATE: " + json.dumps(result, ensure_ascii=False, default=str))
            if result.get("cycle_complete"):
                update_done = True
                break
            if result.get("processed", 0) == 0:
                break
        if not update_done:
            log("ERROR: incremental price update cycle did not reach the end of the symbol list.")
        if update_errors:
            log(f"WARNING: price update completed with {update_errors} reported errors; inspect the log and rerun after resolving connectivity/API issues.")

        try:
            stats = history_status()
            log("FINAL_STATUS: " + json.dumps(stats, ensure_ascii=False, default=str))
        except Exception as exc:
            log(f"ERROR FINAL_STATUS: {type(exc).__name__}: {exc}")

        log(f"END: daily market synchronization | backfill_done={backfill_done} | update_cycle_done={update_done} | update_errors={update_errors}")
        return 0 if backfill_done and update_done and update_errors == 0 else 1
    except Exception as exc:
        log(f"FATAL: {type(exc).__name__}: {exc}")
        log(traceback.format_exc())
        return 1
    finally:
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
