"""Render the README before/after images from the committed sample data.

    pip install -e ".[docs]"
    python scripts/render_before_after.py

Writes docs/img/before.png, docs/img/after.png and docs/img/before_after.png.
The rows are picked from the validator's findings, so the images always match
what `onboard validate` actually does.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Rectangle  # noqa: E402

from onboard.corrupt import read_legacy_xlsx  # noqa: E402
from onboard.mapping import reference_mapping  # noqa: E402
from onboard.validate import validate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "img"

RED, RED_EDGE = "#fde2e1", "#d93025"
GREEN, GREEN_EDGE = "#dff3e3", "#1e8e3e"
AMBER, AMBER_EDGE = "#fff1cc", "#e8a200"
GREY = "#eeeeee"
HEADER = "#f1f3f4"
GRID = "#c9ccd1"
INK = "#202124"
MUTED = "#5f6368"

FONT = 10.5          # pt
ROW_H = 0.34         # in
CHAR_W = 0.083       # in per character at FONT (monospace body)
HEAD_W = 0.118       # in per character at FONT (bold sans headers)
PAD = 0.14           # in

# sheet -> (legacy columns shown, matching target columns)
COLUMNS = {
    "EQUIPMENT": (["EQUIP_NO", "EQUIP_DESC", "PARENT_EQ", "LOC_CD", "INSTALL_DT"],
                  ["asset_id", "description", "parent_asset_id", "location_id",
                   "install_date"]),
    "PM_SCHED": (["PM_NO", "EQUIP_NO", "PM_FREQ", "FREQ_UOM", "LAST_DONE_DT"],
                 ["pm_id", "asset_id", "frequency_value", "frequency_unit", "last_done_date"]),
    "WORK_ORDERS": (["WO_NUM", "EQUIP_NO", "OPEN_DT", "COMP_DT", "LABOR_HRS"],
                    ["wo_id", "asset_id", "reported_date", "completed_date", "labor_hours"]),
}

# Which rows to show: for each sheet, rows whose findings contain these checks.
PICKS = {
    "EQUIPMENT": [{"excel_serial", "location_format"}, {"orphan_parent", "swapped_date"},
                  {"near_duplicate"}],
    "PM_SCHED": [{"free_text_frequency", "swapped_date"}, {"unknown_code"}],
    "WORK_ORDERS": [{"labor_outlier", "excel_serial"}, {"exact_duplicate"},
                    {"whitespace", "iso_date"}],
}


def show(v) -> str:
    """Make whitespace visible and keep cells short."""
    s = "" if v is None else str(v)
    lead = len(s) - len(s.lstrip(" "))
    core = s.strip(" ")
    trail = len(s) - lead - len(core)
    s = "·" * lead + core.replace("\t", "⇥") + "·" * trail
    if s.endswith("\t"):
        s = s[:-1] + "⇥"
    return s if len(s) <= 30 else s[:28] + "…"


def pick_rows(result, corruptions):
    ex = result.exceptions()
    ex = ex[ex.row_index.notna()]
    by_row = ex.groupby(["sheet", "row_index"]).check.apply(set)
    picked = {}
    for sheet, wants in PICKS.items():
        rows = []
        for want in wants:
            cands = sorted(i for (s, i), checks in by_row.items()
                           if s == sheet and want <= checks and i not in rows)
            if cands:
                rows.append(int(cands[0]))
        picked[sheet] = rows
    return picked


def build_blocks(result, legacy, corruptions, picked):
    """Return [(sheet, before_header, after_header, [(before_cells, after_cells)])]."""
    ex = result.exceptions()
    blocks = []
    for sheet, rows in picked.items():
        lcols, tcols = COLUMNS[sheet]
        table = reference_mapping()["sheets"][sheet]["table"]
        staged = result.staged[table]
        status = result.status[table]
        out_rows = []
        for i in rows:
            bad = set(corruptions[(corruptions.sheet == sheet)
                                  & (corruptions.row_index == i)].field)
            before = [(str(i + 2), None)] + [
                (show(legacy[sheet].at[i, c]), "bad" if c in bad else None) for c in lcols]
            f = ex[(ex.sheet == sheet) & (ex.row_index == i)]
            after = [(str(i + 2), None)]
            for lc, tc in zip(lcols, tcols):
                ff = f[f.target_field == tc]
                val = staged.at[i, tc]
                if status[i] == "dropped":
                    after.append(("", "dropped"))
                    continue
                if (ff.action == "review").any():
                    r = ff[ff.action == "review"].iloc[0]
                    orig = r.original if r.original else "(blank)"
                    text = f"{orig} → {r.value}?" if r.value else f"{orig} ?"
                    after.append((show(text), "review"))
                elif (ff.action == "fixed").any():
                    after.append((show(val), "fixed"))
                else:
                    after.append((show(val), None))
            label = {"ok": "loaded", "review": "needs review", "held": "held",
                     "dropped": "duplicate, dropped"}[status[i]]
            after.append((label, {"ok": "fixed" if (f.action == "fixed").any() else None,
                                  "review": "review", "held": "review",
                                  "dropped": "dropped"}[status[i]]))
            before.append(("", None))
            out_rows.append((before, after))
        blocks.append((sheet, ["#"] + lcols, ["#"] + tcols + ["result"], out_rows))
    return blocks


def col_widths(blocks, side):
    """Shared column widths per sheet so before/after line up."""
    widths = {}
    for sheet, bh, ah, rows in blocks:
        header = bh if side == "before" else ah
        n = len(header)
        w = [len(h) * HEAD_W for h in header]
        for before, after in rows:
            cells = before if side == "before" else after
            for k in range(n):
                w[k] = max(w[k], len(cells[k][0]) * CHAR_W if k < len(cells) else 0)
        widths[sheet] = [max(3 * CHAR_W, x) + 2 * PAD for x in w]
    return widths


def draw_side(ax, x0, y0, blocks, widths, side, title, subtitle):
    fills = {"bad": (RED, RED_EDGE), "fixed": (GREEN, GREEN_EDGE),
             "review": (AMBER, AMBER_EDGE), "dropped": (GREY, GRID)}
    ax.text(x0, y0, title, fontsize=FONT + 5, fontweight="bold", color=INK, va="top")
    ax.text(x0, y0 + 0.36, subtitle, fontsize=FONT, color=MUTED, va="top")
    y = y0 + 0.85
    for sheet, bh, ah, rows in blocks:
        header = bh if side == "before" else ah
        ws = widths[sheet]
        label = sheet if side == "before" else {"EQUIPMENT": "assets",
                                                 "PM_SCHED": "pm_schedules",
                                                 "WORK_ORDERS": "work_orders"}[sheet]
        tab_w = len(label) * HEAD_W * 0.85 + 2 * PAD
        ax.add_patch(FancyBboxPatch((x0, y), tab_w, ROW_H * 0.9,
                                    boxstyle="round,pad=0,rounding_size=0.05",
                                    fc="#1a73e8" if side == "after" else "#3c4043", ec="none"))
        ax.text(x0 + PAD, y + ROW_H * 0.45, label, fontsize=FONT - 1, color="white",
                fontweight="bold", va="center")
        y += ROW_H * 0.9
        cells_rows = [[(h, "header") for h in header]] + [
            (b if side == "before" else a) for b, a in rows]
        for r, cells in enumerate(cells_rows):
            x = x0
            for k, w in enumerate(ws):
                text, kind = cells[k] if k < len(cells) else ("", None)
                if kind == "header":
                    fc, ec, weight, color = HEADER, GRID, "bold", INK
                elif kind in fills:
                    fc, ec = fills[kind]
                    weight, color = "normal", INK
                else:
                    fc, ec, weight, color = "white", GRID, "normal", INK
                if k == 0:
                    fc, color = HEADER, MUTED
                    weight = "bold" if kind == "header" else "normal"
                ax.add_patch(Rectangle((x, y), w, ROW_H, fc=fc, ec=GRID, lw=0.6))
                if kind in fills and kind != "dropped":
                    ax.add_patch(Rectangle((x + 0.02, y + 0.02), w - 0.04, ROW_H - 0.04,
                                           fc="none", ec=ec, lw=1.4))
                if kind == "dropped" and k > 0 and not text:
                    ax.plot([x + PAD, x + w - PAD], [y + ROW_H / 2] * 2, color="#9aa0a6",
                            lw=1)
                ax.text(x + PAD, y + ROW_H / 2, text, fontsize=FONT, color=color,
                        fontweight=weight, va="center", ha="left",
                        family="DejaVu Sans Mono" if kind != "header" and k > 0
                        else "DejaVu Sans")
                x += w
            y += ROW_H
        y += 0.32
    return y


def legend(ax, x, y, items):
    for fc, ec, label in items:
        ax.add_patch(Rectangle((x, y), 0.28, 0.22, fc=fc, ec=ec, lw=1.4))
        ax.text(x + 0.38, y + 0.11, label, fontsize=FONT, color=INK, va="center")
        x += 0.38 + len(label) * CHAR_W * 0.95 + 0.4
    return y + 0.4


BEFORE_LEGEND = [(RED, RED_EDGE, "problem cell (logged in the answer key)")]
AFTER_LEGEND = [(GREEN, GREEN_EDGE, "fixed automatically"),
                (AMBER, AMBER_EDGE, "needs review (held out of the clean load)"),
                (GREY, GRID, "dropped duplicate")]
BEFORE_TITLE = ("Before: the legacy export",
                "legacy_export.xlsx, as the old CMMS produced it ( · = stray space )")
AFTER_TITLE = ("After: onboard validate",
               "clean target schema: rule-based fixes applied, the rest routed to a person")


def table_width(widths):
    return max(sum(w) for w in widths.values())


def render_single(blocks, side, path):
    widths = col_widths(blocks, side)
    W = table_width(widths) + 0.8
    n_rows = sum(len(b[3]) + 1 for b in blocks)
    H = 0.85 + n_rows * ROW_H + len(blocks) * (0.9 * ROW_H + 0.32) + 1.0
    fig = plt.figure(figsize=(W, H), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    ax.axis("off")
    title, sub = BEFORE_TITLE if side == "before" else AFTER_TITLE
    y = draw_side(ax, 0.4, 0.35, blocks, widths, side, title, sub)
    legend(ax, 0.4, y, BEFORE_LEGEND if side == "before" else AFTER_LEGEND)
    fig.savefig(path, dpi=200, facecolor="white")
    plt.close(fig)


def render_combined(blocks, path):
    wb, wa = col_widths(blocks, "before"), col_widths(blocks, "after")
    gap = 0.9
    W = 0.4 + table_width(wb) + gap + table_width(wa) + 0.4
    n_rows = sum(len(b[3]) + 1 for b in blocks)
    H = 0.85 + n_rows * ROW_H + len(blocks) * (0.9 * ROW_H + 0.32) + 1.0
    fig = plt.figure(figsize=(W, H), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    ax.axis("off")
    xa = 0.4 + table_width(wb) + gap
    y1 = draw_side(ax, 0.4, 0.35, blocks, wb, "before", *BEFORE_TITLE)
    draw_side(ax, xa, 0.35, blocks, wa, "after", *AFTER_TITLE)
    ax.annotate("", xy=(xa - 0.2, H / 2), xytext=(xa - gap + 0.2, H / 2),
                arrowprops=dict(arrowstyle="-|>", lw=2.5, color="#1a73e8",
                                mutation_scale=25))
    legend(ax, 0.4, y1, BEFORE_LEGEND)
    legend(ax, xa, y1, AFTER_LEGEND)
    fig.savefig(path, dpi=200, facecolor="white")
    plt.close(fig)


def main() -> None:
    legacy = read_legacy_xlsx(ROOT / "data" / "legacy" / "legacy_export.xlsx")
    result = validate(legacy, reference_mapping(), date(2026, 9, 30))
    corruptions = pd.read_csv(ROOT / "data" / "ground_truth" / "corruptions.csv", dtype=str,
                              keep_default_na=False)
    corruptions["row_index"] = corruptions.row_index.astype(int)
    picked = pick_rows(result, corruptions)
    blocks = build_blocks(result, legacy, corruptions, picked)
    OUT.mkdir(parents=True, exist_ok=True)
    render_single(blocks, "before", OUT / "before.png")
    render_single(blocks, "after", OUT / "after.png")
    render_combined(blocks, OUT / "before_after.png")
    for name in ("before.png", "after.png", "before_after.png"):
        print(OUT / name)


if __name__ == "__main__":
    main()
