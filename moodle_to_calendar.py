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
import time
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
FINDINGS_CACHE_FILE = "findings_cache.json"

GROQ_MODEL     = "llama-3.3-70b-versatile"
MAX_PDF_CHARS  = 8000   # Reduced from 12000 to avoid TPM limits on free tier

COURSE_IDS = [10818, 10747, 10289, 10546, 9843, 10164, 5439]

# ── Colored terminal output ───────────────────────────────────────────────────

class _C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    # Foreground
    WHITE  = "\033[97m"
    CYAN   = "\033[96m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    BLUE   = "\033[94m"
    MAGENTA= "\033[95m"
    GRAY   = "\033[90m"

def _section(title: str):
    width = 60
    bar   = "─" * width
    print(f"\n{_C.BOLD}{_C.CYAN}{bar}{_C.RESET}")
    print(f"{_C.BOLD}{_C.CYAN}  {title}{_C.RESET}")
    print(f"{_C.BOLD}{_C.CYAN}{bar}{_C.RESET}")

def _course_header(name: str):
    print(f"\n{_C.BOLD}{_C.BLUE}▶  {name}{_C.RESET}")

def _ok(msg: str):
    print(f"  {_C.GREEN}✓{_C.RESET}  {msg}")

def _info(msg: str):
    print(f"  {_C.CYAN}·{_C.RESET}  {_C.DIM}{msg}{_C.RESET}")

def _warn(msg: str):
    print(f"  {_C.YELLOW}⚠{_C.RESET}  {_C.YELLOW}{msg}{_C.RESET}")

def _err(msg: str):
    print(f"  {_C.RED}✗{_C.RESET}  {_C.RED}{msg}{_C.RESET}")

def _skip(msg: str):
    print(f"  {_C.GRAY}⊘  {msg}{_C.RESET}")

def _event(summary: str, start: str, desc: str):
    print(f"  {_C.MAGENTA}◆{_C.RESET}  {_C.BOLD}{summary}{_C.RESET}")
    print(f"     {_C.DIM}Start : {start}{_C.RESET}")
    if desc:
        print(f"     {_C.DIM}Desc  : {desc[:120]}{_C.RESET}")


class _ColoredFormatter(logging.Formatter):
    _LEVELS = {
        logging.DEBUG:    (_C.GRAY,   "DEBUG"),
        logging.INFO:     (_C.CYAN,   "INFO "),
        logging.WARNING:  (_C.YELLOW, "WARN "),
        logging.ERROR:    (_C.RED,    "ERROR"),
        logging.CRITICAL: (_C.RED,    "CRIT "),
    }
    def format(self, record):
        color, label = self._LEVELS.get(record.levelno, (_C.RESET, "?????"))
        ts  = datetime.now().strftime("%H:%M:%S")
        msg = record.getMessage()
        return f"{_C.GRAY}{ts}{_C.RESET}  {color}{label}{_C.RESET}  {msg}"

_handler = logging.StreamHandler()
_handler.setFormatter(_ColoredFormatter())
logging.basicConfig(level=logging.WARNING, handlers=[_handler])
log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)

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
        _info("Logging in to OYS...")
        r    = self.session.get(LOGIN_URL)
        soup = BeautifulSoup(r.text, "html.parser")
        tok  = soup.find("input", {"name": "logintoken"})
        if not tok:
            _err("logintoken not found")
            return False
        r = self.session.post(LOGIN_URL, data={
            "username": self.username, "password": self.password,
            "logintoken": tok["value"], "anchor": "",
        })
        
        # Check if login was successful
        if "Log out" in r.text or "\u00c7\u0131k\u0131\u015f" in r.text or "sesskey" in r.text:
            _ok("Login successful")
            
            # Extract sesskey and userid from JS config or HTML
            sk_match = re.search(r'"sesskey"\s*:\s*"([^"]+)"', r.text)
            uid_match = re.search(r'"userId"\s*:\s*(\d+)', r.text)
            
            if not sk_match:
                sk_match = re.search(r'sesskey=([^"&]+)', r.text)
            if not uid_match:
                uid_match = re.search(r'/user/profile\.php\?id=(\d+)', r.text)

            self.sesskey = sk_match.group(1)  if sk_match  else None
            self.userid  = int(uid_match.group(1)) if uid_match else None
            
            if self.sesskey:
                _info(f"sesskey extracted  ·  userid={self.userid}")
            else:
                _warn("sesskey not found — message fetching will be skipped")
            return True
        _err("Login failed — check credentials")
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
            
            # Try to extract dates from section name (e.g. "24 February - 2 March")
            # This helps the AI resolve "Week 3" or relative dates.
            section_dates = ""
            date_match = re.search(r"(\d+\s+[A-Za-z]+\s*-\s*\d+\s+[A-Za-z]+)", week)
            if date_match:
                section_dates = date_match.group(1)

            summ_div = section.find("div", class_="summarytext")
            section_info = summ_div.get_text(separator=" ", strip=True) if summ_div else ""
            if section_info or section_dates:
                section_summaries.append({
                    "week": week, 
                    "dates": section_dates,
                    "text": section_info
                })

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
                            "view_url": link, "week": week, "section_dates": section_dates
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

        _info(f"{course_name}  ·  {len(activities)} activities  ·  {len(pdf_links)} PDFs")
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
                _warn(f"Failed to scrape course {cid}: {e}")
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
            _warn(f"Download failed: {e}")
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
            _warn("Skipping messages: sesskey/userid not available")
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
            _warn(f"Failed to fetch conversations: {e}")
            return []

        _info(f"Found {len(conversations)} conversations — fetching messages...")

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
                _warn(f"Failed to fetch messages for conv {conv_id}: {e}")
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

        _ok(f"Fetched {len(all_messages)} messages from the last {days} days")
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
        _warn(f"PDF text extraction failed: {e}")
        return ""


