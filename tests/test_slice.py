import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

import slice.pipeline as slice_pipeline
from core.models import EditDecisionList, EDLRange
from core.timeline import fade_frames_for, rate_str
from slice.ffmpeg_wrapper import (
    AudioPiece,
    build_audio_graph,
    build_video_part_graph,
    video_input_args,
)
from slice.plan import InputSpan, VideoPart, plan_video
from slice.profile import MediaInfo, StreamHeader, video_args

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _media(fps=Fraction(25), frames=500, w=320, h=180):
    return MediaInfo(
        path=Path("src.mp4"), duration=frames / fps, video_duration=float(frames / fps), fps=fps, width=w,
        height=h, pix_fmt="yuv420p", total_frames=frames, has_audio=True, audio_channels=2,
        audio_sample_rate=48000, audio_duration=float(frames / fps),
    )


def _edl(ranges, fps=Fraction(25), d=12, total=500):
    return EditDecisionList(
        ranges=[EDLRange(start=float(s / fps), end=float(e / fps), start_frame=s, end_frame=e) for s, e in ranges],
        source_duration=float(total / fps), fps=rate_str(fps), fade_frames=d, total_frames=total,
    )


# ------------------------------------------------------------ command builders

def test_video_input_seeks_a_quarter_frame_early_and_reads_past_the_end():
    args = video_input_args(Path("s.mp4"), InputSpan(30, 60), Fraction(30))
    # frame 30 starts at 1.0 s; seek to 29.75 frames = 0.991667 s
    assert args[:2] == ["-ss", "0.991667"]
    assert args[2:4] == ["-t", "1.100000"]  # 33 frames
    assert video_input_args(Path("s.mp4"), InputSpan(0, 10), Fraction(30))[0] == "-t"  # no seek at 0


def test_xfade_graph_uses_exact_frame_offsets():
    part = VideoPart("batch", [InputSpan(0, 106), InputSpan(94, 206), InputSpan(194, 300)], "xfade", 0, 12)
    part.frames = sum(s.frames for s in part.inputs) - 2 * 12
    graph = build_video_part_graph(part, Fraction(25), 320, 180)
    # the fps grid is anchored at the seek point, never at the first decoded frame
    assert "[1:v]fps=25/1:start_time=0:round=down," in graph
    # offsets in frames: 106-12=94 -> 3.76 s; then 94+112-12=194 -> 7.76 s
    assert "xfade=transition=fade:duration=0.480000:offset=3.760000[x1]" in graph
    assert "offset=7.760000[vout]" in graph
    assert "trim=end_frame=106" in graph and "trim=end_frame=112" in graph
    assert "tpad=stop_mode=clone" in graph


def test_hard_cut_graph_concats_and_single_input_maps_directly():
    part = VideoPart("batch", [InputSpan(0, 50), InputSpan(80, 120)], "concat", 90, 0)
    assert "concat=n=2:v=1:a=0[vout]" in build_video_part_graph(part, Fraction(25), 320, 180)
    single = VideoPart("batch", [InputSpan(0, 50)], "none", 50, 0)
    graph = build_video_part_graph(single, Fraction(25), 320, 180)
    assert graph.endswith("[vout]") and "xfade" not in graph and "concat" not in graph


def test_audio_graph_cuts_by_sample_count_with_micro_fades_only_inside_the_source():
    pieces = [AudioPiece(0, 48000, fade_in=False, fade_out=True), AudioPiece(96000, 24000, True, False)]
    graph = build_audio_graph(pieces, [0, 480])
    assert "atrim=start_sample=0:end_sample=48000" in graph
    assert "atrim=start_sample=480:end_sample=24480" in graph
    assert "[0:a]apad,atrim" in graph  # unbounded pad; atrim ends the stream
    first, second = graph.split(";\n")[:2]
    assert "afade=t=out:ss=47040:ns=960" in first and "t=in" not in first
    assert "afade=t=in:ss=0:ns=960" in second and "t=out" not in second
    # regular frames for the FLAC encoder, never padded
    assert graph.endswith("concat=n=2:v=0:a=1,asetnsamples=n=4096:p=0[aout]")
    assert build_audio_graph(pieces[:1], [0]).endswith(",asetnsamples=n=4096:p=0[aout]")


