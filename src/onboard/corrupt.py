"""Turn the clean dataset into a realistic legacy CMMS export, with an answer key.

Two layers of difference between the legacy export and the clean target:

1. **Baseline legacy conventions** (systematic, apply to every row): legacy
   column names (EQUIP_NO, LOC_CD, PM_FREQ, ...), legacy codes (criticality
   A/B/C, status A/I, meter HRS/CYC, frequency UOM D/H/KM), and DD/MM/YYYY
   dates. These are documented once in ``column_mapping.csv``.
2. **Injected corruptions** (row-level, random but seeded): duplicates,
   orphans, mixed date formats, free-text frequencies, unit mix-ups, location
   typos, blanks, stray whitespace. Every single one is logged in
   ``corruptions.csv`` with sheet, row, field, original value and corrupted value.

Because the mess is made from a known-good dataset, ground truth is free.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from onboard.generate import DEFAULT_SEED, read_tables

EXCEL_EPOCH = date(1899, 12, 30)

# --------------------------------------------------------------------------
# Baseline legacy conventions
# --------------------------------------------------------------------------

CLASS_CODES = {
    "CONVEYOR": "CONV", "CRUSHER": "CRSH", "HAUL_TRUCK": "HTRK", "PUMP": "PUMP",
    "BESS": "BESS", "SCREEN": "SCRN", "FEEDER": "FEED", "COMPRESSOR": "COMP",
    "TRANSFORMER": "XFMR", "FAN": "FAN", "STACKER_RECLAIMER": "STKR", "MOTOR": "MOTR",
    "GEARBOX": "GBOX", "PULLEY": "PULY", "BELT": "BELT", "LUBE_SYSTEM": "LUBE",
    "HPU": "HPU", "ENGINE": "ENGN", "TRANSMISSION": "TRNS", "FINAL_DRIVE": "FDRV",
    "PCS": "PCS", "BATTERY_CONTAINER": "BATT", "HVAC": "HVAC", "FIRE_SUPPRESSION": "FIRE",
}
CRIT_CODES = {1: "A", 2: "B", 3: "C"}
STATUS_CODES = {"ACTIVE": "A", "INACTIVE": "I"}
METER_CODES = {"HOURS": "HRS", "KM": "KM", "CYCLES": "CYC", "MWH": "MWH", "NONE": "N/A"}
UOM_CODES = {"DAYS": "D", "HOURS": "H", "KM": "KM", "CYCLES": "CYC", "MWH": "MWH"}
TRIGGER_CODES = {"TIME": "T", "METER": "M"}
LOC_TYPE_CODES = {"SITE": "SITE", "AREA": "AREA", "SYSTEM": "SYS"}
WO_STATUS_CODES = {"OPEN": "WAPPR", "IN_PROGRESS": "INPRG", "COMPLETED": "COMP",
                   "CANCELLED": "CAN"}

# sheet -> [(legacy column, target table, target column, transform note)]
COLUMN_MAP: dict[str, tuple[str, list[tuple[str, str, str]]]] = {
    "LOCATIONS": ("locations", [
        ("LOC_CD", "location_id", "as is"),
        ("LOC_DESC", "name", "as is"),
        ("PARENT_LOC", "parent_location_id", "as is"),
        ("SITE_CD", "site_id", "as is"),
        ("LOC_TYPE", "location_type", f"code map {LOC_TYPE_CODES}"),
    ]),
    "EQUIPMENT": ("assets", [
        ("EQUIP_NO", "asset_id", "as is"),
        ("EQUIP_DESC", "description", "as is"),
        ("EQ_CLASS", "asset_class", "4-letter legacy class code, see CLASS_CODES"),
        ("PARENT_EQ", "parent_asset_id", "as is"),
        ("LOC_CD", "location_id", "as is"),
        ("CRIT", "criticality", f"code map {CRIT_CODES}"),
        ("MFR", "manufacturer", "as is"),
        ("MODEL_NO", "model", "as is"),
        ("SERIAL_NO", "serial_number", "as is"),
        ("INSTALL_DT", "install_date", "DD/MM/YYYY"),
        ("METER_TYPE", "meter_type", f"code map {METER_CODES}"),
        ("EQ_STATUS", "status", f"code map {STATUS_CODES}"),
    ]),
    "PM_SCHED": ("pm_schedules", [
        ("PM_NO", "pm_id", "as is"),
        ("EQUIP_NO", "asset_id", "as is"),
        ("PM_DESC", "description", "as is"),
        ("PM_TYPE", "trigger_type", f"code map {TRIGGER_CODES}"),
        ("PM_FREQ", "frequency_value", "number"),
        ("FREQ_UOM", "frequency_unit", f"code map {UOM_CODES}"),
        ("CRAFT", "craft", "as is"),
        ("EST_HRS", "est_labor_hours", "number (hours)"),
        ("LAST_DONE_DT", "last_done_date", "DD/MM/YYYY"),
    ]),
    "WORK_ORDERS": ("work_orders", [
        ("WO_NUM", "wo_id", "as is"),
        ("EQUIP_NO", "asset_id", "as is"),
        ("PM_NO", "pm_id", "as is"),
        ("WO_TYPE", "wo_type", "as is"),
        ("WO_STATUS", "status", f"code map {WO_STATUS_CODES}"),
        ("PRIORITY", "priority", "as is"),
        ("WO_DESC", "description", "as is"),
        ("OPEN_DT", "reported_date", "DD/MM/YYYY"),
        ("SCHED_DT", "scheduled_date", "DD/MM/YYYY"),
        ("COMP_DT", "completed_date", "DD/MM/YYYY"),
        ("LABOR_HRS", "labor_hours", "number (hours)"),
    ]),
}

DATE_FIELDS = {"INSTALL_DT", "LAST_DONE_DT", "OPEN_DT", "SCHED_DT", "COMP_DT"}

# value -> unit -> free-text spellings seen in real legacy systems
FREE_TEXT_FREQ: dict[tuple[int, str], list[str]] = {
    (7, "DAYS"): ["weekly", "1W", "7d", "every 1 wk", "Weekly"],
    (14, "DAYS"): ["fortnightly", "2W", "every 2 wks", "14 days"],
    (30, "DAYS"): ["monthly", "1M", "30d", "every month", "Monthly"],
    (90, "DAYS"): ["quarterly", "3M", "90 days", "every 3 months"],
    (180, "DAYS"): ["6M", "semi-annual", "every 6 months", "180d"],
    (365, "DAYS"): ["annual", "1Y", "yearly", "12M", "Annually"],
}
UOM_MIXUP = {"D": "W", "H": "D", "KM": "MI", "CYC": "H", "MWH": "KWH"}


def _fmt_num(x) -> str:
    return f"{float(x):g}"


def _legacy_date(iso: str | None) -> str:
    if not iso:
        return ""
    d = date.fromisoformat(iso)
    return f"{d:%d/%m/%Y}"


def _free_text_options(value: int, unit: str) -> list[str]:
    if (value, unit) in FREE_TEXT_FREQ:
        return FREE_TEXT_FREQ[(value, unit)]
    v = f"{value:,}"
    if unit == "HOURS":
        return [f"{value} hrs", f"{v} h", f"every {value} hours", f"{value}H", f"{value} run hrs"]
    if unit == "KM":
        return [f"{v} km", f"{value // 1000}k km", f"every {v} km"]
    if unit == "CYCLES":
        return [f"{value} cycles", f"{value} cyc"]
    if unit == "MWH":
        return [f"{value} MWh", f"every {value} MWh"]
    return [f"{value} {unit.lower()}"]


# --------------------------------------------------------------------------
# Baseline export
# --------------------------------------------------------------------------

def to_legacy(clean: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Render the clean tables in legacy conventions (no corruptions yet)."""
    s = lambda v: "" if v is None or (isinstance(v, float) and np.isnan(v)) else str(v)  # noqa: E731

    loc = clean["locations"]
    locations = pd.DataFrame({
        "LOC_CD": loc.location_id, "LOC_DESC": loc.name,
        "PARENT_LOC": loc.parent_location_id.map(s), "SITE_CD": loc.site_id,
        "LOC_TYPE": loc.location_type.map(LOC_TYPE_CODES),
    })
    a = clean["assets"]
    equipment = pd.DataFrame({
        "EQUIP_NO": a.asset_id, "EQUIP_DESC": a.description,
        "EQ_CLASS": a.asset_class.map(CLASS_CODES), "PARENT_EQ": a.parent_asset_id.map(s),
        "LOC_CD": a.location_id, "CRIT": a.criticality.map(CRIT_CODES),
        "MFR": a.manufacturer, "MODEL_NO": a.model, "SERIAL_NO": a.serial_number,
        "INSTALL_DT": a.install_date.map(_legacy_date),
        "METER_TYPE": a.meter_type.map(METER_CODES), "EQ_STATUS": a.status.map(STATUS_CODES),
    })
    p = clean["pm_schedules"]
    pm = pd.DataFrame({
        "PM_NO": p.pm_id, "EQUIP_NO": p.asset_id, "PM_DESC": p.description,
        "PM_TYPE": p.trigger_type.map(TRIGGER_CODES), "PM_FREQ": p.frequency_value.map(str),
        "FREQ_UOM": p.frequency_unit.map(UOM_CODES), "CRAFT": p.craft,
        "EST_HRS": p.est_labor_hours.map(_fmt_num),
        "LAST_DONE_DT": p.last_done_date.map(_legacy_date),
    })
    w = clean["work_orders"]
    wo = pd.DataFrame({
        "WO_NUM": w.wo_id, "EQUIP_NO": w.asset_id, "PM_NO": w.pm_id.map(s),
        "WO_TYPE": w.wo_type, "WO_STATUS": w.status.map(WO_STATUS_CODES),
        "PRIORITY": w.priority.map(str), "WO_DESC": w.description,
        "OPEN_DT": w.reported_date.map(_legacy_date),
        "SCHED_DT": w.scheduled_date.map(_legacy_date),
        "COMP_DT": w.completed_date.map(_legacy_date),
        "LABOR_HRS": w.labor_hours.map(_fmt_num),
    })
    out = {"LOCATIONS": locations, "EQUIPMENT": equipment, "PM_SCHED": pm, "WORK_ORDERS": wo}
    return {k: v.astype(object) for k, v in out.items()}


