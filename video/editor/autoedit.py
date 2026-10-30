"""Automatic editor for the Kavach launch video (landscape, about 60 s, for X).

    python video/editor/autoedit.py --footage video/footage            # real clips (shot1..shot5, any phone format)
    python video/editor/autoedit.py --footage video/footage-test        # stand-ins from make_standins.ps1
    options: --music video/music/bed_c.wav  --no-render  --out video/out/kavach-launch.mp4

What it does, in order:
 1. finds five face clips (mapped to the five script lines by what is said, not by file name when they disagree);
 2. transcribes each locally (faster-whisper small.en, word timings), picks the best take of its line, trims the silence;
 3. normalises picture (1920x1080, 30 fps; portrait clips sit on a blurred fill) and voice (high-pass, -16 LUFS);
 4. lays the takes out with the five motion-graphic inserts from video/out between them;
 5. writes word-timed captions (script spelling, transcript timing) and a music bed that dips under the voice;
 6. renders with HyperFrames, normalises the final mix to -15 LUFS, checks it, and writes a report.
Run it with the media venv python (faster-whisper, numpy, soundfile); needs ffmpeg and npx hyperframes on PATH.
"""
from __future__ import annotations

import argparse
import difflib
import html
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIDEO = ROOT / "video"
PROJECT = VIDEO / "launch-final"
INSERTS = [("mg1", VIDEO / "out" / "mg1_problem.mp4"), ("mg2", VIDEO / "out" / "mg2_three_failures.mp4"),
           ("mg3", VIDEO / "out" / "mg3_pipeline.mp4"), ("mg4", VIDEO / "out" / "mg4_proof.mp4"),
           ("mg5", VIDEO / "out" / "mg5_end_card.mp4")]
AS_SAID = [   # what was actually said in the 30 Oct takes (Whisper small.en and large-v3 cross-checked; Kavach spelled)
    "Every bank has a queue of fraud alerts, and every alert lands on one single person who has minutes to decide.",
    "Rules miss new patterns, and black-box AI can't be trusted to block someone's card.",
    "So we built Kavach. It gathers evidence from the bank's graph, decides by the bank's own policy, and writes up the "
    "case.",
    "Every claim can be re-run. Every block needs a human. If anything breaks, it stops instead of guessing.",
    "And it learns. It proposed a new detector, a bank manager approved it, and it flagged a new fraud ring nobody has "
    "labeled.",
]
SCRIPTS = [   # per shot: the current line first, then the earlier longer version (either may have been filmed)
    ["Banks drown in fraud alerts. And every alert lands on a person with minutes to decide.",
     "Every bank has a queue of fraud alerts. And every alert lands on a person who has minutes to decide."],
    ["Rules miss new patterns. And a black-box AI can't be trusted to block someone's card.",
     "Rules miss the new patterns. Plain AI can't be trusted to block someone's card. And when a black box decides, "
     "nobody can check its work."],
    ["So we built Kavach. It gathers evidence from the bank's graph, decides by the bank's own policy, and writes up "
     "the case.",
     "So we built Kavach: an AI investigator that works like a good analyst. It gathers evidence from the bank's graph, "
     "decides by the bank's own policy, and writes the case up."],
    ["Every claim can be re-run. Every block needs a human. And if anything breaks, it stops instead of guessing.",
     "Every claim it makes can be re-run against the data. Every block needs a human with the right role. And if "
     "anything fails, it stops instead of guessing."],
    ["And it learns. It proposed its own detector, a manager approved it, and it flagged a fraud ring nobody had "
     "labeled.",
     "And it learns. It mined five thousand closed cases, proposed a new detector, and waited for a manager to approve "
     "it. That detector caught a ring nobody had labeled."],
]
for _k, _line in enumerate(AS_SAID):            # the as-said text wins when it matches; captions then follow it exactly
    SCRIPTS[_k].insert(0, _line)
INSERT_MIN = {"mg1": 4.8, "mg2": 5.2, "mg3": 6.4, "mg4": 6.9, "mg5": 4.2}   # shortest safe tail trims (all text landed and read)
TARGET_TOTAL = 66.0
KEYWORDS = {"kavach", "graph", "graph's", "policy", "re-run", "human", "learns", "detector", "ring", "minutes",
            "fraud", "guessing", "tigergraph"}
