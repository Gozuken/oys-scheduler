#!/usr/bin/env python3
"""
Baskent OYS -> Google Calendar
Scrapes all Moodle courses for assignments and announcements.
Uses Groq AI (FREE, fast) to parse dates and create Google Calendar events.
PDFs can be added later.

Setup:
  pip install requests beautifulsoup4 groq google-auth google-auth-oauthlib google-api-python-client python-dotenv tzdata

Create a .env file:
  OYS_USERNAME=your_student_number
  OYS_PASSWORD=your_password
  GROQ_API_KEY=your_free_key   <- get at https://console.groq.com (free, no card)

For Google Calendar setup, follow README_GOOGLE_SETUP.md
"""

import os
import json
import re
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from groq import Groq

# Google Calendar imports
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

BASE_URL   = "https://oys2.baskent.edu.tr"
LOGIN_URL  = f"{BASE_URL}/login/index.php"
TIMEZONE   = "Europe/Istanbul"
SCOPES     = ["https://www.googleapis.com/auth/calendar"]
TOKEN_FILE = "token.json"
CREDS_FILE = "credentials.json"
SEEN_FILE  = "seen_events.json"

GROQ_MODEL = "llama-3.3-70b-versatile"  # free, fast, very capable

COURSE_IDS = [10818, 10747, 10289, 10546, 9843, 10164, 5439]

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ── Moodle Scraper ────────────────────────────────────────────────────────────

class MoodleScraper:
    def __init__(self, username: str, password: str):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})
        self.username = username
        self.password = password

    def login(self) -> bool:
        log.info("Logging in to OYS...")
        r    = self.session.get(LOGIN_URL)
        soup = BeautifulSoup(r.text, "html.parser")
        tok  = soup.find("input", {"name": "logintoken"})
        if not tok:
            log.error("logintoken not found.")
            return False
        r = self.session.post(LOGIN_URL, data={
            "username": self.username, "password": self.password,
            "logintoken": tok["value"], "anchor": "",
        })
        if "Log out" in r.text or "\u00c7\u0131k\u0131\u015f" in r.text:
            log.info("Login successful")
            return True
        log.error("Login failed - check credentials.")
        return False

    def get_course_data(self, course_id: int) -> list[dict]:
        url  = f"{BASE_URL}/course/view.php?id={course_id}"
        soup = BeautifulSoup(self.session.get(url).text, "html.parser")
        h1   = soup.find("h1")
        course_name = h1.get_text(strip=True) if h1 else f"Course {course_id}"

        activities = []

        for section in soup.select("li.section.course-section"):
            week_el  = section.find("h3", class_="sectionname")
            week     = week_el.get_text(strip=True) if week_el else "General"
            summ_div = section.find("div", class_="summarytext")
            section_info = summ_div.get_text(separator=" ", strip=True) if summ_div else ""

            for activity in section.select("li.activity"):
                name_el = activity.find("span", class_="instancename")
                if not name_el:
                    continue
                for span in name_el.find_all("span", class_="accesshide"):
                    span.decompose()
                activity_name = name_el.get_text(strip=True)
                mod_classes   = " ".join(activity.get("class", []))
                is_assign     = "modtype_assign" in mod_classes
                link_el       = activity.find("a", class_="aalink")
                link          = link_el["href"] if link_el and link_el.get("href") else ""

                opened = due = None
                date_region = activity.find("div", {"data-region": "activity-dates"})
                if date_region:
                    for div in date_region.find_all("div"):
                        t = div.get_text(strip=True)
                        if t.startswith("Opened"):
                            opened = t.replace("Opened:", "").replace("Opened", "").strip()
                        elif t.startswith("Due"):
                            due = t.replace("Due:", "").replace("Due", "").strip()

                desc_div    = activity.find("div", class_="activity-description")
                description = desc_div.get_text(separator=" ", strip=True) if desc_div else ""

                activities.append({
                    "course_id": course_id, "course_name": course_name,
                    "week": week, "activity_name": activity_name,
                    "is_assignment": is_assign, "opened": opened, "due": due,
                    "description": description, "section_info": section_info, "link": link,
                })

        log.info(f"  -> {course_name}: {len(activities)} activities")
        return activities

    def scrape_all_courses(self) -> list[dict]:
        all_activities = []
        for cid in COURSE_IDS:
            try:
                all_activities.extend(self.get_course_data(cid))
            except Exception as e:
                log.warning(f"Failed to scrape course {cid}: {e}")
        return all_activities


# ── Groq Parser ───────────────────────────────────────────────────────────────