# --------------------------------------------------------------------------
# Corruption engine
# --------------------------------------------------------------------------

@dataclass
class SheetState:
    name: str
    df: pd.DataFrame
    record_ids: list[str]                 # clean PK behind each row
    kinds: list[str]                      # original / exact_duplicate / near_duplicate
    dup_of: list[int | None]              # row index of the source row
    protected: set[int] = field(default_factory=set)
    used: set[tuple[int, str]] = field(default_factory=set)


class Corruptor:
    def __init__(self, clean: dict[str, pd.DataFrame], seed: int = DEFAULT_SEED):
        self.clean = clean
        self.rng = np.random.default_rng(seed)
        self.log: list[dict] = []
        legacy = to_legacy(clean)
        pk = {"LOCATIONS": "location_id", "EQUIPMENT": "asset_id",
              "PM_SCHED": "pm_id", "WORK_ORDERS": "wo_id"}
        self.sheets: dict[str, SheetState] = {}
        for name, df in legacy.items():
            table = COLUMN_MAP[name][0]
            ids = clean[table][pk[name]].tolist()
            self.sheets[name] = SheetState(name, df.reset_index(drop=True), ids,
                                           ["original"] * len(df), [None] * len(df))
        self.all_asset_ids = set(clean["assets"].asset_id)
        self._target = {sheet: {lc: tc for lc, tc, _ in cols}
                        for sheet, (_, cols) in COLUMN_MAP.items()}

    # ---- helpers ---------------------------------------------------------
    def _choice(self, seq):
        return seq[int(self.rng.integers(len(seq)))]

    def _clean_value(self, sheet: SheetState, row: int, col: str) -> str:
        table = COLUMN_MAP[sheet.name][0]
        tcol = self._target[sheet.name][col]
        ids = self.clean[table].iloc[:, 0]
        rec = sheet.record_ids[row]
        v = self.clean[table].loc[ids == rec, tcol].iloc[0]
        return "" if v is None or (isinstance(v, float) and np.isnan(v)) else str(v)

    def _record(self, sheet: SheetState, row: int, col: str, original, corrupted,
                ctype: str, detail: str = "") -> None:
        self.log.append({
            "sheet": sheet.name,
            "row_index": row,
            "excel_row": row + 2,  # header is Excel row 1
            "record_id": sheet.record_ids[row],
            "field": col,
            "target_table": COLUMN_MAP[sheet.name][0],
            "target_field": self._target[sheet.name][col],
            "clean_value": self._clean_value(sheet, row, col),
            "original_value": str(original),
            "corrupted_value": str(corrupted),
            "corruption_type": ctype,
            "detail": detail,
        })

    def _pick(self, sheet: SheetState, col: str, n: int, where=None,
              exclude_rows: set[int] | None = None) -> list[int]:
        """Pick up to n untouched, non-blank, unprotected cells in a column."""
        exclude_rows = exclude_rows or set()
        cand = [i for i in range(len(sheet.df))
                if i not in sheet.protected and i not in exclude_rows
                and (i, col) not in sheet.used and sheet.df.at[i, col] != ""
                and (where is None or where(i))]
        if not cand:
            return []
        k = min(n, len(cand))
        return sorted(int(x) for x in self.rng.choice(cand, size=k, replace=False))

    def _set(self, sheet: SheetState, row: int, col: str, new, ctype: str,
             detail: str = "") -> bool:
        old = sheet.df.at[row, col]
        if str(new) == str(old):
            return False
        sheet.df.at[row, col] = new
        sheet.used.add((row, col))
        self._record(sheet, row, col, old, new, ctype, detail)
        return True

    # ---- row-level: duplicates ------------------------------------------
    def duplicate_rows(self, sheet_name: str, n_exact: int, n_near: int) -> None:
        sheet = self.sheets[sheet_name]
        df = sheet.df
        n = len(df)
        sources = self.rng.choice(n, size=n_exact + n_near, replace=False)
        keys = list(np.arange(n, dtype=float))
        rows = [df.iloc[i].to_dict() for i in range(n)]
        meta = [("original", None, sheet.record_ids[i]) for i in range(n)]
        pending = []  # (meta idx of dup, changes)
        for j, src in enumerate(sources):
            src = int(src)
            row = dict(rows[src])
            kind = "exact_duplicate" if j < n_exact else "near_duplicate"
            changes = {}
            if kind == "near_duplicate":
                key_col = df.columns[0]
                changes[key_col] = self._near_id(row[key_col])
                desc_col = df.columns[1] if sheet_name != "WORK_ORDERS" else "WO_DESC"
                nd = self._near_desc(row[desc_col])
                if nd != row[desc_col]:
                    changes[desc_col] = nd
                row.update(changes)
            rows.append(row)
            keys.append(src + float(self.rng.uniform(0.5, min(40.0, n - src))))
            meta.append((kind, src, sheet.record_ids[src]))
            pending.append((len(rows) - 1, changes, src))

        order = sorted(range(len(rows)), key=lambda k: keys[k])
        new_pos = {old: new for new, old in enumerate(order)}
        sheet.df = pd.DataFrame([rows[k] for k in order], columns=df.columns).astype(object)
        sheet.record_ids = [meta[k][2] for k in order]
        sheet.kinds = [meta[k][0] for k in order]
        sheet.dup_of = [new_pos[meta[k][1]] if meta[k][1] is not None else None for k in order]

        key_col = df.columns[0]
        for old_idx, changes, src in pending:
            r, s = new_pos[old_idx], new_pos[src]
            kind = sheet.kinds[r]
            sheet.protected.update({r, s})
            if kind == "exact_duplicate":
                v = sheet.df.at[r, key_col]
                self._record(sheet, r, key_col, v, v, "exact_duplicate",
                             f"exact copy of excel_row {s + 2}")
            else:
                for col, new in changes.items():
                    orig = sheet.df.at[s, col]
                    sheet.used.add((r, col))
                    self._record(sheet, r, col, orig, new, "near_duplicate",
                                 f"near-duplicate of excel_row {s + 2}")

    def _near_id(self, v: str) -> str:
        variants = [v.replace("-", "", 1), v.lower(), v.replace("-", "_"),
                    v.replace("-", " ", 1), v.replace("-", "/")]
        variants = [x for x in variants if x != v]
        return self._choice(variants)

    def _near_desc(self, v: str) -> str:
        abbrev = {"Drive": "DRV", "motor": "MTR", "Motor": "MTR", "conveyor": "CONV",
                  "Conveyor": "CONV", "pump": "PMP", "Pump": "PMP", "pulley": "PUL",
                  "Belt": "BLT", "Battery": "BATT", "system": "SYS", "transformer": "XFMR"}
        options = [v.upper(), v + " (OLD)", v + " - DO NOT USE", v.replace(" - ", " ")]
        ab = " ".join(abbrev.get(w, w) for w in v.split(" "))
        if ab != v:
            options.append(ab)
        return self._choice([o for o in options if o != v] or [v + " (OLD)"])

    # ---- cell-level corruptions -----------------------------------------
    def date_formats(self, sheet_name: str, col: str, n_mmdd: int, n_iso: int,
                     n_serial: int) -> None:
        sheet = self.sheets[sheet_name]
        for kind, n in (("date_mmdd", n_mmdd), ("date_iso", n_iso),
                        ("date_excel_serial", n_serial)):
            for r in self._pick(sheet, col, n):
                d = date(int(sheet.df.at[r, col][6:]), int(sheet.df.at[r, col][3:5]),
                         int(sheet.df.at[r, col][:2]))
                if kind == "date_mmdd":
                    if d.day == d.month:
                        continue
                    new = f"{d:%m/%d/%Y}"
                    detail = "ambiguous (day<=12)" if d.day <= 12 else "invalid as DD/MM"
                elif kind == "date_iso":
                    new, detail = d.isoformat(), ""
                else:
                    new = (d - EXCEL_EPOCH).days  # stored as a number in the .xlsx
                    detail = "Excel serial day number"
                self._set(sheet, r, col, new, kind, detail)

    def whitespace(self, sheet_name: str, col: str, n: int) -> None:
        sheet = self.sheets[sheet_name]
        for r in self._pick(sheet, col, n):
            v = sheet.df.at[r, col]
            new = self._choice([v + " ", v + "  ", " " + v + " ", v + "   ", v + "\t"])
            self._set(sheet, r, col, new, "whitespace")

    def blanks(self, sheet_name: str, col: str, n: int) -> None:
        sheet = self.sheets[sheet_name]
        for r in self._pick(sheet, col, n):
            self._set(sheet, r, col, "", "blank_required")

    def location_typos(self, sheet_name: str, col: str, n_typo: int, n_case: int) -> None:
        sheet = self.sheets[sheet_name]
        for r in self._pick(sheet, col, n_typo):
            v = sheet.df.at[r, col]
            site, area, num = v.split("-")
            i = int(self.rng.integers(len(area) - 1))
            swapped = area[:i] + area[i + 1] + area[i] + area[i + 2:]
            options = [f"{site}-{swapped}-{num}", f"{site} {area} {num}",
                       f"{site}{area}-{num}", f"{site}-{area}-{int(num)}",
                       f"{site}_{area}_{num}", f"{site}-{area[:-1]}-{num}"]
            self._set(sheet, r, col, self._choice([o for o in options if o != v]),
                      "location_typo")
        for r in self._pick(sheet, col, n_case):
            v = sheet.df.at[r, col]
            new = self._choice([v.lower(), v.title(), v.capitalize()])
            self._set(sheet, r, col, new, "location_casing")

    def orphan_parents(self, n: int) -> None:
        sheet = self.sheets["EQUIPMENT"]
        for r in self._pick(sheet, "PARENT_EQ", n):
            prefix = sheet.df.at[r, "PARENT_EQ"].split("-")[0]
            while True:
                cand = f"{prefix}-{int(self.rng.integers(1900, 2000))}"
                if cand not in self.all_asset_ids:
                    break
            self._set(sheet, r, "PARENT_EQ", cand, "orphan_parent",
                      "parent not present in EQUIPMENT")

    def orphan_references(self, sheet_name: str, n: int) -> None:
        sheet = self.sheets[sheet_name]
        for r in self._pick(sheet, "EQUIP_NO", n):
            prefix = sheet.df.at[r, "EQUIP_NO"].split("-")[0]
            while True:
                cand = f"{prefix}-{int(self.rng.integers(1900, 2000))}"
                if cand not in self.all_asset_ids:
                    break
            self._set(sheet, r, "EQUIP_NO", cand, "orphan_reference",
                      "asset not present in EQUIPMENT")

    def free_text_frequency(self, n: int) -> None:
        sheet = self.sheets["PM_SCHED"]
        uom_to_unit = {v: k for k, v in UOM_CODES.items()}
        for r in self._pick(sheet, "PM_FREQ", n):
            value = int(sheet.df.at[r, "PM_FREQ"])
            unit = uom_to_unit[sheet.df.at[r, "FREQ_UOM"]]
            self._set(sheet, r, "PM_FREQ", self._choice(_free_text_options(value, unit)),
                      "free_text_frequency")

    def frequency_unit_mixup(self, n: int) -> None:
        sheet = self.sheets["PM_SCHED"]
        # don't stack a unit mix-up on a free-text frequency in the same row
        busy = {r for r, c in sheet.used if c == "PM_FREQ"}
        for r in self._pick(sheet, "FREQ_UOM", n, exclude_rows=busy):
            old = sheet.df.at[r, "FREQ_UOM"]
            self._set(sheet, r, "FREQ_UOM", UOM_MIXUP[old], "unit_mixup",
                      f"frequency unit recorded as {UOM_MIXUP[old]} instead of {old}")

    def labor_minutes(self, n: int) -> None:
        sheet = self.sheets["WORK_ORDERS"]
        for r in self._pick(sheet, "LABOR_HRS", n,
                            where=lambda i: float(sheet.df.at[i, "LABOR_HRS"]) > 0):
            hrs = float(sheet.df.at[r, "LABOR_HRS"])
            self._set(sheet, r, "LABOR_HRS", _fmt_num(hrs * 60), "unit_mixup",
                      "labor recorded in minutes instead of hours")

    # ---- recipe ----------------------------------------------------------
    def run(self) -> None:
        # Row insertions first, so every logged row index is final.
        self.duplicate_rows("EQUIPMENT", n_exact=12, n_near=18)
        self.duplicate_rows("WORK_ORDERS", n_exact=15, n_near=0)

        self.whitespace("LOCATIONS", "LOC_CD", 3)

        self.orphan_parents(20)
        self.location_typos("EQUIPMENT", "LOC_CD", n_typo=25, n_case=20)
        self.blanks("EQUIPMENT", "EQUIP_DESC", 10)
        self.blanks("EQUIPMENT", "LOC_CD", 8)
        self.blanks("EQUIPMENT", "EQ_CLASS", 7)
        self.whitespace("EQUIPMENT", "EQUIP_NO", 15)
        self.whitespace("EQUIPMENT", "EQUIP_DESC", 15)
        self.whitespace("EQUIPMENT", "LOC_CD", 10)
        self.date_formats("EQUIPMENT", "INSTALL_DT", 60, 40, 50)

        self.free_text_frequency(300)
        self.frequency_unit_mixup(25)
        self.blanks("PM_SCHED", "PM_DESC", 8)
        self.blanks("PM_SCHED", "EQUIP_NO", 6)
        self.whitespace("PM_SCHED", "EQUIP_NO", 20)
        self.date_formats("PM_SCHED", "LAST_DONE_DT", 60, 40, 40)

        self.orphan_references("WORK_ORDERS", 12)
        self.labor_minutes(40)
        self.blanks("WORK_ORDERS", "OPEN_DT", 15)
        self.blanks("WORK_ORDERS", "WO_TYPE", 10)
        self.blanks("WORK_ORDERS", "EQUIP_NO", 10)
        self.whitespace("WORK_ORDERS", "EQUIP_NO", 40)
        self.whitespace("WORK_ORDERS", "WO_DESC", 30)
        for col in ("OPEN_DT", "SCHED_DT", "COMP_DT"):
            self.date_formats("WORK_ORDERS", col, 150, 100, 120)

    # ---- outputs ---------------------------------------------------------
    def corruptions(self) -> pd.DataFrame:
        df = pd.DataFrame(self.log)
        return df.sort_values(["sheet", "row_index", "field"], kind="stable").reset_index(drop=True)

    def row_map(self) -> pd.DataFrame:
        rows = []
        for name, s in self.sheets.items():
            for i in range(len(s.df)):
                rows.append({"sheet": name, "row_index": i, "excel_row": i + 2,
                             "record_id": s.record_ids[i], "row_kind": s.kinds[i],
                             "duplicate_of_excel_row": "" if s.dup_of[i] is None
                             else s.dup_of[i] + 2})
        return pd.DataFrame(rows)


