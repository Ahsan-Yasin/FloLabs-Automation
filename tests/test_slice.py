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

def test_video_input_seeks_half_a_frame_early_and_reads_past_the_end():
    args = video_input_args(Path("s.mp4"), InputSpan(30, 60), Fraction(30))
    # frame 30 starts at 1.0 s; seek to 29.5 frames = 0.983333 s
    assert args[:2] == ["-ss", "0.983333"]
    assert args[2:4] == ["-t", "1.100000"]  # 33 frames
    assert video_input_args(Path("s.mp4"), InputSpan(0, 10), Fraction(30))[0] == "-t"  # no seek at 0


def test_xfade_graph_uses_exact_frame_offsets():
    part = VideoPart("batch", [InputSpan(0, 106), InputSpan(94, 206), InputSpan(194, 300)], "xfade", 0, 12)
    part.frames = sum(s.frames for s in part.inputs) - 2 * 12
    graph = build_video_part_graph(part, Fraction(25), 320, 180)
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
    first, second = graph.split(";\n")[:2]
    assert "afade=t=out:ss=47040:ns=960" in first and "t=in" not in first
    assert "afade=t=in:ss=0:ns=960" in second and "t=out" not in second
    assert graph.endswith("concat=n=2:v=0:a=1[aout]")


def test_standard_profile_is_pinned():
    args = video_args(Fraction(30000, 1001))
    assert args[args.index("-preset") + 1] == "veryfast"
    assert args[args.index("-crf") + 1] == "24"
    assert args[args.index("-r") + 1] == "30000/1001"
    assert "cfr" in args


# ------------------------------------------------------------ renderer (faked ffmpeg)

def test_render_cleaned_reports_progress_and_cleans_up(monkeypatch, tmp_path):
    written = {}

    def fake_part(src, part, fps, w, h, graph, dest):
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
                                                           written.get("_audio_s", 0) or None, None, 48000, 2,
                                                           "aac", None))
    monkeypatch.setattr(slice_pipeline, "assert_concat_compatible", lambda paths: [])
    monkeypatch.setenv("XFADE_BATCH_SIZE", "2")
    slice_pipeline.get_settings.cache_clear()

    ranges = [(0, 100), (150, 250), (300, 400)]
    edl = _edl(ranges)
    expected_s = 300 / 25
    written["_audio_s"] = expected_s
    progress = []
    work = tmp_path / "tmp"
    manifest = slice_pipeline.render_cleaned(
        tmp_path / "src.mp4", tmp_path / "a.flac", edl, _media(), tmp_path / "out.mp4", work,
        on_progress=lambda c, t: progress.append((c, t)),
    )
    total = progress[-1][1]
    assert progress == [(i, total) for i in range(1, total + 1)]
    assert not work.exists()
    assert manifest.expected_frames == 300
    assert [p.out_start_frame for p in manifest.pieces] == [0, 100, 200]
    assert [s["out_time_s"] for s in manifest.seams] == [4.0, 8.0]
    assert [p["kind"] for p in manifest.parts] == ["batch", "seam", "batch"]


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


def test_plan_matches_renderer_expectations():
    parts = plan_video([(0, 100), (150, 250), (300, 400)], 12, batch_size=2)
    assert sum(p.frames for p in parts) == 300
