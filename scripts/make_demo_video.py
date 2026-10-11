"""Build the README demo video (docs/media/demo.mp4) and its GIF cut (docs/media/demo.gif).

    pip install -e ".[docs]"            # matplotlib (and Pillow, numpy)
    python scripts/make_demo_video.py   # needs ffmpeg/ffprobe on PATH

Inputs
  * The computer-use screen recording. It is NOT in git (212 s, 1920x1200, 8 fps);
    default path /workspace/onboard_media/computer_use_raw.mp4, override with --raw
    or ONBOARD_RAW_CLIP. The cut points and boxes below are in its timeline/pixels.
  * docs/img/before.png and docs/img/after.png (scripts/render_before_after.py). The red,
    green and amber boxes are found in those images by colour, and a plain copy of the
    "before" table is re-rendered with the same geometry so the red boxes can appear
    one by one.
  * docs/eval.md (scripts/eval_report.py): every number on the results card.
  * A real `onboard run` of the sample export, recorded line by line while this script
    runs. It uses --llm-mode replay, i.e. the recorded grok-4.7 responses committed in
    tests/fixtures/llm/, so it takes seconds and needs no API key.

Outputs: docs/media/demo.mp4 (1920x1080 H.264, no audio) and docs/media/demo.gif
(960x540, highlights only).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "docs" / "media"
IMG = ROOT / "docs" / "img"
RAW_DEFAULT = Path(os.environ.get("ONBOARD_RAW_CLIP",
                                  "/workspace/onboard_media/computer_use_raw.mp4"))
EXPORT = "data/legacy/legacy_export.xlsx"
RUN_CMD = ["onboard", "run", EXPORT, "--llm-mode", "replay", "--as-of", "2026-09-30",
           "--truth", "data/ground_truth", "--out", "out/demo"]
REPO_URL = "github.com/Huw01/onboard"

W, H, FPS = 1920, 1080, 30
XFADE = 0.4                       # crossfade between sections, seconds

# Palette: the accents are the ones used in docs/img/before.png and after.png.
BG = (16, 19, 24)
PANEL = (30, 34, 41)
INK = (241, 243, 244)
MUTED = (154, 160, 166)
RED = (217, 48, 37)
GREEN = (30, 142, 62)
AMBER = (232, 162, 0)
GREY = (128, 134, 139)
BLUE = (26, 115, 232)
TERM_BG = (13, 17, 23)


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------

def _find_font(names: list[str]) -> Path | None:
    for base in (Path("/usr/share/fonts"), Path.home() / ".local/share/fonts",
                 Path("/Library/Fonts"), Path("C:/Windows/Fonts")):
        if base.exists():
            for n in names:
                hit = next(base.rglob(n), None)
                if hit:
                    return hit
    return None


def _mpl_font(name: str) -> Path:
    import matplotlib
    return Path(matplotlib.get_data_path()) / "fonts" / "ttf" / name


SANS = _find_font(["Inter-VariableFont_opsz,wght.ttf"])
MONO = _find_font(["JetBrainsMono-VariableFont_wght.ttf"])
BOLD_WEIGHTS = {"SemiBold", "Bold", "ExtraBold", "Black"}


@lru_cache(maxsize=None)
def font(size: int, weight: str = "Regular", mono: bool = False) -> ImageFont.FreeTypeFont:
    path = MONO if mono else SANS
    if path is None:      # fall back to the DejaVu fonts bundled with matplotlib
        name = "DejaVuSansMono" if mono else "DejaVuSans"
        return ImageFont.truetype(str(_mpl_font(
            name + ("-Bold.ttf" if weight in BOLD_WEIGHTS else ".ttf"))), size)
    f = ImageFont.truetype(str(path), size)
    f.set_variation_by_name(weight)
    return f


# --------------------------------------------------------------------------
# Drawing helpers (all overlays are drawn on transparent RGBA layers)
# --------------------------------------------------------------------------

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def ease(x: float) -> float:
    x = clamp(x)
    return x * x * (3 - 2 * x)


def window(t: float, t0: float, t1: float, d: float = 0.25) -> float:
    """Opacity of an element shown during [t0, t1) with short fades."""
    if t < t0 or t >= t1:
        return 0.0
    return ease(min((t - t0) / d, (t1 - t) / d, 1.0))


def rgba(c, a: float = 1.0, base: int = 255):
    return (c[0], c[1], c[2], int(base * clamp(a)))


def layer() -> Image.Image:
    return Image.new("RGBA", (W, H), (0, 0, 0, 0))


def tsize(f, s: str) -> tuple[int, int]:
    b = f.getbbox(s)
    return b[2] - b[0], b[3] - b[1]


def text(d, xy, s, f, c, a=1.0, anchor="la"):
    d.text(xy, s, font=f, fill=rgba(c, a), anchor=anchor)


def pill(d, center, s, f, bg, fg=INK, a=1.0, pad=(20, 11)):
    tw, th = tsize(f, s)
    asc, desc = f.getmetrics()
    h = asc + desc + 2 * pad[1] - 6
    x0, y0 = center[0] - tw / 2 - pad[0], center[1] - h / 2
    box = (x0, y0, x0 + tw + 2 * pad[0], y0 + h)
    d.rounded_rectangle(box, radius=h / 2, fill=rgba(bg, a))
    d.text((center[0], center[1]), s, font=f, fill=rgba(fg, a), anchor="mm")
    return box


def closest(pt, box):
    return clamp(pt[0], box[0], box[2]), clamp(pt[1], box[1], box[3])


def arrow(d, p0, p1, c, a=1.0, width=6, head=24):
    ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    base = (p1[0] - head * 0.8 * math.cos(ang), p1[1] - head * 0.8 * math.sin(ang))
    d.line([p0, base], fill=rgba(c, a), width=width)
    left = (p1[0] - head * math.cos(ang - 0.45), p1[1] - head * math.sin(ang - 0.45))
    right = (p1[0] - head * math.cos(ang + 0.45), p1[1] - head * math.sin(ang + 0.45))
    d.polygon([p1, left, right], fill=rgba(c, a))


def callout(d, box, label, color, a, label_center, f=None, grow=True):
    """A coloured box around `box` (canvas px) plus a label pill with an arrow to it."""
    f = f or font(30, "SemiBold")
    s = 1 + 0.12 * (1 - ease(a)) if grow else 1
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    hw, hh = (box[2] - box[0]) / 2 * s + 6, (box[3] - box[1]) / 2 * s + 6
    b = (cx - hw, cy - hh, cx + hw, cy + hh)
    d.rounded_rectangle(b, radius=8, outline=rgba(color, a), width=6)
    if label:
        pb = pill(d, label_center, label, f, color, a=a)
        tgt = closest(label_center, b)
        src = closest(tgt, pb)
        if math.dist(src, tgt) > 30:
            arrow(d, src, tgt, color, a)


def step_label(img: Image.Image, kicker: str, title: str, color=BLUE, a: float = 1.0):
    lay = layer()
    d = ImageDraw.Draw(lay)
    fk, ft = font(24, "Bold"), font(34, "SemiBold")
    kw, _ = tsize(fk, kicker)
    tw, _ = tsize(ft, title)
    d.rounded_rectangle((32, 26, 32 + 36 + kw + 24 + tw + 28, 26 + 64), radius=14,
                        fill=rgba((0, 0, 0), a, 175))
    d.rounded_rectangle((44, 38, 44 + kw + 24, 38 + 40), radius=10, fill=rgba(color, a))
    d.text((44 + 12 + kw / 2, 58), kicker, font=fk, fill=rgba(INK, a), anchor="mm")
    d.text((44 + kw + 24 + 18, 58), title, font=ft, fill=rgba(INK, a), anchor="lm")
    img.alpha_composite(lay)


def caption(img: Image.Image, s: str, a: float = 1.0, bar: float | None = None):
    """Bottom caption on a semi-transparent bar. `bar` (default `a`) is the bar opacity,
    so back-to-back captions can swap text without the bar flickering."""
    bar = a if bar is None else bar
    if bar <= 0:
        return
    lay = layer()
    d = ImageDraw.Draw(lay)
    d.rectangle((0, H - 108, W, H), fill=rgba((0, 0, 0), bar, 178))
    if s and a > 0:
        size = 40
        while tsize(font(size, "Medium"), s)[0] > W - 160 and size > 26:
            size -= 2
        d.text((W / 2, H - 54), s, font=font(size, "Medium"), fill=rgba(INK, a), anchor="mm")
    img.alpha_composite(lay)


def captions_at(img, t, cues):
    """cues: [(t0, t1, text)] in section-local time. Text fades in and out; the bar
    stays up across cues that follow each other without a gap."""
    spans = []
    for t0, t1, _ in sorted(cues):
        if spans and t0 - spans[-1][1] < 1e-6:
            spans[-1][1] = max(spans[-1][1], t1)
        else:
            spans.append([t0, t1])
    bar = max((window(t, a, b, 0.2) for a, b in spans), default=0.0)
    cur = [(window(t, t0, t1, 0.15), s) for t0, t1, s in cues if t0 <= t < t1]
    a, s = cur[0] if cur else (0.0, "")
    caption(img, s, a, bar)


def canvas(color=BG) -> Image.Image:
    return Image.new("RGBA", (W, H), rgba(color))


def paste_card(img, card: Image.Image, box, radius=18, shadow=True):
    """Paste `card` resized into box=(x0, y0, x1, y1) with rounded corners."""
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    card = card.convert("RGBA").resize((x1 - x0, y1 - y0), Image.LANCZOS)
    mask = Image.new("L", card.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, card.size[0] - 1, card.size[1] - 1),
                                           radius=radius, fill=255)
    if shadow:
        sh = layer()
        ImageDraw.Draw(sh).rounded_rectangle((x0 + 6, y0 + 10, x1 + 6, y1 + 10),
                                             radius=radius, fill=(0, 0, 0, 120))
        img.alpha_composite(sh)
    img.paste(card, (x0, y0), mask)


# --------------------------------------------------------------------------
# Inputs: eval numbers, the recorded terminal run, the images
# --------------------------------------------------------------------------

def eval_numbers() -> dict:
    md = (ROOT / "docs" / "eval.md").read_text()

    def grab(pattern, cast=str):
        m = re.search(pattern, md)
        if not m:
            raise SystemExit(f"docs/eval.md: no match for {pattern!r}; "
                             "run scripts/eval_report.py first")
        return [cast(g.replace(",", "")) for g in m.groups()]

    n = {}
    n["cols_ok"], n["cols"] = grab(r"columns → fields \| (\d+) / (\d+)", int)
    n["injected"], = grab(r"each of the ([\d,]+) injected corruptions", int)
    n["silent_before"], n["silent_after"] = grab(
        r"silent errors in the load \| (\d+) \| (\d+)", int)
    n["broken"], = grab(r"correct cells changed by the LLM \| – \| (\d+)", int)
    (_, _, n["rows_ok"], n["rows"]) = grab(
        r"rows loaded identical to ground truth \| ([\d,]+) / ([\d,]+) \| "
        r"([\d,]+) / ([\d,]+)", int)
    n["cost"], = grab(r"\*\*\$([\d.]+)\*\*", float)
    n["wall_min"], = grab(r"≈ \*\*(\d+) min\*\*", int)
    n["model"], = grab(r"recorded run \(`([^`]+)`")
    return n


@dataclass
class TermRun:
    command: str
    lines: list[tuple[float, str]]       # (seconds after start, text)
    elapsed: float


def record_run() -> TermRun:
    """Run the real pipeline (replay mode) and timestamp every output line."""
    exe = Path(sys.executable).with_name("onboard")
    cmd = [str(exe) if exe.exists() else "onboard"] + RUN_CMD[1:]
    env = {k: v for k, v in os.environ.items() if k != "XAI_API_KEY"}   # prove: no key
    env["PYTHONUNBUFFERED"] = "1"
    t0 = time.monotonic()
    p = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines = [(time.monotonic() - t0, ln.rstrip("\n")) for ln in p.stdout]
    if p.wait() != 0:
        raise SystemExit("onboard run failed:\n" + "\n".join(s for _, s in lines))
    return TermRun(" ".join(RUN_CMD), lines, time.monotonic() - t0)


def find_boxes(img: Image.Image, color, tol=40, min_size=20) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of outline rectangles drawn in `color` (simple flood fill)."""
    a = np.asarray(img.convert("RGB")).astype(int)
    mask = (np.abs(a - np.array(color)).sum(axis=2) < tol)
    seen = np.zeros_like(mask)
    boxes = []
    ys, xs = np.nonzero(mask)
    h, w = mask.shape
    for y, x in zip(ys, xs):
        if seen[y, x]:
            continue
        stack, x0, y0, x1, y1 = [(y, x)], x, y, x, y
        seen[y, x] = True
        while stack:
            cy, cx = stack.pop()
            x0, y0, x1, y1 = min(x0, cx), min(y0, cy), max(x1, cx), max(y1, cy)
            for ny in (cy - 1, cy, cy + 1):
                for nx in (cx - 1, cx, cx + 1):
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        if x1 - x0 >= min_size and y1 - y0 >= min_size:
            boxes.append((x0, y0, x1, y1))
    return boxes


