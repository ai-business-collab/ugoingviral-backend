"""
services/video_edit.py — pure FFmpeg video primitives for the "make your own
material professional" polish pipeline (Phase 1 foundation).

NO HTTP / auth / store logic lives here. Every function takes and returns file
paths (or plain data) so each primitive is independently unit-testable. The
authed endpoints plus Content-Library / credit wiring live in routes/studio.py
(run_polish_pipeline and the /api/studio/polish + /api/studio/captions routes).

Design principle — SINGLE-PASS: a clip's normalize + reframe (+ optional trim)
are composed into ONE ffmpeg invocation (one filtergraph, one encode) rather
than chained encode passes, so we never degrade quality by re-encoding the same
frames several times. Phases 2–4 (logo overlay, timed captions, music/duck) plug
in as extra filtergraph fragments on the same single pass.

The fix for the old `-c copy` concat glitch (routes/studio.py _concat_videos):
arbitrary user clips differ in codec / resolution / fps / pixel format / SAR and
some have no audio at all. concat_reencode() normalizes every input to a common
canvas + fps + yuv420p + 44.1k stereo audio (synthesizing silence when a clip has
none) inside a single filter_complex, then concats — so the join is always clean.
"""
import os
import subprocess
import textwrap

# Target canvases per social aspect ratio. Must stay in sync with
# routes.studio.ASPECT_RATIOS.
CANVASES = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1":  (1080, 1080),
    "4:5":  (1080, 1350),
}
DEFAULT_FPS  = 30
_AUDIO_RATE  = 44100
_V_CODEC     = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
_A_CODEC     = ["-c:a", "aac", "-b:a", "128k"]
_FASTSTART   = ["-movflags", "+faststart"]


class VideoEditError(RuntimeError):
    """Raised on any ffmpeg/ffprobe failure so callers can refund + surface it."""


# ── probing ──────────────────────────────────────────────────────────────────

def ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def probe_duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15)
        return float((r.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def has_audio(path: str) -> bool:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15)
        return bool((r.stdout or "").strip())
    except Exception:
        return False


def probe_dimensions(path: str) -> tuple:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", path],
            capture_output=True, text=True, timeout=15)
        w, h = (r.stdout or "").strip().split("x")[:2]
        return int(w), int(h)
    except Exception:
        return (0, 0)


def canvas_for(aspect: str) -> tuple:
    return CANVASES.get(aspect, CANVASES["9:16"])


# ── internal helpers ───────────────────────────────────────────────────────────

def _run(cmd: list, timeout: int) -> None:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise VideoEditError("ffmpeg timed out")
    except Exception as e:
        raise VideoEditError(f"ffmpeg could not run: {e}")
    if p.returncode != 0:
        raise VideoEditError(f"ffmpeg failed: {(p.stderr or '')[-600:]}")