SPELL = {r"\b(ka|ca|co|ko)(v|w|b)a(ch|tch|ss|sh)\b": "Kavach", r"\btiger ?graph\b": "TigerGraph",
         r"\bre ?run\b": "re-run", r"\brerun\b": "re-run"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp"}
FPS = 30
VOICE_LUFS = -16
MUSIC_LOW, MUSIC_HIGH = 0.2, 0.45         # bed gain under the voice and under the inserts (bed pre-normalised to -16 LUFS);
                                          # 0.45 keeps the music-only stretches about 3 dB under the talking parts


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def npx(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    exe = shutil.which("npx.cmd") or shutil.which("npx") or "npx"
    return run([exe, "hyperframes", *args], cwd=str(cwd))


def probe(path: Path) -> dict:
    p = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)])
    j = json.loads(p.stdout or "{}")
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        raise SystemExit(f"no video stream in {path.name}")
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    rot = int(v.get("tags", {}).get("rotate", rot) or rot)
    w, h = int(v["width"]), int(v["height"])
    if abs(rot) % 180 == 90:
        w, h = h, w
    return {"w": w, "h": h, "dur": float(j["format"]["duration"]), "audio": a is not None}


def norm_words(text: str) -> list[str]:
    return [re.sub(r"[^a-z0-9'\-]", "", t.lower()) for t in text.split() if re.sub(r"[^a-z0-9]", "", t.lower())]


# ---------- transcription ----------
_MODEL = None


