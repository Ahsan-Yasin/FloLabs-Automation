"""The owner's file names for a finished meeting.

    Final_<Meeting>_<YYYY-MM-DD>_Youtube.mp4   the final video (on disk, in the zip, as the download)
    Final_<Meeting>_<YYYY-MM-DD>.zip           the bundle's download name

<Meeting> is the job's title (Zoom topic / YouTube title / upload name) in
CamelCase with any date in it removed, since the date has its own slot:
"All Tech Team Meeting | August 2, 2026" -> "AllTechTeamMeeting". Only
letters and digits survive, so the name is safe on Windows, in a zip and in a
Content-Disposition header.

<YYYY-MM-DD> is the day the meeting happened: Zoom's start_time on the
meeting's own clock (its time zone, else settings.local_timezone), the YouTube
video's upload date, a plausible date written in an upload's title, else the
day the job was created (local day). It is computed once and stored on the job
(JobRecord.meeting_date), so a re-render keeps the same name.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.config import get_settings
from core.models import JobRecord

FINAL_KEY = "final.mp4"  # the artifact's key in job.artifacts: the API, n8n and the UI use it
MAX_MEETING_CHARS = 60
FALLBACK_MEETING = "Meeting"
# an upload's title date older than this (or after the upload) is not the meeting's day
MAX_TITLE_DATE_AGE_DAYS = 2 * 366

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
    "nov": 11, "dec": 12,
}
_MONTH = (r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
          r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_DAY = r"(?P<day>[0-3]?\d)(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>(?:19|20)\d{2})"
# a date uses one separator throughout: "2026-10_05" is not a date
_SEP = r"(?P<sep>[-/._ ])"
# an abbreviated weekday is only removed in front of a date ("Mon, Oct 5"):
# alone it may be a word ("Sun Microsystems"); full weekday names always go
_WEEKDAY_PREFIX = r"(?:(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\.?,?\s+)?"
_DATE_PATTERNS = [
    # 2026-10-05, 2026/10/05, "2026 10 05" (an upload name "..._2026-10-05" reads "2026 10 05")
    re.compile(rf"(?<!\d){_YEAR}{_SEP}(?P<m>[01]?\d)(?P=sep)(?P<d>[0-3]?\d)(?!\d)"),
    # 10/05/2026, 10-05-2026, 10/05/26 (month first, as Zoom and the US write
    # it; day first when the first number can't be a month). Only "/" or "-",
    # and a 2-digit year only after "/": dots and spaces are version numbers
    # ("Python 3.12.10") or counts ("Grade 10 12 2020"), not dates.
    re.compile(r"(?<!\d)(?P<a>[0-3]?\d)(?P<sep>[-/])(?P<b>[0-3]?\d)(?P=sep)"
               r"(?P<y>(?:19|20)\d{2}|(?<=/)\d{2})(?!\d)"),
    # 20261005
    re.compile(rf"(?<!\d){_YEAR}(?P<m>[01]\d)(?P<d>[0-3]\d)(?!\d)"),
    # August 2, 2026 / Aug 2nd 2026 / Oct 5
    re.compile(rf"{_WEEKDAY_PREFIX}\b{_MONTH}\s+{_DAY}\b(?:,?\s+{_YEAR})?(?!\d)", re.IGNORECASE),
    # 2 August 2026 / 5th Oct
    re.compile(rf"{_WEEKDAY_PREFIX}(?<!\d){_DAY}\s+(?:of\s+)?{_MONTH}\b(?:,?\s+{_YEAR})?(?!\d)", re.IGNORECASE),
    # August 2026
    re.compile(rf"\b{_MONTH}\s+{_YEAR}(?!\d)", re.IGNORECASE),
]
_WEEKDAYS = re.compile(r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b", re.IGNORECASE)
# words left dangling once the date is gone ("Sync on Oct 5" -> "Sync")
_DANGLING = {"on", "of", "for", "at", "from", "dated", "the", "-"}


def meeting_name(title: str) -> str:
    """"FloBrain Weekly Backend Meeting" -> "FloBrainWeeklyBackendMeeting";
    any date text is dropped; "Meeting" when nothing usable is left."""
    text = unicodedata.normalize("NFKC", title or "")
    stripped = _strip_dates(text)
    # "Hareem's sync" -> "HareemsSync", not "HareemSSync"
    words = re.split(r"[^\w]+|_", re.sub("['’]", "", stripped))
    words = [_ascii_fold(w) for w in words]
    words = [w for w in ("".join(c for c in w if c.isalnum()) for w in words) if w]
    if stripped != text:
        while words and words[-1].lower() in _DANGLING:
            words.pop()
    name = ""
    for word in words:
        # keep the word's own capitals ("FloBrain", "API"), capitalise its first letter
        part = word[0].upper() + word[1:]
        if len(name) + len(part) > MAX_MEETING_CHARS:
            if not name:  # one huge word: cut it rather than lose the name
                name = part[:MAX_MEETING_CHARS]
            break
        name += part
    return name or FALLBACK_MEETING


def date_in_text(text: str, *, default_year: int | None = None) -> date | None:
    """The first real date written in `text` (a title or file name), or None.
    A date without a year ("Oct 5") takes `default_year`, or is ignored."""
    text = unicodedata.normalize("NFKC", text or "")
    found: list[tuple[int, date]] = []
    for pattern in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            d = _to_date(m.groupdict(), default_year)
            if d is not None:
                found.append((m.start(), d))
                break
    return min(found, key=lambda f: f[0])[1] if found else None


def meeting_date(job: JobRecord, title: str = "") -> str:
    """YYYY-MM-DD the meeting happened (see the module docstring). A stored
    job.meeting_date always wins, so the name never changes on a re-render."""
    if job.meeting_date:
        return job.meeting_date
    zoom = job.zoom_meeting or {}
    if zoom.get("start_time"):
        d = _zoom_day(str(zoom["start_time"]), zoom.get("timezone"))
        if d is not None:
            return d.isoformat()
    created = _local_day(job.created_at or datetime.now(UTC), _zone())
    d = _plausible_title_date(title or job.title, created)
    return (d or created).isoformat()


def upload_date_iso(value: str | None) -> str | None:
    """yt-dlp's upload_date ("20261005") as "2026-10-05"; None if missing/odd."""
    value = (value or "").strip()
    if not re.fullmatch(r"\d{8}", value):
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:])).isoformat()
    except ValueError:
        return None