def parse_activities_with_groq(items: list[dict], client: Groq) -> list[dict]:
    relevant = [i for i in items if i.get("due") or i.get("opened")]
    if not relevant:
        log.info("No date-bearing activities found.")
        return []

    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%A, %d %B %Y")

    prompt = f"""You are a student assistant helping manage a university schedule.
Convert these Moodle course activities into Google Calendar events.

Today is {today}. Timezone: {TIMEZONE}.
Moodle dates look like "Friday, 6 March 2026, 11:59 PM" - parse them carefully into ISO 8601.

For each activity return a JSON object with EXACTLY these fields:
- summary: short title e.g. "MAT286 Odev-1 Due" or "BIL344 Assignment Due"
- description: 2-3 sentences - what to do, any instructions from section_info, and the link
- start_datetime: ISO 8601 with timezone offset e.g. "2026-03-06T23:59:00+03:00" (Istanbul is UTC+3)
- end_datetime: ISO 8601, exactly 1 hour after start_datetime
- reminder_minutes: [1440, 360, 60] for due dates, [1440] for opened/available
- color_id: "11" for due dates, "5" for opened dates, "9" if due within 3 days of today
- unique_key: lowercase no-spaces string e.g. "mat286_odev1_due"

Return ONLY a valid JSON array, no markdown, no explanation, no extra text.

Activities:
{json.dumps(relevant, ensure_ascii=False, indent=2)}
"""

    log.info(f"Sending {len(relevant)} activities to Groq (1 API call)...")
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,      # low temp = more deterministic JSON
        max_tokens=4096,
    )

    raw    = response.choices[0].message.content
    events = _parse_json_response(raw)
    log.info(f"  -> {len(events)} calendar events parsed")
    return events


def _parse_json_response(text: str) -> list[dict]:
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    try:
        result = json.loads(text)
        return result if isinstance(result, list) else []
    except json.JSONDecodeError as e:
        log.error(f"JSON parse error: {e}\n{text[:400]}")
        return []


# ── Google Calendar ───────────────────────────────────────────────────────────

def get_calendar_service():
    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(CREDS_FILE).exists():
                raise FileNotFoundError(f"'{CREDS_FILE}' not found. See README_GOOGLE_SETUP.md")
            flow  = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def load_seen() -> set:
    if Path(SEEN_FILE).exists():
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f, indent=2)


def create_calendar_events(events: list[dict], dry_run: bool = False) -> int:
    seen    = load_seen()
    service = None if dry_run else get_calendar_service()
    created = 0

    for ev in events:
        key = ev.get("unique_key", "")
        if key in seen:
            log.info(f"  [skip duplicate] {ev.get('summary')}")
            continue

        if not ev.get("start_datetime") or not ev.get("end_datetime"):
            log.warning(f"  Skipping event with missing datetime: {ev.get('summary')}")
            continue

        body = {
            "summary":     ev.get("summary", "Untitled"),
            "description": ev.get("description", ""),
            "start":       {"dateTime": ev["start_datetime"], "timeZone": TIMEZONE},
            "end":         {"dateTime": ev["end_datetime"],   "timeZone": TIMEZONE},
            "colorId":     ev.get("color_id", "11"),
            "reminders":   {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": m}
                    for m in ev.get("reminder_minutes", [1440, 360, 60])
                    if m > 0
                ],
            },
        }

        if dry_run:
            print(f"\n  [DRY RUN] {body['summary']}")
            print(f"    Start    : {body['start']['dateTime']}")
            print(f"    Reminders: {ev.get('reminder_minutes')}")
            print(f"    Desc     : {body['description'][:120]}")
        else:
            try:
                result = service.events().insert(calendarId="primary", body=body).execute()
                log.info(f"  Created: {body['summary']}  ->  {result.get('htmlLink')}")
                created += 1
                seen.add(key)
            except HttpError as e:
                log.error(f"  Failed '{body['summary']}': {e}")

    if not dry_run:
        save_seen(seen)
    return created


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="OYS -> Google Calendar sync (powered by Groq)")
    parser.add_argument("--dry-run", action="store_true", help="Show events without creating them")
    parser.add_argument("--no-ai",   action="store_true", help="Print raw scraped data only")
    args = parser.parse_args()

    username = os.environ.get("OYS_USERNAME")
    password = os.environ.get("OYS_PASSWORD")
    if not username or not password:
        raise ValueError("Set OYS_USERNAME and OYS_PASSWORD in your .env file")

    # 1. Scrape
    scraper = MoodleScraper(username, password)
    if not scraper.login():
        return

    log.info("Scraping all courses...")
    activities = scraper.scrape_all_courses()
    log.info(f"Total: {len(activities)} activities found")

    if args.no_ai:
        print(json.dumps(activities, ensure_ascii=False, indent=2))
        return

    # 2. Set up Groq
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        raise ValueError("Set GROQ_API_KEY in your .env file  (free at https://console.groq.com)")
    client = Groq(api_key=groq_key)

    # 3. Parse with Groq (single API call)
    events = parse_activities_with_groq(activities, client)

    if not events:
        log.info("No events to add.")
        return

    # 4. Create calendar events
    n = create_calendar_events(events, dry_run=args.dry_run)
    if not args.dry_run:
        log.info(f"\nDone! Added {n} new events to Google Calendar.")
    else:
        log.info(f"\n[DRY RUN] Would create {len(events)} events total.")


if __name__ == "__main__":
    main()
