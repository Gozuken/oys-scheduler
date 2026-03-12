#!/usr/bin/env python3
"""
Baskent OYS -> Google Calendar
- Per-course prompts with full context (activities + PDF text)
- Student profile (name + number) so AI filters for your group/section automatically
- pypdf for PDF text extraction (no API needed)
- Groq AI (free, fast) for parsing
- Google Calendar integration with deduplication

Setup:
  pip install requests beautifulsoup4 groq pypdf google-auth google-auth-oauthlib google-api-python-client python-dotenv tzdata

.env file:
  OYS_USERNAME=22593244
  OYS_PASSWORD=your_password
  GROQ_API_KEY=gsk_...
  STUDENT_NAME=Ahmet Ercan Saz
  STUDENT_NUMBER=22593244
"""

import os
import io
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
from pypdf import PdfReader

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
SEEN_FILE        = "seen_events.json"
PDF_CACHE        = "pdf_cache"
PDF_CHOICES_FILE = "pdf_choices.json"

GROQ_MODEL     = "llama-3.3-70b-versatile"
MAX_PDF_CHARS  = 12000   # truncate very long PDFs to stay within token limits

COURSE_IDS = [10818, 10747, 10289, 10546, 9843, 10164, 5439]

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# Silence noisy pypdf warnings about malformed PDFs
logging.getLogger("pypdf").setLevel(logging.ERROR)

# ── Moodle Scraper ────────────────────────────────────────────────────────────