def transcribe(clip: Path, work: Path) -> list[dict]:
    st = clip.stat()
    cache = work / f"{clip.stem}.{st.st_size}.{int(st.st_mtime)}.words.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    wav = work / f"{clip.stem}.16k.wav"
    p = run(["ffmpeg", "-v", "error", "-y", "-i", str(clip), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    if p.returncode != 0:
        raise SystemExit(f"could not read the audio of {clip.name}: {p.stderr[-300:]}")
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel("small.en", device="cpu", compute_type="int8")
    segs, _ = _MODEL.transcribe(str(wav), word_timestamps=True, language="en", beam_size=5, vad_filter=False,
                                initial_prompt="Kavach, TigerGraph, fraud alerts, re-run, detector.")
    words = [{"text": w.word.strip(), "start": round(float(w.start), 3), "end": round(float(w.end), 3)}
             for s in segs for w in (s.words or []) if w.word.strip()]
    cache.write_text(json.dumps(words), encoding="utf-8")
    return words


# ---------- take selection ----------
def best_take_any(words: list[dict], k: int) -> tuple[float, int, int, str]:
    """Best take of shot k against every version of its line: (similarity, i, j, the matching version)."""
    best = (0.0, 0, max(len(words) - 1, 0), SCRIPTS[k][0])
    for line in SCRIPTS[k]:
        r, i, j = best_take(words, line)
        if r > best[0] + 1e-9:
            best = (r, i, j, line)
    return best


def best_take(words: list[dict], line: str) -> tuple[float, int, int]:
    """Best contiguous window of transcript words for the script line: (similarity, i, j) inclusive."""
    target = norm_words(line)
    toks = [norm_words(w["text"])[0] if norm_words(w["text"]) else "" for w in words]
    n = len(words)
    starts = [i for i in range(n) if i == 0 or words[i]["start"] - words[i - 1]["end"] >= 0.45]
    ends = [j for j in range(n) if j == n - 1 or words[j + 1]["start"] - words[j]["end"] >= 0.45]
    best = (0.0, 0, max(n - 1, 0))
    for i in starts:
        for j in ends:
            if j < i or (j - i + 1) > 2 * len(target) + 6:
                continue
            r = difflib.SequenceMatcher(None, toks[i:j + 1], target, autojunk=False).ratio()
            if r > best[0] + 1e-9 or (abs(r - best[0]) < 0.02 and i > best[1]):   # ties go to the later take
                best = (r, i, j)
    return best


def assign_lines(transcripts: list[list[dict]]) -> dict[int, int]:
    """{clip index: shot line} -- by file order when that order matches, else by the best content match overall."""
    from itertools import permutations
    nc, nl = len(transcripts), len(SCRIPTS)
    score = [[best_take_any(t, k)[0] if t else 0.0 for k in range(nl)] for t in transcripts]
    if nc <= nl and all(score[c][c] >= 0.55 for c in range(nc)):
        return {c: c for c in range(nc)}
    best, best_s = {}, -1.0
    if nc <= nl:                                   # each clip gets a different line
        for perm in permutations(range(nl), nc):
            s = sum(score[c][perm[c]] for c in range(nc))
            if s > best_s:
                best, best_s = {c: perm[c] for c in range(nc)}, s
    else:                                          # more clips than lines: each line gets its best clip
        for perm in permutations(range(nc), nl):
            s = sum(score[perm[k]][k] for k in range(nl))
            if s > best_s:
                best, best_s = {perm[k]: k for k in range(nl)}, s
    return best


def caption_words(words: list[dict], i: int, j: int, line: str, offset: float) -> tuple[list[dict], str]:
    """Words to show: the script's spelling with the transcript's timing when the take follows the script."""
    took = words[i:j + 1]
    target_raw = line.split()
    target = [norm_words(t)[0] if norm_words(t) else "" for t in target_raw]
    toks = [norm_words(w["text"])[0] if norm_words(w["text"]) else "" for w in took]
    sm = difflib.SequenceMatcher(None, toks, target, autojunk=False)
    if sm.ratio() < (0.6 if line in AS_SAID else 0.8):     # an as-said text is trusted over small mishearings
        out = []
        for w in took:
            t = w["text"]
            for pat, rep in SPELL.items():
                t = re.sub(pat, rep, t, flags=re.I)
            out.append({"text": t, "start": w["start"] - offset, "end": w["end"] - offset})
        return out, "transcript"
    times: list[tuple[float, float] | None] = [None] * len(target_raw)
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op in ("equal", "replace") and a1 > a0:
            span_s, span_e = took[a0]["start"], took[a1 - 1]["end"]
            k = b1 - b0
            for m in range(k):
                times[b0 + m] = (span_s + (span_e - span_s) * m / k, span_s + (span_e - span_s) * (m + 1) / k) \
                    if op == "replace" else (took[a0 + m]["start"], took[a0 + m]["end"])
    for m in range(len(times)):                                    # words the speaker skipped: share the gap
        if times[m] is None:
            prev = next((times[q][1] for q in range(m - 1, -1, -1) if times[q]), took[0]["start"])
            nxt = next((times[q][0] for q in range(m + 1, len(times)) if times[q]), took[-1]["end"])
            times[m] = (prev, max(prev + 0.05, nxt))
    return [{"text": target_raw[m], "start": times[m][0] - offset, "end": times[m][1] - offset}
            for m in range(len(target_raw))], "script"


# ---------- media ----------
# Studio voice: rumble and hiss out, gentle tracked de-noise, less box (250 Hz), more presence (3.6 kHz) and air,
# de-essing after the presence lift, then even dynamics, then loudness to the voice target.
VOCAL_CHAIN = ("highpass=f=75,lowpass=f=16000,afftdn=nr=10:nf=-50:tn=1,"
               "equalizer=f=250:t=q:w=1.1:g=-2.5,equalizer=f=3600:t=q:w=1.3:g=2.2,highshelf=f=9500:g=1.8,"
               "deesser=i=0.35:m=0.5:f=0.55:s=o,"
               "acompressor=threshold=-20dB:ratio=3:attack=6:release=110:knee=4:makeup=3")
GRADE = "eq=contrast=1.04:saturation=1.07"
PUNCH = 1.12          # punch-in scale for the jump cut at a sentence break


def punch_time(words: list[dict], dur: float) -> float | None:
    """A cut point at a phrase break near the middle of the take (just before the next phrase starts)."""
    best = None
    for k, w in enumerate(words[:-1]):
        if not re.search(r"[.,;:!?]$", w["text"]):
            continue
        t = max(w["end"] + 0.02, words[k + 1]["start"] - 0.06)
        if 0.25 <= t / dur <= 0.8 and (best is None or abs(t / dur - 0.5) < abs(best / dur - 0.5)):
            best = t
    return best


def encode_face(src: Path, info: dict, t_in: float, t_out: float, dst: Path, cut: float | None = None,
                tight_first: bool = False) -> None:
    if info["w"] >= info["h"]:
        base = f"[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps={FPS},{GRADE},format=yuv420p"
    else:
        base = (f"[0:v]split=2[s0][s1];[s0]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
                f"boxblur=28:4,eq=brightness=-0.06[bg];[s1]scale=-2:1080[fg];[bg][fg]overlay=(W-w)/2:0,fps={FPS},"
                f"{GRADE},format=yuv420p")
    cw, ch = int(1920 / PUNCH) // 2 * 2, int(1080 / PUNCH) // 2 * 2
    tight = f",crop={cw}:{ch}:{(1920 - cw) // 2}:{int((1080 - ch) * 0.3)},scale=1920:1080,setsar=1"
    if cut:
        graph = (f"{base},split=2[p][q];[p]trim=end={cut:.3f},setpts=PTS-STARTPTS{tight if tight_first else ''}[a];"
                 f"[q]trim=start={cut:.3f},setpts=PTS-STARTPTS{'' if tight_first else tight}[b];"
                 f"[a][b]concat=n=2:v=1:a=0[v]")
    else:
        graph = f"{base}[v]"
    af = f"{VOCAL_CHAIN},loudnorm=I={VOICE_LUFS}:TP=-1.5:LRA=9,aresample=48000"
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{t_in:.3f}", "-to", f"{t_out:.3f}", "-i", str(src),
           "-filter_complex", graph, "-map", "[v]", "-map", "0:a?", "-af", af, "-ac", "2",
           "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", str(dst)]
    p = run(cmd)
    if p.returncode != 0:
        raise SystemExit(f"encoding {src.name} failed: {p.stderr[-700:]}")


def duration(path: Path) -> float:
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(p.stdout.strip())


def loudnorm_two_pass(src: Path, dst: Path, target: float, extra_out: list[str]) -> dict:
    p1 = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-af",
              f"loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"])
    m = json.loads(p1.stderr[p1.stderr.rfind("{"):p1.stderr.rfind("}") + 1])
    af = (f"loudnorm=I={target}:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
          f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    p2 = run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-af", af + ",aresample=48000", *extra_out, str(dst)])
    if p2.returncode != 0:
        raise SystemExit(f"loudness pass failed on {src.name}: {p2.stderr[-400:]}")
    return m


def measure(path: Path) -> dict:
    p = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true:framelog=quiet",
             "-f", "null", "-"])
    s = p.stderr[p.stderr.find("Summary"):]
    get = lambda k: float(re.search(k + r":\s*(-?[\d.]+)", s).group(1)) if re.search(k + r":\s*(-?[\d.]+)", s) else None
    return {"lufs": get("I"), "lra": get("LRA"), "peak_dbfs": get("Peak")}


