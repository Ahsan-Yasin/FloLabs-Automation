"""ffmpeg command builders/runners for the v2 renderer (plan §6).

Every video piece is re-encoded exactly once with the STANDARD profile
(slice/profile.py); stream copy is retired because copy-mode clips start at the
previous keyframe, which put ~78 s of removed content back into the 98-minute
regression job. Cut points are whole source frames:

* video inputs seek a quarter frame early (`-ss`) and `fps` resamples onto a
  grid anchored at the SEEK POINT (start_time=0, round=down), so output frame j
  is whatever is on screen at source time (a + j)/F. Anchoring on the first
  decoded frame instead (setpts=PTS-STARTPTS) shifted whole ranges out of A/V
  sync when that frame is late: a VFR held frame across the cut, or a video
  stream that starts after the audio. The chain then pads the tail by cloning
  (so a short source can never make a piece a frame short) and trims to
  exactly N frames;
* audio is cut from a timestamp-normalised 48 kHz FLAC by sample count, with
  cumulative sample boundaries so the total is exact even at 29.97 fps.

Filter graphs are written to a file and passed with `-/filter_complex` (the
old `-filter_complex_script` flag no longer exists in ffmpeg 9, and long
graphs would hit the Windows command-line limit).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.proc import run_checked
from core.timeline import AUDIO_RATE, rate_str, ts

from .fonts import filter_path
from .plan import InputSpan, VideoPart
from .profile import AUDIO_ARGS, BASE_ARGS, ffmpeg_timeout, video_args

logger = get_logger(__name__)

# Samples read on each side of an audio cut so the accurate-seek rounding
# (microsecond -ss) never eats into the samples we keep.
AUDIO_SEEK_MARGIN = 480  # 10 ms @ 48 kHz
MICRO_FADE_SAMPLES = 960  # 20 ms


def ffmpeg_bin() -> str:
    return get_settings().ffmpeg_bin


# --------------------------------------------------------------------- video


def video_input_args(src: Path, span: InputSpan, fps: Fraction) -> list[str]:
    """Seek a quarter frame before `span.start` and read a few frames past the
    end (the graph trims exactly). After the seek, frame n sits at (n - a +
    1/4)/F, so the chain's round-down fps grid maps it to slot n - a with a
    quarter-frame margin either side: frame a-1 is dropped by the accurate
    seek, and a coarse track timescale (e.g. 1/fps, where half a frame is not
    representable) or the microsecond -ss rounding cannot move a frame into
    the neighbouring slot."""
    args: list[str] = []
    if span.start > 0:
        args += ["-ss", ts(Fraction(4 * span.start - 1, 4) / fps)]
    args += ["-t", ts(Fraction(span.frames + 3) / fps), "-i", str(src)]
    return args


def exact_frames_chain(k: int, frames: int, fps: Fraction, normalise: str = "") -> str:
    """The start of every video input chain: exactly `frames` frames on the
    source grid, timestamps from 0 (`normalise`, if given, ends with ",").

    No setpts before `fps`: the input timestamps are already relative to the
    seek point (or to the file start, which is also where the audio FLAC
    starts), and `start_time=0` keeps that anchor, padding with the first
    decoded frame if it arrives late. Re-anchoring on the first frame would
    pull everything after a late first frame early, out of sync with audio."""
    return (f"[{k}:v]fps={rate_str(fps)}:start_time=0:round=down,{normalise}"
            f"tpad=stop_mode=clone:stop=3,trim=end_frame={frames},setpts=PTS-STARTPTS")


def video_input_chain(k: int, span: InputSpan, fps: Fraction, width: int, height: int, label: str,
                      overlay: str = "") -> str:
    """Normalised input: `overlay` (e.g. a drawtext label) is applied after the trim."""
    chain = exact_frames_chain(k, span.frames, fps, f"scale={width}:{height},setsar=1,format=yuv420p,")
    return f"{chain}{',' + overlay if overlay else ''}[{label}]"


def build_video_part_graph(part: VideoPart, fps: Fraction, width: int, height: int,
                           overlays: dict[int, str] | None = None) -> str:
    """Filter graph for one batch/seam: per-input normalise+trim (+ an optional
    per-range overlay), then an xfade chain (dissolves) or a concat (hard
    cuts). Output label [vout]."""
    n = len(part.inputs)
    lines = []
    for k, span in enumerate(part.inputs):
        label = "vout" if n == 1 else f"v{k}"
        overlay = ""
        if overlays and k < len(part.input_ranges):
            overlay = overlays.get(part.input_ranges[k], "")
        lines.append(video_input_chain(k, span, fps, width, height, label, overlay))
    if n > 1 and part.join == "concat":
        lines.append("".join(f"[v{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=0[vout]")
    elif n > 1:
        d = Fraction(part.fade_frames) / fps
        prev = "v0"
        for k, off in enumerate(part.xfade_offsets(), start=1):
            out = "vout" if k == n - 1 else f"x{k}"
            lines.append(
                f"[{prev}][v{k}]xfade=transition=fade:duration={ts(d)}:offset={ts(Fraction(off) / fps)}[{out}]"
            )
            prev = out
    return ";\n".join(lines)


def build_video_part_cmd(
    src: Path, part: VideoPart, fps: Fraction, width: int, height: int, graph_path: Path, dest: Path,
    overlays: dict[int, str] | None = None,
) -> list[str]:
    graph_path.write_text(build_video_part_graph(part, fps, width, height, overlays), encoding="utf-8")
    cmd = [ffmpeg_bin(), *BASE_ARGS]
    for span in part.inputs:
        cmd += video_input_args(src, span, fps)
    cmd += ["-/filter_complex", str(graph_path), "-map", "[vout]", "-an", *video_args(fps),
            "-frames:v", str(part.frames), str(dest)]
    return cmd


def render_video_part(
    src: Path, part: VideoPart, fps: Fraction, width: int, height: int, graph_path: Path, dest: Path,
    overlays: dict[int, str] | None = None,
) -> None:
    cmd = build_video_part_cmd(src, part, fps, width, height, graph_path, dest, overlays)
    logger.debug("render %s (%d inputs, %d frames): %s", part.kind, len(part.inputs), part.frames, dest.name)
    run_checked(cmd, timeout=ffmpeg_timeout(float(Fraction(part.frames) / fps)))


def _concat_quote(path: str) -> str:
    """Single-quote a path for a concat-demuxer list. Inside quotes the only
    special character is the quote itself, written as '\\'' (close, escaped
    quote, reopen); unescaped, an apostrophe anywhere in the storage path
    ("O'Brien jobs") broke every multi-part render."""
    return "'" + path.replace("'", "'\\''") + "'"


def concat_copy(pieces: list[Path], dest: Path, list_path: Path, durations_s: list[str] | None = None,
                content_s: float = 0.0) -> None:
    """concat demuxer, stream copy. Callers must assert the pieces share one
    encoder profile first (profile.assert_concat_compatible)."""
    lines = []
    for i, piece in enumerate(pieces):
        lines.append(f"file {_concat_quote(piece.resolve().as_posix())}")
        if durations_s is not None:
            lines.append(f"duration {durations_s[i]}")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy",
           "-movflags", "+faststart", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(content_s / 20.0))


# --------------------------------------------------------------------- audio


def extract_audio_flac(src: Path, dest: Path, channels: int, source_duration_s: float) -> None:
    """Normalise the source audio ONCE to a gap-free 48 kHz FLAC. Cutting audio
    straight from the mp4 drifted +319 ms over 111 cuts (source pts gaps);
    every deliverable's audio is cut from this file instead."""
    ch = 1 if channels == 1 else 2
    # regular frames: a late-starting track makes aresample emit a short
    # padding frame first, which the FLAC encoder would take as its block size
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-i", str(src), "-vn", "-af",
           f"aresample=async=1:first_pts=0,aresample={AUDIO_RATE},{AUDIO_REFRAME}",
           "-ar", str(AUDIO_RATE), "-ac", str(ch), "-c:a", "flac", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(source_duration_s / 10.0))