class MoodleScraper:
    def __init__(self, username: str, password: str):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})
        self.username = username
        self.password = password
        self.sesskey: str | None = None
        self.userid:  int | None = None

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
            # Extract sesskey and userid from the post-login page JS config
            sk = re.search(r'"sesskey"\s*:\s*"([^"]+)"', r.text)
            uid = re.search(r'"userId"\s*:\s*(\d+)', r.text)
            self.sesskey = sk.group(1)  if sk  else None
            self.userid  = int(uid.group(1)) if uid else None
            if self.sesskey:
                log.info(f"  sesskey extracted, userid={self.userid}")
            else:
                log.warning("  sesskey not found in page — message fetching will be skipped")
            return True
        log.error("Login failed - check credentials.")
        return False

    def get_course_data(self, course_id: int) -> dict:
        """Scrape one course page, return activities and PDF links."""
        url  = f"{BASE_URL}/course/view.php?id={course_id}"
        soup = BeautifulSoup(self.session.get(url).text, "html.parser")
        h1   = soup.find("h1")
        course_name = h1.get_text(strip=True) if h1 else f"Course {course_id}"

        activities, pdf_links, section_summaries = [], [], []

        for section in soup.select("li.section.course-section"):
            week_el  = section.find("h3", class_="sectionname")
            week     = week_el.get_text(strip=True) if week_el else "General"
            summ_div = section.find("div", class_="summarytext")
            section_info = summ_div.get_text(separator=" ", strip=True) if summ_div else ""
            if section_info:
                section_summaries.append({"week": week, "text": section_info})

            for activity in section.select("li.activity"):
                name_el = activity.find("span", class_="instancename")
                if not name_el:
                    continue
                for span in name_el.find_all("span", class_="accesshide"):
                    span.decompose()
                activity_name = name_el.get_text(strip=True)
                mod_classes   = " ".join(activity.get("class", []))
                is_assign     = "modtype_assign"   in mod_classes
                is_resource   = "modtype_resource" in mod_classes
                link_el       = activity.find("a", class_="aalink")
                link          = link_el["href"] if link_el and link_el.get("href") else ""

                # Collect PDF links (skip archives)
                if is_resource and link:
                    badge      = activity.find("span", class_="activitybadge")
                    badge_text = badge.get_text(strip=True).upper() if badge else ""
                    if badge_text not in ("RAR", "ZIP", "7Z", "TAR", "GZ"):
                        pdf_links.append({
                            "name": activity_name, "file_type": badge_text,
                            "view_url": link, "week": week,
                        })

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
                    "activity_name": activity_name, "is_assignment": is_assign,
                    "week": week, "opened": opened, "due": due,
                    "description": description, "section_info": section_info, "link": link,
                })

        log.info(f"  -> {course_name}: {len(activities)} activities, {len(pdf_links)} PDFs")
        return {
            "course_id":        course_id,
            "course_name":      course_name,
            "activities":       activities,
            "pdf_links":        pdf_links,
            "section_summaries": section_summaries,
        }

    def scrape_all_courses(self) -> list[dict]:
        courses = []
        for cid in COURSE_IDS:
            try:
                courses.append(self.get_course_data(cid))
            except Exception as e:
                log.warning(f"Failed to scrape course {cid}: {e}")
        return courses

    def download_pdf(self, view_url: str) -> bytes | None:
        try:
            r            = self.session.get(view_url, allow_redirects=True, stream=True, timeout=30)
            content_type = r.headers.get("Content-Type", "")

            if "text/html" in content_type:
                soup  = BeautifulSoup(r.text, "html.parser")
                pdf_a = (
                    soup.find("a", href=re.compile(r"pluginfile\.php.*\.pdf", re.I)) or
                    soup.find("a", href=re.compile(r"\.pdf", re.I)) or
                    soup.find("a", href=re.compile(r"pluginfile\.php", re.I))
                )
                if not pdf_a:
                    return None
                r            = self.session.get(pdf_a["href"], stream=True, timeout=30)
                content_type = r.headers.get("Content-Type", "")

            if "pdf" not in content_type.lower():
                return None

            data = b"".join(r.iter_content(chunk_size=65536))
            return data if data and b"%PDF" in data[:10] else None

        except Exception as e:
            log.warning(f"  Download failed {view_url}: {e}")
            return None

    def get_cached_pdf(self, view_url: str) -> bytes | None:
        cache_dir  = Path(PDF_CACHE)
        cache_dir.mkdir(exist_ok=True)
        cache_path = cache_dir / f"{hashlib.md5(view_url.encode()).hexdigest()}.pdf"

        if cache_path.exists():
            return cache_path.read_bytes()

        data = self.download_pdf(view_url)
        if data:
            cache_path.write_bytes(data)
        return data

    def _ajax(self, payload: list) -> list:
        """POST to the Moodle AJAX endpoint; returns the parsed response list."""
        url = f"{BASE_URL}/lib/ajax/service.php"
        r   = self.session.post(url, params={"sesskey": self.sesskey}, json=payload, timeout=15)
        r.raise_for_status()
        return r.json()

    def get_recent_messages(self, days: int = 30) -> list[dict]:
        """
        Return recent inbox messages as plain-text dicts.
        Fetches all conversations then pulls messages from each.
        Only returns messages newer than `days` days.
        """
        if not self.sesskey or not self.userid:
            log.warning("  Skipping messages: sesskey/userid not available")
            return []

        cutoff = datetime.now(ZoneInfo(TIMEZONE)).timestamp() - days * 86400

        # Step 1: get all conversations
        try:
            resp = self._ajax([{
                "index": 0,
                "methodname": "core_message_get_conversations",
                "args": {
                    "userid":     self.userid,
                    "type":       1,          # 1 = individual DMs
                    "limitnum":   50,
                    "limitfrom":  0,
                    "favourites": False,
                    "mergeself":  True,
                },
            }])
            conversations = resp[0]["data"]["conversations"]
        except Exception as e:
            log.warning(f"  Failed to fetch conversations: {e}")
            return []

        log.info(f"  Found {len(conversations)} conversations, fetching recent messages...")

        all_messages = []
        for conv in conversations:
            conv_id      = conv["id"]
            other_member = next((m for m in conv["members"] if m["id"] != self.userid), None)
            other_name   = other_member["fullname"] if other_member else "Unknown"

            try:
                resp = self._ajax([{
                    "index": 0,
                    "methodname": "core_message_get_conversation_messages",
                    "args": {
                        "currentuserid": self.userid,
                        "convid":        conv_id,
                        "newest":        True,
                        "limitnum":      50,
                        "limitfrom":     0,
                    },
                }])
                messages = resp[0]["data"]["messages"]
            except Exception as e:
                log.warning(f"  Failed to fetch messages for conv {conv_id}: {e}")
                continue

            for msg in messages:
                if msg["timecreated"] < cutoff:
                    continue
                # Strip HTML tags from message text
                plain = re.sub(r"<[^>]+>", " ", msg["text"]).strip()
                plain = re.sub(r"\s+", " ", plain)
                if not plain:
                    continue
                all_messages.append({
                    "from":        other_name if msg["useridfrom"] != self.userid else "Me",
                    "to":          "Me"        if msg["useridfrom"] != self.userid else other_name,
                    "text":        plain,
                    "timecreated": msg["timecreated"],
                    "date":        datetime.fromtimestamp(msg["timecreated"], ZoneInfo(TIMEZONE)).strftime("%d %B %Y %H:%M"),
                })

        log.info(f"  Fetched {len(all_messages)} recent messages (last {days} days)")
        return all_messages


