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
import re
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

# ── Brand Kit geometry — kept identical to services.branding.apply_logo_watermark
# so the logo looks the same on video as on generated images. ─────────────────
LOGO_W_FRAC   = 0.22       # logo width as a fraction of the canvas width
PAD_FRAC      = 0.04       # edge padding as a fraction of the canvas width
LOGO_OPACITY  = 0.85
LOGO_POSITIONS = {"bottom-right", "bottom-left", "top-right", "top-left", "center"}

# Bundled OFL (SIL Open Font License) Google Fonts — the exact set the Brand Kit
# frontend exposes (routes.brand_kit.ALLOWED_FONTS). All free to embed/redistribute
# (see the accompanying *.OFL.txt). This finally wires the Brand Kit `font` field.
FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")
_FONT_FILES = {
    "inter": "Inter.ttf", "poppins": "Poppins.ttf", "montserrat": "Montserrat.ttf",
    "playfairdisplay": "PlayfairDisplay.ttf", "oswald": "Oswald.ttf", "raleway": "Raleway.ttf",
}
DEFAULT_FONT_FILE = os.path.join(FONTS_DIR, "Inter.ttf")
_SYSTEM_FALLBACK_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


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


# ── Brand Kit primitives (Phase 2) ─────────────────────────────────────────────

def font_resolver(brand_font_name: str) -> str:
    """Map a Brand Kit font name (Inter / Poppins / Montserrat / Playfair Display
    / Oswald / Raleway — all bundled OFL Google Fonts) to a real .ttf path for
    drawtext. Falls back to the bundled Inter, then the system DejaVu Sans Bold,
    so a caption always has a loadable font file."""
    key = re.sub(r"[^a-z0-9]", "", (brand_font_name or "").lower())
    fname = _FONT_FILES.get(key)
    if fname:
        p = os.path.join(FONTS_DIR, fname)
        if os.path.exists(p):
            return p
    if os.path.exists(DEFAULT_FONT_FILE):
        return DEFAULT_FONT_FILE
    return _SYSTEM_FALLBACK_FONT


def _clamp01(x) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except Exception:
        return LOGO_OPACITY


def _hex_to_ffmpeg(color, default: str) -> str:
    """Normalize a Brand Kit color (#RRGGBB or a plain name) to an ffmpeg color
    token (0xRRGGBB). Returns `default` for anything unrecognized."""
    if not color:
        return default
    c = str(color).strip()
    h = c.lstrip("#")
    if len(h) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in h):
        return f"0x{h.lower()}"
    if c.replace(" ", "").isalpha():
        return c.lower()
    return default


def _overlay_xy(position: str, pad: int) -> tuple:
    """Overlay x/y expressions (in overlay's W/H main + w/h overlay vars) for a
    Brand Kit logo position — same geometry as the PIL image watermark."""
    p = position if position in LOGO_POSITIONS else "bottom-right"
    if p == "center":
        return "(W-w)/2", "(H-h)/2"
    vert, horiz = p.split("-")
    x = f"{pad}" if horiz == "left" else f"W-w-{pad}"
    y = f"{pad}" if vert == "top" else f"H-h-{pad}"
    return x, y


def logo_overlay_fragment(logo_pad: str, base_label: str, out_label: str,
                          position: str, opacity: float,
                          canvas_w: int, canvas_h: int) -> str:
    """Filtergraph fragment: scale the logo to LOGO_W_FRAC of the canvas width
    (aspect preserved — no stretching), apply opacity, and overlay it at
    `position`. Geometry mirrors services.branding so image + video branding
    match. Consumes `logo_pad` (e.g. '1:v') and `base_label`, emits `out_label`."""
    logo_w = max(1, int(canvas_w * LOGO_W_FRAC))
    pad = max(0, int(canvas_w * PAD_FRAC))
    x, y = _overlay_xy(position, pad)
    prep = (f"[{logo_pad}]scale={logo_w}:-1,format=rgba,"
            f"colorchannelmixer=aa={_clamp01(opacity)}[_ovl]")
    ov = f"[{base_label}][_ovl]overlay={x}:{y}:format=auto[{out_label}]"
    return f"{prep};{ov}"


