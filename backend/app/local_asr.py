"""Bounded offline Mandarin ASR. No audio, transcript or tool stderr in logs."""

import array
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from threading import Event
from typing import Any

from app.config import Settings
from app.service import BLOCKED_TEXT, sanitize

MAX_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 20


class ASRError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def available(settings: Settings) -> bool:
    return (
        settings.v15_enabled and settings.asr_enabled
        and settings.asr_model_path.is_file() and settings.asr_model_path.stat().st_size > 10_000_000
        and os.access(settings.asr_cli_path, os.X_OK) and os.access(settings.asr_ffmpeg_path, os.X_OK)
    )


def temp_root(settings: Settings) -> Path:
    suffix = hashlib.sha256(settings.database_url.encode()).hexdigest()[:16]
    root = Path(tempfile.gettempdir()) / f"bank-demo-asr-{suffix}"
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    return root


def cleanup_interrupted(settings: Settings) -> None:
    # Only this database's ASR workspace, before workers start; never other app files.
    for path in temp_root(settings).glob("turn-*"):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)


def validate_container(data: bytes, mime: str) -> str:
    mime = mime.split(";", 1)[0].strip().lower()
    if not data or len(data) > MAX_BYTES:
        raise ASRError("AUDIO_SIZE")
    valid = {
        "audio/webm": data.startswith(b"\x1a\x45\xdf\xa3"),
        "audio/ogg": data.startswith(b"OggS"),
        "audio/mp4": len(data) > 12 and data[4:8] == b"ftyp",
        "audio/wav": len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE",
    }
    if not valid.get(mime, False):
        raise ASRError("AUDIO_FORMAT")
    return mime


def run_tool(command: list[str], seconds: float, cancelled: Event, data: bytes | None = None) -> None:
    if cancelled.is_set():
        raise ASRError("CANCELLED")
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + seconds
    try:
        while True:
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise ASRError("CANCELLED" if cancelled.is_set() else "ASR_TIMEOUT")
            try:
                process.communicate(input=data, timeout=0.2)
                if process.returncode:
                    raise ASRError("ASR_FAILED")
                return
            except subprocess.TimeoutExpired:
                data = None  # communicate resumes the same pipe, without duplicating input.
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def check_wave(path: Path) -> None:
    with wave.open(str(path), "rb") as audio:
        frames, rate = audio.getnframes(), audio.getframerate()
        if rate != 16000 or audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise ASRError("AUDIO_FORMAT")
        if frames < 4000 or frames > MAX_SECONDS * rate:
            raise ASRError("AUDIO_DURATION")
        samples = array.array("h", audio.readframes(frames))
        rms = math.sqrt(sum(float(x) ** 2 for x in samples) / len(samples)) / 32768
        if rms < 0.003:
            raise ASRError("NO_SPEECH")


def parse_transcript(raw: Any) -> str:
    if not isinstance(raw, dict) or not isinstance(raw.get("transcription"), list):
        raise ASRError("ASR_OUTPUT")
    parts = raw["transcription"]
    if len(parts) > 100 or any(not isinstance(p, dict) or not isinstance(p.get("text"), str) for p in parts):
        raise ASRError("ASR_OUTPUT")
    text = "".join(p["text"] for p in parts).strip()
    if not text or len(text) > 1000 or any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise ASRError("NO_SPEECH" if not text else "ASR_OUTPUT")
    # Collapse contradictory adjacent commas/end marks only; never touch words/numbers.
    text = re.sub(r"[，,]+([。！？!?])", r"\1", text)
    text = re.sub(r"[，,]{2,}", lambda match: match.group()[-1], text)
    text = sanitize(text)
    if text == BLOCKED_TEXT:
        raise ASRError("SENSITIVE_TRANSCRIPT")
    return text


def normalize_wave(path: Path) -> None:
    """Bounded linear gain, after silence validation; preserve every sample/word."""
    with wave.open(str(path), "rb") as audio:
        samples = array.array("h", audio.readframes(audio.getnframes()))
    peak = max(abs(x) for x in samples)
    if peak == 0:
        return
    gain = min(4.0, 27852 / peak)
    if gain <= 1:
        return
    adjusted = array.array("h", (round(x * gain) for x in samples))
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(adjusted.tobytes())


def decoder_args(settings: Settings) -> list[str]:
    if not settings.asr_enhanced:
        return []
    # Fixed, versioned vocabulary only; never user text or a guessed answer.
    prompt = (Path(__file__).with_name("prompts") / "asr_zh.txt").read_text().strip()
    if not prompt or len(prompt) > 300:
        raise ASRError("ASR_UNAVAILABLE")
    return ["--prompt", prompt, "-tp", "0", "-nf", "-bs", "5", "-sns"]


def transcribe(data: bytes, mime: str, settings: Settings, cancelled: Event) -> str:
    if not available(settings):
        raise ASRError("ASR_UNAVAILABLE")
    mime = validate_container(data, mime)
    try:
        with tempfile.TemporaryDirectory(prefix="turn-", dir=temp_root(settings)) as directory:
            root = Path(directory)
            audio = root / "normalized.wav"
            run_tool([
                str(settings.asr_ffmpeg_path), "-nostdin", "-hide_banner", "-loglevel", "error",
                "-protocol_whitelist", "pipe", "-f", {
                    "audio/wav": "wav", "audio/webm": "matroska", "audio/ogg": "ogg", "audio/mp4": "mov",
                }[mime], "-i", "pipe:0", "-t", "21", "-vn",
                "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(audio),
            ], 8, cancelled, data)
            check_wave(audio)
            if settings.asr_enhanced:
                normalize_wave(audio)
            output = root / "transcript"
            run_tool([
                str(settings.asr_cli_path), "-m", str(settings.asr_model_path), "-f", str(audio),
                "-l", "zh", "-t", "4", "-oj", "-of", str(output),
                *decoder_args(settings),
            ], 45, cancelled)
            result = output.with_suffix(".json")
            if not result.is_file() or result.stat().st_size > 64_000:
                raise ASRError("ASR_OUTPUT")
            return parse_transcript(json.loads(result.read_text()))
    except ASRError:
        raise
    except Exception:
        raise ASRError("ASR_FAILED") from None
