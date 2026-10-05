"""The owner's intro / outro clips around final.mp4: finding them, converting
them to the meeting's format, and the timeline of final.mp4 with them."""

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from core.config import get_settings
from core.models import (
    Chapter,
    EditDecisionList,
    EDLRange,
    JobRecord,
    JobStatus,
    Moment,
    RemovedRange,
    Word,
)
from core.timeline import AUDIO_RATE, rate_str, samples_at_frame
from decide.chapters import ChapterResult
from slice import intro_outro
from slice.ffmpeg_wrapper import build_clip_graph
from slice.intro_outro import find_intro_outro, role_of

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
OWNER_INTRO, OWNER_OUTRO = "CTD - Opening.mp4", "FloLabs - Widescreen outro.mp4"

# ---------------------------------------------------------------- discovery


def _folder(*names: str) -> Path:
    """Files in the per-test intro_outro folder (tests/conftest.py points INTRO_OUTRO_DIR at it)."""
    folder = get_settings().intro_outro_dir
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"x")
    return folder


def test_the_owners_file_names_are_recognised():
    assert role_of(OWNER_INTRO) == "intro" and role_of(OWNER_OUTRO) == "outro"
    assert role_of("FloLabsIntro.MP4") == "intro" and role_of("THE_END.mov") == "outro"
    assert role_of("Closing credits v2.mkv") == "outro" and role_of("start-here.webm") == "intro"
    # whole words only, and a name with both roles is neither
    assert role_of("Frontend demo.mp4") is None and role_of("Reopening.mp4") is None
    assert role_of("intro_outro.mp4") is None


def test_finds_the_owners_clips_and_ignores_other_files():
    folder = _folder(OWNER_INTRO, OWNER_OUTRO, "notes.txt", "Opening still.png")
    found = find_intro_outro(get_settings())
    assert (found.intro, found.outro) == (folder / OWNER_INTRO, folder / OWNER_OUTRO)
    assert found.notes == []


def test_switched_off_by_the_setting_or_per_job():
    _folder(OWNER_INTRO, OWNER_OUTRO)
    assert find_intro_outro(get_settings(), enabled=False).intro is None
    settings = get_settings().model_copy(update={"intro_outro_enabled": False})
    assert find_intro_outro(settings).intro is None and find_intro_outro(settings).outro is None
    # the job's own choice wins over the setting, both ways
    assert find_intro_outro(settings, enabled=True).outro is not None
    assert find_intro_outro(get_settings(), enabled=None).outro is not None


def test_a_missing_folder_or_clip_only_leaves_that_part_out():
    found = find_intro_outro(get_settings())  # the folder does not exist
    assert (found.intro, found.outro, found.notes) == (None, None, [])
    folder = _folder("Opening.mp4")
    found = find_intro_outro(get_settings())
    assert (found.intro, found.outro, found.notes) == (folder / "Opening.mp4", None, [])


def test_with_two_videos_one_name_is_enough():
    folder = _folder("Opening.mp4", "FloLabs brand.mp4")
    found = find_intro_outro(get_settings())
    assert (found.intro, found.outro) == (folder / "Opening.mp4", folder / "FloLabs brand.mp4")
    shutil.rmtree(folder)
    folder = _folder("outro.mp4", "brand.mov")
    found = find_intro_outro(get_settings())
    assert (found.intro, found.outro) == (folder / "brand.mov", folder / "outro.mp4")


def test_unnamed_videos_are_not_guessed_but_reported():
    _folder("clip one.mp4", "clip two.mp4")
    found = find_intro_outro(get_settings())
    assert (found.intro, found.outro) == (None, None)
    assert "clip one.mp4, clip two.mp4" in found.notes[0] and '"intro" or "outro"' in found.notes[0]


def test_two_intros_use_the_alphabetically_first_with_a_warning():
    folder = _folder("intro b.mp4", "Intro A.mp4", "outro.mp4")
    found = find_intro_outro(get_settings())
    assert found.intro == folder / "Intro A.mp4" and found.outro == folder / "outro.mp4"
    assert found.notes == ["2 intro videos in the intro_outro folder (Intro A.mp4, intro b.mp4); used Intro A.mp4"]