def load_findings_cache() -> dict:
    if Path(FINDINGS_CACHE_FILE).exists():
        with open(FINDINGS_CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_findings_cache(cache: dict):
    with open(FINDINGS_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

def get_content_hash(content: str | dict | list) -> str:
    if not isinstance(content, str):
        content = json.dumps(content, sort_keys=True)
    return hashlib.md5(content.encode()).hexdigest()

# ── Groq Parser (Multi-Stage Reasoning) ──────────────────────────────────────

EXTRACTION_SYSTEM_PROMPT = """You are an expert academic event extractor.
Your task is to find ALL potential calendar events (exams, quizzes, deadlines, project rules, lab schedules).
For each event found, provide:
- name
- date/time info (even if vague, e.g., 'Week 5')
- requirements or description
- mention of specific student groups or sections if any

Return a JSON array of objects. No explanation."""

RECONCILIATION_SYSTEM_PROMPT = """You are a highly analytical student assistant.
You will receive 'Findings' from multiple sources (PDFs, Moodle, Announcements).
Your goal is to RECONCILE these into a single, accurate Google Calendar.

LOGIC:
1. MATCHING: If Source A says 'Quiz 1' is in 'Week 4' and Source B (Syllabus) says 'Week 4' is 'March 10-16', create the event for March 10.
2. DEDUPLICATION: Merge findings about the same event.
3. PERSONALIZATION: Use the Student Profile to only include events for their specific section/group.
4. VALIDATION: Ensure dates are valid and in the future.

Output a valid JSON array of final calendar events only. No markdown."""

def call_groq_safe(client: Groq, system: str, user: str, retries: int = 3) -> str:
    """Call Groq with retry logic for rate limits (429)."""
    for i in range(retries):
        try:
            time.sleep(1.5) # Minimum spacing
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.0,
                max_tokens=4096,
            )
            return resp.choices[0].message.content
        except Exception as e:
            if "429" in str(e) and i < retries - 1:
                wait_time = 30 * (i + 1)
                _warn(f"Rate limit hit (429). Waiting {wait_time}s before retry {i+1}/{retries}...")
                time.sleep(wait_time)
                continue
            _warn(f"Groq error: {e}")
            return "[]"
    return "[]"

def parse_course_with_groq(course: dict, pdf_texts: list[dict],
                            student_name: str, student_number: str,
                            client: Groq, messages: list[dict] | None = None) -> list[dict]:
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%A, %d %B %Y")
    course_header = f"COURSE: {course['course_name']} (ID: {course['course_id']})\nTODAY: {today}\nSTUDENT: {student_name} ({student_number})"
    
    cache = load_findings_cache()
    findings = []

    # 1. Analyze each PDF individually (with section context)
    for pdf in pdf_texts:
        content_hash = get_content_hash(pdf['text'])
        # Key includes week/section because a PDF might have different meaning depending on where it's posted
        cache_key = f"pdf_{pdf['name']}_{pdf.get('week', 'Gen')}_{content_hash}"
        
        if cache_key in cache:
            _info(f"Using cached findings for PDF: {pdf['name']}")
            findings.append({"source": f"PDF: {pdf['name']} (from {pdf.get('week', 'Gen')})", "data": cache[cache_key]})
        else:
            _info(f"Extracting findings from PDF: {pdf['name']} (from {pdf.get('week', 'General')})...")
            user_prompt = f"{course_header}\n\nSOURCE: PDF '{pdf['name']}' found in section '{pdf.get('week', 'General')}'\n\nCONTENT:\n{pdf['text']}"
            raw = call_groq_safe(client, EXTRACTION_SYSTEM_PROMPT, user_prompt)
            data = _parse_json_response(raw)
            findings.append({"source": f"PDF: {pdf['name']} (from {pdf.get('week', 'Gen')})", "data": data})
            cache[cache_key] = data

    # 2. Analyze Activities in small chunks
    acts = course["activities"]
    chunk_size = 40
    for i in range(0, len(acts), chunk_size):
        chunk = acts[i:i+chunk_size]
        content_hash = get_content_hash(chunk)
        cache_key = f"acts_{course['course_id']}_{i}_{content_hash}"
        
        if cache_key in cache:
            findings.append({"source": f"Activities Batch {i//chunk_size+1}", "data": cache[cache_key]})
        else:
            _info(f"Extracting findings from activities batch {i//chunk_size + 1}...")
            user_prompt = f"{course_header}\n\nSOURCE: Moodle Activities Batch\n\nCONTENT:\n{json.dumps(chunk, ensure_ascii=False)}"
            raw = call_groq_safe(client, EXTRACTION_SYSTEM_PROMPT, user_prompt)
            data = _parse_json_response(raw)
            findings.append({"source": f"Activities Batch {i//chunk_size+1}", "data": data})
            cache[cache_key] = data

    # 3. Analyze Announcements
    summ_hash = get_content_hash(course.get("section_summaries", []))
    cache_key = f"announcements_{course['course_id']}_{summ_hash}"
    if summ_hash != get_content_hash([]):
        if cache_key in cache:
            findings.append({"source": "Announcements", "data": cache[cache_key]})
        else:
            _info(f"Extracting findings from announcements...")
            user_prompt = f"{course_header}\n\nSOURCE: Section Announcements\n\nCONTENT:\n{json.dumps(course['section_summaries'], ensure_ascii=False)}"
            raw = call_groq_safe(client, EXTRACTION_SYSTEM_PROMPT, user_prompt)
            data = _parse_json_response(raw)
            findings.append({"source": "Announcements", "data": data})
            cache[cache_key] = data

    # 4. Filter and add relevant instructor messages
    from moodle_to_calendar import filter_messages_for_course
    rel_msgs = filter_messages_for_course(course, messages or [])
    if rel_msgs:
        msg_hash = get_content_hash(rel_msgs)
        cache_key = f"messages_{course['course_id']}_{msg_hash}"
        if cache_key in cache:
            findings.append({"source": "Messages", "data": cache[cache_key]})
        else:
            _info(f"Extracting findings from messages...")
            user_prompt = f"{course_header}\n\nSOURCE: Instructor Messages\n\nCONTENT:\n{json.dumps(rel_msgs, ensure_ascii=False)}"
            raw = call_groq_safe(client, EXTRACTION_SYSTEM_PROMPT, user_prompt)
            data = _parse_json_response(raw)
            findings.append({"source": "Messages", "data": data})
            cache[cache_key] = data

    save_findings_cache(cache)

    # 5. Final Reconciliation (The "Brain")
    _info(f"Reconciling {len(findings)} sources for {course['course_name']}...")
    week_ref = "\n".join([f"- {s['week']}: {s.get('dates', 'Unknown')}" for s in course.get("section_summaries", [])])
    
    recon_prompt = f"""{course_header}

WEEK TO DATE REFERENCE (Crucial for resolving 'Week X' mentions):
{week_ref}

RAW FINDINGS FROM ALL SOURCES:
{json.dumps(findings, ensure_ascii=False, indent=2)}

FINAL TASK:
You are the central coordinator. Cross-reference all findings to build a final schedule.
- LINKING: If an activity name is 'Quiz 1' and a PDF source says 'Quiz 1 is May 5', that is the date.
- INFERENCE: If an announcement says 'Exam next Monday' and the announcement is in a section dated 'March 9-15', the date is Monday, March 16.
- HIERARCHY: Trust specific dates (e.g. 'March 10') over relative ones (e.g. 'Next Week') if they conflict.
- FORMAT: Provide a JSON array of objects with: summary, description, start_datetime, end_datetime, reminder_minutes, color_id, unique_key.
- UNIQUE KEY: Use a stable, descriptive key (e.g., 'mat286_midterm_2026').

Return ONLY the JSON array.
"""
    final_raw = call_groq_safe(client, RECONCILIATION_SYSTEM_PROMPT, recon_prompt)
    events = _parse_json_response(final_raw)
    
    for ev in events:
        ev.setdefault("course", course["course_name"])
    
    _ok(f"Logic complete: {len(events)} events reconciled")
    return events


def filter_messages_for_course(course: dict, messages: list[dict]) -> list[dict]:
    """Filter global messages to only those that mention this course ID or name."""
    if not messages:
        return []
    
    course_id = str(course["course_id"])
    course_name_clean = re.sub(r"[^a-zA-Z0-9 ]", " ", course["course_name"]).lower()
    course_tokens = set(course_name_clean.split())
    
    filtered = []
    for m in messages:
        text_lower = m["text"].lower()
        if course_id in text_lower:
            filtered.append(m)
            continue
        for token in course_tokens:
            if len(token) > 2 and token in text_lower:
                filtered.append(m)
                break
    return filtered
    """Filter global messages to only those that mention this course ID or name."""
    if not messages:
        return []
    
    course_id = str(course["course_id"])
    course_name_clean = re.sub(r"[^a-zA-Z0-9 ]", " ", course["course_name"]).lower()
    course_tokens = set(course_name_clean.split())
    
    filtered = []
    for m in messages:
        text_lower = m["text"].lower()
        if course_id in text_lower:
            filtered.append(m)
            continue
        for token in course_tokens:
            if len(token) > 2 and token in text_lower:
                filtered.append(m)
                break
    return filtered



def ai_filter_pdfs(pdf_links: list[dict], course_name: str,
                   choices: dict, client: Groq) -> list[dict]:
    """Ask Groq to classify PDF names; skip any already cached in choices."""
    if not pdf_links:
        return []

    # Only classify PDFs we haven't seen before
    unknown = [p for p in pdf_links if p["view_url"] not in choices]

    if unknown:
        names_list = "\n".join(
            f"{i+1}. {p['name']} (Located in section: {p.get('week', 'General')})" 
            for i, p in enumerate(unknown)
        )
        prompt = (
            f"Course: {course_name}\n\n"
            f"Classify these university course files. We only want files with administrative/scheduling info.\n"
            f"INCLUDE (likely to have dates/deadlines):\n"
            f"- Syllabi, Course Outlines, Semester Schedules\n"
            f"- Lab Manuals/Schedules, Project Descriptions, Assignment Sheets\n"
            f"- Exam Schedules, Grading Policies\n\n"
            f"EXCLUDE (likely lecture content/readings):\n"
            f"- Lecture Slides (e.g., 'Chapter 1', 'Week 2 Notes')\n"
            f"- Textbook Chapters, Research Papers\n"
            f"- Problem Sets without due dates (unless they are assignment sheets)\n\n"
            f"Files to classify:\n{names_list}\n\n"
            f"Return ONLY a JSON array of the numbers (1-based) of PDFs to INCLUDE. "
            f"Return [] if none qualify. No explanation."
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
            _warn(f"AI PDF filter failed ({e}) — including all as fallback")
            selected = set(range(1, len(unknown) + 1))

        for i, p in enumerate(unknown):
            choices[p["view_url"]] = ((i + 1) in selected)
        save_pdf_choices(choices)
        _ok(f"AI classified {len(unknown)} new PDFs — kept {len(selected)}")

    useful = [p for p in pdf_links if choices.get(p["view_url"], False)]
    skipped = len(pdf_links) - len(useful)
    if skipped:
        _info(f"Skipping {skipped} lecture PDFs (cached)  ·  downloading {len(useful)} useful")
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
        _err(f"JSON parse error: {e}")
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
            _skip(f"Duplicate: {ev.get('summary')}")
            continue
        if not ev.get("start_datetime") or not ev.get("end_datetime"):
            _warn(f"Missing datetime, skipping: {ev.get('summary')}")
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
                    for m in (ev.get("reminder_minutes") if isinstance(ev.get("reminder_minutes"), list) else [1440, 360, 60])
                    if isinstance(m, int) and m > 0
                ],
            },
        }

        if dry_run:
            _event(f"[DRY RUN] {body['summary']}", body['start']['dateTime'], body['description'])
        else:
            try:
                result = service.events().insert(calendarId="primary", body=body).execute()
                _ok(f"Created: {body['summary']}  ·  {body['start']['dateTime']}")
                created += 1
                seen.add(key)
            except HttpError as e:
                _err(f"Failed to create '{body['summary']}': {e}")

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
        _ok(f"Choices saved to {PDF_CHOICES_FILE}")

    chosen = [p for p in pdf_links if choices.get(p["view_url"], False)]
    skipped = len(pdf_links) - len(chosen)
    if skipped:
        _info(f"{len(chosen)} included  ·  {skipped} skipped (saved choices)")
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
        _ok("PDF cache cleared")

    if args.reset_choices:
        Path(PDF_CHOICES_FILE).unlink(missing_ok=True)
        _ok("PDF choices reset — you will be asked again on next run")

    # 1. Scrape all courses
    scraper = MoodleScraper(username, password)
    if not scraper.login():
        return

    _section("SCRAPING COURSES")
    courses = scraper.scrape_all_courses()
    _ok(f"Found {len(courses)} courses")

    if args.no_ai:
        print(json.dumps(courses, ensure_ascii=False, indent=2))
        return

    # 2. Set up Groq
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        raise ValueError("Set GROQ_API_KEY in .env  (free at https://console.groq.com)")
    client = Groq(api_key=groq_key)

    # Fetch recent messages once — passed to every course prompt as extra context
    _section("MESSAGES")
    recent_messages = scraper.get_recent_messages(days=30)

    all_events = []
    pdf_choices = load_pdf_choices()  # persistent across runs

    # 3. Process each course separately
    for course in courses:
        _course_header(course['course_name'])

        # Download and extract PDF text for this course
        pdf_texts = []
        if not args.no_pdfs and course["pdf_links"]:
            if args.pick_pdfs:
                useful = pick_pdfs_interactively(course["course_name"], course["pdf_links"], pdf_choices)
                _info(f"Downloading {len(useful)} selected PDFs...")
            else:
                useful = ai_filter_pdfs(course["pdf_links"], course["course_name"], pdf_choices, client)
            for pdf_info in useful:
                pdf_bytes = scraper.get_cached_pdf(pdf_info["view_url"])
                if pdf_bytes:
                    text = extract_pdf_text(pdf_bytes)
                    if text:
                        pdf_texts.append({"name": pdf_info["name"], "text": text})
                        _ok(f"Extracted {len(text):,} chars from '{pdf_info['name']}'")
                    else:
                        _warn(f"No text extracted from '{pdf_info['name']}' (may be scanned)")

        # Skip course if nothing to process
        has_dated_activities = any(a.get("due") or a.get("opened") for a in course["activities"])
        has_summaries        = bool(course.get("section_summaries"))
        if not has_dated_activities and not pdf_texts and not has_summaries and not recent_messages:
            _skip(f"Nothing to process — skipping")
            continue

        # Send to Groq
        events = parse_course_with_groq(course, pdf_texts, student_name, student_number, client, recent_messages)
        all_events.extend(events)

    _section(f"RESULTS")
    _ok(f"Total events found: {len(all_events)}")
    if not all_events:
        _info("Nothing new to add to calendar")
        return

    # 4. Create calendar events
    n = create_calendar_events(all_events, dry_run=args.dry_run)
    if not args.dry_run:
        _ok(f"Done! Added {n} new events to Google Calendar")
    else:
        _info(f"[DRY RUN] Would create {len(all_events)} events total")


if __name__ == "__main__":
    main()