def nearest_box(boxes, frac, size):
    px, py = frac[0] * size[0], frac[1] * size[1]
    return min(boxes, key=lambda b: math.dist(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2),
                                               (px, py)))


def render_plain_before(path: Path) -> None:
    """Re-render the 'before' table with the problem-cell highlight switched off.
    Same code and data as docs/img/before.png, so the geometry is identical."""
    spec = importlib.util.spec_from_file_location("rba", ROOT / "scripts" /
                                                  "render_before_after.py")
    rba = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rba)
    rba.RED = rba.RED_EDGE = "white"
    from datetime import date

    import pandas as pd

    legacy = rba.read_legacy_xlsx(ROOT / EXPORT)
    result = rba.validate(legacy, rba.reference_mapping(), date(2026, 9, 30))
    corr = pd.read_csv(ROOT / "data" / "ground_truth" / "corruptions.csv", dtype=str,
                       keep_default_na=False)
    corr["row_index"] = corr.row_index.astype(int)
    blocks = rba.build_blocks(result, legacy, corr, rba.pick_rows(result, corr))
    rba.render_single(blocks, "before", path)


# --------------------------------------------------------------------------
# The raw computer-use clip
# --------------------------------------------------------------------------

class RawClip:
    """Frames of the screen recording between T0 and T1, cached as JPEGs on disk."""
    T0, T1 = 20.0, 101.0

    def __init__(self, path: Path, tmp: Path):
        if not path.exists():
            raise SystemExit(f"raw clip not found: {path} (pass --raw)")
        info = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,r_frame_rate", "-of", "json", str(path)],
            check=True, capture_output=True, text=True).stdout)["streams"][0]
        self.size = (info["width"], info["height"])
        num, den = info["r_frame_rate"].split("/")
        self.fps = float(num) / float(den)
        self.dir = tmp / "raw"
        self.dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-ss", str(self.T0), "-to", str(self.T1),
                        "-i", str(path), "-qscale:v", "2", str(self.dir / "%05d.jpg")],
                       check=True)
        self.n = len(list(self.dir.glob("*.jpg")))
        self._cache: OrderedDict[int, Image.Image] = OrderedDict()

    def frame(self, t: float) -> Image.Image:
        i = int(clamp(round((t - self.T0) * self.fps) + 1, 1, self.n))
        if i not in self._cache:
            self._cache[i] = Image.open(self.dir / f"{i:05d}.jpg").convert("RGB")
            if len(self._cache) > 48:
                self._cache.popitem(last=False)
        return self._cache[i]


