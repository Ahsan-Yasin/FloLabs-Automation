"""The owner's file names: Final_<Meeting>_<YYYY-MM-DD>_Youtube.mp4 and
Final_<Meeting>_<YYYY-MM-DD>.zip (core/naming.py)."""

import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import pipeline as pipeline_module
from core.models import ArtifactInfo, JobRecord
from core.naming import (
    date_in_text,
    download_stem,
    final_file_name,
    final_video_name,
    meeting_date,
    meeting_name,
    upload_date_iso,
)
from ingest.youtube import YoutubeDownload


@pytest.mark.parametrize(("title", "expected"), [
    ("All Tech Team Meeting | August 2, 2026", "AllTechTeamMeeting"),
    ("FloBrain Weekly Backend Meeting", "FloBrainWeeklyBackendMeeting"),
    ("backendmeeting", "Backendmeeting"),
    ("backend meeting 2026-10-05", "BackendMeeting"),
    ("Sync 10/05/2026", "Sync"),
    ("Standup Oct 5", "Standup"),
    ("Mon, Oct 5 standup", "Standup"),
    ("Thursday Standup", "Standup"),
    ("Sync on Oct 5", "Sync"),
    ("2nd Aug 2026 - API review", "APIReview"),
    ("Planning August 2026", "Planning"),
    ("weekly sync 2026 10 05", "WeeklySync"),  # an upload name "weekly_sync-2026-10-05.mp4"
    ("zoom_20261005_recording", "ZoomRecording"),
    ("Hareem's API review", "HareemsAPIReview"),
    ("Café meeting", "CafeMeeting"),
    ("Q3 2026 Planning", "Q32026Planning"),  # a bare year is not a date
    ("Sun Microsystems sync", "SunMicrosystemsSync"),  # an abbreviated weekday alone is a word
])
def test_meeting_name(title, expected):
    assert meeting_name(title) == expected


@pytest.mark.parametrize("title", ["", "   ", "***", "| August 2, 2026", "Monday 10/05/2026", None])
def test_an_empty_or_date_only_title_falls_back_to_meeting(title):
    assert meeting_name(title) == "Meeting"


def test_the_name_is_windows_safe_and_at_most_60_characters():
    name = meeting_name('a/b\\c:d*e?f"g<h>i|j' + " word" * 40)
    assert re.fullmatch(r"[A-Za-z0-9]+", name) and len(name) <= 60
    assert name.startswith("ABCDEFGHIJWord")
    assert meeting_name("x" * 100) == "X" + "x" * 59  # one huge word is cut, not lost
    full = final_video_name('CON: "Q&A" / review?', "2026-10-05")
    assert full == "Final_CONQAReview_2026-10-05_Youtube.mp4"
    assert not set(full) & set('<>:"/\\|?*')


@pytest.mark.parametrize(("text", "expected"), [
    ("All Tech Team Meeting | August 2, 2026", date(2026, 8, 2)),
    ("backend 2026-10-05", date(2026, 10, 5)),
    ("Sync 10/05/2026", date(2026, 10, 5)),  # month first
    ("Sprint review 25/12/2026", date(2026, 12, 25)),  # day first when it can't be a month
    ("Standup Oct 5", date(2025, 10, 5)),  # no year: the default year
    ("5th of March 2026", date(2026, 3, 5)),
    ("zoom_20261005", date(2026, 10, 5)),
    ("Sync 2026-02-30", None),  # not a real day
    ("Q3 2026 Planning", None),
    ("v1.2.3 release", None),
])
def test_date_in_text(text, expected):
    assert date_in_text(text, default_year=2025) == expected


def test_a_date_without_a_year_needs_a_default_year():
    assert date_in_text("Standup Oct 5") is None


