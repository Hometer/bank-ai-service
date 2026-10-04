"""Free local macOS TTS fallback. Synthesize only previously validated answers."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def synthesize(text: str) -> tuple[Path, Path]:
    if (
        sys.platform != "darwin"
        or not Path("/usr/bin/say").is_file()
        or not Path("/usr/bin/afconvert").is_file()
    ):
        raise ValueError("Local speech unavailable")
    if not text or len(text) > 2500:
        raise ValueError("Speech text exceeds limit")
    directory = Path(tempfile.mkdtemp(prefix="bank-v15-speech-"))
    try:
        # No shell, no text in command-line arguments/process listings, no stdout logging.
        subprocess.run(
            ["/usr/bin/say", "-v", "Ting-Ting", "-o", str(directory / "speech.aiff")],
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=True,
        )
        wave = directory / "speech.wav"
        subprocess.run(
            ["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16", str(directory / "speech.aiff"), str(wave)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
        )
        if not wave.is_file() or not 44 < wave.stat().st_size < 10_000_000:
            raise ValueError("Invalid synthesized audio")
        with wave.open("rb") as stream:
            header = stream.read(12)
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise ValueError("Invalid wave format")
        return directory, wave
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise ValueError("Local speech unavailable") from None