def test_explicit_files_override_the_folder_and_resolve_against_the_project(monkeypatch, tmp_path):
    folder = _folder(OWNER_INTRO, OWNER_OUTRO)
    elsewhere = tmp_path / "brand"
    elsewhere.mkdir()
    (elsewhere / "my opener.mp4").write_bytes(b"x")
    monkeypatch.setattr(intro_outro, "PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(folder)  # the working directory must not matter
    settings = get_settings().model_copy(update={"intro_file": "brand/my opener.mp4"})
    found = find_intro_outro(settings)
    assert found.intro == tmp_path / "brand" / "my opener.mp4" and found.outro == folder / OWNER_OUTRO
    # a named file that does not exist: no intro (and the folder's is not used instead)
    found = find_intro_outro(settings.model_copy(update={"intro_file": "brand/missing.mp4"}))
    assert found.intro is None and found.outro == folder / OWNER_OUTRO


def test_a_relative_folder_is_under_the_project_not_the_working_directory(monkeypatch, tmp_path):
    (tmp_path / "clips").mkdir()
    (tmp_path / "clips" / OWNER_INTRO).write_bytes(b"x")
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr(intro_outro, "PROJECT_ROOT", tmp_path)
    found = find_intro_outro(get_settings().model_copy(update={"intro_outro_dir": Path("clips")}))
    assert found.intro == tmp_path / "clips" / OWNER_INTRO


def test_the_real_settings_default_is_the_projects_intro_outro_folder(monkeypatch):
    monkeypatch.delenv("INTRO_OUTRO_DIR")
    from core.config import PROJECT_ROOT, Settings

    assert intro_outro.project_path(Settings(_env_file=None).intro_outro_dir) == PROJECT_ROOT / "intro_outro"


def test_clip_graph_fits_pads_and_tags_like_the_meeting():
    graph = build_clip_graph(fps=Fraction(30), width=1280, height=720, frames=208,
                             color_tags={"color_primaries": "bt709", "color_space": "bt709", "color_range": "tv"})
    assert graph.startswith("[0:v]fps=30/1:start_time=0:round=down,")
    # square pixels first, then fit inside the frame with the aspect kept, on black
    assert "scale=w=trunc(iw*sar/2)*2:h=ih,setsar=1," in graph
    assert ("scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2:out_range=tv,"
            "pad=1280:720:-1:-1:color=black") in graph
    # tagged exactly like the meeting; a tag the meeting lacks is reset to unknown
    assert "setparams=color_primaries=bt709:color_trc=unknown:colorspace=bt709:range=tv," in graph
    assert graph.endswith("trim=end_frame=208,setpts=PTS-STARTPTS[vout]")


# ------------------------------------------------------------ real ffmpeg


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def _make_meeting(path: Path, size: str, seconds: int, channels: int) -> None:
    """testsrc2 + a 440 Hz tone, bt709-tagged like a YouTube/Zoom recording."""
    _ffmpeg("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-pix_fmt", "yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
            "-c:a", "aac", "-ac", str(channels), str(path))


def _make_intro(path: Path) -> None:
    """Like the owner's: 1920x1080 at 59.94 fps, untagged, stereo AAC (here at
    44.1 kHz, so it is resampled). 120 frames = 2.002 s -> 60 frames at 30 fps."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=60000/1001:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2", str(path))


def _make_outro(path: Path) -> None:
    """4:3, 25 fps, and NO audio stream: 40 frames = 1.6 s -> 48 frames at 30 fps, silent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x480:rate=25:duration=1.6",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", str(path))


def _gray(path: Path, t: float) -> tuple[bytes, int, int]:
    from slice.profile import probe_header

    h = probe_header(path)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], check=True, capture_output=True).stdout
    return raw, h.width, h.height


def _mean(raw: bytes, width: int, x0: int, x1: int, y0: int, y1: int) -> float:
    total = sum(sum(raw[y * width + x0:y * width + x1]) for y in range(y0, y1))
    return total / ((x1 - x0) * (y1 - y0))