@pytest.mark.parametrize(("title", "name"), [
    ("Release 1.2.10 Review", "Release1210Review"),
    ("Python 3.12.10 upgrade sync", "Python31210UpgradeSync"),
    ("Grade 10 12 2020 results", "Grade10122020Results"),
    ("Sync 10-05-26", "Sync100526"),  # a 2-digit year only after "/"
    ("Sync 10/05-2026", "Sync10052026"),  # mixed separators are not a date
])
def test_version_numbers_and_number_triples_are_not_dates(title, name):
    # regression: these were read as 2010-01-02 / 2010-03-12 / 2020-10-12 and
    # their numbers were dropped from the name
    assert date_in_text(title, default_year=2026) is None
    assert meeting_name(title) == name


@pytest.mark.parametrize(("text", "expected"), [
    ("Sync 10-05-2026", date(2026, 10, 5)),
    ("Sync 10/05/26", date(2026, 10, 5)),
])
def test_slash_and_dash_dates_still_count(text, expected):
    assert date_in_text(text) == expected


def test_an_upload_title_date_that_cant_be_the_meeting_day_is_ignored():
    created = datetime(2026, 10, 6, 9, tzinfo=UTC)
    # long before the upload ("1/2/10" read as 2010-01-02) or after it: the upload's day
    assert meeting_date(JobRecord(job_id="u", title="Release 1/2/10 Review", created_at=created)) == "2026-10-06"
    assert meeting_date(JobRecord(job_id="u", title="Sync 2026-12-01", created_at=created)) == "2026-10-06"
    assert meeting_date(JobRecord(job_id="u", title="Sync 2025-01-15", created_at=created)) == "2025-01-15"
    # a yearless date that would be in the future is last year's
    jan = datetime(2027, 1, 2, 9, tzinfo=UTC)
    assert meeting_date(JobRecord(job_id="u", title="Standup Dec 30", created_at=jan)) == "2026-12-30"


def test_a_zoom_meeting_after_midnight_in_pakistan_gets_the_pakistan_day():
    # regression: 20:30Z on Oct 4 is 01:30 PKT on Oct 5; the UTC day was used and saved
    job = JobRecord(job_id="z", title="My Meeting", zoom_meeting={"start_time": "2026-10-04T20:30:00Z"})
    assert meeting_date(job) == "2026-10-05"
    assert download_stem(job) == "Final_MyMeeting_2026-10-05"
    late = JobRecord(job_id="z", title="My Meeting", zoom_meeting={"start_time": "2026-09-29T21:00:29Z"})
    assert meeting_date(late) == "2026-09-30"


def test_a_zoom_meetings_own_time_zone_wins_over_the_default():
    start = {"start_time": "2026-10-04T20:30:00Z"}
    ny = JobRecord(job_id="z", zoom_meeting={**start, "timezone": "America/New_York"})
    assert meeting_date(ny) == "2026-10-04"  # 16:30 in New York
    # an unknown zone falls back to settings.local_timezone (Asia/Karachi)
    bogus = JobRecord(job_id="z", zoom_meeting={**start, "timezone": "Not/AZone"})
    assert meeting_date(bogus) == "2026-10-05"


def test_the_upload_fallback_day_is_the_local_day():
    # 20:30Z on Oct 5 is Oct 6 in Karachi
    job = JobRecord(job_id="u", title="Weekly Sync", created_at=datetime(2026, 10, 5, 20, 30, tzinfo=UTC))
    assert meeting_date(job) == "2026-10-06"


def test_zoom_meetings_use_their_start_time():
    job = JobRecord(job_id="z", title="All Tech Team Meeting | August 2, 2026",
                    zoom_meeting={"start_time": "2026-10-05T15:00:00Z"},
                    created_at=datetime(2026, 10, 6, tzinfo=UTC))
    # Zoom's own date wins over a date typed in the topic
    assert meeting_date(job) == "2026-10-05"
    assert final_file_name(job) == "Final_AllTechTeamMeeting_2026-10-05_Youtube.mp4"


