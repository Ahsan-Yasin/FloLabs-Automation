from pathlib import Path

from core.config import get_settings
from core.logging import get_logger
from core.models import Word

logger = get_logger(__name__)


def transcribe(path: Path) -> list[Word]:
    """Run WhisperX transcription + forced alignment + pyannote diarization.

    Self-hosted and free (section 5) — the only external dependency is the HF
    token needed to pull the gated pyannote diarization models. Imports are
    lazy so the rest of the service can run/test without torch installed.
    """
    import whisperx

    settings = get_settings()
    device = settings.whisperx_device
    compute_type = settings.whisperx_compute_type

    audio = whisperx.load_audio(str(path))

    model = whisperx.load_model(settings.whisperx_model, device, compute_type=compute_type)
    result = model.transcribe(audio, batch_size=16)

    align_model, metadata = whisperx.load_align_model(language_code=result["language"], device=device)
    result = whisperx.align(result["segments"], align_model, metadata, audio, device, return_char_alignments=False)

    if not settings.hf_token:
        raise RuntimeError(
            "HF_TOKEN is not set — a Hugging Face token is required to load the "
            "gated pyannote diarization models used by WhisperX."
        )

    diarize_model = whisperx.diarize.DiarizationPipeline(use_auth_token=settings.hf_token, device=device)
    diarize_segments = diarize_model(audio)
    result = whisperx.assign_word_speakers(diarize_segments, result)

    words: list[Word] = []
    for segment in result.get("segments", []):
        for w in segment.get("words", []):
            if "start" not in w or "end" not in w:
                # Punctuation-only tokens sometimes fail forced alignment; skip them.
                continue
            words.append(
                Word(
                    word=w["word"],
                    start=float(w["start"]),
                    end=float(w["end"]),
                    speaker=w.get("speaker", "UNKNOWN"),
                )
            )

    words.sort(key=lambda w: w.start)
    return words
