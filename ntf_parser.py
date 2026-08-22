import os
import sqlite3
import plistlib
import re
import glob
import shutil
import hashlib
import tempfile
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import gspread
import logging

# absolute path for the log file
log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracker.log")

# configure the logging system once
logging.basicConfig(
    filename=log_path,
    level=logging.INFO, 
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

EXPENSE_BUNDLES = {
    # delivery/food
    "com.grofers.consumer",     # blinkit
    "com.zeptonow.customer",    # zepto
    "bundl.instamart",          # instamart
    "bundl.swiggy",             # swiggy
    "com.zomato.zomato",        # zomato
    "com.dominosin.olo",        # dominos
    # transport/shopping
    "com.ubercab.UberClient",   # uber
    "com.amazon.AmazonIN",      # amazon india
    "com.myntra.Myntra",        # myntra
    # payments/bank
    "com.hdfcbank.ios.now",     # hdfc bank app
    "com.phonepe.PhonePeApp",   # phonepe
    "com.google.paisa",         # google pay india
    # messaging (bank sms + order pings)
    # macOS Continuity variants
    "com.apple.MobileSMS",      # ios messages (mirrored)
    "com.apple.mobilesms",      # macos continuity sms forwarding
    "com.apple.ichat"
}

AMOUNT_RE = re.compile(
    r"(?:₹|INR|Rs\.?)\s*([\d,]+(?:\.\d{1,2})?)"
    r"|(?:grand total|total|amount|paid|debited|charged|spent)\s*[:\-]?\s*"
    r"(?:₹|INR|Rs\.?|\$)?\s*([\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

SMS_MERCHANTS = [
    ("BLINKIT", ("Blinkit", "Groceries")),
    ("GROFERS", ("Blinkit", "Groceries")),
    ("ZEPTO", ("Zepto", "Groceries")),
    ("SWIGGY INSTAMART", ("Instamart", "Groceries")),
    ("INSTAMART", ("Instamart", "Groceries")),
    ("SWIGGY", ("Swiggy", "Food")),
    ("BUNDL", ("Swiggy", "Food")),
    ("ZOMATO", ("Zomato", "Food")),
    ("DOMINO", ("Dominos", "Food")),
    ("AMAZON", ("Amazon", "Shopping")),
    ("MYNTRA", ("Myntra", "Shopping")),
    ("UBER", ("Uber", "Transport"))
]

ORDER_RE = re.compile(
    r"order (?:confirmed|placed|delivered|received|completed)"
    r"|your order (?:is|has been) (?:delivered|on the way|out for delivery)"
    r"|out for delivery"
    r"|arriving (?:today|tomorrow)"
    r"|pay your driver"
    r"|trip (?:completed|ended)"
    r"|payment successful"
    r"|(?:total|amount) paid"
    r"|(?:has been|was) debited",
    re.IGNORECASE,
)

# absolute path to expenses DB (same folder as this script)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXPENSES_DB = os.path.join(SCRIPT_DIR, "expenses.db")
IST = ZoneInfo("Asia/Kolkata")
CREDENTIALS_FILE = os.path.join(SCRIPT_DIR, "credentials.json")
SHEET_NAME = "My Expenses"

CASH = "Cash (NEEDS AMOUNT)"

# iPhone Mirroring writes mirrored notifications here as NSKeyedArchiver plists.
# this is a different store from usernoted's sqlite db, which only holds mac-local
# notifications (no third-party iOS app ever appears there)
REMOTE_NOTIF_DIR = os.path.expanduser(
    "~/Library/Group Containers/group.com.apple.UserNotifications"
    "/Library/UserNotifications/Remote"
)

# apple's core data epoch: 2001-01-01 UTC
MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

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

def copy_nc_db(dst_path: str) -> bool:
    """copy the live Notification Center DB using plain file reads"""

    src = get_notification_db_path()
    try:
        shutil.copy2(src, dst_path)
        if os.path.exists(src + "-wal"):
            shutil.copy2(src + "-wal", dst_path + "-wal")
        return True
    except OSError as e:
        logging.error(f"Could not copy Notification Center DB: {e}")
        return False

def unarchive_plist(path: str):
    """read an NSKeyedArchiver plist into plain dicts/lists"""
    
    with open(path, "rb") as fh:
        raw = plistlib.load(fh)
    objects = raw.get("$objects", [])

    def resolve(node, depth=0):
        if depth > 16:
            return None
        if isinstance(node, plistlib.UID):
            return resolve(objects[node.data], depth + 1)
        if isinstance(node, dict):
            if "NS.keys" in node:
                return {resolve(k, depth + 1): resolve(v, depth + 1)
                        for k, v in zip(node["NS.keys"], node["NS.objects"])}
            if "NS.objects" in node:
                return [resolve(v, depth + 1) for v in node["NS.objects"]]
            return {k: resolve(v, depth + 1) for k, v in node.items() if k != "$class"}
        return node

    return resolve(raw["$top"]["root"]) if objects else None

def extract_amount(text: str) -> float | None:
    m = AMOUNT_RE.search(text or "")
    if not m:
        return None
    val = next((g for g in m.groups() if g is not None), None)
    if not val:
        return None
    try:
        return float(val.replace(",", ""))
    except ValueError:
        return None

def looks_like_expense(text: str) -> bool:
    """true only for notifications describing a transaction, not an ad"""
    return bool(text and ORDER_RE.search(text))

def init_expenses_db(db_path: str = EXPENSES_DB):
    logging.info(f"Initializing expenses DB at: {db_path}")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rec_id INTEGER UNIQUE,
            date TEXT NOT NULL,
            time TEXT,
            merchant TEXT,
            amount REAL,
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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_notifications (
            key TEXT PRIMARY KEY,
            seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            bundle_id TEXT
        )
    """)

    conn.commit()
    conn.close()

def notif_key(source: str, bundle_id: str, delivered_ts, text: str) -> str:
    """stable id for one notification, derived from its content"""
    raw = f"{source}|{bundle_id}|{delivered_ts}|{text}"
    return f"{source}:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:20]}"

def already_processed(key: str) -> bool:
    conn = sqlite3.connect(EXPENSES_DB)
    hit = conn.execute(
            "SELECT 1 FROM processed_notifications WHERE key = ?", (key,)
        ).fetchone()
    conn.close()
    return hit is not None

def mark_processed(key: str, bundle_id: str):
    conn = sqlite3.connect(EXPENSES_DB)
    conn.execute(
        "INSERT OR IGNORE INTO processed_notifications (key, bundle_id) VALUES (?, ?)",
        (key, bundle_id),
    )
    conn.commit()
    conn.close()

def infer_from_sms_merchant(sms_merchant: str):
    payee = (sms_merchant or "").upper()
    for needle, result in SMS_MERCHANTS:
        if needle in payee:
            return result
    return None

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
    if "zeptonow" in bundle_id or "zepto" in text:
        return "Zepto", "Groceries"
    if "swiggy" in bundle_id:
        return "Swiggy", "Food"
    if "zomato" in bundle_id:
        return "Zomato", "Food"
    if "instamart" in text:
        return "Instamart", "Groceries"
    if "ubercab" in bundle_id or "uber" in text:
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
        logging.warning("Warning: credentials.json not found -> skipping google sheets sync")
        return

    try:
        # authenticate using the Service Account JSON
        gc = gspread.service_account(filename=CREDENTIALS_FILE)
        sh = gc.open(SHEET_NAME)
        worksheet = sh.sheet1  # first tab

        row = [rec_id, date_str, time_str, merchant, amount, category, payment_mode, source_app]

        worksheet.append_row(row, value_input_option="RAW")
        logging.info(f"Successfully synced ₹{amount} ({merchant}) to Google Sheets")

    except Exception as e:
        logging.error(f"Failed to sync row to Google Sheets: {e}", exc_info=True)

def handle_app_notification(rec_id, date_str, time_str, merchant, amount, category, source_app):
    """called when an app notification arrives"""

    conn = sqlite3.connect(EXPENSES_DB)
    cur = conn.cursor()

    thirty_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    
    cur.execute("""
        SELECT id, rec_id, date_str, time_str, merchant, category, source_app
        FROM pending_orders
        WHERE status = 'PENDING'
            AND source_app = ?
            AND created_at >= ?
        ORDER BY created_at DESC LIMIT 1
    """, (source_app, thirty_mins_ago))

    match = cur.fetchone()

    if match:
        pending_id = match[0]
        if amount is not None:
            cur.execute(
                "UPDATE pending_orders SET amount = COALESCE(amount, ?) WHERE id = ?",
                (amount, pending_id),
            )
    else:
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

    matching runs in three tiers:
    - a pending order with the SAME amount in the last 30 min
    - a pending order with NO amount in the last 30 min 
    - no order at all -> classify from the SMS payee name
    """

    conn = sqlite3.connect(EXPENSES_DB)
    cur = conn.cursor()

    thirty_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")

    # tier 1: exact amount
    cur.execute("""
        SELECT id, rec_id, date_str, time_str, merchant, category, source_app
        FROM pending_orders
        WHERE status = 'PENDING'
          AND amount = ?
          AND created_at >= ?
        ORDER BY created_at DESC LIMIT 1
    """, (amount, thirty_mins_ago))

    match = cur.fetchone()
    match_kind = "amount"

    # tier 2: time window only, for orders that never carried an amount
    if not match:
        cur.execute("""
            SELECT id, rec_id, date_str, time_str, merchant, category, source_app
            FROM pending_orders
            WHERE status = 'PENDING'
              AND amount IS NULL
              AND created_at >= ?
            ORDER BY created_at DESC
        """, (thirty_mins_ago,))
        candidates = cur.fetchall()

        known = infer_from_sms_merchant(sms_merchant)
        if known and candidates:
            match = next((c for c in candidates if c[4] == known[0]), candidates[0])
        elif candidates:
            match = candidates[0]
        match_kind = "time window"

    if match:
        pending_id, app_rec_id, app_date, app_time, app_merchant, category, app_source = match
        cur.execute("UPDATE pending_orders SET status = 'COMPLETED' WHERE id = ?", (pending_id,))
        cur.execute("""
            INSERT OR IGNORE INTO expenses 
            (rec_id, date, time, merchant, amount, category, payment_mode, source_app)
            VALUES (?, ?, ?, ?, ?, ?, 'upi', ?)
        """, (app_rec_id, app_date, app_time, app_merchant, amount, category, app_source))

        if cur.rowcount > 0:
            sync_expense_to_sheet(app_rec_id, app_date, app_time, app_merchant, amount, category, "upi", app_source)
            logging.info(f"Matched Bank SMS to App Order by {match_kind}: [{app_merchant} - ₹{amount} via upi]")
    else:
        # tier 3: no pending order 
        known = infer_from_sms_merchant(sms_merchant)
        if known:
            merchant, category = known
        else:
            merchant, category = sms_merchant, "Transfer / Personal"

        cur.execute("""
            INSERT OR IGNORE INTO expenses
            (rec_id, date, time, merchant, amount, category, payment_mode, source_app)
            VALUES (?, ?, ?, ?, ?, ?, 'upi', ?)
        """, (rec_id, date_str, time_str, merchant, amount, category, source_app))

        if cur.rowcount > 0:
            sync_expense_to_sheet(rec_id, date_str, time_str, merchant, amount, category, "upi", source_app)
            logging.info(f"Recorded Standalone Bank SMS Expense: [{merchant} - ₹{amount} | {category} via upi]")

    conn.commit()
    conn.close()

def handle_expired_pending_orders():
    """checks for orders older than 30 minutes without matching bank sms and marks them as cash payments"""

    conn = sqlite3.connect(EXPENSES_DB)
    cur = conn.cursor()

    thirty_mins_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")

    # fetch all orders older than 30 mins still marked as PENDING
    cur.execute("""
        SELECT id, rec_id, date_str, time_str, merchant, amount, category, source_app 
        FROM pending_orders 
        WHERE status = 'PENDING' AND created_at <= ?
    """, (thirty_mins_ago,))

    expired_orders = cur.fetchall()

    for order in expired_orders:
        pending_id, rec_id, date_str, time_str, merchant, amount, category, source_app = order

        if amount is None:
            cur.execute("UPDATE pending_orders SET status = 'AWAITING_AMOUNT' WHERE id = ?", (pending_id,))
            cur.execute("""
                INSERT OR IGNORE INTO expenses
                (rec_id, date, time, merchant, amount, category, payment_mode, source_app)
                VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
            """, (rec_id, date_str, time_str, merchant, category, CASH, source_app))

            if cur.rowcount > 0:
                sync_expense_to_sheet(rec_id, date_str, time_str, merchant, "", category,
                                      CASH, source_app)
                logging.info(f"Cash order, amount unknown -> row added for manual fill: [{merchant} {date_str} {time_str}]")
            continue

        cur.execute("UPDATE pending_orders SET status = 'CASH_RECORDED' WHERE id = ?", (pending_id,))
        cur.execute("""
            INSERT OR IGNORE INTO expenses 
            (rec_id, date, time, merchant, amount, category, payment_mode, source_app)
            VALUES (?, ?, ?, ?, ?, ?, 'Cash', ?)
        """, (rec_id, date_str, time_str, merchant, amount, category, source_app))
        
        if cur.rowcount > 0:
            sync_expense_to_sheet(rec_id, date_str, time_str, merchant, amount, category, "Cash", source_app)
            logging.info(f"No Bank SMS within 30 mins. Recorded as CASH: [{merchant} - ₹{amount}]")

    conn.commit()
    conn.close()

def read_and_store_notifications(limit: int = 100):
    """read mac-local notifications (bank SMS via Continuity) from usernoted's sqlite db"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_file:
        tmp_db_path = tmp_file.name

    if not copy_nc_db(tmp_db_path):
        return

    conn_nc = sqlite3.connect(tmp_db_path)
    conn_nc.row_factory = sqlite3.Row
    cur_nc = conn_nc.cursor()

    cur_nc.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur_nc.fetchall()]
    
    if "record" not in tables:
        logging.warning("WARNING: 'record' table not found in NC DB. The DB was likely just reset.")
        conn_nc.close()
        os.remove(tmp_db_path)
        return

    # join record with app to get bundle identifier
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

    app_notifications_processed = 0
    bank_sms_processed = 0

    for row in cur_nc.fetchall():
        bundle_id = row["bundle_id"]
        if bundle_id not in EXPENSE_BUNDLES:
            continue

        delivered_at_ist = (MAC_EPOCH + timedelta(seconds=row["delivered_date"])).astimezone(IST)

        data_blob = row["data"]
        try:
            plist = plistlib.loads(data_blob)
        except Exception:
            plist = {}

        req = plist.get("req", {}) or plist
        title = req.get("titl") or req.get("title") or ""
        subtitle = req.get("subt") or req.get("subtitle") or ""
        body = req.get("body") or ""
        text = f"{title} {subtitle} {body}".strip()

        key = notif_key("nc", bundle_id, row["delivered_date"], text)
        if already_processed(key):
            continue

        date_str = delivered_at_ist.strftime("%Y-%m-%d")
        time_str = delivered_at_ist.strftime("%H:%M")
        mark_processed(key, bundle_id)

        # 1. bank sms notification (mobilesms / ichat)
        if "mobilesms" in bundle_id.lower() or "ichat" in bundle_id.lower() or "hdfc" in text.lower():
            parsed_sms = parse_hdfc_sms(text)
            if parsed_sms:
                # Pass extracted SMS amount to the matcher logic
                handle_bank_sms(
                    rec_id = key,
                    date_str = date_str,
                    time_str = time_str,
                    amount = parsed_sms["amount"],
                    sms_merchant = parsed_sms["merchant"], 
                    source_app = bundle_id
                )
                bank_sms_processed += 1
            continue

        # 2. app notifications (blinkit, zepto, uber, etc.)
        if not looks_like_expense(text):
            continue
        amount = extract_amount(text)

        merchant, category = infer_merchant_category(bundle_id, title, f"{subtitle} {body}")

        # route app notification through the pending order handler
        handle_app_notification(
            rec_id = key,
            date_str = date_str,
            time_str = time_str,
            merchant = merchant,
            amount = amount,
            category = category,
            source_app = bundle_id
        )
        app_notifications_processed += 1
        
    conn_nc.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp_db_path + suffix):
            os.remove(tmp_db_path + suffix)

    logging.info(f"  - Mac-local app orders: {app_notifications_processed}")
    logging.info(f"  - Bank SMS processed: {bank_sms_processed}")

def read_remote_notifications():
    """read notifications mirrored from the iPhone"""

    pattern = os.path.join(REMOTE_NOTIF_DIR, "*", "*", "DeliveredNotifications.plist")
    files = glob.glob(pattern)
    if not files:
        logging.warning("No mirrored notifications found. Check if iPhone Mirroring is connected.")
        return 0

    processed = 0
    for path in files:
        try:
            entries = unarchive_plist(path)
        except Exception as e:
            logging.error(f"Could not decode {os.path.basename(os.path.dirname(path))}: {e}")
            continue

        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            details = (entry.get("EventBehavior") or {}).get("eventDetails") or {}

            bundle_id = details.get("bundleIdentifier")
            if bundle_id not in EXPENSE_BUNDLES:
                continue

            title = entry.get("AppNotificationTitle") or details.get("title") or ""
            body = entry.get("AppNotificationMessage") or details.get("body") or ""
            subtitle = details.get("subtitle") or ""
            if subtitle == title:
                subtitle = ""
            text = f"{title} {subtitle} {body}".strip()

            created = entry.get("AppNotificationCreationDate")
            if isinstance(created, dict):
                created = created.get("NS.time")
            if not isinstance(created, (int, float)):
                continue
            delivered_at = (MAC_EPOCH + timedelta(seconds=created)).astimezone(IST)

            key = notif_key("remote", bundle_id, created, text)
            if already_processed(key):
                continue
            mark_processed(key, bundle_id)

            if not looks_like_expense(text):
                logging.info(f"skipped (not a transaction): {bundle_id} | {text[:70]}")
                continue
            amount = extract_amount(text)

            merchant, category = infer_merchant_category(bundle_id, title, f"{subtitle} {body}")
            handle_app_notification(
                rec_id = key,
                date_str = delivered_at.strftime("%Y-%m-%d"),
                time_str = delivered_at.strftime("%H:%M"),
                merchant = merchant,
                amount = amount,
                category = category,
                source_app = bundle_id,
            )
            processed += 1
            logging.info(f"Mirrored expense queued: {merchant} - ₹{amount} [{bundle_id}]")

    return processed

def process_notifications():
    logging.info("Starting Expense Processing Pipeline...")
    init_expenses_db()

    logging.info("Processed notifications:")

    # app orders mirrored from the iPhone
    mirrored = read_remote_notifications()
    logging.info(f"  - Mirrored app orders: {mirrored}")

    # bank SMS forwarded to the Mac via Continuity
    read_and_store_notifications(limit=100)

    # resolve any pending orders older than 30 minutes as CASH
    handle_expired_pending_orders()

    logging.info("Pipeline Execution Complete\n")

if __name__ == "__main__":
    try:
        process_notifications()
    except Exception:
        logging.exception("Pipeline failed\n")
        raise