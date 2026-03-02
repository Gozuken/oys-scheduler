# Google Calendar Setup (One-Time)

## Step 1 — Create a Google Cloud Project
1. Go to https://console.cloud.google.com
2. Click "New Project", name it anything (e.g. "OYS Sync")
3. Select the project

## Step 2 — Enable the Calendar API
1. Go to "APIs & Services" → "Library"
2. Search "Google Calendar API" → Click it → Click "Enable"

## Step 3 — Create OAuth Credentials
1. Go to "APIs & Services" → "Credentials"
2. Click "Create Credentials" → "OAuth client ID"
3. Application type: **Desktop app**
4. Name it anything, click "Create"
5. Click "Download JSON"
6. **Rename the downloaded file to `credentials.json`**
7. **Put it in the same folder as `moodle_to_calendar.py`**

## Step 4 — OAuth Consent Screen (if prompted)
1. Go to "OAuth consent screen"
2. Choose "External" → Fill in app name (anything)
3. Add your own Gmail as a test user
4. Save

## Step 5 — First Run Auth
When you run the script for the first time, a browser window will open.
Log in with your Google account and grant calendar access.
A `token.json` file will be saved so you won't need to do this again.

---

# Running the Script

## Install dependencies
```bash
pip install requests beautifulsoup4 anthropic google-auth google-auth-oauthlib google-api-python-client python-dotenv
```

## Set up .env file
Copy `.env.example` to `.env` and fill in your actual password and API keys.

## Test run (no calendar changes)
```bash
python moodle_to_calendar.py --dry-run
```

## Real run
```bash
python moodle_to_calendar.py
```

## Just see raw scraped data
```bash
python moodle_to_calendar.py --no-claude
```

---

# Automate with cron (runs every morning at 8am)

```bash
crontab -e
# Add this line:
0 8 * * * cd /path/to/script && python moodle_to_calendar.py >> sync.log 2>&1
```

---

# Color coding in Google Calendar
- 🔴 Red (11): Assignment due dates
- 🟡 Yellow (5): Assignment opened/available
- 🔵 Blue (9): Urgent (due within 3 days)