# Source-pixel regions in the 1920x1200 recording (found by inspecting the frames).
FULL = (0, 0, 1920, 1200)
ZOOM_NAMEBOX = (0, 40, 960, 640)                 # Name Box + top-left of the sheet
NAMEBOX = (3, 101, 131, 123)
FORMULA = (218, 101, 700, 123)
INFO_DIALOG = (737, 525, 1187, 666)              # "You must enter a valid reference..."
CELL_J28 = (1027, 688, 1123, 709)                # the stray "2" in INSTALL_DT
CELL_J180 = (1027, 629, 1123, 650)               # "$WORK_O" typed into a cell
SAVE_BAR = (0, 1176, 300, 1199)                  # "Save document" progress bar
RECOVERY = (708, 395, 1212, 782)                 # LibreOffice Document Recovery
AGENT_START = 22.0                               # Calc window appears


@dataclass
class Note:
    rect: tuple                 # source px
    label: str
    t0: float                   # segment-local output seconds
    t1: float
    offset: tuple = (0, 120)    # label centre relative to the box centre, canvas px
    color: tuple = RED


@dataclass
class Seg:
    src0: float
    src1: float
    speed: float = 2.0          # 0 = freeze on src0 for `hold` seconds
    hold: float = 0.0
    crop: tuple = FULL
    zoom_from: tuple | None = None
    caption: str = ""
    notes: list[Note] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.hold if self.speed == 0 else (self.src1 - self.src0) / self.speed