@dataclass(frozen=True)
class AudioPiece:
    src_start: int  # sample index in audio.flac
    samples: int  # exact length
    fade_in: bool
    fade_out: bool


def audio_input_args(flac: Path, piece: AudioPiece) -> tuple[list[str], int]:
    pre = min(AUDIO_SEEK_MARGIN, piece.src_start)
    seek = piece.src_start - pre
    args: list[str] = []
    if seek > 0:
        args += ["-ss", ts(Fraction(seek, AUDIO_RATE))]
    args += ["-t", ts(Fraction(pre + piece.samples + AUDIO_SEEK_MARGIN, AUDIO_RATE)), "-i", str(flac)]
    return args, pre


# Re-chunk the joined audio into regular frames before the FLAC encoder,
# which takes its block size from the first frame it gets: a cut that left
# only 12 samples in the first decoded frame made it fail with "invalid block
# size: 12" (found on the 98-minute regression meeting). p=0: never pad, the
# sample count must stay exact.
AUDIO_REFRAME = "asetnsamples=n=4096:p=0"


def build_audio_graph(pieces: list[AudioPiece], pres: list[int]) -> str:
    lines = []
    n = len(pieces)
    for k, (piece, pre) in enumerate(zip(pieces, pres)):
        # tail-pad with silence so a piece that runs into the end of the file
        # still yields exactly `samples` samples. Unbounded: the source audio
        # may end up to 1 s before the video (ingest allows it), and atrim
        # ends the stream at end_sample anyway.
        chain = (f"[{k}:a]apad,"
                 f"atrim=start_sample={pre}:end_sample={pre + piece.samples},asetpts=PTS-STARTPTS")
        fade = min(MICRO_FADE_SAMPLES, piece.samples // 4)
        if piece.fade_in and fade:
            chain += f",afade=t=in:ss=0:ns={fade}"
        if piece.fade_out and fade:
            chain += f",afade=t=out:ss={piece.samples - fade}:ns={fade}"
        if n == 1:
            lines.append(f"{chain},{AUDIO_REFRAME}[aout]")
        else:
            lines.append(f"{chain}[a{k}]")
    if n > 1:
        lines.append("".join(f"[a{k}]" for k in range(n)) + f"concat=n={n}:v=0:a=1,{AUDIO_REFRAME}[aout]")
    return ";\n".join(lines)


def build_audio_chunk_cmd(flac: Path, pieces: list[AudioPiece], graph_path: Path, dest: Path) -> list[str]:
    cmd = [ffmpeg_bin(), *BASE_ARGS]
    pres = []
    for piece in pieces:
        args, pre = audio_input_args(flac, piece)
        cmd += args
        pres.append(pre)
    graph_path.write_text(build_audio_graph(pieces, pres), encoding="utf-8")
    cmd += ["-/filter_complex", str(graph_path), "-map", "[aout]", "-c:a", "flac", "-ar", str(AUDIO_RATE), str(dest)]
    return cmd


def render_audio_chunk(flac: Path, pieces: list[AudioPiece], graph_path: Path, dest: Path) -> None:
    cmd = build_audio_chunk_cmd(flac, pieces, graph_path, dest)
    total_s = sum(p.samples for p in pieces) / AUDIO_RATE
    run_checked(cmd, timeout=ffmpeg_timeout(total_s / 10.0))


def encode_aac(chunks: list[Path], dest: Path, graph_path: Path, content_s: float = 0.0) -> None:
    """Join the exact FLAC chunks (lossless concat filter) and encode AAC once.
    ffmpeg's AAC encoder is single-threaded (~50x realtime here), so the
    timeout scales with the audio length."""
    cmd = [ffmpeg_bin(), *BASE_ARGS]
    for chunk in chunks:
        cmd += ["-i", str(chunk)]
    if len(chunks) == 1:
        cmd += ["-map", "0:a:0"]
    else:
        graph_path.write_text("".join(f"[{k}:a]" for k in range(len(chunks))) +
                              f"concat=n={len(chunks)}:v=0:a=1[aout]", encoding="utf-8")
        cmd += ["-/filter_complex", str(graph_path), "-map", "[aout]"]
    cmd += [*AUDIO_ARGS, "-vn", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(content_s / 10.0))


def mux(video: Path, audio: Path, dest: Path, content_s: float = 0.0) -> None:
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
           "-c", "copy", "-movflags", "+faststart", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(content_s / 20.0))