def _drawtext_fragment(cap_file: str, caption: dict) -> str:
    """A single drawtext node styled from the Brand Kit: brand `font` (fontfile),
    brand primary color (fontcolor) and optional brand secondary color (box)."""
    fontfile  = caption.get("fontfile") or DEFAULT_FONT_FILE
    fontcolor = _hex_to_ffmpeg(caption.get("fontcolor"), "white")
    parts = [
        f"drawtext=textfile='{cap_file}'",
        f"fontfile='{fontfile}'",
        "fontsize=54", f"fontcolor={fontcolor}",
        "borderw=3", "bordercolor=black",
        "x=(w-text_w)/2", "y=h-text_h-80",
        "fix_bounds=1", "line_spacing=10",
    ]
    boxcolor = _hex_to_ffmpeg(caption.get("boxcolor"), "")
    if boxcolor:
        parts += ["box=1", f"boxcolor={boxcolor}@0.5", "boxborderw=18"]
    return ":".join(parts)


def _result(path: str, w: int, h: int, had_audio: bool) -> dict:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise VideoEditError("render produced no output")
    pw, ph = probe_dimensions(path)
    return {"path": path, "duration": round(probe_duration(path), 2),
            "width": pw or w, "height": ph or h, "size_bytes": os.path.getsize(path),
            "had_audio": had_audio}


def _single_pass(src: str, out: str, w: int, h: int, fps: int,
                 trim=None, logo=None, caption=None, timeout: int = 600) -> dict:
    """Normalize (+ optional trim) a SINGLE clip onto a WxH canvas, optionally
    overlaying a Brand Kit logo and burning a brand-styled caption — ALL in ONE
    encode (single filtergraph, no chained re-encodes). Injects a silent stereo
    track when the source has no audio. trim=(start,dur) or None; logo={path,
    position,opacity} or None; caption={text,fontfile,fontcolor,boxcolor} or None."""
    src_has_audio = has_audio(src)

    # Assemble inputs; track each input's index so later filters reference the
    # right pad (logo image and/or the silent-audio source shift the indices).
    cmd = ["ffmpeg", "-y", "-i", src]
    next_idx = 1
    logo_idx = None
    if logo and logo.get("path") and os.path.exists(logo["path"]):
        cmd += ["-i", logo["path"]]
        logo_idx = next_idx
        next_idx += 1
    else:
        logo = None
    sil_idx = None
    if not src_has_audio:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={_AUDIO_RATE}:cl=stereo"]
        sil_idx = next_idx
        next_idx += 1

    # Video chain: normalize/reframe (+trim) → logo overlay → caption, one graph.
    fc = []
    vchain = ""
    if trim:
        s, d = float(trim[0]), float(trim[1])
        vchain += f"trim=start={s}:duration={d},setpts=PTS-STARTPTS,"
    vchain += _norm_chain(w, h, fps)
    fc.append(f"[0:v]{vchain}[_vbase]")
    cur = "_vbase"
    if logo:
        fc.append(logo_overlay_fragment(f"{logo_idx}:v", cur, "_vlogo",
                                        logo.get("position", "bottom-right"),
                                        logo.get("opacity", LOGO_OPACITY), w, h))
        cur = "_vlogo"
    cap_file = None
    if caption and (caption.get("text") or "").strip():
        cap_file = out + ".caption.txt"
        with open(cap_file, "w", encoding="utf-8") as f:
            f.write("\n".join(textwrap.wrap(caption["text"].strip(), 30)) or " ")
        fc.append(f"[{cur}]{_drawtext_fragment(cap_file, caption)}[_vcap]")
        cur = "_vcap"

    maps = ["-map", f"[{cur}]"]
    if src_has_audio:
        achain = ""
        if trim:
            s, d = float(trim[0]), float(trim[1])
            achain += f"atrim=start={s}:duration={d},asetpts=PTS-STARTPTS,"
        fc.append(f"[0:a]{achain}aresample={_AUDIO_RATE}[a]")
        maps += ["-map", "[a]"]
    else:
        maps += ["-map", f"{sil_idx}:a", "-shortest"]

    cmd += ["-filter_complex", ";".join(fc)] + maps + _V_CODEC + _A_CODEC + _FASTSTART + [out]
    try:
        _run(cmd, timeout)
    finally:
        if cap_file:
            try:
                os.remove(cap_file)
            except Exception:
                pass
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
                  trim=None, logo=None, caption=None, timeout: int = 600) -> dict:
    """Polish engine (pure): normalize + reframe (+ optional trim) of a single
    upload, plus optional Brand Kit logo overlay and brand-styled caption, in ONE
    pass. Returns {path,duration,width,height,size_bytes,had_audio}. logo/caption
    are the Phase-2 fragments; Phases 3–4 extend the same single graph."""
    w, h = canvas_for(aspect)
    return _single_pass(src, out, w, h, fps, trim=trim, logo=logo, caption=caption, timeout=timeout)


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
