"""Transcribe audio via ElevenLabs Scribe. Produces .txt, .srt, .json."""
from __future__ import annotations

import json
import pathlib
import time

import requests

API = "https://api.elevenlabs.io/v1/speech-to-text"


def fmt_ts(seconds: float) -> str:
    ms = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(words: list[dict]) -> str:
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
            or start - cur["end"] > 1.2
            or len(cur["text"]) > 140
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
        prefix = f"[{s['spk']}] " if s["spk"] else ""
        lines.append(str(i))
        lines.append(f"{fmt_ts(s['start'])} --> {fmt_ts(s['end'])}")
        lines.append(prefix + s["text"].strip())
        lines.append("")
    return "\n".join(lines)


def to_paragraphs(words: list[dict]) -> str:
    out: list[str] = []
    cur_spk: str | None = None
    cur: list[str] = []
    last_end = 0.0
    for w in words:
        if w.get("type") != "word":
            continue
        text = w.get("text", "")
        start = w.get("start", 0.0)
        end = w.get("end", start)
        spk = w.get("speaker_id", "")
        new_para = cur_spk is not None and (spk != cur_spk or start - last_end > 2.0)
        if new_para and cur:
            out.append(_join(cur_spk, cur))
            cur = []
        cur.append(text)
        cur_spk = spk
        last_end = end
    if cur:
        out.append(_join(cur_spk, cur))
    return "\n\n".join(out) + "\n"


def _join(spk: str | None, words: list[str]) -> str:
    prefix = f"[{spk}] " if spk else ""
    text = " ".join(words)
    for bad, good in ((" ,", ","), (" .", "."), (" ?", "?"), (" !", "!"), (" ;", ";"), (" :", ":")):
        text = text.replace(bad, good)
    return prefix + text


def transcribe(path: pathlib.Path, api_key: str, language: str = "rus") -> dict:
    t0 = time.time()
    with path.open("rb") as f:
        r = requests.post(
            API,
            headers={"xi-api-key": api_key},
            data={
                "model_id": "scribe_v1",
                "language_code": language,
                "diarize": "true",
                "timestamps_granularity": "word",
                "tag_audio_events": "false",
            },
            files={"file": (path.name, f, "audio/ogg")},
            timeout=60 * 60 * 6,
        )
    dt = time.time() - t0
    if not r.ok:
        raise RuntimeError(f"Scribe HTTP {r.status_code} ({dt:.1f}s): {r.text[:500]}")
    return r.json()


def write_outputs(data: dict, out_dir: pathlib.Path, stem: str) -> dict[str, pathlib.Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{stem}.json"
    txt_path = out_dir / f"{stem}.txt"
    srt_path = out_dir / f"{stem}.srt"

    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    words = data.get("words", [])
    txt_path.write_text(to_paragraphs(words) if words else (data.get("text", "").strip() + "\n"))
    if words:
        srt_path.write_text(to_srt(words))
    return {"json": json_path, "txt": txt_path, "srt": srt_path}