# ── PDF Text Extraction ───────────────────────────────────────────────────────

def extract_pdf_text(pdf_bytes: bytes, max_chars: int = MAX_PDF_CHARS) -> str:
    """Extract text from PDF bytes using pypdf."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text   = "\n".join(
            page.extract_text() or "" for page in reader.pages
        ).strip()
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... [truncated at {max_chars} chars]"
        return text
    except Exception as e:
        log.warning(f"  PDF text extraction failed: {e}")
        return ""


# ── Groq Parser ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a university student assistant that creates Google Calendar events.
You receive course data including activities and PDF content.
You use the student's name and number to filter group/section assignments — 
only create events relevant to THIS student, not other groups or sections.
Always return valid JSON arrays only. No markdown, no explanation."""


def build_course_prompt(course: dict, pdf_texts: list[dict], student_name: str, student_number: str,
                         messages: list[dict] | None = None) -> str:
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%A, %d %B %Y")

    # Format activities — send all, AI decides what's relevant
    activities_text = json.dumps(course["activities"], ensure_ascii=False, indent=2) if course["activities"] else "None"

    # Section summaries often contain quiz schedules, grading info, announcements
    summaries = course.get("section_summaries", [])
    summaries_text = "\n\n".join(
        f"[{s['week']}]\n{s['text']}" for s in summaries if s["text"]
    ) if summaries else "None"

    # Format recent messages from instructors
    if messages:
        msgs_text = "\n\n".join(
            f"[{m['date']} | From: {m['from']}]\n{m['text']}" for m in messages
        )
    else:
        msgs_text = "None"

    # Format PDF texts
    if pdf_texts:
        pdfs_text = "\n\n".join(
            f"=== PDF: {p['name']} ===\n{p['text']}" for p in pdf_texts if p["text"]
        )
    else:
        pdfs_text = "None"

    return f"""STUDENT PROFILE:
  Name: {student_name}
  Student Number: {student_number}

COURSE: {course["course_name"]}
Today: {today} | Timezone: {TIMEZONE} (UTC+3)

INSTRUCTIONS:
- Create Google Calendar events for this course only
- If a PDF contains a group/section/lab schedule, find the student by name or number
  and ONLY create the event for their specific day/time — skip all other groups
- Extract exam dates, assignment due dates, project deadlines, grading policy from PDFs
- Moodle dates look like "Friday, 6 March 2026, 11:59 PM"

For each event return a JSON object with EXACTLY:
- summary: e.g. "MAT286 Odev-1 Due" or "BIL332 Lab-1 Due"
- description: details including requirements, topics, grading weight, link
- start_datetime: ISO 8601 with +03:00 offset e.g. "2026-03-06T23:59:00+03:00"
- end_datetime: ISO 8601, 1 hour after start (2 hours for exams)
- reminder_minutes: [10080, 1440, 180] for exams | [1440, 360, 60] for deadlines | [1440] for info
- color_id: "6" for exams | "11" for deadlines | "3" for grading/info | "5" for office hours
- unique_key: lowercase no-spaces e.g. "mat286_odev1_due" or "bil332_lab1_sec1"

Return [] if there is nothing relevant to calendar for this student.
Return ONLY a valid JSON array, no markdown, no extra text.

--- COURSE ACTIVITIES ---
{activities_text}

--- SECTION ANNOUNCEMENTS / SUMMARIES ---
{summaries_text}

--- RECENT INSTRUCTOR MESSAGES ---
{msgs_text}

--- COURSE PDFs ---
{pdfs_text}
"""


def parse_course_with_groq(course: dict, pdf_texts: list[dict],
                            student_name: str, student_number: str,
                            client: Groq, messages: list[dict] | None = None) -> list[dict]:
    prompt = build_course_prompt(course, pdf_texts, student_name, student_number, messages)

    log.info(f"  Sending to Groq: {course['course_name']}")
    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.1,
            max_tokens=4096,
        )
        raw    = response.choices[0].message.content
        events = _parse_json_response(raw)
        # Tag each event with course info for debugging
        for ev in events:
            ev.setdefault("course", course["course_name"])
        log.info(f"  -> {len(events)} events for {course['course_name']}")
        return events
    except Exception as e:
        log.error(f"  Groq failed for {course['course_name']}: {e}")
        return []