def render_silence_flac(dest: Path, samples: int, channels: int) -> None:
    """Exactly `samples` samples of silence (the title card's audio)."""
    layout = "mono" if channels == 1 else "stereo"
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-f", "lavfi", "-i", f"anullsrc=r={AUDIO_RATE}:cl={layout}",
           "-af", f"atrim=end_sample={samples}", "-c:a", "flac", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(samples / AUDIO_RATE))


# ------------------------------------------------------------ text overlays


def drawtext(font: Path, textfile: Path, *, size: int, x: str = "(w-tw)/2", y: str = "24", color: str = "white",
             box: bool = True) -> str:
    """A drawtext filter reading its text from a UTF-8 file (no escaping of
    arbitrary text) with an explicit font file (fontconfig lookups crash the
    Windows build). expansion=none: drawtext still expands %{...} and
    backslashes in a textfile, so "100% done" failed and "C:\\new" lost its
    backslashes."""
    parts = [f"fontfile={filter_path(font)}", f"textfile={filter_path(textfile)}", "expansion=none",
             f"fontsize={size}", f"fontcolor={color}", "text_align=C", f"x={x}", f"y={y}"]
    if box:
        parts += ["box=1", "boxcolor=black@0.6", f"boxborderw={max(4, size // 3)}"]
    return "drawtext=" + ":".join(parts)


