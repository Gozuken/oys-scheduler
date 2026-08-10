# New Moodle System Migration Plan

Since Baskent University is moving to a new Moodle version (`oys.baskent.edu.tr`) starting after the 2025-2026 summer term, the HTML layout of the course pages will likely change significantly from the old system (`oys2.baskent.edu.tr`).

The script currently relies on specific CSS selectors (`li.section.course-section`, `li.activity`, `div.activity-description`, etc.) that were standard in older Moodle versions. These will likely break on the new platform.

## TODOs when the new system is active:

1. **Obtain Sample HTML**:
   - Log in to `https://oys.baskent.edu.tr`.
   - Open a course page.
   - Save the HTML source of the page and place it in the project root for testing (e.g., `sample_course_new.html`).

2. **Update BeautifulSoup Selectors in `moodle_to_calendar.py`**:
   - Update `MoodleScraper.get_course_data` function.
   - Find the new selector for course sections/weeks (formerly `li.section.course-section`).
   - Find the new selector for activities/resources (formerly `li.activity`).
   - Check how dates (Opened/Due) are displayed and update the parsing logic inside `date-region`.
   - Ensure PDF links can still be extracted properly.

3. **Test with `OYS_BASE_URL`**:
   - Set `OYS_BASE_URL=https://oys.baskent.edu.tr` in `.env`.
   - Run the script in dry-run mode (`python moodle_to_calendar.py --dry-run`) to verify extraction works without making calendar changes.