def ai_filter_pdfs(pdf_links: list[dict], course_name: str,
                   choices: dict, client: Groq) -> list[dict]:
    """Ask Groq to classify PDF names; skip any already cached in choices."""
    if not pdf_links:
        return []

    # Only classify PDFs we haven't seen before
    unknown = [p for p in pdf_links if p["view_url"] not in choices]

    if unknown:
        names_list = "\n".join(
            f"{i+1}. {p['name']}" for i, p in enumerate(unknown)
        )
        prompt = (
            f"Course: {course_name}\n"
            f"Below are PDF file names attached to a university course page.\n"
            f"Return ONLY a JSON array of the numbers (1-based) of PDFs that are likely "
            f"to contain scheduling or admin info useful for a calendar — such as syllabi, "
            f"lab schedules, assignment sheets, exam dates, group schedules, or project rules.\n"
            f"Exclude pure lecture slides, lecture notes, or reading material.\n"
            f"Return [] if none qualify. No explanation, only the JSON array.\n\n"
            f"{names_list}"
        )
        try:
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=256,
            )
            raw = resp.choices[0].message.content.strip()
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
            selected = set(json.loads(raw)) if raw != "[]" else set()
        except Exception as e:
            log.warning(f"  AI PDF filter failed ({e}); including all unknown PDFs as fallback")
            selected = set(range(1, len(unknown) + 1))

        for i, p in enumerate(unknown):
            choices[p["view_url"]] = ((i + 1) in selected)
        save_pdf_choices(choices)
        log.info(f"  AI classified {len(unknown)} new PDFs, kept {len(selected)}")

    useful = [p for p in pdf_links if choices.get(p["view_url"], False)]
    skipped = len(pdf_links) - len(useful)
    if skipped:
        log.info(f"  Skipping {skipped} lecture PDFs (cached), downloading {len(useful)} useful PDFs")
    return useful


def _parse_json_response(text: str) -> list[dict]:
    """Strip markdown fences and parse JSON array."""
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
            log.warning(f"  Skipping event missing datetime: {ev.get('summary')}")
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
            print(f"    Desc     : {body['description'][:150]}")
        else:
            try:
                result = service.events().insert(calendarId="primary", body=body).execute()
                log.info(
                    f"  Created: {body['summary']}\n"
                    f"    Start   : {body['start']['dateTime']}\n"
                    f"    End     : {body['end']['dateTime']}\n"
                    f"    Desc    : {body['description'][:200]}\n"
                    f"    Link    : {result.get('htmlLink')}"
                )
                created += 1
                seen.add(key)
            except HttpError as e:
                log.error(f"  Failed '{body['summary']}': {e}")

    if not dry_run:
        save_seen(seen)
    return created


def load_pdf_choices() -> dict:
    """Load saved per-URL PDF include/exclude choices."""
    if Path(PDF_CHOICES_FILE).exists():
        with open(PDF_CHOICES_FILE) as f:
            return json.load(f)
    return {}


def save_pdf_choices(choices: dict):
    with open(PDF_CHOICES_FILE, "w") as f:
        json.dump(choices, f, indent=2, ensure_ascii=False)