COLOR_OPTS = {"color_primaries": "color_primaries", "color_transfer": "color_trc", "color_space": "colorspace",
              "color_range": "range"}


def color_params(tags: dict[str, str], *, reset_missing: bool = False) -> str:
    """setparams for the source's colour tags, so a generated frame (title
    card) encodes with the same SPS/VUI as pieces cut from the source — else
    `concat -c copy` would join two different streams (the extradata assert
    catches that).

    reset_missing: also set every tag the source does NOT have to "unknown",
    for frames that bring tags of their own (an intro/outro clip). Left
    alone, a bt709-tagged or full-range clip kept its tags when the meeting
    was untagged, so x264 wrote a different SPS and the clip was always
    dropped. The title card's colour source carries no tags, so it doesn't
    need this."""
    opts = []
    for key, opt in COLOR_OPTS.items():
        value = tags.get(key)
        if value and value != "unknown":
            opts.append(f"{opt}={value}")
        elif reset_missing:
            opts.append(f"{opt}=unknown")
    return ("setparams=" + ":".join(opts) + ",") if opts else ""


def build_card_graph(*, fps: Fraction, width: int, height: int, frames: int, font: Path, bold_font: Path,
                     title_file: Path | None, subtitle_file: Path, title_size: int, subtitle_size: int,
                     color_tags: dict[str, str]) -> str:
    """Black card, the meeting topic (optional) above a smaller grey
    "Full meeting", fading in and out."""
    duration = Fraction(frames) / fps
    fade = min(Fraction(3, 10), duration / 4)
    gap = max(8, height // 40)
    chain = (f"color=c=black:s={width}x{height}:r={rate_str(fps)},format=yuv420p,setsar=1,"
             f"{color_params(color_tags)}")
    if title_file is not None:
        chain += drawtext(bold_font, title_file, size=title_size, y=f"h/2-th-{gap}", box=False) + ","
        sub_y = f"h/2+{gap}"
    else:
        sub_y = "(h-th)/2"
    chain += drawtext(font, subtitle_file, size=subtitle_size, y=sub_y, color="0xBBBBBB", box=False) + ","
    chain += (f"fade=t=in:st=0:d={ts(fade)},fade=t=out:st={ts(duration - fade)}:d={ts(fade)},"
              f"trim=end_frame={frames}[vout]")
    return chain


def render_card_video(dest: Path, graph: str, graph_path: Path, fps: Fraction, frames: int) -> None:
    graph_path.write_text(graph, encoding="utf-8")
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-/filter_complex", str(graph_path), "-map", "[vout]", "-an",
           *video_args(fps), "-frames:v", str(frames), str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(float(Fraction(frames) / fps)))


# ------------------------------------------------------------ intro / outro


def build_clip_graph(*, fps: Fraction, width: int, height: int, frames: int, color_tags: dict[str, str]) -> str:
    """An intro/outro clip on the meeting's frame grid and frame size: the
    same exact-frames chain as a piece cut from the source (fps resample from
    t=0, clone-pad, trim to `frames`), then square pixels (an anamorphic clip
    keeps its shape), scaled to fit inside width x height with its aspect
    kept, centred on black, and tagged EXACTLY like the meeting (every tag
    the meeting lacks reset to unknown) so it encodes to the same SPS.

    Levels are converted to the meeting's range (full-range pixels labelled
    tv looked crushed, tv pixels labelled pc washed out; the scaler treats
    an untagged clip as tv). Matrix/primaries/transfer are only labelled,
    not converted: right for the usual SDR bt709 or untagged HD clip, while
    a bt601 or HDR clip shows slightly shifted colours."""
    out_range = "pc" if color_tags.get("color_range") == "pc" else "tv"
    normalise = (f"scale=w=trunc(iw*sar/2)*2:h=ih,setsar=1,"
                 f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2:"
                 f"out_range={out_range},"
                 f"pad={width}:{height}:-1:-1:color=black,setsar=1,format=yuv420p,"
                 f"{color_params(color_tags, reset_missing=True)}")
    return exact_frames_chain(0, frames, fps, normalise) + "[vout]"


