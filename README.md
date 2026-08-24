An automated macOS expense tracker and an AI-powered code reviewer.

---

## Components

### 1. Automated Expense Tracker (`ntf_parser.py`)

An automated background pipeline for macOS that extracts expenses from bank SMS and iOS app notifications without manual entry.

#### How It Works
- Extracts bank SMS and app notifications stored in macOS notification databases via Continuity and iPhone Mirroring.
- Converts timestamps to Indian Standard Time (IST) and generates unique hashes to deduplicate incoming notifications.
- Auto-classifies payments as UPI or Cash using a 30-minute reconciliation window.
- Syncs deduplicated expense records live to a private Google Sheet.
- Runs silently in the background every 5 minutes using a macOS `launchd` daemon.

#### Setup Instructions

##### Prerequisites
- macOS Sequoia (or newer) with iPhone Mirroring enabled and configured with an iOS device.
- Python 3.10 or higher.
- A Google Cloud account for Google Sheets API access.

##### Step 1: Install Dependencies
Install `gspread` for Google Sheets integration:
```bash
pip install gspread
```

##### Step 2: Configure Google Sheets API
1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a new project.
2. Enable the **Google Sheets API**.
3. Navigate to **Credentials**, create a **Service Account**, and generate a new **JSON key**.
4. Save the JSON file as `credentials.json` in the root directory of this project.
5. Create a Google Sheet named `My Expenses`.
6. Share `My Expenses` with the service account email address (found inside `credentials.json` under `client_email`) and grant it **Editor** permissions.

##### Step 3: Test the Tracker
Run the script manually to ensure database connections and Google Sheets syncing work correctly:
```bash
python ntf_parser.py
```
Check `tracker.log` in the project directory for execution logs.

##### Step 4: Create macOS .app Wrapper and Grant Full Disk Access
Reading the system notification database requires Full Disk Access. To grant access safely without granting Full Disk Access to your entire Python environment, wrap the script inside a dedicated macOS `.app` bundle:

1. Create the application bundle using `osacompile`:
```bash
osacompile -o ~/Applications/ExpenseTracker.app -e 'do shell script "/path/to/project/folder/myvenv/bin/python3 /path/to/project/folder/ntf_parser.py"'
```

2. Grant Full Disk Access to `ExpenseTracker.app`:
   - Open **System Settings** -> **Privacy & Security** -> **Full Disk Access**.
   - Click the **+** button.
   - Press `Cmd` + `Shift` + `G`, enter `~/Applications/`, and select `ExpenseTracker.app`.
   - Turn the toggle **ON**.

##### Step 5: Automate with `launchd`
To run the parser every 5 minutes silently in the background:

1. Create a launch agent file at `~/Library/LaunchAgents/com.user.expensetracker.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.expensetracker</string>

    <key>ProgramArguments</key>
    <array>
        <string>/path/to/ExpenseTracker.app/Contents/MacOS/applet</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/path/to/project/folder</string>

    <key>StartInterval</key>
    <integer>300</integer>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/path/to/project/folder/tracker_error.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/project/folder/tracker_error.log</string>
</dict>
</plist>
```
*Note: Replace `/path/to/project/folder` and `/path/to/ExpenseTracker.app` with your absolute system paths.*

2. Load the daemon:
```bash
launchctl load ~/Library/LaunchAgents/com.user.expensetracker.plist
```

---

### 2. Code Reviewer (`review.py` & GitHub Actions)

A targeted code review workflow that analyzes git diffs on every commit push to detect up to 2 critical runtime failure points.

#### How It Works
- Runs automatically on every push event using GitHub Actions.
- Extracts git diffs between previous and current commit SHAs.
- Uses the Groq API (`openai/gpt-oss-120b`) with structured JSON schema outputs to analyze code changes.
- Identifies up to 2 critical runtime failure points while ignoring style and formatting noise.

#### Setup Instructions

1. Get an API key from [Groq](https://console.groq.com/).
2. In your GitHub repository, go to **Settings** > **Secrets and variables** > **Actions**.
3. Click **New repository secret**.
4. Name the secret `GROQ_API_KEY` and paste your key.
5. Ensure `.github/workflows/review.yml` and `review.py` are committed to your default branch.

The workflow will execute on every subsequent code push and print identified breaking points in the Action log.