def test_standard_profile_is_pinned():
    args = video_args(Fraction(30000, 1001))
    assert args[args.index("-preset") + 1] == "veryfast"
    assert args[args.index("-crf") + 1] == "24"
    assert args[args.index("-r") + 1] == "30000/1001"
    assert "cfr" in args


# ------------------------------------------------------------ renderer (faked ffmpeg)

def _fake_ffmpeg(monkeypatch, written, output_audio_s):
    """Replace every ffmpeg/ffprobe call in slice.pipeline with a fake that
    writes a marker file and records its frame/sample count in `written`.
    The final header reports `output_audio_s` as the muxed audio duration."""

    def fake_part(src, part, fps, w, h, graph, dest, overlays=None):
        dest.write_bytes(b"v")
        written[dest.name] = part.frames

    def fake_concat(pieces, dest, list_path, durations_s=None, content_s=0.0):
        dest.write_bytes(b"v")
        written[dest.name] = sum(written[p.name] for p in pieces)

    def fake_chunk(flac, pieces, graph, dest):
        dest.write_bytes(b"a")
        written[dest.name] = sum(p.samples for p in pieces)

    def fake_header(path):
        n = written.get(path.name)
        return StreamHeader(n, None, "25/1", 320, 180, "yuv420p", "MD5:x", 7.0, n, 48000, 2, "flac", None)

    def fake_aac(chunks, dest, graph, content_s=0.0):
        dest.write_bytes(b"a")

    def fake_mux(video, audio, dest, content_s=0.0):
        dest.write_bytes(b"out")
        written[dest.name] = written[video.name]

    monkeypatch.setattr(slice_pipeline, "render_video_part", fake_part)
    monkeypatch.setattr(slice_pipeline, "concat_copy", fake_concat)
    monkeypatch.setattr(slice_pipeline, "render_audio_chunk", fake_chunk)
    monkeypatch.setattr(slice_pipeline, "probe_header", fake_header)
    monkeypatch.setattr(slice_pipeline, "encode_aac", fake_aac)
    monkeypatch.setattr(slice_pipeline, "mux", fake_mux)
    monkeypatch.setattr(slice_pipeline, "assert_frames",
                        lambda path, n, what: StreamHeader(n, None, "25/1", 320, 180, "yuv420p", "x",
                                                           output_audio_s, None, 48000, 2, "aac", None))
    monkeypatch.setattr(slice_pipeline, "assert_concat_compatible", lambda paths: [])
    monkeypatch.setenv("XFADE_BATCH_SIZE", "2")
    slice_pipeline.get_settings.cache_clear()


def test_render_cleaned_reports_progress_and_cleans_up(monkeypatch, tmp_path):
    written = {}
    _fake_ffmpeg(monkeypatch, written, output_audio_s=300 / 25)
    ranges = [(0, 100), (150, 250), (300, 400)]
    edl = _edl(ranges)
    progress = []
    work = tmp_path / "tmp"
    manifest = slice_pipeline.render_cleaned(
        tmp_path / "src.mp4", tmp_path / "a.flac", edl, _media(), tmp_path / "out.mp4", work,
        on_progress=lambda c, t: progress.append((c, t)),
    )
    total = progress[-1][1]
    assert progress == [(i, total) for i in range(1, total + 1)]
    assert not work.exists()
    assert (tmp_path / "out.mp4").read_bytes() == b"out"  # published from work_dir
    assert manifest.expected_frames == 300
    assert [p.out_start_frame for p in manifest.pieces] == [0, 100, 200]
    assert [s["out_time_s"] for s in manifest.seams] == [4.0, 8.0]
    assert [p["kind"] for p in manifest.parts] == ["batch", "seam", "batch"]


def test_failed_post_mux_assert_leaves_nothing_at_out_path(monkeypatch, tmp_path):
    """Regression: the mux wrote straight to out_path, so a render whose final
    audio assert failed still left a wrong cleaned.mp4 behind."""
    from core.errors import RenderAssertError

    _fake_ffmpeg(monkeypatch, {}, output_audio_s=None)  # muxed file has no audio
    out, work = tmp_path / "out.mp4", tmp_path / "tmp"
    with pytest.raises(RenderAssertError):
        slice_pipeline.render_cleaned(tmp_path / "src.mp4", tmp_path / "a.flac", _edl([(0, 100), (150, 250), (300, 400)]),
                                      _media(), out, work)
    assert not out.exists()
    assert not work.exists()