# ---------- composition ----------
def groups_from(words: list[dict]) -> list[list[dict]]:
    groups, cur = [], []
    for k, w in enumerate(words):
        cur.append(w)
        nxt = words[k + 1] if k + 1 < len(words) else None
        chars = sum(len(x["text"]) + 1 for x in cur)
        brk = (nxt is None or len(cur) >= 5 or chars + len(nxt["text"]) > 30 or re.search(r"[.,:;!?]$", w["text"])
               or nxt["start"] - w["end"] >= 0.3)
        if brk:
            groups.append(cur)
            cur = []
    return groups


def captions_html(groups: list[tuple[float, float, list[dict]]], total: float) -> str:
    divs, tweens = [], []
    for g, (gs, ge, words) in enumerate(groups):
        spans = []
        for k, w in enumerate(words):
            key = re.sub(r"[^a-z\-']", "", w["text"].lower())
            cls = "w key" if key in KEYWORDS else "w"
            spans.append(f'<span class="{cls}" id="cg{g}w{k}">{html.escape(w["text"])}</span>')
            t = max(0.0, w["start"] - gs)
            tweens.append(f'tl.fromTo("#cg{g}w{k}", {{ color: "#9AA5B3" }}, '
                          f'{{ color: "{"#FF8A4C" if cls.endswith("key") else "#F7F8FA"}", duration: 0.1, ease: "none" }}, '
                          f'{gs + t:.3f});')
        divs.append(f'    <div id="cg{g}" class="clip grp" data-start="{gs:.3f}" data-duration="{ge - gs:.3f}">'
                    f'<div class="band"><span class="txt">{" ".join(spans)}</span></div></div>')
        tweens.append(f'tl.fromTo("#cg{g} .txt", {{ opacity: 0, filter: "blur(6px)" }}, '
                      f'{{ opacity: 1, filter: "blur(0px)", duration: 0.12, ease: "power3.out" }}, {gs:.3f});')
    return f"""<template>
  <style>
    #root {{ position: absolute; inset: 0; overflow: visible; }}
    #root .grp {{ position: absolute; inset: 0; }}
    #root .band {{ position: absolute; left: 0; right: 0; bottom: 92px; text-align: center; overflow: visible; }}
    #root .txt {{
      display: inline-block; padding: 8px 26px 12px; background: rgba(12, 16, 22, 0.62); border-radius: 4px;
      font-family: "Oswald", sans-serif; font-weight: 700; font-size: 66px; line-height: 1.08; letter-spacing: 0.01em;
      text-transform: uppercase; color: #F7F8FA; white-space: nowrap;
    }}
    #root .w {{ display: inline-block; }}
  </style>
  <div id="root" data-composition-id="captions" data-width="1920" data-height="1080">
{chr(10).join(divs)}
  </div>
  <script>
    const tl = gsap.timeline({{ paused: true }});
    {chr(10).join("    " + t for t in tweens).strip()}
    window.__timelines["captions"] = tl;
  </script>
</template>
"""


