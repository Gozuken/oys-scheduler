# Baskent OYS → Google Calendar

Scrapes your Baskent University course pages and automatically creates Google Calendar events for assignments, quizzes, exams, and deadlines — using AI to parse dates from activities, section announcements, and PDFs.

## How it works

1. Logs into OYS and scrapes all your courses
2. Collects activities, section announcement text, and PDF links
3. Uses AI (Groq) to filter out lecture PDFs — only downloads syllabus, lab schedule, assignment sheets, etc.
4. Sends everything to Groq (LLaMA 3.3 70B) per course, which extracts calendar events
5. Creates deduplicated events in your Google Calendar with reminders

## Setup

### 1. Install dependencies

```bash
pip install requests beautifulsoup4 groq pypdf google-auth google-auth-oauthlib google-api-python-client python-dotenv tzdata
```

### 2. Create a `.env` file

```env
OYS_USERNAME=your_student_number
OYS_PASSWORD=your_password
GROQ_API_KEY=gsk_...
STUDENT_NAME=Your Full Name
STUDENT_NUMBER=your_student_number
```

Get a free Groq API key at [console.groq.com](https://console.groq.com).

### 3. Set up Google Calendar API

1. Go to [Google Cloud Console](https://console.cloud.google.com) and create a project
2. Enable the **Google Calendar API**
3. Create **OAuth 2.0 credentials** (Desktop app) and download as `credentials.json`
4. Place `credentials.json` in the same folder as the script
5. On first run, a browser window will open asking you to authorize — after that, `token.json` is saved and reused

### 4. Set your course IDs

Edit `COURSE_IDS` in the script to match your enrolled courses. The ID is the number in the URL when you open a course: `oys2.baskent.edu.tr/course/view.php?id=XXXXX`.

## Usage

```bash
# Normal run — scrape, parse, and add events to calendar
python moodle_to_calendar.py

# Preview what would be added without touching the calendar
python moodle_to_calendar.py --dry-run

# Manually choose which PDFs to include per course (choices are saved)
python moodle_to_calendar.py --pick-pdfs

# Skip all PDF processing
python moodle_to_calendar.py --no-pdfs

# Print raw scraped data without calling AI
python moodle_to_calendar.py --no-ai

# Re-download all PDFs (clears local cache)
python moodle_to_calendar.py --clear-cache

# Forget saved PDF include/exclude choices and re-classify
python moodle_to_calendar.py --reset-choices
```

## Persistent state

The script maintains three local files so repeated runs are efficient:

| File | Purpose |
|---|---|
| `token.json` | Google OAuth token, auto-refreshed |
| `seen_events.json` | Tracks created events to avoid duplicates |
| `pdf_choices.json` | Caches AI decisions on which PDFs to include |
| `pdf_cache/` | Downloaded PDF binaries, keyed by URL hash |

## Event types

| Type | Color | Reminders |
|---|---|---|
| Exam / Quiz | Red | 1 week, 1 day, 3 hours |
| Assignment deadline | Green | 1 day, 6 hours, 1 hour |
| Grading / info | Purple | 1 day |
| Office hours | Yellow | 1 day |