# ------------------------------------------------------------ real ffmpeg end to end

def _make_source(path: Path, rate: str, seconds: int) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate={rate}:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ac", "2", "-shortest", str(path),
        ],
        check=True,
    )


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
@pytest.mark.parametrize("rate", ["25/1", "30000/1001"])
def test_real_render_is_frame_exact_with_dissolves(tmp_path, monkeypatch, rate):
    from core.timeline import parse_rate
    from slice.ffmpeg_wrapper import extract_audio_flac
    from slice.profile import probe_header, probe_media

    monkeypatch.setenv("XFADE_BATCH_SIZE", "2")  # force a seam between batches
    slice_pipeline.get_settings.cache_clear()
    src = tmp_path / "src.mp4"
    _make_source(src, rate, 12)
    media = probe_media(src)
    fps = parse_rate(rate)
    d = fade_frames_for(fps)
    f = float(fps)
    ranges = [(int(0.5 * f), int(3 * f)), (int(4.2 * f), int(7 * f)), (int(8.5 * f), int(11 * f))]
    edl = _edl(ranges, fps=fps, d=d, total=media.total_frames)
    flac = tmp_path / "audio.flac"
    extract_audio_flac(src, flac, media.audio_channels, media.duration)
    out = tmp_path / "cleaned.mp4"
    manifest = slice_pipeline.render_cleaned(src, flac, edl, media, out, tmp_path / "work")

    expected = sum(e - s for s, e in ranges)
    header = probe_header(out)
    assert header.video_frames == expected == manifest.measured_frames
    assert abs(header.audio_duration - float(Fraction(expected) / fps)) < 0.05
    assert not (tmp_path / "work").exists()
    assert [p["kind"] for p in manifest.parts] == ["batch", "seam", "batch"]


def _make_indexed_source(path: Path, *, video_filter="", mux=(), video_delay_s=0.0, audio_s=None) -> None:
    """4 s @ 25 fps, 64x64, lossless; frame n is flat luma 16 + 2n so its
    index can be read back from the rendered output (_frame_ids)."""
    audio_s = 4 + video_delay_s if audio_s is None else audio_s
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", *(["-itsoffset", str(video_delay_s)] if video_delay_s else []),
            "-f", "lavfi", "-i", f"color=s=64x64:r=25:d=4,format=yuv420p,geq=lum='16+2*N':cb=128:cr=128{video_filter}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={audio_s}",
            "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-g", "25", "-c:a", "aac", *mux, str(path),
        ],
        check=True,
    )


def _frame_ids(path: Path) -> list[int]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
                         check=True, capture_output=True).stdout
    y, frame = 64 * 64, 64 * 64 * 3 // 2
    return [round((sum(raw[i:i + y]) / y - 16) / 2) for i in range(0, len(raw), frame)]