def option_a_segments() -> list[Seg]:
    return [
        Seg(22.0, 27.0, 4, caption="Task: fix 6 cells in the export by hand, in LibreOffice Calc"),
        Seg(55.5, 59.0, 2, crop=ZOOM_NAMEBOX, zoom_from=FULL,
            caption="It moves between cells by typing addresses into the Name Box",
            notes=[Note(NAMEBOX, "Name Box", 0.3, 1.75, (330, 40), BLUE)]),
        Seg(64.5, 66.5, 2, crop=ZOOM_NAMEBOX,
            caption="Name Box autocomplete swallows keystrokes: cell values land in the Name Box",
            notes=[Note(NAMEBOX, "009-0360-72, then 9/12/2019", 0.25, 1.0, (420, 60))]),
        Seg(66.5, 66.5, 0, hold=2.6, crop=FULL, zoom_from=ZOOM_NAMEBOX,
            caption="Error dialog, and a stray “2” lands in an INSTALL_DT cell",
            notes=[Note(INFO_DIALOG, "Invalid reference", 0.35, 2.6, (0, -150)),
                   Note(CELL_J28, "“2”", 0.9, 2.6, (230, 70))]),
        Seg(66.6, 69.0, 2, crop=ZOOM_NAMEBOX, zoom_from=FULL,
            caption="Again: a tag meant for a cell goes into the Name Box",
            notes=[Note(NAMEBOX, "CV-1015-BT1", 0.2, 1.2, (330, 60))]),
        Seg(69.0, 70.25, 2, crop=FULL, zoom_from=ZOOM_NAMEBOX),
        Seg(70.25, 70.25, 0, hold=2.4,
            caption="…and the start of a sheet reference lands in cell J180",
            notes=[Note(CELL_J180, "$WORK_O", 0.3, 9, (260, -110))]),
        Seg(70.25, 70.25, 0, hold=2.2,
            caption="The file is saved with the wrong values in it",
            notes=[Note(CELL_J180, "$WORK_O", -1, 2.2, (260, -110)),
                   Note(SAVE_BAR, "Save document", 0.2, 2.2, (300, -230), AMBER)]),
        Seg(70.25, 87.25, 4, caption="Then Calc stops responding"),
        Seg(87.25, 99.0, 4, caption="Calc closes; LibreOffice offers document recovery",
            notes=[Note(RECOVERY, "", 0.6, 3.0)]),
    ]


class OptionA:
    END_TAG = 3.0

    def __init__(self, clip: RawClip):
        self.clip = clip
        self.segs = option_a_segments()
        self.starts = np.cumsum([0] + [s.duration for s in self.segs]).tolist()
        self.duration = self.starts[-1] + self.END_TAG
        self.cues = []
        for k, sg in enumerate(self.segs):
            if not sg.caption:
                continue
            if self.cues and self.cues[-1][2] == sg.caption and \
                    abs(self.cues[-1][1] - self.starts[k]) < 1e-6:
                self.cues[-1] = (self.cues[-1][0], self.starts[k + 1], sg.caption)
            else:
                self.cues.append((self.starts[k], self.starts[k + 1], sg.caption))
        # the clip is shown at full canvas height, pillarboxed
        self.dest = ((W - H * 1.6) / 2, 0, (W + H * 1.6) / 2, H)

    def _locate(self, t):
        for k, s in enumerate(self.segs):
            if t < self.starts[k + 1]:
                return k, s, t - self.starts[k]
        k = len(self.segs) - 1
        return k, self.segs[k], self.segs[k].duration

    def __call__(self, t: float) -> Image.Image:
        tag = t - self.starts[-1]
        k, s, lt = self._locate(min(t, self.starts[-1] - 1e-6))
        src_t = s.src0 if s.speed == 0 else s.src0 + lt * s.speed
        crop = s.crop
        if s.zoom_from:
            z = ease(lt / 0.45)
            crop = tuple(a + (b - a) * z for a, b in zip(s.zoom_from, s.crop))
        frame = self.clip.frame(src_t)
        img = canvas()
        view = frame.crop(tuple(int(round(v)) for v in crop))
        x0, y0, x1, y1 = self.dest
        img.paste(view.resize((int(x1 - x0), int(y1 - y0)), Image.LANCZOS), (int(x0), 0))
        sx, sy = (x1 - x0) / (crop[2] - crop[0]), (y1 - y0) / (crop[3] - crop[1])

        def to_canvas(r):
            return (x0 + (r[0] - crop[0]) * sx, y0 + (r[1] - crop[1]) * sy,
                    x0 + (r[2] - crop[0]) * sx, y0 + (r[3] - crop[1]) * sy)

        lay = layer()
        d = ImageDraw.Draw(lay)
        if tag < 0:
            for n in s.notes:
                a = window(lt, n.t0, n.t1, 0.2)
                if a:
                    b = to_canvas(n.rect)
                    c = ((b[0] + b[2]) / 2 + n.offset[0], (b[1] + b[3]) / 2 + n.offset[1])
                    callout(d, b, n.label, n.color, a, c)
        img.alpha_composite(lay)
        step_label(img, "STEP 2", "Option A: computer use", AMBER)
        self._counter(img, src_t, s, tag)
        if tag < 0:
            captions_at(img, t, self.cues)
        else:
            self._end_tag(img, tag)
        return img

    def _counter(self, img, src_t, seg, tag):
        lay = layer()
        d = ImageDraw.Draw(lay)
        x0, y0, x1, y1 = W - 420, 26, W - 32, 236
        d.rounded_rectangle((x0, y0, x1, y1), radius=14, fill=(0, 0, 0, 185))
        text(d, (x0 + 24, y0 + 20), "computer-use agent on a desktop", font(22, "Medium"), MUTED)
        text(d, (x0 + 24, y0 + 56), "6 cells, ~30 actions", font(32, "SemiBold"), INK)
        el = max(0.0, src_t - AGENT_START)
        text(d, (x0 + 24, y0 + 108), f"{int(el // 60)}:{int(el % 60):02d}",
             font(54, "Bold", mono=True), INK)
        text(d, (x0 + 24, y0 + 172), "elapsed (recording time)", font(20), MUTED)
        badge = ("paused" if seg.speed == 0 else f"{seg.speed:g}× speed") if tag < 0 else "end"
        pill(d, (x1 - 90, y0 + 136), badge, font(24, "SemiBold"),
             GREY if seg.speed == 0 or tag >= 0 else BLUE)
        img.alpha_composite(lay)

    def _end_tag(self, img, tag):
        a = ease(tag / 0.4)
        lay = layer()
        d = ImageDraw.Draw(lay)
        d.rectangle((0, 0, W, H), fill=(0, 0, 0, int(150 * a)))
        cw, ch = 980, 300
        bx = ((W - cw) / 2, (H - ch) / 2 + 20, (W + cw) / 2, (H + ch) / 2 + 20)
        d.rounded_rectangle(bx, radius=24, fill=rgba(PANEL, a, 245), outline=rgba(AMBER, a),
                            width=4)
        text(d, (W / 2, bx[1] + 105), "1 of 6 correct", font(104, "Bold"), INK, a, "mm")
        text(d, (W / 2, bx[1] + 205),
             "~30 actions in ~50 s · keystrokes landed in the wrong cells",
             font(30, "Medium"), MUTED, a, "mm")
        text(d, (W / 2, bx[1] + 248), "a damaged file was saved · then Calc froze",
             font(30, "Medium"), MUTED, a, "mm")
        img.alpha_composite(lay)


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