def test_uploads_use_the_date_in_their_title_else_the_day_the_job_was_made():
    created = datetime(2026, 10, 6, 9, tzinfo=UTC)
    assert meeting_date(JobRecord(job_id="u", title="weekly sync 2026 09 30", created_at=created)) == "2026-09-30"
    assert meeting_date(JobRecord(job_id="u", title="Standup Oct 1", created_at=created)) == "2026-10-01"
    assert meeting_date(JobRecord(job_id="u", title="Weekly Sync", created_at=created)) == "2026-10-06"
    assert meeting_date(JobRecord(job_id="u", title="x", zoom_meeting={"start_time": None},
                                  created_at=created)) == "2026-10-06"


def test_a_stored_meeting_date_always_wins():
    job = JobRecord(job_id="s", title="Sync 2026-01-01", meeting_date="2026-08-02",
                    zoom_meeting={"start_time": "2026-10-05T15:00:00Z"})
    assert meeting_date(job) == "2026-08-02"


def test_youtube_upload_date():
    assert upload_date_iso("20260802") == "2026-08-02"
    assert upload_date_iso("") is None and upload_date_iso(None) is None
    assert upload_date_iso("20261340") is None and upload_date_iso("2026-08-02") is None


def test_a_youtube_job_keeps_the_videos_upload_date(monkeypatch, tmp_path):
    source = tmp_path / "abc.mp4"
    source.write_bytes(b"x")
    monkeypatch.setattr(pipeline_module, "download_youtube",
                        lambda url: YoutubeDownload("abc", source, "FloBrain Weekly Backend Meeting", "2026-08-02"))
    ran = []
    monkeypatch.setattr(pipeline_module, "run_pipeline", lambda job, update: ran.append(job))
    job = JobRecord(job_id="yt", created_at=datetime(2026, 10, 6, tzinfo=UTC))
    pipeline_module.run_youtube_pipeline(job, lambda j: None, "https://youtube.com/watch?v=abc")
    assert ran and job.meeting_date == "2026-08-02"
    assert final_file_name(job) == "Final_FloBrainWeeklyBackendMeeting_2026-08-02_Youtube.mp4"
    assert download_stem(job) == "Final_FloBrainWeeklyBackendMeeting_2026-08-02"


def test_a_youtube_video_without_an_upload_date_uses_the_job_date(monkeypatch, tmp_path):
    source = tmp_path / "abc.mp4"
    source.write_bytes(b"x")
    monkeypatch.setattr(pipeline_module, "download_youtube", lambda url: YoutubeDownload("abc", source, "Sync"))
    monkeypatch.setattr(pipeline_module, "run_pipeline", lambda job, update: None)
    job = JobRecord(job_id="yt", created_at=datetime(2026, 10, 6, tzinfo=UTC))
    pipeline_module.run_youtube_pipeline(job, lambda j: None, "https://youtube.com/watch?v=abc")
    assert job.meeting_date is None and meeting_date(job) == "2026-10-06"


def test_names_for_new_and_old_jobs():
    new = JobRecord(job_id="n", title="Renamed later", meeting_date="2026-10-05", artifacts={
        "final.mp4": ArtifactInfo(path="Final_WeeklySync_2026-08-02_Youtube.mp4")})
    # the name it was saved under, even if the title changed since
    assert final_file_name(new) == "Final_WeeklySync_2026-08-02_Youtube.mp4"
    assert download_stem(new) == "Final_WeeklySync_2026-08-02"
    # a job rendered before these names: final.mp4 on disk, the name computed for downloads
    old = JobRecord(job_id="o", title="Weekly Sync", created_at=datetime(2026, 9, 1, tzinfo=UTC),
                    artifacts={"final.mp4": ArtifactInfo(path="final.mp4")})
    assert Path(old.artifacts["final.mp4"].path).name == "final.mp4"
    assert download_stem(old) == "Final_WeeklySync_2026-09-01"