def _rms(path: Path, start: float, end: float) -> float:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i",
                          str(path), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         check=True, capture_output=True).stdout
    samples = [int.from_bytes(raw[i:i + 2], "little", signed=True) for i in range(0, len(raw) - 1, 2)]
    return (sum(s * s for s in samples) / max(1, len(samples))) ** 0.5


def _edl(ranges, total, fps=Fraction(30), d=0):
    return EditDecisionList(
        ranges=[EDLRange(start=float(Fraction(s) / fps), end=float(Fraction(e) / fps), start_frame=s, end_frame=e)
                for s, e in ranges],
        source_duration=float(Fraction(total) / fps), fps=rate_str(fps), fade_frames=d, total_frames=total)


def _deliver(tmp_path, src: Path, *, reel: bool, intro: Path | None, outro: Path | None,
             chapters: list[Chapter] | None = None):
    """A whole job's delivery (render + transcripts + chapters + report +
    zip) on a 40 s synthetic meeting: cleaned = all but 10-12 s (38 s), the
    reel = 20-30 s of the source."""
    from deliver import DeliverInputs, deliver
    from outputs import RenderInputs
    from slice.profile import probe_media

    media = probe_media(src)
    job = JobRecord(job_id="io-job", status=JobStatus.SLICING, title="Weekly sync")
    job_dir = get_settings().jobs_dir / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    words = [Word(word="Opening remarks.", start=0.5, end=2.0, speaker="A"),
             Word(word="The demo works.", start=21.0, end=23.0, speaker="B")]
    render = RenderInputs(
        job_dir=job_dir, source=src, media=media, edl=_edl([(0, 300), (360, 1200)], 1200),
        removed=[RemovedRange(start=10.0, end=12.0, start_frame=300, end_frame=360, tier="video",
                              reason="small talk")],
        reel_edl=_edl([(600, 900)], 1200) if reel else None, shorts=[], words=words, title="Weekly sync",
        intro=intro, outro=outro)
    moments = [Moment(id=0, start=20.0, end=30.0, first_index=1, last_index=1, score=9, title="The demo")]
    chapter_fn = (lambda clean_words, cleaned_s: ChapterResult(chapters, True)) if chapters else None
    deliver(job, lambda j: None, DeliverInputs(render=render, reel=moments if reel else [], chapter_fn=chapter_fn))
    assert job.status == JobStatus.DONE, job.error
    return job, job_dir


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_real_final_is_intro_reel_card_meeting_outro_frame_exact_and_in_sync(tmp_path):
    from slice.pipeline import AAC_TOLERANCE_S
    from slice.profile import probe_header

    src = tmp_path / "meeting.mp4"
    _make_meeting(src, "1280x720", 40, channels=2)
    intro, outro = tmp_path / "clips" / OWNER_INTRO, tmp_path / "clips" / OWNER_OUTRO
    _make_intro(intro)
    _make_outro(outro)
    chapters = [Chapter(start=0.0, title="Welcome"), Chapter(start=12.0, title="Roadmap"),
                Chapter(start=24.0, title="Q&A")]
    job, job_dir = _deliver(tmp_path, src, reel=True, intro=intro, outro=outro, chapters=chapters)
    manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
    final = manifest["final"]
    card = round(final["title_card_s"] * 30)  # 60 frames, or 0 on a machine without a font
    # 60 (intro, 2.002 s at 59.94 -> nearest frame at 30) + 300 (reel) + card + 1140 (meeting) + 48 (outro)
    want = 60 + 300 + card + 1140 + 48
    header = probe_header(job_dir / "final.mp4")
    assert header.video_frames == final["frames"] == want
    assert (header.width, header.height, header.fps) == (1280, 720, "30/1")
    want_samples = sum(samples_at_frame(n, Fraction(30)) for n in (60, 300, card, 1140, 48))
    assert abs(header.audio_duration - want_samples / AUDIO_RATE) <= AAC_TOLERANCE_S
    # the timeline: intro 2 s, then the reel, the card, the meeting at 12 s + card, the outro
    assert (final["intro_file"], final["outro_file"]) == (OWNER_INTRO, OWNER_OUTRO)
    assert (final["intro_s"], final["highlights_s"], final["cleaned_s"], final["outro_s"]) == (2.0, 10.0, 38.0, 1.6)
    assert job.final_offset_s == final["cleaned_starts_at_s"] == 12.0 + card / 30
    assert (job.intro_s, job.outro_s) == (2.0, 1.6)
    parts = final["intro_s"] + final["highlights_s"] + final["title_card_s"] + final["cleaned_s"] + final["outro_s"]
    assert abs(parts - final["duration_s"]) < 0.002
    assert manifest["highlights"][0]["final_at_s"] == 2.0  # the reel starts after the intro
    # transcript: the reel's line 1 s into the reel, the meeting's 0.5 s into the meeting
    lines = json.loads((job_dir / "transcript_clean.json").read_text(encoding="utf-8"))["lines"]
    assert [(x["section"], x["start"]) for x in lines] == [("highlights", 3.0),
                                                          ("meeting", round(job.final_offset_s + 0.5, 3)),
                                                          ("meeting", round(job.final_offset_s + 19.0, 3))]
    text = (job_dir / "transcript_clean.txt").read_text(encoding="utf-8")
    assert "opens with the intro (2s) and the highlights reel" in text and "ends with the outro (2s)" in text
    # chapters: "Highlights" covers intro + reel + card; topics shifted by all three
    offset = int(job.final_offset_s)
    assert Path(job.chapters_path).read_text(encoding="utf-8").splitlines() == [
        "00:00 Highlights", f"00:{offset:02d} Welcome", f"00:{offset + 12:02d} Roadmap",
        f"00:{offset + 24:02d} Q&A"]
    # the intro's own sound is at the start; the outro (no audio stream) is silent
    end = final["duration_s"]
    assert _rms(job_dir / "final.mp4", 0.2, 1.8) > 1000
    assert _rms(job_dir / "final.mp4", end - 1.3, end - 0.2) < 50
    assert _rms(job_dir / "final.mp4", job.final_offset_s + 1, job.final_offset_s + 3) > 1000
    # 16:9 intro fills the 16:9 frame; the 4:3 outro is pillarboxed (960 px wide, centred)
    raw, w, h = _gray(job_dir / "final.mp4", 1.0)
    assert _mean(raw, w, 0, 100, 100, 620) > 30
    raw, w, h = _gray(job_dir / "final.mp4", end - 0.7)
    assert _mean(raw, w, 0, 150, 0, h) < 20 and _mean(raw, w, 1130, 1280, 0, h) < 20
    assert _mean(raw, w, 400, 880, 100, 620) > 30
    # the report says what final.mp4 is made of
    from pypdf import PdfReader

    pdf = "".join(page.extract_text() for page in PdfReader(job_dir / "report.pdf").pages)
    assert OWNER_INTRO in pdf and OWNER_OUTRO in pdf and "intro 2.0s" in pdf


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_real_intro_without_a_reel_is_part_of_the_first_chapter_at_750x480(tmp_path):
    from slice.profile import probe_header

    src = tmp_path / "meeting.mp4"
    _make_meeting(src, "750x480", 40, channels=1)  # the regression meeting's odd size, mono
    intro, outro = tmp_path / "clips" / OWNER_INTRO, tmp_path / "clips" / OWNER_OUTRO
    _make_intro(intro)
    _make_outro(outro)
    chapters = [Chapter(start=0.0, title="Welcome"), Chapter(start=12.0, title="Roadmap"),
                Chapter(start=24.0, title="Q&A")]
    job, job_dir = _deliver(tmp_path, src, reel=False, intro=intro, outro=outro, chapters=chapters)
    header = probe_header(job_dir / "final.mp4")
    assert header.video_frames == 60 + 1140 + 48 and (header.width, header.height) == (750, 480)
    assert header.audio_channels == 2  # AAC is always stereo; the mono meeting's chunks joined the mono clips
    assert job.final_offset_s == 2.0 and job.intro_s == 2.0
    lines = json.loads((job_dir / "transcript_clean.json").read_text(encoding="utf-8"))["lines"]
    assert lines[0]["section"] == "meeting" and lines[0]["start"] == 2.5
    # no reel: the first topic covers the intro from 00:00, the others move by the intro
    assert Path(job.chapters_path).read_text(encoding="utf-8").splitlines() == [
        "00:00 Welcome", "00:14 Roadmap", "00:26 Q&A"]
    text = (job_dir / "transcript_clean.txt").read_text(encoding="utf-8")
    assert "opens with the intro (2s); the full meeting starts at 00:02" in text
    # 16:9 intro letterboxed into 750x480 (1.5625:1): black above and below, picture in the middle
    raw, w, h = _gray(job_dir / "final.mp4", 1.0)
    assert _mean(raw, w, 0, w, 0, 20) < 20 and _mean(raw, w, 0, w, h - 20, h) < 20
    assert _mean(raw, w, 0, w, 100, 380) > 30
    # 4:3 outro: 640x480 in the middle, 55 px bars left and right
    raw, w, h = _gray(job_dir / "final.mp4", 2.0 + 38.0 + 0.8)
    assert _mean(raw, w, 0, 50, 0, h) < 20 and _mean(raw, w, 700, 750, 0, h) < 20
    assert _mean(raw, w, 100, 650, 50, 430) > 30


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_real_anamorphic_clip_keeps_its_displayed_shape(tmp_path):
    """720x576 stored, 16:9 displayed (SAR 64:45): it fills a 16:9 meeting.
    Fitting the stored 5:4 pixels instead would pillarbox it 190 px a side."""
    from slice.pipeline import render_clip
    from slice.profile import MediaInfo, assert_concat_compatible

    clip = tmp_path / "intro.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=720x576:rate=25:duration=0.4", "-vf", "setsar=64/45",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", str(clip))
    media = MediaInfo(path=tmp_path / "meeting.mp4", duration=60.0, video_duration=60.0, fps=Fraction(30),
                      width=640, height=360, pix_fmt="yuv420p", total_frames=1800, has_audio=True, audio_channels=1,
                      audio_sample_rate=48000, audio_duration=60.0)
    track = render_clip(clip, media, tmp_path / "w", "intro")
    assert (track.frames, track.samples) == (12, samples_at_frame(12, Fraction(30)))
    raw, w, h = _gray(track.video, 0.1)
    assert (w, h) == (640, 360) and _mean(raw, w, 0, 60, 40, 320) > 30 and _mean(raw, w, 580, 640, 40, 320) > 30
    assert_concat_compatible([track.video, track.video])


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_real_corrupt_intro_is_skipped_with_a_warning_and_never_fails_the_job(tmp_path):
    from outputs import RenderInputs, render_outputs
    from slice.profile import probe_header, probe_media

    src = tmp_path / "meeting.mp4"
    _make_meeting(src, "320x180", 6, channels=1)
    intro, outro = tmp_path / "clips" / OWNER_INTRO, tmp_path / "clips" / OWNER_OUTRO
    intro.parent.mkdir(parents=True)
    intro.write_bytes(b"\x00\x00\x00\x18ftypmp42 this is not a video")
    _make_outro(outro)
    job_dir = get_settings().jobs_dir / "corrupt-intro"
    job_dir.mkdir(parents=True)
    out = render_outputs(RenderInputs(
        job_dir=job_dir, source=src, media=probe_media(src), edl=_edl([(0, 180)], 180), removed=[],
        reel_edl=None, shorts=[], words=[], intro=intro, outro=outro), set_status=lambda s: None)
    assert any(w.startswith(f"intro skipped: {OWNER_INTRO}: ") for w in out.warnings), out.warnings
    assert out.intro_s == 0 and out.intro_name == "" and out.final_offset_s == 0
    assert out.outro_s == 1.6 and out.final_frames == 180 + 48 == probe_header(out.final_path).video_frames