def _norm_chain(w: int, h: int, fps: int) -> str:
    """Video filter that fits ANY input onto a WxH canvas: scale to cover, crop
    to exact size, square pixels, constant fps, 8-bit 4:2:0 — the shared
    normalize+reframe fragment."""
    return (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,fps={fps},format=yuv420p")


def _result(path: str, w: int, h: int, had_audio: bool) -> dict:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise VideoEditError("render produced no output")
    pw, ph = probe_dimensions(path)
    return {"path": path, "duration": round(probe_duration(path), 2),
            "width": pw or w, "height": ph or h, "size_bytes": os.path.getsize(path),
            "had_audio": had_audio}


def _single_pass(src: str, out: str, w: int, h: int, fps: int,
                 trim=None, timeout: int = 600) -> dict:
    """Normalize (+ optional trim) a SINGLE clip onto a WxH canvas in one encode.
    Injects a silent stereo track when the source has no audio so downstream
    steps (concat / mix) always have an audio stream to work with. trim =
    (start_sec, duration_sec) or None."""
    src_has_audio = has_audio(src)
    vchain = ""
    achain = ""
    if trim:
        s, d = float(trim[0]), float(trim[1])
        vchain += f"trim=start={s}:duration={d},setpts=PTS-STARTPTS,"
        achain += f"atrim=start={s}:duration={d},asetpts=PTS-STARTPTS,"
    vchain += _norm_chain(w, h, fps)

    cmd = ["ffmpeg", "-y", "-i", src]
    fc = [f"[0:v]{vchain}[v]"]
    maps = ["-map", "[v]"]
    if src_has_audio:
        fc.append(f"[0:a]{achain}aresample={_AUDIO_RATE}[a]")
        maps += ["-map", "[a]"]
    else:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={_AUDIO_RATE}:cl=stereo"]
        maps += ["-map", "1:a", "-shortest"]
    cmd += ["-filter_complex", ";".join(fc)] + maps + _V_CODEC + _A_CODEC + _FASTSTART + [out]
    _run(cmd, timeout)
    return _result(out, w, h, src_has_audio)


# ── public primitives ──────────────────────────────────────────────────────────

def normalize_clip(src: str, out: str, w: int = 1080, h: int = 1920,
                   fps: int = DEFAULT_FPS, timeout: int = 600) -> dict:
    """Scale+pad/crop an arbitrary clip onto a WxH canvas, force yuv420p + CFR,
    and inject a silent audio track if none exists. Single encode."""
    return _single_pass(src, out, w, h, fps, trim=None, timeout=timeout)


def reframe(src: str, out: str, aspect: str = "9:16",
            fps: int = DEFAULT_FPS, timeout: int = 600) -> dict:
    """Reframe a clip to a social aspect ratio (default 9:16 vertical/reel)."""
    w, h = canvas_for(aspect)
    return _single_pass(src, out, w, h, fps, trim=None, timeout=timeout)


def render_polish(src: str, out: str, *, aspect: str = "9:16", fps: int = DEFAULT_FPS,
                  trim=None, timeout: int = 600) -> dict:
    """Phase-1 polish engine (pure): normalize + reframe (+ optional trim) of a
    single upload in ONE pass. Returns {path,duration,width,height,size_bytes,
    had_audio}. This is the fragment host that Phases 2–4 extend."""
    w, h = canvas_for(aspect)
    return _single_pass(src, out, w, h, fps, trim=trim, timeout=timeout)


def concat_reencode(paths: list, out: str, *, aspect: str = "9:16",
                    fps: int = DEFAULT_FPS, timeout: int = 900) -> dict:
    """Concatenate arbitrary clips into one video with a clean join. Replaces the
    old `-c copy` concat demuxer, which glitched whenever inputs differed in
    codec/resolution/fps/SAR or lacked audio. Every input is normalized onto a
    shared canvas + fps + yuv420p + 44.1k stereo (silence synthesized per
    audioless clip) inside a single filter_complex, then concatenated."""
    if not paths:
        raise VideoEditError("no inputs to concat")
    if len(paths) == 1:
        return render_polish(paths[0], out, aspect=aspect, fps=fps, timeout=timeout)
    w, h = canvas_for(aspect)
    audio_flags = [has_audio(p) for p in paths]

    cmd = ["ffmpeg", "-y"]
    for p in paths:
        cmd += ["-i", p]
    # One dedicated silent source per audioless clip — an input pad can only be
    # consumed once, so we can't share a single anullsrc across several clips.
    n_silent = sum(1 for a in audio_flags if not a)
    for _ in range(n_silent):
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={_AUDIO_RATE}:cl=stereo"]

    fc = []
    labels = []
    sil_ptr = len(paths)
    for i, p in enumerate(paths):
        fc.append(f"[{i}:v]{_norm_chain(w, h, fps)}[v{i}]")
        if audio_flags[i]:
            fc.append(f"[{i}:a]aresample={_AUDIO_RATE},asetpts=PTS-STARTPTS[a{i}]")
        else:
            dur = max(0.1, probe_duration(p))
            fc.append(f"[{sil_ptr}:a]atrim=duration={dur},asetpts=PTS-STARTPTS[a{i}]")
            sil_ptr += 1
        labels.append(f"[v{i}][a{i}]")
    fc.append(f"{''.join(labels)}concat=n={len(paths)}:v=1:a=1[v][a]")

    cmd += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]"]
    cmd += _V_CODEC + _A_CODEC + _FASTSTART + [out]
    _run(cmd, timeout)
    return _result(out, w, h, any(audio_flags))


def burn_caption(src: str, out: str, text: str, *, fontsize: int = 54,
                 fontcolor: str = "white", timeout: int = 300) -> str:
    """Burn a single static caption block onto a video (Phase-1 parity with the
    old _run_ffmpeg_captions — timed hook/CTA styling arrives in Phase 3). Audio
    is passed through untouched."""
    wrapped = "\n".join(textwrap.wrap((text or "").strip(), 36)) or " "
    txt_file = out + ".caption.txt"
    with open(txt_file, "w", encoding="utf-8") as f:
        f.write(wrapped)
    vf = (f"drawtext=textfile='{txt_file}':fontsize={fontsize}:fontcolor={fontcolor}"
          ":borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-text_h-80"
          ":font='DejaVu Sans Bold':fix_bounds=1:line_spacing=10")
    cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf,
           "-c:v", "libx264", "-preset", "fast", "-crf", "23",
           "-c:a", "copy"] + _FASTSTART + [out]
    try:
        _run(cmd, timeout)
    finally:
        try:
            os.remove(txt_file)
        except Exception:
            pass
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise VideoEditError("caption render produced no output")
    return out