def pick_pdfs_interactively(course_name: str, pdf_links: list[dict],
                             choices: dict) -> list[dict]:
    """Prompt only for PDFs not yet decided; reuse saved choices for the rest."""
    if not pdf_links:
        return []

    new_pdfs = [p for p in pdf_links if p["view_url"] not in choices]

    if new_pdfs:
        print(f"\n  PDFs for: {course_name}  (new — not yet decided)")
        for i, p in enumerate(new_pdfs, 1):
            print(f"    [{i}] {p['name']}  ({p.get('file_type', 'PDF')})")
        print(f"    [0] Skip all new")
        raw = input("  Select PDFs to INCLUDE (e.g. 1 3, or 0 to skip all): ").strip()

        selected_indices: set[int] = set()
        if raw and raw != "0":
            for tok in raw.split():
                try:
                    idx = int(tok)
                    if 1 <= idx <= len(new_pdfs):
                        selected_indices.add(idx - 1)
                except ValueError:
                    pass

        for i, p in enumerate(new_pdfs):
            choices[p["view_url"]] = (i in selected_indices)
        save_pdf_choices(choices)
        log.info(f"  Choices saved to {PDF_CHOICES_FILE}")

    chosen = [p for p in pdf_links if choices.get(p["view_url"], False)]
    skipped = len(pdf_links) - len(chosen)
    if skipped:
        log.info(f"  {len(chosen)} included, {skipped} skipped (saved choices)")
    return chosen


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="OYS -> Google Calendar (per-course, PDF-aware)")
    parser.add_argument("--dry-run",     action="store_true", help="Preview events without creating them")
    parser.add_argument("--no-ai",       action="store_true", help="Print raw scraped data only")
    parser.add_argument("--no-pdfs",     action="store_true", help="Skip PDF downloading and parsing")
    parser.add_argument("--pick-pdfs",   action="store_true", help="Interactively choose which PDFs to use per course")
    parser.add_argument("--clear-cache",   action="store_true", help="Re-download all PDFs")
    parser.add_argument("--reset-choices", action="store_true", help="Forget all saved PDF include/exclude choices")
    args = parser.parse_args()

    # Load credentials
    username       = os.environ.get("OYS_USERNAME")
    password       = os.environ.get("OYS_PASSWORD")
    student_name   = os.environ.get("STUDENT_NAME")
    student_number = os.environ.get("STUDENT_NUMBER")

    if not username or not password:
        raise ValueError("Set OYS_USERNAME and OYS_PASSWORD in .env")
    if not student_name or not student_number:
        raise ValueError("Set STUDENT_NAME and STUDENT_NUMBER in .env")

    if args.clear_cache:
        import shutil
        shutil.rmtree(PDF_CACHE, ignore_errors=True)
        log.info("PDF cache cleared.")

    if args.reset_choices:
        Path(PDF_CHOICES_FILE).unlink(missing_ok=True)
        log.info("PDF choices reset — you will be asked again on next run.")

    # 1. Scrape all courses
    scraper = MoodleScraper(username, password)
    if not scraper.login():
        return

    log.info("Scraping all courses...")
    courses = scraper.scrape_all_courses()
    log.info(f"Found {len(courses)} courses")

    if args.no_ai:
        print(json.dumps(courses, ensure_ascii=False, indent=2))
        return

    # 2. Set up Groq
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        raise ValueError("Set GROQ_API_KEY in .env  (free at https://console.groq.com)")
    client = Groq(api_key=groq_key)

    # Fetch recent messages once — passed to every course prompt as extra context
    log.info("Fetching recent messages...")
    recent_messages = scraper.get_recent_messages(days=30)

    all_events = []
    pdf_choices = load_pdf_choices()  # persistent across runs

    # 3. Process each course separately
    for course in courses:
        log.info(f"\nProcessing: {course['course_name']}")

        # Download and extract PDF text for this course
        pdf_texts = []
        if not args.no_pdfs and course["pdf_links"]:
            if args.pick_pdfs:
                useful = pick_pdfs_interactively(course["course_name"], course["pdf_links"], pdf_choices)
                log.info(f"  Downloading {len(useful)} selected PDFs...")
            else:
                useful = ai_filter_pdfs(course["pdf_links"], course["course_name"], pdf_choices, client)
            for pdf_info in useful:
                pdf_bytes = scraper.get_cached_pdf(pdf_info["view_url"])
                if pdf_bytes:
                    text = extract_pdf_text(pdf_bytes)
                    if text:
                        pdf_texts.append({"name": pdf_info["name"], "text": text})
                        log.info(f"    Extracted {len(text)} chars from '{pdf_info['name']}'")
                    else:
                        log.info(f"    No text extracted from '{pdf_info['name']}' (may be scanned)")

        # Skip course if nothing to process
        has_dated_activities = any(a.get("due") or a.get("opened") for a in course["activities"])
        has_summaries        = bool(course.get("section_summaries"))
        if not has_dated_activities and not pdf_texts and not has_summaries and not recent_messages:
            log.info(f"  Nothing to process for {course['course_name']}, skipping.")
            continue

        # Send to Groq
        events = parse_course_with_groq(course, pdf_texts, student_name, student_number, client, recent_messages)
        all_events.extend(events)

    log.info(f"\nTotal events across all courses: {len(all_events)}")
    if not all_events:
        log.info("Nothing to add to calendar.")
        return

    # 4. Create calendar events
    n = create_calendar_events(all_events, dry_run=args.dry_run)
    if not args.dry_run:
        log.info(f"\nDone! Added {n} new events to Google Calendar.")
    else:
        log.info(f"\n[DRY RUN] Would create {len(all_events)} events total.")


if __name__ == "__main__":
    main()
