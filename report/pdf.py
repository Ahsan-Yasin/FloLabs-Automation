"""report.pdf (plan D11): what the edit did, in a form the owner can read.

Built with reportlab's platypus from a plain `ReportData`, so it has no
knowledge of the pipeline. Text is drawn with the same TTF the videos use
(Arial / DejaVu Sans) so any character in a transcript renders; the built-in
Helvetica is only the fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.charts.barcharts import HorizontalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from core.timeline import fmt_clock

from .text import RemovedEntry

ACCENT = colors.HexColor("#1e3a8a")
MUTED = colors.HexColor("#6b7280")
RULE = colors.HexColor("#d1d5db")
ZEBRA = colors.HexColor("#f3f4f6")
EXCERPT_CHARS = 260


@dataclass
class ReportData:
    meeting: str
    job_id: str
    version: str
    created: str
    source_name: str
    source_duration_s: float
    final_duration_s: float
    cleaned_duration_s: float
    highlights_duration_s: float
    card_s: float
    removed: list[RemovedEntry]
    merged_back_count: int = 0
    merged_back_s: float = 0.0
    # (final.mp4 time, source start, source end, title)
    highlights: list[tuple[float, float, float, str]] = field(default_factory=list)
    # (file, source start, source end, title, hook)
    shorts: list[tuple[str, float, float, str, str]] = field(default_factory=list)
    chapters: str = ""
    # (name, status, duration_s, bytes, reason)
    artifacts: list[tuple[str, str, float | None, int | None, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    llm_usage: dict[str, float] = field(default_factory=dict)
    stage_timings: dict[str, float] = field(default_factory=dict)


def _fonts(regular: Path | None, bold: Path | None) -> tuple[str, str]:
    if regular is None:
        return "Helvetica", "Helvetica-Bold"
    try:
        if "HCBody" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("HCBody", str(regular)))
            pdfmetrics.registerFont(TTFont("HCBody-Bold", str(bold or regular)))
        return "HCBody", "HCBody-Bold"
    except Exception:  # noqa: BLE001 — an unreadable TTF falls back to the built-in font
        return "Helvetica", "Helvetica-Bold"


def _esc(text: str) -> str:
    return escape(str(text or ""))


def _mb(n: int | None) -> str:
    if n is None:
        return ""
    return f"{n / 1024**2:.1f} MB" if n >= 1024**2 else f"{max(1, round(n / 1024))} KB"


def _dur(s: float | None) -> str:
    return "" if s is None else fmt_clock(s)


def build_report(path: Path, data: ReportData, *, font: Path | None = None, bold_font: Path | None = None) -> None:
    body_font, bold = _fonts(font, bold_font)
    base = getSampleStyleSheet()
    s_body = ParagraphStyle("b", parent=base["BodyText"], fontName=body_font, fontSize=9.5, leading=13)
    s_small = ParagraphStyle("s", parent=s_body, fontSize=8, leading=10.5)
    s_cell = ParagraphStyle("c", parent=s_body, fontSize=8, leading=10)
    s_h1 = ParagraphStyle("h1", parent=base["Title"], fontName=bold, fontSize=20, leading=24, textColor=ACCENT,
                          alignment=0, spaceAfter=2)
    s_sub = ParagraphStyle("sub", parent=s_body, fontSize=11, leading=14, textColor=MUTED, spaceAfter=10)
    s_h2 = ParagraphStyle("h2", parent=base["Heading2"], fontName=bold, fontSize=13, leading=16, textColor=ACCENT,
                          spaceBefore=12, spaceAfter=6, keepWithNext=1)

    def P(text: str, style=s_cell) -> Paragraph:
        return Paragraph(_esc(text), style)

    def table(rows: list[list], widths: list[float], header: bool = True) -> Table:
        t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
        style = [
            ("FONTNAME", (0, 0), (-1, -1), body_font),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        if header:
            style += [("FONTNAME", (0, 0), (-1, 0), bold), ("TEXTCOLOR", (0, 0), (-1, 0), ACCENT),
                      ("LINEBELOW", (0, 0), (-1, 0), 0.8, ACCENT)]
            style += [("BACKGROUND", (0, r), (-1, r), ZEBRA) for r in range(2, len(rows), 2)]
        t.setStyle(TableStyle(style))
        return t

    width = A4[0] - 36 * mm
    story: list = [
        Paragraph("Meeting edit report", s_h1),
        Paragraph(_esc(data.meeting or data.source_name or data.job_id), s_sub),
    ]

    removed_s = sum(e.duration for e in data.removed)
    pct = 100 * removed_s / data.source_duration_s if data.source_duration_s else 0.0
    final_parts = []
    if data.highlights_duration_s:
        final_parts.append(f"highlights {fmt_clock(data.highlights_duration_s)}")
    if data.card_s:
        final_parts.append(f"title card {data.card_s:.0f}s")
    final_parts.append(f"cleaned meeting {fmt_clock(data.cleaned_duration_s)}")
    facts = [
        ["Original recording", f"{fmt_clock(data.source_duration_s)}  ({data.source_name})"],
        ["Final video", f"{fmt_clock(data.final_duration_s)}  = " + " + ".join(final_parts)],
        ["Removed", f"{fmt_clock(removed_s)} in {len(data.removed)} cuts ({pct:.0f}% of the recording)"],
        ["Job", f"{data.job_id}  ·  {data.created}  ·  v{data.version}"],
    ]
    story.append(table([[P(k, s_small), P(v, s_small)] for k, v in facts], [38 * mm, width - 38 * mm],
                       header=False))

    # ---- bundle contents
    if data.artifacts:
        story.append(Paragraph("What's in the bundle", s_h2))
        rows = [["File", "Status", "Length", "Size"]]
        for name, status, duration, size, reason in data.artifacts:
            label = status if status == "ok" else f"{status}: {reason}"
            rows.append([P(name), P(label), P(_dur(duration)), P(_mb(size))])
        story.append(table(rows, [62 * mm, width - 112 * mm, 22 * mm, 28 * mm]))

    # ---- removed summary
    story.append(Paragraph("What was removed", s_h2))
    shown = [e for e in data.removed if e.removed_video_at is not None]
    where = (f"{len(shown)} of them (1 s or longer) are collected in removed.mp4, labelled with their original "
             f"time and reason; the other {len(data.removed) - len(shown)} are listed in the table below only."
             if shown else "There is no removed.mp4 for this meeting; every cut is listed in the table below.")
    silences = [e for e in data.removed if e.silence]
    if silences:
        where += (f" {len(silences)} of the cuts ({sum(e.duration for e in silences):.0f}s in total) are pauses "
                  "where no one was speaking, shortened to a short beat; removed.mp4 leaves them out, as there is "
                  "nothing to see or hear.")
    story.append(Paragraph(
        _esc(f"{len(data.removed)} cuts totalling {fmt_clock(removed_s)}. {where} "
             f"{data.merged_back_count} tiny gaps ({data.merged_back_s:.1f}s in total) were too short to cut "
             "cleanly and were left in the video."), s_body))
    by_reason: dict[str, float] = {}
    for e in data.removed:
        by_reason[e.reason or "other"] = by_reason.get(e.reason or "other", 0.0) + e.duration
    if by_reason:
        ordered = sorted(by_reason.items(), key=lambda kv: -kv[1])
        story.append(Spacer(1, 6))
        story.append(_bar_chart(ordered, width, body_font))
        rows = [["Reason", "Minutes", "Cuts"]]
        for reason, secs in ordered:
            rows.append([P(reason), P(f"{secs / 60:.1f}"), P(str(sum(1 for e in data.removed
                                                                    if (e.reason or 'other') == reason)))])
        story.append(table(rows, [width - 50 * mm, 25 * mm, 25 * mm]))

    # ---- highlights
    story.append(Paragraph("Highlights reel", s_h2))
    if data.highlights:
        rows = [["#", "In final.mp4", "Original time", "Length", "Moment"]]
        for i, (at, s, e, title) in enumerate(data.highlights, 1):
            rows.append([P(str(i)), P(fmt_clock(at)), P(f"{fmt_clock(s)}–{fmt_clock(e)}"), P(f"{e - s:.0f}s"),
                         P(title)])
        story.append(table(rows, [8 * mm, 22 * mm, 30 * mm, 16 * mm, width - 76 * mm]))
    else:
        story.append(Paragraph("No highlights reel: the meeting did not have enough highlight-worthy material.",
                               s_body))

    # ---- shorts
    story.append(Paragraph("Shorts", s_h2))
    if data.shorts:
        rows = [["File", "Original time", "Length", "Title / hook"]]
        for name, s, e, title, hook in data.shorts:
            text = "<br/>".join(_esc(x) for x in (title, hook) if x)
            rows.append([P(name), P(f"{fmt_clock(s)}–{fmt_clock(e)}"), P(f"{e - s:.0f}s"), Paragraph(text, s_cell)])
        story.append(table(rows, [36 * mm, 30 * mm, 16 * mm, width - 82 * mm]))
    else:
        story.append(Paragraph("No shorts: nothing in this meeting stood out as a stand-alone moment to post.",
                               s_body))

    # ---- chapters
    if data.chapters.strip():
        story.append(Paragraph("YouTube chapters", s_h2))
        story.append(Paragraph("Paste these lines into the video description (they are also in chapters.txt).",
                               s_small))
        story.append(Spacer(1, 4))
        story.append(Paragraph("<br/>".join(_esc(line) for line in data.chapters.strip().splitlines()), s_body))

    # ---- removed detail
    story.append(Paragraph("Removed parts in detail", s_h2))
    story.append(Paragraph("Times are in the original recording. \"removed.mp4\" is where the cut can be watched.",
                           s_small))
    story.append(Spacer(1, 4))
    rows = [["#", "Original time", "Length", "Reason", "What was said", "removed.mp4"]]
    for e in data.removed:
        excerpt = e.text
        if len(excerpt) > EXCERPT_CHARS:
            excerpt = excerpt[: EXCERPT_CHARS - 1].rstrip() + "…"
        rows.append([P(str(e.index)), P(f"{fmt_clock(e.start)}–{fmt_clock(e.end)}"), P(f"{e.duration:.1f}s"),
                     P(e.reason or "—"), P(excerpt or ("(no one speaking)" if e.silence else "(no speech)")),
                     P(fmt_clock(e.removed_video_at) if e.removed_video_at is not None else "—")])
    story.append(table(rows, [9 * mm, 27 * mm, 15 * mm, 30 * mm, width - 102 * mm, 21 * mm]))

    # ---- notes
    notes = list(data.warnings)
    if data.llm_usage:
        u = data.llm_usage
        notes.append(f"AI usage: {int(u.get('calls', 0))} calls, {int(u.get('prompt_tokens', 0)):,} input and "
                     f"{int(u.get('output_tokens', 0)):,} output tokens.")
    if data.stage_timings:
        notes.append("Processing time: " + ", ".join(f"{k} {v / 60:.1f} min" if v >= 60 else f"{k} {v:.0f}s"
                                                    for k, v in data.stage_timings.items()))
    if notes:
        story.append(KeepTogether([Paragraph("Notes", s_h2)] + [Paragraph("• " + _esc(n), s_small) for n in notes]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(body_font, 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 10 * mm, f"{data.meeting or data.job_id} — edit report")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"page {doc.page}")
        canvas.restoreState()

    tmp = path.with_suffix(".pdf.tmp")
    doc = SimpleDocTemplate(str(tmp), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title=f"Edit report — {data.meeting or data.job_id}",
                            author="Highlight Cutter")
    try:
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _bar_chart(items: list[tuple[str, float]], width: float, font: str) -> Drawing:
    items = items[:8]
    bar_h = 14
    height = bar_h * len(items) + 24
    d = Drawing(width, height)
    chart = HorizontalBarChart()
    chart.x, chart.y = 120, 12
    chart.width, chart.height = width - 160, height - 20
    # first item at the top
    chart.data = [[round(v / 60, 2) for _, v in reversed(items)]]
    chart.categoryAxis.categoryNames = [k[:28] for k, _ in reversed(items)]
    chart.categoryAxis.labels.fontName = font
    chart.categoryAxis.labels.fontSize = 7.5
    chart.categoryAxis.labels.boxAnchor = "e"
    chart.valueAxis.labels.fontName = font
    chart.valueAxis.labels.fontSize = 7
    chart.valueAxis.valueMin = 0
    chart.bars[0].fillColor = ACCENT
    chart.bars[0].strokeColor = None
    chart.barLabelFormat = "%.1f min"
    chart.barLabels.fontName = font
    chart.barLabels.fontSize = 7
    chart.barLabels.boxAnchor = "w"
    chart.barLabels.dx = 3
    d.add(chart)
    return d
