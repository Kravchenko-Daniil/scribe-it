"""Transcribe audio via ElevenLabs Scribe. Produces .txt, .srt, .json."""
from __future__ import annotations

import json
import pathlib
import time

import requests

API = "https://api.elevenlabs.io/v1/speech-to-text"

PARAGRAPH_PAUSE_SEC = 20.0  # split paragraph if same-speaker pause is this long
PARAGRAPH_MAX_CHARS = 1500  # very long paragraphs get broken at sentence ends
SRT_PAUSE_SEC = 1.2
SRT_MAX_CHARS = 140


def fmt_ts(seconds: float) -> str:
    ms = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _label(spk: str, names: dict[str, str] | None) -> str:
    if not spk:
        return ""
    return (names or {}).get(spk, spk)


def _clean(text: str) -> str:
    for bad, good in (
        (" ,", ","), (" .", "."), (" ?", "?"), (" !", "!"),
        (" ;", ";"), (" :", ":"), (" )", ")"), ("( ", "("),
        (" …", "…"),
    ):
        text = text.replace(bad, good)
    while "  " in text:
        text = text.replace("  ", " ")
    return text.strip()


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    cur = ""
    for ch in text:
        cur += ch
        if ch in ".!?" and len(cur) > 1:
            parts.append(cur)
            cur = ""
    if cur:
        parts.append(cur)
    out: list[str] = []
    buf = ""
    for p in parts:
        if buf and len(buf) + len(p) > limit:
            out.append(buf.strip())
            buf = p
        else:
            buf += p
    if buf.strip():
        out.append(buf.strip())
    return out


def to_srt(words: list[dict], speaker_names: dict[str, str] | None = None) -> str:
    segs: list[dict] = []
    cur: dict | None = None
    for w in words:
        if w.get("type") != "word":
            continue
        text = w.get("text", "")
        start = w.get("start", 0.0)
        end = w.get("end", start)
        spk = w.get("speaker_id", "")
        if (
            cur is None
            or spk != cur["spk"]
            or start - cur["end"] > SRT_PAUSE_SEC
            or len(cur["text"]) > SRT_MAX_CHARS
            or (text in ".!?" and len(cur["text"]) > 40)
        ):
            if cur is not None:
                segs.append(cur)
            cur = {"spk": spk, "start": start, "end": end, "text": text}
        else:
            sep = "" if text in ",.!?;:" else " "
            cur["text"] = cur["text"] + sep + text
            cur["end"] = end
    if cur is not None:
        segs.append(cur)

    lines: list[str] = []
    for i, s in enumerate(segs, 1):
        label = _label(s["spk"], speaker_names)
        prefix = f"[{label}] " if label else ""
        lines.append(str(i))
        lines.append(f"{fmt_ts(s['start'])} --> {fmt_ts(s['end'])}")
        lines.append(prefix + s["text"].strip())
        lines.append("")
    return "\n".join(lines)


def to_paragraphs(
    words: list[dict],
    *,
    speaker_names: dict[str, str] | None = None,
    pause_threshold: float = PARAGRAPH_PAUSE_SEC,
    max_chars: int = PARAGRAPH_MAX_CHARS,
) -> str:
    out: list[str] = []
    cur_spk: str | None = None
    cur_tokens: list[str] = []
    last_end = 0.0

    def flush():
        if not cur_tokens:
            return
        text = _clean(" ".join(cur_tokens))
        if not text:
            return
        label = _label(cur_spk or "", speaker_names)
        prefix = f"[{label}] " if label else ""
        for chunk in _split_long(text, max_chars):
            out.append(prefix + chunk)

    for w in words:
        if w.get("type") != "word":
            continue
        text = w.get("text", "")
        start = w.get("start", 0.0)
        end = w.get("end", start)
        spk = w.get("speaker_id", "")
        new_para = cur_spk is not None and (spk != cur_spk or start - last_end > pause_threshold)
        if new_para:
            flush()
            cur_tokens = []
        cur_tokens.append(text)
        cur_spk = spk
        last_end = end
    flush()
    return "\n\n".join(out) + "\n"


def transcribe(
    path: pathlib.Path,
    api_key: str,
    language: str = "rus",
    *,
    num_speakers: int | None = None,
    biased_keywords: list[str] | None = None,
) -> dict:
    data = {
        "model_id": "scribe_v1",
        "language_code": language,
        "diarize": "true",
        "timestamps_granularity": "word",
        "tag_audio_events": "false",
    }
    if num_speakers is not None:
        data["num_speakers"] = str(num_speakers)
    if biased_keywords:
        data["biased_keywords"] = json.dumps(biased_keywords)

    t0 = time.time()
    with path.open("rb") as f:
        r = requests.post(
            API,
            headers={"xi-api-key": api_key},
            data=data,
            files={"file": (path.name, f, "audio/ogg")},
            timeout=60 * 60 * 6,
        )
    dt = time.time() - t0
    if not r.ok:
        raise RuntimeError(f"Scribe HTTP {r.status_code} ({dt:.1f}s): {r.text[:500]}")
    return r.json()


def write_outputs(
    data: dict,
    out_dir: pathlib.Path,
    stem: str,
    *,
    speaker_names: dict[str, str] | None = None,
) -> dict[str, pathlib.Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{stem}.json"
    txt_path = out_dir / f"{stem}.txt"
    srt_path = out_dir / f"{stem}.srt"

    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    words = data.get("words", [])
    if words:
        txt_path.write_text(to_paragraphs(words, speaker_names=speaker_names))
        srt_path.write_text(to_srt(words, speaker_names=speaker_names))
    else:
        txt_path.write_text((data.get("text", "").strip() + "\n"))
    return {"json": json_path, "txt": txt_path, "srt": srt_path}