def column_mapping() -> pd.DataFrame:
    rows = []
    for sheet, (table, cols) in COLUMN_MAP.items():
        for lc, tc, note in cols:
            rows.append({"legacy_sheet": sheet, "legacy_column": lc, "target_table": table,
                         "target_column": tc, "baseline_transform": note})
    return pd.DataFrame(rows)


def corrupt(clean_dir: Path, legacy_dir: Path, truth_dir: Path,
            seed: int = DEFAULT_SEED) -> Corruptor:
    clean_dir, legacy_dir, truth_dir = Path(clean_dir), Path(legacy_dir), Path(truth_dir)
    c = Corruptor(read_tables(clean_dir), seed)
    c.run()

    legacy_dir.mkdir(parents=True, exist_ok=True)
    xlsx = legacy_dir / "legacy_export.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
        for name, s in c.sheets.items():
            s.df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            for col_cells in ws.columns:
                width = max(len(str(cell.value or "")) for cell in col_cells[:200])
                ws.column_dimensions[col_cells[0].column_letter].width = min(width + 2, 45)
    for name, s in c.sheets.items():
        s.df.to_csv(legacy_dir / f"{name}.csv", index=False, lineterminator="\n")

    truth_dir.mkdir(parents=True, exist_ok=True)
    for f in sorted(clean_dir.glob("*.csv")):
        shutil.copyfile(f, truth_dir / f.name)
    c.corruptions().to_csv(truth_dir / "corruptions.csv", index=False, lineterminator="\n")
    c.row_map().to_csv(truth_dir / "row_map.csv", index=False, lineterminator="\n")
    column_mapping().to_csv(truth_dir / "column_mapping.csv", index=False, lineterminator="\n")
    return c


def read_legacy_xlsx(path: Path) -> dict[str, pd.DataFrame]:
    """Read every sheet as raw strings (blanks -> "", whitespace preserved)."""
    sheets = pd.read_excel(path, sheet_name=None, dtype=object, keep_default_na=False,
                           na_filter=False)
    return {k: v.map(lambda x: "" if x is None else str(x)) for k, v in sheets.items()}


def read_legacy_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False)
