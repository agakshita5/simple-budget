import os
import sqlite3
import plistlib
import re
import tempfile
from datetime import datetime, timezone, timedelta

EXPENSE_BUNDLES = {
    "com.blinkit.consumer",
    "com.zepto.consumer",
    "com.ubercab.uberclient",
    "com.amazon.amazon",
    "net.whatsapp.whatsapp",
    "com.apple.mobilesms",
    "com.apple.iChat", # mac messages
}

AMOUNT_RE = re.compile(
    r"₹\s*(\d+(?:\.\d{2})?)"
    r"|INR\s*(\d+(?:\.\d{2})?)"
    r"|Total[:\s]+[\$₹]?\s*(\d+(?:\.\d{2})?)"
    r"|Grand Total[:\s]+[\$₹]?\s*(\d+(?:\.\d{2})?)"
    r"|Amount[:\s]+[\$₹]?\s*(\d+(?:\.\d{2})?)"
    r"|paid[:\s]+[\$₹]?\s*(\d+(?:\.\d{2})?)"
    r"|debited[:\s]+[\$₹]?\s*(\d+(?:\.\d{2})?)"
    , re.IGNORECASE
)

# Absolute path to expenses DB (same folder as this script)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXPENSES_DB = os.path.join(SCRIPT_DIR, "expenses.db")

# HELPERS 
def get_notification_db_path() -> str:
    user_home = os.path.expanduser("~")
    db_path = os.path.join(
        user_home,
        "Library",
        "Group Containers",
        "group.com.apple.usernoted",
        "db2",
        "db"
    )
    
    if os.path.exists(db_path):
        return db_path
        
    raise RuntimeError(
        f"Notification Center DB not found at: {db_path}\n"
        "Run a test notification or restart usernoted to create it."
    )

def extract_amount(text: str) -> float | None:
    m = AMOUNT_RE.search(text or "")
    if not m:
        return None
    val = next((g for g in m.groups() if g is not None), None)
    return float(val) if val else None

def init_expenses_db(db_path: str = EXPENSES_DB):
    print("Initializing expenses DB at:", db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rec_id INTEGER UNIQUE,
            date TEXT NOT NULL,
            time TEXT,
            merchant TEXT,
            amount REAL NOT NULL,
            category TEXT,
            payment_mode TEXT,
            source_app TEXT,
            raw_title TEXT,
            raw_body TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def infer_merchant_category(bundle_id: str, title: str, body: str):
    text = f"{title} {body}".lower()

    if "blinkit" in bundle_id or "blinkit" in text:
        return "Blinkit", "Groceries"
    if "zepto" in bundle_id or "zepto" in text:
        return "Zepto", "Groceries"
    if "grofers" in bundle_id or "instamart" in text:
        return "Instamart", "Groceries"
    if "uber" in bundle_id or "uber" in text:
        return "Uber", "Transport"
    if "amazon" in bundle_id or "amazon" in text:
        return "Amazon", "Shopping"

    if "whatsapp" in bundle_id:
        if "blinkit" in text: 
            return "Blinkit (WhatsApp)", "Groceries"
        if "zepto" in text:
            return "Zepto (WhatsApp)", "Groceries"
        if "instamart" in text:
            return "Instamart (WhatsApp)", "Groceries"
        if "amazon" in text:
            return "Amazon (WhatsApp)", "Shopping"
        return "WhatsApp Expense", "Other"

    if "mobilesms" in bundle_id or "ichat" in bundle_id:
        return "SMS/iMessage", "Other"

    return bundle_id, "Other"

def read_and_store_notifications(limit: int = 100, db_path: str = EXPENSES_DB):
    nc_db_src = get_notification_db_path()
    print("Using Notification Center DB (source):", nc_db_src)
    print("Expenses DB (target):", db_path)

    # Verify source DB exists
    if not os.path.exists(nc_db_src):
        raise RuntimeError(f"Notification Center DB not found at: {nc_db_src}")

    # Create the temporary file object using SQLITE BACKUP API, which captures db, db-wal, and db-shm altogether
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_file:
        tmp_db_path = tmp_file.name

    try:
        src_conn = sqlite3.connect(f"file:{nc_db_src}?mode=ro", uri=True) # source connction (READ-ONLY)
        dst_conn = sqlite3.connect(tmp_db_path) # destination connection
        src_conn.backup(dst_conn)
        src_conn.close()
        dst_conn.close()
        print("Safely backed up live DB (including WAL) to temp file.")
    except Exception as e:
        raise RuntimeError(f"Failed to copy Notification Center DB safely: {e}")

    conn_nc = sqlite3.connect(tmp_db_path)
    conn_nc.row_factory = sqlite3.Row
    cur_nc = conn_nc.cursor()

    # Debug: list tables in temp DB
    cur_nc.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur_nc.fetchall()]
    print("Tables in temp NC DB:", tables)
    
    if "record" not in tables:
        print("WARNING: 'record' table not found in NC DB. The DB was likely just reset.")
        conn_nc.close()
        os.remove(tmp_db_path)
        return
    
    conn_exp = sqlite3.connect(db_path)
    cur_exp = conn_exp.cursor()

    # Join record with app to get bundle identifier
    cur_nc.execute("""
        SELECT
            r.rec_id,
            r.delivered_date,
            r.data,
            a.identifier AS bundle_id
        FROM record r
        JOIN app a ON r.app_id = a.app_id
        ORDER BY r.delivered_date DESC
        LIMIT ?
    """, (limit,))

    mac_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc) # Apple Core Data epoch is Jan 1, 2001 UTC
    inserted = 0
    skipped_no_amount = 0
    skipped_bundle = 0

    for row in cur_nc.fetchall():
        bundle_id = row["bundle_id"]
        print("Bundle Id of current ntf", {bundle_id})
        if bundle_id not in EXPENSE_BUNDLES:
            skipped_bundle += 1
            continue

        delivered_ts = row["delivered_date"]
        delivered_at = mac_epoch + timedelta(seconds=delivered_ts)

        data_blob = row["data"]
        try:
            plist = plistlib.loads(data_blob)
        except Exception:
            plist = {}

        req = plist.get("req", {}) or plist
        title = req.get("title", "") or plist.get("title", "")
        body = req.get("body", "") or plist.get("body", "")

        amount = extract_amount(f"{title} {body}")
        if amount is None:
            skipped_no_amount += 1
            continue

        merchant, category = infer_merchant_category(bundle_id, title, body)

        cur_exp.execute("""
            INSERT OR IGNORE INTO expenses (
                rec_id, date, time, merchant, amount, category, payment_mode, source_app, raw_title, raw_body
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["rec_id"],
            delivered_at.strftime("%Y-%m-%d"),
            delivered_at.strftime("%H:%M"),
            merchant,
            amount,
            category,
            "UPI/Cash",
            bundle_id,
            title,
            body,
        ))
        inserted += 1

    conn_exp.commit()
    conn_exp.close()
    conn_nc.close()

    print(f"Processed recent notifications:")
    print(f"  - Inserted expenses: {inserted}")
    print(f"  - Skipped (no amount match): {skipped_no_amount}")
    print(f"  - Skipped (untracked app bundle): {skipped_bundle}")

    os.remove(tmp_db_path)

if __name__ == "__main__":
    init_expenses_db()
    read_and_store_notifications()