SFX_DIR = Path.home() / ".claude" / "skills" / "media-use" / "audio" / "assets" / "sfx"   # Pixabay licence, bundled
# (file, seconds from file start to its peak, gain): the peak lands one frame after the picture event it belongs to
SFX = {"whoosh": ("whoosh.mp3", 0.16, 0.16), "impact": ("impact-bass-1.mp3", 0.48, 0.12)}


def sfx_events(timeline: list[dict]) -> list[dict]:
    """A soft whoosh on three cuts into the graphics, and one low hit when each Kavach shield stamps in."""
    start = {c["id"]: c["start"] for c in timeline}
    ev = []
    for mid in ("mg1", "mg3", "mg5"):
        if mid in start:
            ev.append({"kind": "whoosh", "at": start[mid]})
    if "mg2" in start:
        ev.append({"kind": "impact", "at": start["mg2"] + 3.62})       # KAVACH shield stamp and ring in mg2
    if "mg5" in start:
        ev.append({"kind": "impact", "at": start["mg5"] + 0.88})       # end-card shield stamp
    out = []
    for n, e in enumerate(ev, start=1):
        f, peak, gain = SFX[e["kind"]]
        out.append({"id": f"sfx{n}", "file": f, "start": round(max(0.0, e["at"] + 1 / FPS - peak), 3), "gain": gain})
    return out


