import os
import sqlite3
import plistlib
import re
import tempfile
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import gspread

EXPENSE_BUNDLES = {
    "com.kirana2k19",
    "com.amazon.Amazon",
    "net.whatsapp.whatsapp",
    "com.apple.ScreenContinuity",
    "com.apple.MobileSMS",
    "com.apple.mobilesms",
    "com.apple.ichat",
    "com.grofers.Grofers",       # Blinkit
    "com.zepto.consumer",       # Zepto
    "com.kiranakart.zepto",     # Zepto (Alt)
    "com.bundl.swiggy",         # Swiggy
    "com.zomato.zomato",        # Zomato
    "com.ubercab.UberClient",   # Uber
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
IST = ZoneInfo("Asia/Kolkata")
CREDENTIALS_FILE = os.path.join(SCRIPT_DIR, "credentials.json")
SHEET_NAME = "My Expenses"

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
            source_app TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rec_id TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            date_str TEXT,
            time_str TEXT,
            merchant TEXT,
            amount REAL,
            category TEXT,
            source_app TEXT,
            status TEXT DEFAULT 'PENDING'
        )
    """)
    conn.commit()
    conn.close()

def parse_hdfc_sms(text):
    """parses HDFC Bank SMS text to extract merchant, amount"""

    debit_pattern = r"Sent\s+(?:Rs\.?|INR)\s*([\d,]+(?:\.\d{2})?)\s+From\s+HDFC\s+Bank.*?To\s+(.*?)(?:\s+On|\s+Ref|\s*$)"
    debit_match = re.search(debit_pattern, text, re.IGNORECASE | re.DOTALL)

    if debit_match:
        amount = float(debit_match.group(1).replace(",", ""))
        merchant = debit_match.group(2).strip()

        return {"merchant": merchant, "amount": amount}

    # fallback if sms format is unrecognized
    return None

def infer_merchant_category(bundle_id: str, title: str, body: str):
    text = f"{title} {body}".lower()

    if "grofers" in bundle_id or "blinkit" in text:
        return "Blinkit", "Groceries"
    if "kirana2k19" in bundle_id or "zepto" in text:
        return "Zepto", "Groceries"
    if "swiggy" in bundle_id or "instamart" in text:
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

def sync_expense_to_sheet(rec_id, date_str, time_str, merchant, amount, category, payment_mode, source_app):
    if not os.path.exists(CREDENTIALS_FILE):
        print("Warning: credentials.json not found. Skipping Google Sheets sync.")
        return

    try:
        # authenticate using the Service Account JSON
        gc = gspread.service_account(filename=CREDENTIALS_FILE)
        sh = gc.open(SHEET_NAME)
        worksheet = sh.sheet1  # first tab

        row = [rec_id, date_str, time_str, merchant, amount, category, payment_mode, source_app]

        worksheet.append_row(row, value_input_option="USER_ENTERED")
        print(f"Successfully synced ₹{amount} ({merchant}) to Google Sheets.")

    except Exception as e:
        print(f"Failed to sync row to Google Sheets: {e}")

def handle_app_notification(rec_id, date_str, time_str, merchant, amount, category, source_app):
    """called when an app notification arrives"""

    conn = sqlite3.connect("expenses.db")
    cur = conn.cursor()

    cur.execute("""
        INSERT OR IGNORE INTO pending_orders 
        (rec_id, date_str, time_str, merchant, amount, category, source_app, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')
    """, (rec_id, date_str, time_str, merchant, amount, category, source_app))

    conn.commit()
    conn.close()

    # run check immediately to match if SMS came first or if already expired
    handle_expired_pending_orders()

def handle_bank_sms(rec_id, date_str, time_str, amount, sms_merchant, source_app):
    """called when a bank sms notification arrives
    checks any transaction with same amount in last 10 mins
    if found - update pending_orders and expenses table & sync to gsheet
    if not found - update expenses as transfer & sync to gsheet
    """

    conn = sqlite3.connect("expenses.db")
    cur = conn.cursor()

    # Look for a pending app order with matching amount created in the last 10 minutes
    ten_mins_ago = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")

    cur.execute("""
        SELECT id, rec_id, date_str, time_str, merchant, category, source_app 
        FROM pending_orders 
        WHERE status = 'PENDING' 
          AND amount = ? 
          AND created_at >= ?
        ORDER BY created_at DESC LIMIT 1
    """, (amount, ten_mins_ago))

    match = cur.fetchone()

    if match:
        pending_id, app_rec_id, app_date, app_time, app_merchant, category, app_source = match
        cur.execute("UPDATE pending_orders SET status = 'COMPLETED' WHERE id = ?", (pending_id,))
        cur.execute("""
            INSERT OR IGNORE INTO expenses 
            (rec_id, date, time, merchant, amount, category, payment_mode, source_app)
            VALUES (?, ?, ?, ?, ?, ?, "upi, ?)
        """, (app_rec_id, app_date, app_time, app_merchant, amount, category, app_source))

        if cur.rowcount > 0:
            sync_expense_to_sheet(app_rec_id, app_date, app_time, app_merchant, amount, category, "upi", app_source)
            print(f"Matched Bank SMS with App Order! [{app_merchant} - ₹{amount} via upi]")
    else:
        category = "Transfer / Personal"
        
        cur.execute("""
            INSERT OR IGNORE INTO expenses 
            (rec_id, date, time, merchant, amount, category, payment_mode, source_app)
            VALUES (?, ?, ?, ?, ?, ?, 'upi', ?)
        """, (rec_id, date_str, time_str, sms_merchant, amount, category, source_app))

        if cur.rowcount > 0:
            sync_expense_to_sheet(rec_id, date_str, time_str, sms_merchant, amount, category, "upi", source_app)
            print(f"Recorded Standalone Bank SMS Expense: [{sms_merchant} - ₹{amount} via upi]")

    conn.commit()
    conn.close()

def handle_expired_pending_orders():
    """checks for orders older than 10 minutes without matching bank sms and marks them as cash payments"""

    conn = sqlite3.connect("expenses.db")
    cur = conn.cursor()

    ten_mins_ago = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")

    # fetch all orders older than 10 mins still marked as PENDING
    cur.execute("""
        SELECT id, rec_id, date_str, time_str, merchant, amount, category, source_app 
        FROM pending_orders 
        WHERE status = 'PENDING' AND created_at <= ?
    """, (ten_mins_ago,))

    expired_orders = cur.fetchall()

    for order in expired_orders:
        pending_id, rec_id, date_str, time_str, merchant, amount, category, source_app = order
        cur.execute("UPDATE pending_orders SET status = 'COMPLETED' WHERE id = ?", (pending_id,))
        cur.execute("""
            INSERT OR IGNORE INTO expenses 
            (rec_id, date, time, merchant, amount, category, payment_mode, source_app)
            VALUES (?, ?, ?, ?, ?, ?, 'Cash', ?)
        """, (rec_id, date_str, time_str, merchant, amount, category, source_app))
        
        if cur.rowcount > 0:
            sync_expense_to_sheet(date_str, time_str, merchant, amount, category, "Cash", source_app)
            print(f"No Bank SMS within 10 mins. Recorded as CASH: [{merchant} - ₹{amount}]")

    conn.commit()
    conn.close()

def read_and_store_notifications(limit: int = 100, db_path: str = EXPENSES_DB):
    nc_db_src = get_notification_db_path()
    print("Using Notification Center DB (source):", nc_db_src)

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
    
    if "record" not in tables:
        print("WARNING: 'record' table not found in NC DB. The DB was likely just reset.")
        conn_nc.close()
        os.remove(tmp_db_path)
        return

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

    app_notifications_processed = 0
    bank_sms_processed = 0

    for row in cur_nc.fetchall():
        bundle_id = row["bundle_id"]
        print("Bundle Id of current ntf", bundle_id)
        if bundle_id not in EXPENSE_BUNDLES:
            print("bundle id skipped", bundle_id)
            continue

        delivered_ts = row["delivered_date"]
        # utc time
        delivered_at_utc = mac_epoch + timedelta(seconds=delivered_ts) 
        # convert utc to ist 
        delivered_at_ist = delivered_at_utc.astimezone(IST)

        data_blob = row["data"]
        try:
            plist = plistlib.loads(data_blob)
        except Exception:
            plist = {}

        req = plist.get("req", {}) or plist
        title = req.get("title", "") or plist.get("title", "")
        body = req.get("body", "") or plist.get("body", "")
        text = f"{title} {body}".strip()

        date_str = delivered_at_ist.strftime("%Y-%m-%d")
        time_str = delivered_at_ist.strftime("%H:%M")
        rec_id = row["rec_id"]

        # 1. bank sms notification (mobilesms / ichat)
        if "mobilesms" in bundle_id or "ichat" in bundle_id or "hdfc" in text.lower():
            parsed_sms = parse_hdfc_sms(text)
            if parsed_sms:
                # Pass extracted SMS amount to the matcher logic
                handle_bank_sms(
                    rec_id = rec_id,
                    date_str = date_str,
                    time_str = time_str,
                    amount = parsed_sms["amount"],
                    sms_merchant = parsed_sms["merchant"], 
                    source_app = bundle_id
                )
                bank_sms_processed += 1
            continue

        # 2. app notifications (blinkit, zepto, uber, etc.)
        amount = extract_amount(text)
        if amount is None:
            continue

        merchant, category = infer_merchant_category(bundle_id, title, body)

        # route app notification through the pending order handler
        handle_app_notification(
            rec_id = rec_id,
            date_str = date_str,
            time_str = time_str,
            merchant = merchant,
            amount = amount,
            category = category,
            source_app = bundle_id
        )
        app_notifications_processed += 1
        
    conn_nc.close()
    os.remove(tmp_db_path)

    print(f"Processed notifications:")
    print(f"  - App orders added to pending: {app_notifications_processed}")
    print(f"  - Bank SMS processed: {bank_sms_processed}")

def process_notifications():
    print("Starting Expense Processing Pipeline...")
    init_expenses_db()

    # read new app & bank notifications from macOS Notification Center
    read_and_store_notifications(limit=100)

    # resolve any pending orders older than 10 minutes as CASH
    handle_expired_pending_orders()

    print("Pipeline Execution Complete")

if __name__ == "__main__":
    process_notifications()