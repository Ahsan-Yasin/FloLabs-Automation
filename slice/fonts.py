"""Fonts for burned-in text, and escaping for values inside ffmpeg filter graphs.

drawtext and libass (subtitles) are always given an explicit font FILE: a
fontconfig lookup by name (`font=Arial`) crashes the Windows ffmpeg build, and
on a bare Linux image there may be no default font at all (install
fonts-dejavu-core there).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from core.config import get_settings
from core.logging import get_logger

logger = get_logger(__name__)

_REGULAR = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
)
_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)


def find_font(bold: bool = False) -> Path | None:
    """`settings.font_file` if set (also used for bold), else the first
    standard font that exists. None = no usable font (text is skipped)."""
    configured = get_settings().font_file.strip()
    if configured:
        path = Path(configured)
        if path.is_file():
            return path
        logger.warning("FONT_FILE %s does not exist; falling back to the system fonts", configured)
    for candidate in (_BOLD if bold else ()) + _REGULAR:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


@lru_cache
def font_family(path: Path) -> str:
    """The family name libass needs to pick the font out of `fontsdir`
    ("Arial", "DejaVu Sans"); the file stem if the name table can't be read."""
    try:
        from reportlab.pdfbase.ttfonts import TTFontFile

        name = TTFontFile(str(path)).familyName
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        if name:
            return str(name)
    except Exception as exc:  # noqa: BLE001 — any unreadable font falls back to the stem
        logger.debug("cannot read the family name of %s: %s", path, exc)
    return path.stem


def filter_escape(value: str) -> str:
    """Escape a value (a path, a colour, a style string) for use as a filter
    option inside a filter graph: first the option level (`\\ ' :`), then
    the graph level (`\\ ' [ ] , ;`), as the ffmpeg filtergraph docs describe.
    Works for Windows paths ("C:/...") and paths with apostrophes."""
    value = re.sub(r"([\\':])", r"\\\1", value)
    return re.sub(r"([\\'\[\],;])", r"\\\1", value)


def filter_path(path: Path) -> str:
    return filter_escape(path.resolve().as_posix())