def index_html(timeline: list[dict], total: float, face_windows: list[tuple[float, float]],
               sfx: list[dict] | None = None) -> str:
    pts = [{"t": 0.0, "v": MUSIC_LOW}]
    for fs, fe in face_windows:
        if fs > 0.5:
            pts += [{"t": round(max(0.0, fs - 0.45), 3), "v": MUSIC_HIGH}, {"t": round(fs - 0.05, 3), "v": MUSIC_LOW}]
        pts += [{"t": round(fe + 0.05, 3), "v": MUSIC_LOW}, {"t": round(min(total, fe + 0.5), 3), "v": MUSIC_HIGH}]
    pts = sorted({p["t"]: p for p in pts}.values(), key=lambda p: p["t"])
    lane = json.dumps({"version": 1, "lanes": [{"target": "volume", "points": pts}]}).replace('"', "&quot;")
    clips = []
    for z, c in enumerate(timeline, start=1):
        audio = 'data-has-audio="true"' if c["kind"] == "face" else "muted"
        clips.append(f'      <video id="{c["id"]}" class="clip" src="assets/{c["id"]}.mp4" data-start="{c["start"]:.3f}" '
                     f'data-duration="{c["dur"]:.3f}" data-track-index="1" {audio} playsinline '
                     f'style="position:absolute;left:0;top:0;width:1920px;height:1080px;object-fit:cover;z-index:{z}"></video>')
    return f"""<!doctype html>
<html lang="en" data-resolution="landscape">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=1920, height=1080" />
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{ margin: 0; width: 1920px; height: 1080px; overflow: hidden; background: #12171e; }}
      #root {{ position: relative; width: 100%; height: 100%; }}
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="{total:.3f}" data-width="1920" data-height="1080">
{chr(10).join(clips)}
      <div id="captions" data-composition-id="captions" data-composition-src="compositions/captions.html" data-start="0" data-duration="{total:.3f}" data-track-index="5" data-track-kind="captions" data-width="1920" data-height="1080" style="z-index:50"></div>
      <audio id="music" src="assets/bgm.wav" data-start="0" data-duration="{total:.3f}" data-track-index="8" data-volume="1" data-fade-in="0.3" data-fade-out="1.8" data-automation="{lane}"></audio>
{chr(10).join(f'      <audio id="{s["id"]}" src="assets/sfx/{s["file"]}" data-start="{s["start"]:.3f}" data-track-index="9" data-volume="{s["gain"]}"></audio>' for s in (sfx or []))}
    </div>
    <script>
      window.__timelines = window.__timelines || {{}};
      window.__timelines["main"] = gsap.timeline({{ paused: true }});
    </script>
  </body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--footage", default=str(VIDEO / "footage"))
    ap.add_argument("--music", default=str(VIDEO / "music" / "bed_c.wav"))
    ap.add_argument("--out", default=str(VIDEO / "out" / "kavach-launch.mp4"))
    ap.add_argument("--no-render", action="store_true")
    a = ap.parse_args()

    foot = Path(a.footage)
    clips = sorted([p for p in foot.iterdir() if p.suffix.lower() in VIDEO_EXT],
                   key=lambda p: (int(re.search(r"\d+", p.stem).group()) if re.search(r"\d+", p.stem) else 99, p.name))
    if not clips:
        print(f"no video clips in {foot}")
        return 1
    if len(clips) > 5:
        print(f"note: {len(clips)} clips found; using the five that best match the script lines")
    work = PROJECT / ".work"
    (PROJECT / "assets").mkdir(parents=True, exist_ok=True)
    (PROJECT / "compositions").mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    report: dict = {"clips": []}

    infos = [probe(c) for c in clips]
    trans = []
    for c in clips:
        print(f"transcribing {c.name} ...", flush=True)
        trans.append(transcribe(c, work))
    lines_for = assign_lines(trans)
    order = sorted(lines_for, key=lambda c: lines_for[c])
    for c in range(len(clips)):
        if c not in lines_for:
            print(f"note: {clips[c].name} not used (another clip matches its line better)")

    faces = []
    for c in order:
        k = lines_for[c]
        words = trans[c]
        if not words:
            print(f"WARNING {clips[c].name}: no speech found; using the whole clip without captions")
            t_in, t_out, cw, src, sim = 0.0, infos[c]["dur"], [], "none", 0.0
        else:
            sim, i, j, line = best_take_any(words, k)
            t_in = max(0.0, words[i]["start"] - 0.25)
            t_out = min(infos[c]["dur"], words[j]["end"] + 0.45)
            cw, src = caption_words(words, i, j, line, t_in)
        dst = PROJECT / "assets" / f"face{k + 1}.mp4"
        cut = punch_time(cw, t_out - t_in) if cw else None
        encode_face(clips[c], infos[c], t_in, t_out, dst, cut=cut, tight_first=(k % 2 == 1))
        d = duration(dst)
        faces.append({"line": k, "id": f"face{k + 1}", "dur": d, "words": cw})
        rec = {"file": clips[c].name, "line": k + 1, "similarity": round(sim, 2), "take": [round(t_in, 2), round(t_out, 2)],
               "seconds": round(d, 2), "captions_from": src, "source": f'{infos[c]["w"]}x{infos[c]["h"]}'}
        report["clips"].append(rec)
        flag = "" if sim >= 0.75 else "   <- check this take"
        print(f'{clips[c].name}: line {k + 1}, match {sim:.2f}, take {t_in:.2f}-{t_out:.2f} s ({d:.1f} s), '
              f'{rec["source"]}, captions from {src}{flag}', flush=True)

    insert_len = {mid: duration(src) for mid, src in INSERTS}
    over = sum(f["dur"] for f in faces) + sum(insert_len.values()) - TARGET_TOTAL
    for mid in ("mg1", "mg3", "mg2", "mg5", "mg4"):       # trim held insert tails toward about a minute, never below
        if over <= 0:                                      # the point where all their text has landed and been read
            break
        cut = min(over, insert_len[mid] - INSERT_MIN[mid])
        if cut > 0:
            insert_len[mid] = round(insert_len[mid] - cut, 3)
            over -= cut

    timeline, t = [], 0.0
    face_windows, groups = [], []
    by_line = {f["line"]: f for f in faces}
    for k in range(5):
        if k in by_line:
            f = by_line[k]
            timeline.append({"id": f["id"], "kind": "face", "start": t, "dur": f["dur"]})
            face_windows.append((t, t + f["dur"]))
            for g in groups_from(f["words"]):
                gs = t + max(0.0, g[0]["start"] - 0.08)
                ge = t + min(f["dur"], g[-1]["end"] + 0.2)
                groups.append([gs, ge, [{**w, "start": t + w["start"], "end": t + w["end"]} for w in g]])
            t += f["dur"]
        mid, src = INSERTS[k]
        d = insert_len[mid]
        if d < duration(src) - 0.01:                       # trimmed tail: cut the file itself so the clip and file agree
            p = run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-t", f"{d:.3f}", "-c:v", "libx264", "-preset", "medium",
                     "-crf", "16", "-pix_fmt", "yuv420p", "-an", str(PROJECT / "assets" / f"{mid}.mp4")])
            if p.returncode != 0:
                raise SystemExit(f"trimming {mid} failed: {p.stderr[-300:]}")
            d = duration(PROJECT / "assets" / f"{mid}.mp4")
        else:
            shutil.copyfile(src, PROJECT / "assets" / f"{mid}.mp4")
        timeline.append({"id": mid, "kind": "insert", "start": t, "dur": d})
        t += d
    total = round(t, 3)
    for g in range(len(groups) - 1):                     # one caption group on screen at a time
        groups[g][1] = min(groups[g][1], groups[g + 1][0] - 0.02)
    groups = [(gs, ge, w) for gs, ge, w in groups if ge - gs > 0.15]

    loudnorm_two_pass(Path(a.music), PROJECT / "assets" / "bgm.wav", -16, ["-t", f"{total + 0.5:.2f}", "-ac", "2"])
    (PROJECT / "compositions" / "captions.html").write_text(captions_html(groups, total), encoding="utf-8")
    sfx = sfx_events(timeline)
    (PROJECT / "assets" / "sfx").mkdir(parents=True, exist_ok=True)
    for s in sfx:
        shutil.copyfile(SFX_DIR / s["file"], PROJECT / "assets" / "sfx" / s["file"])
    report["sfx"] = sfx
    (PROJECT / "index.html").write_text(index_html(timeline, total, face_windows, sfx), encoding="utf-8")
    report.update({"total_seconds": total, "caption_groups": len(groups), "music": Path(a.music).name,
                   "timeline": [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in c.items()} for c in timeline]})
    print(f"timeline: {total:.1f} s, {len(groups)} caption groups, music {Path(a.music).name}", flush=True)

    chk = npx("check", "--json", cwd=PROJECT)
    try:
        cj = json.loads(chk.stdout[chk.stdout.find("{"):])
        errs = [f for sec in ("lint", "runtime", "layout", "contrast") for f in (cj.get(sec) or {}).get("findings", [])
                if (f.get("severity") == "error")]
        report["check"] = {"ok": cj.get("ok"), "errors": [(e.get("code") or e.get("rule"), e.get("message", "")[:120]) for e in errs]}
        print(f'check: ok={cj.get("ok")} errors={len(errs)}', flush=True)
    except Exception as e:                                    # noqa: BLE001
        report["check"] = {"ok": None, "raw": (chk.stdout + chk.stderr)[-600:]}
        print("check: could not parse output", e, flush=True)
    if a.no_render:
        (PROJECT / "AUTOEDIT-REPORT.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 0

    raw = PROJECT / "renders" / "raw.mp4"
    print("rendering ...", flush=True)
    r = npx("render", "--quality", "delivery", "--output", "renders/raw.mp4", cwd=PROJECT)
    if r.returncode != 0 or not raw.exists():
        print("render failed:", (r.stdout + r.stderr)[-1500:])
        return 2
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    loudnorm_two_pass(raw, out, -15, ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"])
    report["output"] = {"path": str(out), "seconds": round(duration(out), 2), **measure(out)}
    shots = []
    for gs, ge, _ in groups[:: max(1, len(groups) // 6)]:
        tm = (gs + ge) / 2
        png = PROJECT / ".work" / f"check_{tm:05.1f}.png"
        run(["ffmpeg", "-v", "error", "-y", "-ss", f"{tm:.2f}", "-i", str(out), "-frames:v", "1", str(png)])
        shots.append(str(png))
    report["check_frames"] = shots
    (PROJECT / "AUTOEDIT-REPORT.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    o = report["output"]
    print(f'done: {out} ({o["seconds"]} s, {o["lufs"]} LUFS, peak {o["peak_dbfs"]} dBFS)')
    return 0


if __name__ == "__main__":
    sys.exit(main())