class Title:
    duration = 5.0

    def __call__(self, t):
        img = canvas()
        lay = layer()
        d = ImageDraw.Draw(lay)
        a1, a2, a3 = ease(t / 0.6), ease((t - 0.6) / 0.6), ease((t - 1.2) / 0.6)
        f1 = font(84, "Bold")
        head, rest = "onboard", ": LLM-assisted data onboarding"
        w1, w2 = tsize(f1, head)[0], tsize(f1, rest)[0]
        x = (W - w1 - w2) / 2
        y = H / 2 - 90 - 20 * (1 - a1)
        d.text((x, y), head, font=f1, fill=rgba(BLUE, a1), anchor="ls")
        d.text((x + w1, y), rest, font=f1, fill=rgba(INK, a1), anchor="ls")
        text(d, (W / 2, H / 2 + 10),
             "An LLM maps the legacy export, deterministic rules check every row,",
             font(38), INK, a2, "mm")
        text(d, (W / 2, H / 2 + 64), "and anything uncertain goes to a person.",
             font(38), INK, a2, "mm")
        text(d, (W / 2, H / 2 + 170),
             "Worked example: a CMMS migration · all data synthetic", font(28), MUTED, a3, "mm")
        d.rounded_rectangle((W / 2 - 60, H / 2 - 50, W / 2 + 60, H / 2 - 44), radius=3,
                            fill=rgba(BLUE, a1))
        img.alpha_composite(lay)
        return img


class Before:
    """STEP 1: the plain legacy table, red boxes appear one by one."""
    CROP = (0.0, 0.145, 1.0, 0.885)    # drop the title and legend of before.png
    PICKS = [  # (label, caption, approx. centre as a fraction of before.png)
        ("Excel serial date", "39990 is an Excel serial number, not a date",
         (893 / 1024, 232 / 862)),
        ("MM/DD swap", "12/29/2019 in a day-first file: month and day swapped",
         (893 / 1024, 271 / 862)),
        ("orphan parent", "Parent CV-1944 does not exist", (603 / 1024, 271 / 862)),
        ("free-text frequency", "“every 6 months” in a numeric frequency column",
         (447 / 1024, 461 / 862)),
        ("labor in minutes", "720 labor hours on one work order: it was recorded in minutes",
         (735 / 1024, 651 / 862)),
    ]
    T_FIRST, T_STEP = 1.4, 1.75

    def __init__(self, tmp: Path, nums: dict, rows_total: int):
        real = Image.open(IMG / "before.png").convert("RGB")
        plain_path = tmp / "before_plain.png"
        render_plain_before(plain_path)
        plain = Image.open(plain_path).convert("RGB")
        if plain.size != real.size:
            raise SystemExit("plain before table does not match docs/img/before.png; "
                             "re-run scripts/render_before_after.py")
        boxes = find_boxes(real, RED)
        self.picks = [(lab, cap, nearest_box(boxes, frac, real.size))
                      for lab, cap, frac in self.PICKS]
        w, h = real.size
        c = (int(self.CROP[0] * w), int(self.CROP[1] * h), int(self.CROP[2] * w),
             int(self.CROP[3] * h))
        self.c = c
        self.plain, self.real = plain.crop(c), real.crop(c)
        cw, ch = self.plain.size
        scale = min(1300 / cw, 800 / ch)
        self.scale = scale
        self.box = (80, 140, 80 + cw * scale, 140 + ch * scale)
        self.base_plain = canvas()
        paste_card(self.base_plain, self.plain, self.box)
        self.base_real = canvas()
        paste_card(self.base_real, self.real, self.box)
        self.t_all = self.T_FIRST + len(self.picks) * self.T_STEP + 0.6
        self.duration = self.t_all + 3.4
        self.injected, self.rows_total = nums["injected"], rows_total

    def _map(self, b):
        x0, y0 = self.box[0], self.box[1]
        return (x0 + (b[0] - self.c[0]) * self.scale, y0 + (b[1] - self.c[1]) * self.scale,
                x0 + (b[2] - self.c[0]) * self.scale, y0 + (b[3] - self.c[1]) * self.scale)

    def __call__(self, t):
        mix = ease((t - self.t_all) / 0.6)
        img = self.base_plain.copy() if mix < 1 else self.base_real.copy()
        if 0 < mix < 1:
            img = Image.blend(img, self.base_real, mix)
        lay = layer()
        d = ImageDraw.Draw(lay)
        lx = self.box[2] + 56
        f, fb = font(30, "SemiBold"), font(24, "Bold")
        cue = "legacy_export.xlsx, as the old CMMS produced it: 4 sheets, " \
              f"{self.rows_total:,} rows"
        for k, (lab, cap, b) in enumerate(self.picks):
            t0 = self.T_FIRST + k * self.T_STEP
            a = ease((t - t0) / 0.3)
            if a <= 0:
                continue
            if t < self.t_all and t >= t0:
                cue = cap
            r = self._map(b)
            s = 1 + 0.15 * (1 - a)
            cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
            hw, hh = (r[2] - r[0]) / 2 * s + 3, (r[3] - r[1]) / 2 * s + 3
            d.rounded_rectangle((cx - hw, cy - hh, cx + hw, cy + hh), radius=6,
                                outline=rgba(RED, a), width=5)
            # numbered badge on the box, and the same number in the legend column
            bx, by = cx + hw - 2, cy - hh - 2
            d.ellipse((bx - 19, by - 19, bx + 19, by + 19), fill=rgba(RED, a),
                      outline=rgba((255, 255, 255), a), width=3)
            d.text((bx, by + 1), str(k + 1), font=fb, fill=rgba(INK, a), anchor="mm")
            ly = self.box[1] + 40 + k * 84
            d.ellipse((lx, ly - 22, lx + 44, ly + 22), fill=rgba(RED, a))
            d.text((lx + 22, ly + 1), str(k + 1), font=fb, fill=rgba(INK, a), anchor="mm")
            d.text((lx + 62, ly), lab, font=f, fill=rgba(INK, a), anchor="lm")
        if t >= self.t_all:
            cue = f"{self.injected:,} corruptions injected, every one logged in an answer key"
        img.alpha_composite(lay)
        step_label(img, "STEP 1", "The messy legacy export", RED)
        caption(img, cue)
        return img


