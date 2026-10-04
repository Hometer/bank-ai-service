"""Frozen synthetic Mandarin audio comparison, never a real-microphone accuracy claim."""

import array
import json
import random
import shutil
import subprocess
import tempfile
import time
import unicodedata
import wave
from dataclasses import replace
from pathlib import Path
from threading import Event

from app.config import ROOT, Settings
from app.local_asr import ASRError, transcribe

CASES = [
    ("balance-clean", "帮我查询一下我名下的卡还有多少余额。", 180, "clean"),
    ("balance-fast", "帮我查询一下我名下的卡还有多少余额。", 260, "clean"),
    ("balance-quiet", "帮我查询一下我名下的卡还有多少余额。", 180, "quiet"),
    ("balance-noisy", "帮我查询一下我名下的卡还有多少余额。", 180, "noisy"),
    ("fps", "交银香港的转数快怎样办理退款？", 180, "clean"),
    ("online-fees", "网上银行服务免费吗？哪些交易会收费？", 180, "clean"),
    ("negation", "不要替我办理转账，我只想了解手续费。", 180, "clean"),
    ("repayment", "我已经还款，为什么还显示待还款？", 180, "clean"),
    ("non-bank", "今天的天气怎么样？", 180, "clean"),
    ("correction", "现在先不用查询余额，我想问存款保障。", 180, "clean"),
]


def normalized(text: str) -> str:
    return "".join(c.lower() for c in text if unicodedata.category(c)[0] in ("L", "N"))


def distance(left: str, right: str) -> int:
    row = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        next_row = [i]
        for j, b in enumerate(right, 1):
            next_row.append(min(row[j] + 1, next_row[-1] + 1, row[j - 1] + (a != b)))
        row = next_row
    return row[-1]


def sample(text: str, rate: int, profile: str, directory: Path) -> bytes:
    # Only the frozen, non-sensitive synthetic script; no real customer audio retained.
    aiff, wav = directory / "sample.aiff", directory / "sample.wav"
    subprocess.run(["/usr/bin/say", "-v", "Ting-Ting", "-r", str(rate), "-o", str(aiff)],
                   input=text.encode(), check=True, capture_output=True, timeout=20)
    subprocess.run(["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16", str(aiff), str(wav)],
                   check=True, capture_output=True, timeout=5)
    if profile != "clean":
        with wave.open(str(wav), "rb") as audio:
            params = audio.getparams()
            values = array.array("h", audio.readframes(audio.getnframes()))
        rng = random.Random(20261004)
        adjusted = array.array("h", (round(x * 0.10) if profile == "quiet" else
                                     max(-32768, min(32767, x + rng.randint(-1200, 1200))) for x in values))
        with wave.open(str(wav), "wb") as audio:
            audio.setparams(params)
            audio.writeframes(adjusted.tobytes())
    return wav.read_bytes()


def main() -> None:
    base = replace(Settings(), v15_enabled=True, asr_enabled=True,
                   database_url=f"sqlite:///{ROOT / 'data/asr-quality-benchmark.sqlite3'}")
    profiles = {
        "before-small": replace(base, asr_enhanced=False),
        "after-turbo-q5": replace(base, asr_model_path=ROOT / "data/models/ggml-large-v3-turbo-q5_0.bin"),
    }
    rows = []
    with tempfile.TemporaryDirectory(prefix="bank-synthetic-asr-") as temporary:
        folder = Path(temporary)
        for identifier, sentence, rate, audio_profile in CASES:
            data = sample(sentence, rate, audio_profile, folder)
            expected = normalized(sentence)
            row: dict[str, object] = {"id": identifier, "expected": sentence, "audio": audio_profile, "rate": rate}
            for name, settings in profiles.items():
                started = time.monotonic()
                try:
                    text = transcribe(data, "audio/wav", settings, Event())
                    errors, error_code = distance(expected, normalized(text)), None
                except ASRError as exc:
                    text, errors, error_code = "", len(expected), exc.code
                row[name] = {"text": text, "errors": errors, "chars": len(expected),
                             "seconds": round(time.monotonic() - started, 3), "failure": error_code}
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            aiff, wav = folder / "sample.aiff", folder / "sample.wav"
            aiff.unlink(missing_ok=True)
            wav.unlink(missing_ok=True)
    totals = {}
    for name in profiles:
        metrics = [row[name] for row in rows]
        assert all(isinstance(m, dict) for m in metrics)
        total_errors = sum(m["errors"] for m in metrics)  # type: ignore[index]
        total_chars = sum(m["chars"] for m in metrics)  # type: ignore[index]
        totals[name] = {"errors": total_errors, "chars": total_chars,
                        "cer": round(total_errors / total_chars, 4),
                        "seconds": round(sum(m["seconds"] for m in metrics), 3)}  # type: ignore[index]
    result = {"date": "2026-10-04", "kind": "synthetic TTS; not real user-microphone accuracy",
              "normalization": "remove punctuation/whitespace; retain letters/numbers; no word correction",
              "rows": rows, "totals": totals}
    output = ROOT / "data/asr-quality-comparison.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"totals": totals, "output": str(output)}, ensure_ascii=False))
    shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    main()
