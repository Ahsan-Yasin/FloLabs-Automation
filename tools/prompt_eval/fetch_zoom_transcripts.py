"""Fetch Zoom TRANSCRIPT files (never MP4s) of real FloLabs meetings for the
prompt eval dataset.

    python tools/prompt_eval/fetch_zoom_transcripts.py --list [--days 60]
    python tools/prompt_eval/fetch_zoom_transcripts.py --fetch <uuid> [<uuid> ...]

--list prints the host's recent cloud recordings (topic, start, minutes, has a
transcript) so a person can pick varied meetings. --fetch downloads, for each
meeting instance UUID, only the audio transcript (.vtt) of every recorded
segment into tools/prompt_eval/raw/<slug>.vtt (+ <slug>.json with the
meeting's metadata). Uses the app's own Zoom client (ingest/zoom.py), so the
Bearer token only ever goes to Zoom's own hosts. No secrets are printed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from core.config import get_settings
from ingest import zoom

RAW = Path(__file__).resolve().parent / "raw"
HOST = ""  # falls back to ZOOM_HOST_EMAIL from .env


def slug(topic: str, start: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (topic or "meeting").lower()).strip("-")[:40]
    day = (start or "")[:10]
    return f"zoom-{base}-{day}"


def cmd_list(days: int) -> None:
    client = zoom.get_client()
    host = (get_settings().zoom_host_email or HOST).strip()
    today = datetime.now(UTC).date()
    rows = client.list_recordings(host, today - timedelta(days=days), today)
    for r in rows:
        print(f"{r['start_time']}  {str(r['duration_min']).rjust(4)} min  transcript={r['has_transcript']!s:5}  "
              f"parts={r['parts']}  {r['topic'][:45]!r}  uuid={r['uuid']}")
    print(f"{len(rows)} recordings")


def cmd_fetch(uuids: list[str]) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    client = zoom.get_client()
    for uuid in uuids:
        meeting = client.get_meeting(uuid)
        choice = zoom.choose_files(meeting)
        name = slug(meeting.get("topic", ""), meeting.get("start_time", ""))
        transcripts = [p.transcript for p in choice.parts if p.transcript is not None]
        if not transcripts:
            print(f"{name}: no transcript, skipped")
            continue
        texts = []
        for k, t in enumerate(transcripts):
            if (t.get("file_type") or "").upper() not in ("TRANSCRIPT", "CC"):
                raise SystemExit(f"refusing to download a non-transcript file ({t.get('file_type')})")
            dest = RAW / f"{name}.part{k}.vtt"
            client.download_file(t["download_url"], dest, size=t.get("file_size"), expect_mp4=False)
            texts.append(dest.read_text(encoding="utf-8-sig", errors="replace"))
        out = RAW / f"{name}.vtt"
        if len(texts) == 1:
            out.write_text(texts[0], encoding="utf-8")
        else:
            # only the first recorded segment is used: offsets between parts
            # would need the MP4 lengths, which we never download
            out.write_text(texts[0], encoding="utf-8")
        for k in range(len(texts)):
            (RAW / f"{name}.part{k}.vtt").unlink(missing_ok=True)
        meta = zoom.meeting_info(meeting, choice)
        meta.pop("host_email", None)
        meta["parts_with_transcript"] = len(texts)
        meta["used_part"] = 0
        (RAW / f"{name}.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        print(f"{name}: {len(texts)} transcript part(s) -> {out.name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--fetch", nargs="+")
    args = ap.parse_args()
    if args.list:
        cmd_list(args.days)
    if args.fetch:
        cmd_fetch(args.fetch)


if __name__ == "__main__":
    main()