def final_video_name(title: str, meeting_day: str) -> str:
    return f"Final_{meeting_name(title)}_{meeting_day}_Youtube.mp4"


def final_file_name(job: JobRecord, title: str = "") -> str:
    """The final video's file name: the one it was saved under (old jobs:
    "final.mp4"), else the name a render of this job gives it."""
    info = job.artifacts.get(FINAL_KEY)
    if info is not None and info.path.startswith("Final_"):
        return info.path
    return final_video_name(title or job.title, meeting_date(job, title))


def download_stem(job: JobRecord) -> str:
    """"Final_<Meeting>_<YYYY-MM-DD>": the bundle's download name and the
    final video's, without "_Youtube.mp4". Old jobs (a final.mp4 on disk)
    get it computed from their title and dates."""
    name = final_file_name(job)
    return name.removesuffix(".mp4").removesuffix("_Youtube")


# ------------------------------------------------------------------ helpers


def _strip_dates(text: str) -> str:
    for pattern in _DATE_PATTERNS:
        text = pattern.sub(" ", text)
    return _WEEKDAYS.sub(" ", text)


def _ascii_fold(word: str) -> str:
    """"Café" -> "Cafe"; letters with no ASCII form (Urdu, Chinese) stay."""
    decomposed = unicodedata.normalize("NFKD", word)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _to_date(g: dict, default_year: int | None) -> date | None:
    try:
        if g.get("mon"):
            year = int(g["year"]) if g.get("year") else default_year
            if year is None:
                return None
            if g.get("day") is None:  # "August 2026": the month is all we know
                return None
            return date(year, _MONTHS[g["mon"].lower()[:3]], int(g["day"]))
        if g.get("a") is not None:
            a, b, y = int(g["a"]), int(g["b"]), int(g["y"])
            y = y + 2000 if y < 100 else y
            month, day = (a, b) if a <= 12 else (b, a)
            return date(y, month, day)
        return date(int(g["year"]), int(g["m"]), int(g["d"]))
    except (ValueError, KeyError, TypeError):
        return None


def _zone(name: str | None = None) -> tzinfo:
    """The zone a meeting's day is counted in: the meeting's own (Zoom sends
    one), else settings.local_timezone (the team's), else UTC."""
    for candidate in (name, get_settings().local_timezone):
        if candidate:
            try:
                return ZoneInfo(str(candidate))
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return UTC


def _local_day(moment: datetime, zone: tzinfo) -> date:
    # a naive time is UTC: Zoom's start_time and created_at both are
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(zone).date()


def _zoom_day(start: str, zone_name: str | None) -> date | None:
    """Zoom's start_time ("2026-10-04T20:30:00Z") as the day it was where the
    meeting happened (2026-10-05 in Karachi), not the UTC day."""
    try:
        return _local_day(datetime.fromisoformat(start.replace("Z", "+00:00")), _zone(zone_name))
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", start)
        try:
            return date(int(m[1]), int(m[2]), int(m[3])) if m else None
        except ValueError:
            return None


def _plausible_title_date(title: str, created: date) -> date | None:
    """A date written in an upload's title, if it can be the meeting's day:
    not after the job was made and at most MAX_TITLE_DATE_AGE_DAYS before.
    Anything else is a number that looks like a date ("1/2/10"), not the
    meeting's day. A date without a year that lands in the future
    ("Dec 30" on a job made on Jan 2) is last year's."""
    for year in (created.year, created.year - 1):
        d = date_in_text(title, default_year=year)
        if d is None:
            return None
        if created - timedelta(days=MAX_TITLE_DATE_AGE_DAYS) <= d <= created:
            return d
    return None