class OptionB:
    """STEP 2, option B: the real `onboard run`, replayed at real speed."""
    TYPE_T, HOLD, MIN_CUE = 1.8, 4.0, 2.2
    LINE_H, FS = 32, 22

    def __init__(self, run: TermRun, nums: dict):
        self.run = run
        self.t_run = self.TYPE_T + 0.4
        self.duration = self.t_run + run.elapsed + self.HOLD
        self.box = (70, 128, W - 70, H - 128)
        self.rows = int((self.box[3] - self.box[1] - 78) // self.LINE_H)
        find = lambda pred: next((ts for ts, s in run.lines if pred(s)), None)  # noqa: E731
        t_map = find(lambda s: re.match(r"\s+\d+ sheets, \d+ columns", s))
        t_val = find(lambda s: s.startswith("> validate"))
        t_rev = find(lambda s: s.startswith("> review"))
        t_score = find(lambda s: s.startswith("corruptions"))
        det = next(int(s.split()[1]) for _, s in run.lines if s.startswith("detected "))
        tot = next(int(s.split()[1]) for _, s in run.lines if s.startswith("corruptions "))
        self.detected, self.corruptions = det, tot
        R = self.t_run
        texts = [
            "One command, replaying a recorded grok-4.7 run (no API key)",
            f"LLM maps {nums['cols_ok']}/{nums['cols']} columns "
            "(structured output, a confidence and reason for each)",
            "Validator checks every row with deterministic rules",
            "LLM resolves ambiguous dates and duplicates; answers are re-validated",
            f"Scored against the answer key: {det:,} of {tot:,} corruptions caught",
        ]
        # each caption starts with its stage, but stays up for at least MIN_CUE seconds
        # (the replayed mapping finishes in the same instant validation starts)
        starts = [0.2, R + t_map, R + t_val, R + t_rev, R + t_score]
        for k in range(1, len(starts)):
            starts[k] = max(starts[k], starts[k - 1] + self.MIN_CUE)
        self.duration = max(self.duration, starts[-1] + 3.0)
        self.cues = [(starts[k], starts[k + 1] if k + 1 < len(starts) else self.duration + 1,
                      texts[k]) for k in range(len(texts))]

    def _style(self, s):
        if s.startswith("$ "):
            return INK, "Medium"
        if s.startswith("> "):
            return (121, 192, 255), "Bold"
        if s.startswith("  ") and not s.startswith("   ") and ("sheets," in s or "cells fixed" in s
                                                              or "answers accepted" in s):
            return (126, 231, 135), "Medium"
        if s.startswith(("LLM ", "detected", "detection_rate", "rows_recovered")):
            return (255, 214, 102), "Medium"
        return (201, 209, 217), "Regular"

    def __call__(self, t):
        img = canvas()
        lay = layer()
        d = ImageDraw.Draw(lay)
        b = self.box
        d.rounded_rectangle((b[0] + 6, b[1] + 10, b[2] + 6, b[3] + 10), radius=16,
                            fill=(0, 0, 0, 130))
        d.rounded_rectangle(b, radius=16, fill=rgba(TERM_BG), outline=rgba((48, 54, 61)),
                            width=2)
        d.rounded_rectangle((b[0], b[1], b[2], b[1] + 48), radius=16, fill=rgba((33, 38, 45)))
        d.rectangle((b[0], b[1] + 30, b[2], b[1] + 48), fill=rgba((33, 38, 45)))
        for k, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse((b[0] + 22 + k * 30, b[1] + 16, b[0] + 38 + k * 30, b[1] + 32), fill=c)
        text(d, ((b[0] + b[2]) / 2, b[1] + 24), "~/onboard", font(22, "Medium"), MUTED,
             anchor="mm")
        pill(d, (b[2] - 270, b[1] + 24), "replay · recorded responses · real time",
             font(18, "SemiBold"), (48, 54, 61), pad=(14, 6))
        # transcript up to time t
        n_chars = int(len(self.run.command) * clamp(t / self.TYPE_T))
        lines = ["$ " + self.run.command[:n_chars]]
        if t >= self.t_run:
            lines += [s for ts, s in self.run.lines if ts <= t - self.t_run]
            done = t - self.t_run >= self.run.elapsed
            if done:
                lines.append("$ ")
        shown = lines[-self.rows:]
        y = b[1] + 66
        for s in shown:
            c, wgt = self._style(s)
            if s.startswith("$ "):
                d.text((b[0] + 28, y), "$", font=font(self.FS, "Bold", True),
                       fill=rgba((126, 231, 135)))
                d.text((b[0] + 28 + 2 * self.FS * 0.6, y), s[2:], font=font(self.FS, wgt, True),
                       fill=rgba(c))
            else:
                d.text((b[0] + 28, y), s, font=font(self.FS, wgt, True), fill=rgba(c))
            y += self.LINE_H
        # cursor
        if int(t * 2) % 2 == 0:
            last = shown[-1]
            cx = b[0] + 28 + tsize(font(self.FS, "Regular", True), last + "x")[0] - \
                tsize(font(self.FS, "Regular", True), "x")[0] + 6
            if last.startswith("$ ") and last != "$ ":
                pass
            d.rectangle((cx, y - self.LINE_H + 4, cx + 13, y - 4), fill=rgba(INK, 0.8))
        img.alpha_composite(lay)
        step_label(img, "STEP 2", "Option B: direct API (Grok) + deterministic validator",
                   GREEN)
        captions_at(img, t, self.cues)
        return img


class After:
    CROP = (0.0, 0.15, 1.0, 0.885)
    T0, T_STEP = 1.2, 2.0

    def __init__(self):
        real = Image.open(IMG / "after.png").convert("RGB")
        w, h = real.size
        c = (int(self.CROP[0] * w), int(self.CROP[1] * h), int(self.CROP[2] * w),
             int(self.CROP[3] * h))
        self.c = c
        crop = real.crop(c)
        cw, ch = crop.size
        self.scale = min(1760 / cw, 740 / ch)
        self.box = ((W - cw * self.scale) / 2, 150, (W + cw * self.scale) / 2,
                    150 + ch * self.scale)
        self.base = canvas()
        paste_card(self.base, crop, self.box)
        greens = find_boxes(real, GREEN)
        ambers = find_boxes(real, AMBER)
        # the dropped duplicate row: cells filled with #eeeeee
        a = np.asarray(real).astype(int)
        m = (np.abs(a - np.array((238, 238, 238))).sum(axis=2) < 4)
        m[int(0.85 * h):] = False
        rows = np.nonzero(m.sum(axis=1) > 0.3 * w)[0]
        cols = np.nonzero(m[rows].sum(axis=0) > 0.5 * len(rows))[0]
        grey = (cols.min(), rows.min(), cols.max(), rows.max())
        g = nearest_box(greens, (693 / 1024, 160 / 593), real.size)     # location_id fixed
        am = nearest_box(ambers, (549 / 1024, 187 / 593), real.size)    # orphan parent held
        self.items = [
            (g, "fixed automatically", GREEN, (-60, -150),
             "Green: fixed by a rule that is certain (date parsed, location normalised)"),
            (am, "needs review: held out of the clean load", AMBER, (-60, 128),
             "Amber: held for a person, with the reason, never loaded with a guess"),
            (grey, "dropped duplicate", GREY, None,
             "Grey: exact duplicate kept out of the load"),
        ]
        self.duration = self.T0 + len(self.items) * self.T_STEP + 2.6

    def _map(self, b):
        x0, y0 = self.box[0], self.box[1]
        return (x0 + (b[0] - self.c[0]) * self.scale, y0 + (b[1] - self.c[1]) * self.scale,
                x0 + (b[2] - self.c[0]) * self.scale, y0 + (b[3] - self.c[1]) * self.scale)

    def __call__(self, t):
        img = self.base.copy()
        lay = layer()
        d = ImageDraw.Draw(lay)
        cue = "The clean target schema: every cell is untouched, fixed by a certain rule, " \
              "or held"
        for k, (b, lab, col, off, cap) in enumerate(self.items):
            t0 = self.T0 + k * self.T_STEP
            a = ease((t - t0) / 0.3)
            if a <= 0:
                continue
            if t < self.T0 + len(self.items) * self.T_STEP:
                cue = cap
            r = self._map(b)
            if off is None:      # to the right of the table, level with the row
                c = (r[2] + 200, (r[1] + r[3]) / 2)
            else:
                c = ((r[0] + r[2]) / 2 + off[0], (r[1] + r[3]) / 2 + off[1])
            callout(d, r, lab, col, a, c)
        if t >= self.T0 + len(self.items) * self.T_STEP:
            cue = "Every cell is untouched, fixed by a certain rule, or held for a person"
        img.alpha_composite(lay)
        step_label(img, "STEP 3", "After", BLUE)
        caption(img, cue)
        return img


class Results:
    duration = 10.0

    def __init__(self, nums: dict, detected: int, corruptions: int):
        n = nums
        self.tiles = [
            (f"{detected:,} / {corruptions:,}", f"injected corruptions caught "
             f"({detected / corruptions:.1%})", GREEN),
            (f"{n['rows_ok']:,} / {n['rows']:,}", "rows load identical to ground truth", GREEN),
            (f"{n['broken']}", "correct cells changed by the LLM", GREEN),
            (f"{n['cols_ok']} / {n['cols']}", "columns mapped correctly by the LLM", BLUE),
            (f"${n['cost']:.2f}", f"per full run ({n['model']}, provider-reported)", BLUE),
            (f"≈ {n['wall_min']} min", "per live run; replay in CI takes seconds", BLUE),
        ]
        self.silent = (n["silent_before"], n["silent_after"])

    def __call__(self, t):
        img = canvas()
        lay = layer()
        d = ImageDraw.Draw(lay)
        text(d, (W / 2, 150), "Results on the sample export", font(56, "Bold"), INK,
             ease(t / 0.5), "mm")
        text(d, (W / 2, 210), "every number from docs/eval.md, re-checked in CI from the "
             "recorded responses", font(26), MUTED, ease(t / 0.5), "mm")
        tw, th, gx, gy = 520, 210, 40, 36
        x0 = (W - 3 * tw - 2 * gx) / 2
        for k, (big, small, col) in enumerate(self.tiles):
            a = ease((t - 0.5 - 0.3 * k) / 0.4)
            if a <= 0:
                continue
            x = x0 + (k % 3) * (tw + gx)
            y = 270 + (k // 3) * (th + gy) + 16 * (1 - a)
            d.rounded_rectangle((x, y, x + tw, y + th), radius=18, fill=rgba(PANEL, a))
            d.rounded_rectangle((x, y, x + 8, y + th), radius=4, fill=rgba(col, a))
            text(d, (x + tw / 2 + 4, y + 88), big, font(68, "Bold"), INK, a, "mm")
            text(d, (x + tw / 2 + 4, y + 158), small, font(25, "Medium"), MUTED, a, "mm")
        a = ease((t - 3.0) / 0.5)
        text(d, (W / 2, 800), f"Cells still loaded with a wrong value: {self.silent[1]} "
             f"(rules only: {self.silent[0]}), almost all day/month dates the data cannot settle",
             font(28, "Medium"), INK, a, "mm")
        a = ease((t - 4.0) / 0.6)
        d.rounded_rectangle((W / 2 - 420, 880, W / 2 + 420, 980), radius=50,
                            fill=rgba(BLUE, a))
        text(d, (W / 2, 930), REPO_URL, font(52, "Bold"), INK, a, "mm")
        img.alpha_composite(lay)
        return img


# --------------------------------------------------------------------------
# Timeline and encoding
# --------------------------------------------------------------------------

@dataclass
class Timeline:
    sections: list

    def __post_init__(self):
        self.starts, t = [], 0.0
        for k, s in enumerate(self.sections):
            self.starts.append(t)
            t += s.duration - (XFADE if k < len(self.sections) - 1 else 0)
        self.duration = t

    def frame(self, T: float) -> Image.Image:
        act = [(k, T - st) for k, (st, s) in enumerate(zip(self.starts, self.sections))
               if st <= T < st + s.duration]
        if not act:
            act = [(len(self.sections) - 1, self.sections[-1].duration - 1e-3)]
        img = self.sections[act[0][0]](act[0][1])
        if len(act) > 1:
            nxt = self.sections[act[1][0]](act[1][1])
            img = Image.blend(img, nxt, ease(act[1][1] / XFADE))
        return img.convert("RGB")


def encode_mp4(frames, n: int, path: Path, crf: int) -> None:
    p = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-an", "-c:v", "libx264",
         "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p", "-profile:v", "high",
         "-movflags", "+faststart", str(path)], stdin=subprocess.PIPE)
    for k in range(n):
        p.stdin.write(frames(k).tobytes())
        if k % (FPS * 5) == 0:
            print(f"  mp4 {k / FPS:5.1f}s / {n / FPS:.1f}s", flush=True)
    p.stdin.close()
    if p.wait() != 0:
        raise SystemExit("ffmpeg failed")