def _render_indexed(tmp_path: Path, src: Path, ranges, d=12):
    from slice.ffmpeg_wrapper import extract_audio_flac
    from slice.profile import probe_media

    media = probe_media(src)
    flac = tmp_path / "audio.flac"
    extract_audio_flac(src, flac, media.audio_channels, media.duration)
    out = tmp_path / "cleaned.mp4"
    edl = _edl(ranges, fps=media.fps, d=d, total=media.total_frames)
    return out, slice_pipeline.render_cleaned(src, flac, edl, media, out, tmp_path / "work")


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
@pytest.mark.parametrize("case", ["vfr_hold_across_cut", "video_starts_late"])
def test_real_render_stays_in_sync_when_the_first_decoded_frame_is_late(tmp_path, case):
    """Regression: each input was re-anchored on its first decoded frame
    (setpts=PTS-STARTPTS), so a VFR frame held across a cut, or a video stream
    starting after the audio, shifted whole kept ranges out of A/V sync (the
    late start with every frame-count assert still passing)."""
    src = tmp_path / "src.mp4"
    if case == "vfr_hold_across_cut":
        # Zoom-like VFR: 1/90000 timescale, static 1.0-2.99 s refreshed once a
        # second (frames 25 and 50 only); the second range starts inside it.
        _make_indexed_source(src, video_filter=",select='not(between(t\\,1\\,2.99))+eq(mod(n\\,25)\\,0)'",
                             mux=("-fps_mode", "vfr", "-video_track_timescale", "90000"))
        ranges = [(0, 30), (58, 90)]

        def on_screen(n):
            return 25 * (n // 25) if 25 <= n < 75 else n

        # before the first frame decoded after the seek (75) the held frame 50
        # is not available; that static stretch is padded, not asserted.
        skip = range(50, 75)
    else:
        _make_indexed_source(src, video_delay_s=0.4)  # video starts 10 frames after the audio
        ranges, skip = [(0, 40), (60, 100)], range(0)

        def on_screen(n):  # EDL frame n is n/25 s after the audio start
            return max(0, n - 10)

    out, manifest = _render_indexed(tmp_path, src, ranges)
    ids = _frame_ids(out)
    assert len(ids) == manifest.expected_frames == sum(e - s for s, e in ranges)
    cut, h = ranges[0][1] - ranges[0][0], 6
    checked = [(o, n) for o, n in enumerate(n for s, e in ranges for n in range(s, e))
               if not cut - h <= o < cut + h and n not in skip]  # outside the dissolve
    assert [ids[o] for o, _ in checked] == [on_screen(n) for _, n in checked]


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_real_render_pads_audio_that_ends_before_the_video(tmp_path):
    """Regression: the silence pad covered only 10 ms, so a source whose audio
    ends 50 ms before its video (ingest allows up to 1 s) failed the exact
    sample assert whenever a kept range reached the end."""
    from slice.profile import probe_header

    src = tmp_path / "src.mp4"
    _make_indexed_source(src, audio_s=3.95)
    out, _ = _render_indexed(tmp_path, src, [(0, 40), (60, 100)])  # 100 = last video frame
    header = probe_header(out)
    assert header.video_frames == 80
    assert abs(header.audio_duration - 80 / 25) <= slice_pipeline.AAC_TOLERANCE_S


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
@pytest.mark.parametrize("left", [12, 1])
def test_real_audio_cut_leaving_a_tiny_first_frame_still_encodes(tmp_path, left):
    """Regression (98-minute meeting, M4 gate): the FLAC encoder takes its
    block size from the first frame; a cut ending `left` samples before a
    decoded-frame boundary handed it a 12-sample frame -> "invalid block
    size: 12" and a failed job."""
    from slice.ffmpeg_wrapper import extract_audio_flac, render_audio_chunk
    from slice.profile import probe_header

    src = tmp_path / "sine.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=3", "-c:a", "aac", str(src)], check=True)
    flac = tmp_path / "audio.flac"
    extract_audio_flac(src, flac, 2, 3.0)  # 1024-sample blocks (the AAC frames)
    pieces = [AudioPiece(20 * 1024 - left, 20000, True, True), AudioPiece(100_000, 5000, True, True)]
    out = tmp_path / "chunk.flac"
    render_audio_chunk(flac, pieces, tmp_path / "g.txt", out)
    assert probe_header(out).audio_duration_ts == 25000


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_concat_copy_survives_an_apostrophe_in_the_path(tmp_path):
    """Regression: the concat list wrote file '<path>' unescaped, so an
    apostrophe in the storage path broke every multi-part render."""
    from slice.ffmpeg_wrapper import _concat_quote, concat_copy
    from slice.profile import probe_header

    assert _concat_quote("/x/O'Brien/p.mp4") == "'/x/O'\\''Brien/p.mp4'"
    work = tmp_path / "O'Brien jobs"
    work.mkdir()
    pieces = [work / "p0.mp4", work / "p1.mp4"]
    for p in pieces:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=64x64:r=25:d=1",
                        "-c:v", "libx264", "-preset", "ultrafast", str(p)], check=True)
    concat_copy(pieces, work / "out.mp4", work / "list.txt", content_s=2)
    assert probe_header(work / "out.mp4").video_frames == 50


def test_plan_matches_renderer_expectations():
    parts = plan_video([(0, 100), (150, 250), (300, 400)], 12, batch_size=2)
    assert sum(p.frames for p in parts) == 300


# ------------------------------------------------------------ M4 renderers (real ffmpeg)