def render_clip_video(src: Path, dest: Path, graph: str, graph_path: Path, fps: Fraction, frames: int) -> None:
    graph_path.write_text(graph, encoding="utf-8")
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-i", str(src), "-/filter_complex", str(graph_path), "-map", "[vout]", "-an",
           *video_args(fps), "-frames:v", str(frames), str(dest)]
    # decoding a 1080p60 clip costs more than its length suggests
    run_checked(cmd, timeout=ffmpeg_timeout(4.0 * float(Fraction(frames) / fps)))


def render_clip_audio(src: Path, dest: Path, samples: int, channels: int) -> None:
    """The clip's audio as exactly `samples` samples of 48 kHz FLAC with the
    meeting's channel count: normalised like audio.flac (gap-free from t=0,
    where the clip's video starts too), then padded with silence or cut to
    the length of the clip's frames, so A/V stay in sync at every join."""
    layout = "mono" if channels == 1 else "stereo"
    chain = (f"aresample=async=1:first_pts=0,aresample={AUDIO_RATE},"
             f"aformat=sample_rates={AUDIO_RATE}:channel_layouts={layout},"
             f"apad,atrim=end_sample={samples},{AUDIO_REFRAME}")
    cmd = [ffmpeg_bin(), *BASE_ARGS, "-i", str(src), "-vn", "-af", chain, "-c:a", "flac", str(dest)]
    run_checked(cmd, timeout=ffmpeg_timeout(samples / AUDIO_RATE))


# --------------------------------------------------------------------- shorts

SHORT_FADE_SAMPLES = 4800  # 100 ms in/out: a short starts and ends mid-conversation


def build_short_graph(*, frames: int, fps: Fraction, width: int, height: int, pre: int, samples: int,
                      subtitles: str = "") -> str:
    """Vertical short: the frame scaled to fit the width, over a blurred,
    zoomed copy of itself (blurred at quarter size, then upscaled — same look,
    a fraction of the cost), optional burned captions, audio from the FLAC."""
    bw, bh = max(2, width // 4 // 2 * 2), max(2, height // 4 // 2 * 2)
    fade = min(SHORT_FADE_SAMPLES, samples // 4)
    lines = [
        exact_frames_chain(0, frames, fps) + ",split=2[b0][f0]",
        (f"[b0]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
         f"boxblur=luma_radius=8:luma_power=2:chroma_radius=4:chroma_power=2,scale={width}:{height},setsar=1[bg]"),
        f"[f0]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[fg]",
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2{',' + subtitles if subtitles else ''},format=yuv420p[vout]",
        f"[1:a]apad,atrim=start_sample={pre}:end_sample={pre + samples},asetpts=PTS-STARTPTS"
        + (f",afade=t=in:ss=0:ns={fade},afade=t=out:ss={samples - fade}:ns={fade}" if fade else "") + "[aout]",
    ]
    return ";\n".join(lines)


def subtitles_filter(ass_path: Path, fonts_dir: Path | None) -> str:
    opts = [f"filename={filter_path(ass_path)}"]
    if fonts_dir is not None:
        opts.append(f"fontsdir={filter_path(fonts_dir)}")
    return "subtitles=" + ":".join(opts)


def render_short(src: Path, flac: Path, span: InputSpan, piece: AudioPiece, graph: str, graph_path: Path,
                 dest: Path, fps: Fraction) -> None:
    graph_path.write_text(graph, encoding="utf-8")
    audio_args, _ = audio_input_args(flac, piece)
    cmd = [ffmpeg_bin(), *BASE_ARGS, *video_input_args(src, span, fps), *audio_args,
           "-/filter_complex", str(graph_path), "-map", "[vout]", "-map", "[aout]",
           *video_args(fps), *AUDIO_ARGS, "-frames:v", str(span.frames), "-movflags", "+faststart", str(dest)]
    # a 1080x1920 blur+overlay encode is ~3-4x the work of a 720p piece
    run_checked(cmd, timeout=ffmpeg_timeout(4.0 * float(Fraction(span.frames) / fps)))