def encode_gif(frames: list[Image.Image], fps: int, path: Path, tmp: Path) -> None:
    d = tmp / "gif"
    d.mkdir(exist_ok=True)
    for k, im in enumerate(frames):
        im.save(d / f"{k:05d}.png")
    pal = tmp / "palette.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", str(fps), "-i",
                    str(d / "%05d.png"), "-vf", "palettegen=max_colors=128:stats_mode=full",
                    str(pal)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", str(fps), "-i",
                    str(d / "%05d.png"), "-i", str(pal), "-lavfi",
                    "paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
                    "-loop", "0", str(path)], check=True)


def gif_plan(tl: Timeline, a: OptionA, b: OptionB) -> list[tuple[int, float, float, float]]:
    """(section, local start, local end, speed): highlights, under 30 s in total."""
    title, before, opt_a, opt_b, after, results = range(6)
    sa = a.starts
    seg = {k: (sa[k], sa[k + 1]) for k in range(len(a.segs))}
    R = b.t_run
    return [
        (title, 1.4, 3.4, 1.0),
        (before, 1.2, tl.sections[before].t_all + 1.6, 2.5),
        (opt_a, seg[3][0] + 0.2, seg[3][1], 1.0),                 # error dialog
        (opt_a, seg[7][0], seg[7][1], 1.0),                       # $WORK_O + saved
        (opt_a, a.starts[-1], a.duration - 0.6, 1.0),             # 1 of 6 correct
        (opt_b, R - 0.3, R + 4.9, 1.0),                           # terminal, real time
        (opt_b, b.duration - 2.4, b.duration - 0.8, 1.0),         # ...cut to the score
        (after, 1.0, tl.sections[after].duration - 1.5, 2.0),
        (results, 1.5, 5.5, 1.0),
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--raw", type=Path, default=RAW_DEFAULT, help="computer-use recording")
    ap.add_argument("--crf", type=int, default=25)
    ap.add_argument("--no-gif", action="store_true")
    ap.add_argument("--frames", type=float, nargs="*", default=None,
                    help="only write PNG stills at these times (seconds) to docs/media/")
    args = ap.parse_args()
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found on PATH")
    MEDIA.mkdir(parents=True, exist_ok=True)
    nums = eval_numbers()
    print("recording `" + " ".join(RUN_CMD) + "` (replay) ...", flush=True)
    run = record_run()
    rows_total = int(re.search(r"([\d,]+) rows", next(
        s for _, s in run.lines if s.startswith("> map"))).group(1).replace(",", ""))
    with tempfile.TemporaryDirectory(prefix="onboard_demo_") as td:
        tmp = Path(td)
        clip = RawClip(args.raw, tmp)
        opt_a, opt_b = OptionA(clip), OptionB(run, nums)
        tl = Timeline([Title(), Before(tmp, nums, rows_total), opt_a, opt_b, After(),
                       Results(nums, opt_b.detected, opt_b.corruptions)])
        print(f"timeline {tl.duration:.1f}s: " + ", ".join(
            f"{type(s).__name__} {st:.1f}s" for s, st in zip(tl.sections, tl.starts)))
        if args.frames is not None:
            for T in args.frames:
                p = MEDIA / f"frame_{T:05.1f}s.png"
                tl.frame(T).save(p)
                print(p)
            return
        n = int(tl.duration * FPS)
        mp4 = MEDIA / "demo.mp4"
        encode_mp4(lambda k: tl.frame(k / FPS), n, mp4, args.crf)
        print(f"{mp4} {mp4.stat().st_size / 1e6:.2f} MB, {n / FPS:.1f}s")
        if not args.no_gif:
            gfps, frames = 10, []
            for sec, t0, t1, speed in gif_plan(tl, opt_a, opt_b):
                st = tl.starts[sec]
                k = 0
                while t0 + k * speed / gfps < t1:
                    T = st + t0 + k * speed / gfps
                    frames.append(tl.frame(T).resize((960, 540), Image.LANCZOS))
                    k += 1
            gif = MEDIA / "demo.gif"
            encode_gif(frames, gfps, gif, tmp)
            print(f"{gif} {gif.stat().st_size / 1e6:.2f} MB, {len(frames) / gfps:.1f}s")


if __name__ == "__main__":
    main()