def _tagged_source(path: Path, seconds: int = 12) -> None:
    """testsrc2 + sine, bt709-tagged like a Zoom recording, so the title card
    must copy the tags to concatenate."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-pix_fmt", "yuv420p",
         "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
         "-c:a", "aac", "-ac", "1", str(path)],
        check=True,
    )


@pytest.fixture
def tagged(tmp_path):
    from slice.ffmpeg_wrapper import extract_audio_flac
    from slice.profile import probe_media

    src = tmp_path / "src.mp4"
    _tagged_source(src)
    media = probe_media(src)
    flac = tmp_path / "audio.flac"
    extract_audio_flac(src, flac, media.audio_channels, media.duration)
    return src, flac, media


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_real_final_is_highlights_card_and_cleaned_joined_exactly(tmp_path, tagged):
    from slice.fonts import find_font
    from slice.profile import probe_header

    src, flac, media = tagged
    assert dict(media.color_tags)["color_space"] == "bt709" and media.audio_channels == 1
    cleaned = slice_pipeline.render_track(src, flac, _edl([(0, 100), (150, 250)], total=300), media,
                                          tmp_path / "w", "cleaned")
    reel = slice_pipeline.render_track(src, flac, _edl([(50, 90), (200, 240)], total=300), media, tmp_path / "w",
                                       "highlights")
    # "%" and "\" used to reach drawtext's expansion ("Stray %" -> no card)
    card = slice_pipeline.render_card("Q3 review: 100% done \\o/", media, 2.0, tmp_path / "w", find_font(),
                                      find_font(bold=True))
    assert card.frames == 50 and card.samples == 96000
    out = tmp_path / "final.mp4"
    header = slice_pipeline.assemble([reel, card, cleaned], out, tmp_path / "w", "final")
    # dissolves never change the length: 80 (reel) + 50 (card) + 200 (cleaned)
    assert header.video_frames == 330 == probe_header(out).video_frames
    assert abs(header.audio_duration - 330 / 25) <= slice_pipeline.AAC_TOLERANCE_S
    # the card (3.2-5.2 s) is black with a little text; the reel before it is the colourful testsrc2
    lum = [_mean_luma(out, t) for t in (1.0, 4.2)]
    assert lum[1] < 40 < lum[0]


def _mean_luma(path: Path, t: float) -> float:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], check=True, capture_output=True).stdout
    return sum(raw) / len(raw)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
@pytest.mark.parametrize("with_font", [True, False])
def test_real_removed_video_labels_only_the_visible_cuts(tmp_path, tagged, with_font):
    from core.models import RemovedRange
    from slice.fonts import find_font

    src, flac, media = tagged
    removed = [RemovedRange(start=0.0, end=2.0, start_frame=0, end_frame=50, tier="video", reason="small talk"),
               RemovedRange(start=4.0, end=4.4, start_frame=100, end_frame=110, tier="transcript_only"),
               RemovedRange(start=6.0, end=8.0, start_frame=150, end_frame=200, tier="video", reason="it's \"fine\"")]
    out = tmp_path / "removed.mp4"
    manifest = slice_pipeline.render_removed(src, flac, removed, media, out, tmp_path / "O'work",
                                             find_font() if with_font else None)
    assert manifest.measured_frames == 100 and [p.out_start_frame for p in manifest.pieces] == [0, 50]
    assert not (tmp_path / "O'work").exists()
    assert slice_pipeline.render_removed(src, flac, removed[1:2], media, out, tmp_path / "w", None) is None


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg/ffprobe on PATH")
def test_real_short_is_vertical_frame_exact_with_captions(tmp_path, tagged):
    from core.models import ShortClip, Word
    from slice.fonts import find_font

    src, flac, media = tagged
    clip = ShortClip(index=1, moment_id=0, start=2.0, end=6.0, score=9, title="The coffee incident")
    words = [Word(word="So the coffee machine exploded again this morning.", start=2.2, end=5.5, speaker="A")]
    result = slice_pipeline.render_short_clip(src, flac, clip, media, words, tmp_path / "shorts" / "short_01.mp4",
                                              tmp_path / "shorts" / "short_01.srt", tmp_path / "w", find_font(),
                                              width=216, height=384)
    h = result.header
    assert (h.width, h.height, h.video_frames) == (216, 384, 100) and result.captions >= 2
    assert abs(h.audio_duration - 4.0) <= slice_pipeline.AAC_TOLERANCE_S
    assert "coffee machine" in (tmp_path / "shorts" / "short_01.srt").read_text(encoding="utf-8")
    assert not (tmp_path / "w").exists()
